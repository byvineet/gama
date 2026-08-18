"""
actions/goal_tracker.py — Gama Long-Horizon Goal Tracking
===========================================================
Reminders/timers (actions/reminder.py) and the task queue
(core.task_queue) are single-turn: fire once, done. A goal
is different — it spans days/weeks, has a target date, gets updated
with progress over many separate conversations, and GAMA should
proactively check in on it rather than just waiting to be asked.

Storage: a small SQLite DB under the user's Gama data dir (same pattern
as memory/long_term.py) so goals survive restarts.

Check-ins: a lightweight background thread wakes periodically, finds
goals that are due for a check-in (either because the deadline is
close, or because it's just been a while since the owner touched it),
and announces them via actions.reminder.fire_notification() — reusing
the exact same notify/wake/speak pipeline reminders already use,
instead of duplicating it.

Author : Gama contributor
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
DB_PATH = Path.home() / ".gama" / "goals.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# How often the background watcher wakes to look for due check-ins.
_POLL_INTERVAL_S = 15 * 60  # 15 minutes
# A goal gets a proactive check-in if it hasn't been touched in this long...
_STALE_CHECKIN_S = 3 * 24 * 3600  # 3 days
# ...or if its deadline is within this window and it hasn't been
# checked in on today yet.
_DEADLINE_WARN_S = 2 * 24 * 3600  # 2 days

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_watcher_thread: Optional[threading.Thread] = None
_watcher_stop = threading.Event()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT NOT NULL,
                description   TEXT DEFAULT '',
                created_at    TEXT NOT NULL,
                deadline      TEXT,
                status        TEXT NOT NULL DEFAULT 'active',  -- active|paused|done|abandoned
                progress_pct  INTEGER NOT NULL DEFAULT 0,
                last_update   TEXT NOT NULL,
                last_checkin  TEXT
            )
        """)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_updates (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id  INTEGER NOT NULL,
                note     TEXT NOT NULL,
                at       TEXT NOT NULL,
                FOREIGN KEY(goal_id) REFERENCES goals(id)
            )
        """)
        _conn.commit()
    return _conn


@contextmanager
def _cursor():
    conn = _connect()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@dataclass
class Goal:
    id: int
    title: str
    description: str
    created_at: str
    deadline: Optional[str]
    status: str
    progress_pct: int
    last_update: str
    last_checkin: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Goal":
        return cls(
            id=row["id"], title=row["title"], description=row["description"],
            created_at=row["created_at"], deadline=row["deadline"],
            status=row["status"], progress_pct=row["progress_pct"],
            last_update=row["last_update"], last_checkin=row["last_checkin"],
        )


def _parse_deadline(text: str) -> Optional[str]:
    """Best-effort natural-ish date parsing without a heavy dependency.
    Accepts ISO dates (2026-08-01) and a few relative phrases. Returns
    an ISO date string or None if it couldn't be parsed — callers should
    fall back to asking the user for a clearer date rather than guessing."""
    text = (text or "").strip().lower()
    if not text:
        return None
    now = datetime.now()
    try:
        if text in ("today",):
            return now.date().isoformat()
        if text in ("tomorrow",):
            return (now + timedelta(days=1)).date().isoformat()
        if text.startswith("in ") and "day" in text:
            n = int("".join(ch for ch in text.split()[1] if ch.isdigit()) or 0)
            if n > 0:
                return (now + timedelta(days=n)).date().isoformat()
        if text.startswith("in ") and "week" in text:
            n = int("".join(ch for ch in text.split()[1] if ch.isdigit()) or 0)
            if n > 0:
                return (now + timedelta(weeks=n)).date().isoformat()
        # Try ISO format directly.
        return datetime.fromisoformat(text).date().isoformat()
    except Exception:
        return None


def goal_tracker(action: str = "list", **kwargs) -> str:
    """Entry point.

    Actions:
      create       - title, description="", deadline="" (natural or ISO)
      update       - id, note="", progress_pct=None
      checkin      - id  (mark as manually checked in on today, no nagging)
      list         - status="active" (active|paused|done|abandoned|all)
      complete     - id  (marks done AND removes it from the DB/list automatically)
      pause        - id
      resume       - id
      abandon      - id
      history      - id  (recent update notes for one goal)
      delete       - id  (permanently remove a goal + its history, on demand)
    """
    action = (action or "list").lower().strip()
    if action == "create":
        return _create(kwargs.get("title", ""), kwargs.get("description", ""),
                        kwargs.get("deadline", ""))
    if action == "update":
        return _update(int(kwargs.get("id", 0) or 0), kwargs.get("note", ""),
                        kwargs.get("progress_pct"))
    if action == "checkin":
        return _checkin(int(kwargs.get("id", 0) or 0))
    if action == "list":
        return _list(kwargs.get("status", "active"))
    if action == "complete":
        return _complete_and_remove(int(kwargs.get("id", 0) or 0))
    if action == "pause":
        return _set_status(int(kwargs.get("id", 0) or 0), "paused")
    if action == "resume":
        return _set_status(int(kwargs.get("id", 0) or 0), "active")
    if action == "abandon":
        return _set_status(int(kwargs.get("id", 0) or 0), "abandoned")
    if action == "history":
        return _history(int(kwargs.get("id", 0) or 0))
    if action in ("delete", "remove"):
        return _delete(int(kwargs.get("id", 0) or 0))
    return (f"Unknown goal action: {action}. Use: create, update, checkin, "
            f"list, complete, pause, resume, abandon, history, delete.")


def _create(title: str, description: str, deadline: str) -> str:
    title = (title or "").strip()
    if not title:
        return "What's the goal?"
    now = datetime.now().isoformat()
    deadline_iso = _parse_deadline(deadline) if deadline else None
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO goals (title, description, created_at, deadline, "
            "status, progress_pct, last_update, last_checkin) "
            "VALUES (?, ?, ?, ?, 'active', 0, ?, ?)",
            (title, description or "", now, deadline_iso, now, now),
        )
        gid = cur.lastrowid
    tail = f" — targeting {deadline_iso}." if deadline_iso else "."
    if deadline and not deadline_iso:
        tail = f" (couldn't parse deadline '{deadline}' — tell me a clearer date if you want one set.)"
    return f"Goal #{gid} created: {title}{tail}"


def _get(goal_id: int) -> Optional[Goal]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM goals WHERE id = ?", (goal_id,))
        row = cur.fetchone()
    return Goal.from_row(row) if row else None


def _update(goal_id: int, note: str, progress_pct: Optional[int]) -> str:
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    now = datetime.now().isoformat()
    with _cursor() as cur:
        if progress_pct is not None:
            pct = max(0, min(100, int(progress_pct)))
            cur.execute("UPDATE goals SET progress_pct = ?, last_update = ?, "
                        "last_checkin = ? WHERE id = ?", (pct, now, now, goal_id))
        else:
            cur.execute("UPDATE goals SET last_update = ?, last_checkin = ? WHERE id = ?",
                        (now, now, goal_id))
        if note:
            cur.execute("INSERT INTO goal_updates (goal_id, note, at) VALUES (?, ?, ?)",
                        (goal_id, note, now))
    return f"Updated goal #{goal_id}: {goal.title}."


def _checkin(goal_id: int) -> str:
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    now = datetime.now().isoformat()
    with _cursor() as cur:
        cur.execute("UPDATE goals SET last_checkin = ? WHERE id = ?", (now, goal_id))
    return f"Noted — goal #{goal_id} ({goal.title}) checked in on."


def _set_status(goal_id: int, status: str) -> str:
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    now = datetime.now().isoformat()
    with _cursor() as cur:
        pct = 100 if status == "done" else goal.progress_pct
        cur.execute("UPDATE goals SET status = ?, progress_pct = ?, last_update = ? WHERE id = ?",
                    (status, pct, now, goal_id))
    verbs = {"done": "Marked complete", "paused": "Paused", "active": "Resumed", "abandoned": "Abandoned"}
    return f"{verbs.get(status, 'Updated')}: goal #{goal_id} — {goal.title}."


def _delete(goal_id: int) -> str:
    """Permanently remove a goal and its update history — on demand."""
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    with _cursor() as cur:
        cur.execute("DELETE FROM goal_updates WHERE goal_id = ?", (goal_id,))
        cur.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    return f"Deleted goal #{goal_id} — {goal.title}."


def _complete_and_remove(goal_id: int) -> str:
    """Mark a goal complete and immediately remove it from the DB/list —
    so completed goals don't linger and have to be cleaned up separately."""
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    with _cursor() as cur:
        cur.execute("DELETE FROM goal_updates WHERE goal_id = ?", (goal_id,))
        cur.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    return f"🎉 Completed and removed goal #{goal_id} — {goal.title}."


def _list(status: str) -> str:
    status = (status or "active").lower().strip()
    with _cursor() as cur:
        if status == "all":
            cur.execute("SELECT * FROM goals ORDER BY status, deadline IS NULL, deadline")
        else:
            cur.execute("SELECT * FROM goals WHERE status = ? ORDER BY deadline IS NULL, deadline",
                        (status,))
        rows = cur.fetchall()
    if not rows:
        return f"No {status} goals." if status != "all" else "No goals tracked yet."
    lines = []
    for r in rows:
        g = Goal.from_row(r)
        dl = f", due {g.deadline}" if g.deadline else ""
        lines.append(f"#{g.id} [{g.status}] {g.title} — {g.progress_pct}%{dl}")
    return "\n".join(lines)


def _history(goal_id: int) -> str:
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    with _cursor() as cur:
        cur.execute("SELECT note, at FROM goal_updates WHERE goal_id = ? ORDER BY at DESC LIMIT 10",
                    (goal_id,))
        rows = cur.fetchall()
    if not rows:
        return f"No updates logged yet for goal #{goal_id} ({goal.title})."
    lines = [f"Recent updates for #{goal_id} ({goal.title}):"]
    lines += [f"  [{r['at'][:16]}] {r['note']}" for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background proactive check-in watcher
# ---------------------------------------------------------------------------
def _due_for_checkin(goal: Goal) -> Optional[str]:
    """Returns a human reason string if this active goal is due for a
    proactive check-in right now, else None."""
    if goal.status != "active":
        return None
    now = datetime.now()
    last = datetime.fromisoformat(goal.last_checkin or goal.last_update)
    stale = (now - last).total_seconds() > _STALE_CHECKIN_S
    deadline_soon = False
    if goal.deadline:
        try:
            dl = datetime.fromisoformat(goal.deadline)
            deadline_soon = 0 <= (dl - now).total_seconds() <= _DEADLINE_WARN_S
        except Exception:
            pass
    if deadline_soon:
        return f"deadline {goal.deadline} is approaching"
    if stale:
        return "no update in a few days"
    return None


def _watch_loop() -> None:
    logger.info("[goal_tracker] Proactive check-in watcher started.")
    while not _watcher_stop.is_set():
        try:
            with _cursor() as cur:
                cur.execute("SELECT * FROM goals WHERE status = 'active'")
                rows = cur.fetchall()
            for row in rows:
                goal = Goal.from_row(row)
                reason = _due_for_checkin(goal)
                if reason:
                    try:
                        from state_engine.arbitrator import arbitrator
                        from state_engine.user_state import PriorityLevel
                        arbitrator.dispatch(
                            title="Goal check-in",
                            message=f"\"{goal.title}\" — {reason}. You're at {goal.progress_pct}%. Want to give me an update?",
                            priority=PriorityLevel.P3_PROACTIVE,
                            category="goal",
                            speak=True,
                        )
                    except Exception as exc:
                        logger.warning(f"[goal_tracker] Could not announce check-in: {exc}")
                    # Avoid re-nagging every poll cycle for the same goal —
                    # bump last_checkin so it waits another full window.
                    with _cursor() as cur2:
                        cur2.execute("UPDATE goals SET last_checkin = ? WHERE id = ?",
                                     (datetime.now().isoformat(), goal.id))
        except Exception as exc:
            logger.error(f"[goal_tracker] Watch loop error: {exc}")
        _watcher_stop.wait(_POLL_INTERVAL_S)
    logger.info("[goal_tracker] Watcher stopped.")


def start_goal_watcher() -> None:
    """Start the background proactive check-in thread. Idempotent —
    safe to call once at startup, mirrors class_schedule's watcher."""
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(target=_watch_loop, name="gama-goal-watcher", daemon=True)
    _watcher_thread.start()


def stop_goal_watcher() -> None:
    _watcher_stop.set()


__all__ = ["goal_tracker", "start_goal_watcher", "stop_goal_watcher"]
