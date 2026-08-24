"""
actions/goal_tracker.py — Gama Long-Horizon Goal Tracking (AGI / JARVIS style)
==============================================================================
Reminders/timers (actions/reminder.py) and the task queue (core.task_queue)
are single-turn: fire once, done. A Goal is different — it spans days/weeks,
has a target date, contains ordered subtasks (some owned by GAMA, some by
the user), gets updated with progress over many separate conversations, and
GAMA proactively checks in rather than just waiting to be asked.

This version adds:
  • Subtasks with owner (gama | user | either), status, optional depends_on
  • Rich status reporting (progress from subtasks when present)
  • World-model sync so active goals appear in the live prompt block
  • `advance` — returns the next ready GAMA-owned subtask (for autonomous work)
  • `break_down` — simple heuristic split of a goal into starter subtasks
  • Schema migration from the original single-table goals.db
  • Consistent data path via utils.paths.user_data_path

Storage: SQLite under the Gama data dir so goals survive restarts.

Author : Vineet Machchal / Gama
"""

from __future__ import annotations

from utils.logger import get_logger
from utils.paths import user_data_path

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

log = get_logger(__name__)
logger = log

DB_PATH = user_data_path("memory/goals.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# How often the background watcher wakes to look for due check-ins.
_POLL_INTERVAL_S = 15 * 60          # 15 minutes
_STALE_CHECKIN_S = 3 * 24 * 3600    # 3 days
_DEADLINE_WARN_S = 2 * 24 * 3600    # 2 days

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_watcher_thread: Optional[threading.Thread] = None
_watcher_stop = threading.Event()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
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
                status        TEXT NOT NULL DEFAULT 'active',
                progress_pct  INTEGER NOT NULL DEFAULT 0,
                last_update   TEXT NOT NULL,
                last_checkin  TEXT,
                priority      INTEGER NOT NULL DEFAULT 5,
                success_criteria TEXT DEFAULT '[]',
                notes         TEXT DEFAULT '[]'
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
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS subtasks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id       INTEGER NOT NULL,
                description   TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                owner         TEXT NOT NULL DEFAULT 'either',
                depends_on    TEXT DEFAULT '[]',
                result        TEXT,
                sort_order    INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                FOREIGN KEY(goal_id) REFERENCES goals(id)
            )
        """)
        _migrate_schema(_conn)
        _conn.commit()
    return _conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from older goal_tracker installs."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(goals)")
    cols = {row[1] for row in cur.fetchall()}
    if "priority" not in cols:
        cur.execute("ALTER TABLE goals ADD COLUMN priority INTEGER NOT NULL DEFAULT 5")
    if "success_criteria" not in cols:
        cur.execute("ALTER TABLE goals ADD COLUMN success_criteria TEXT DEFAULT '[]'")
    if "notes" not in cols:
        cur.execute("ALTER TABLE goals ADD COLUMN notes TEXT DEFAULT '[]'")


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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SubTask:
    id: int
    goal_id: int
    description: str
    status: str          # pending | in_progress | blocked | done | cancelled
    owner: str           # gama | user | either
    depends_on: List[int] = field(default_factory=list)
    result: Optional[str] = None
    sort_order: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SubTask":
        deps: List[int] = []
        raw = row["depends_on"] or "[]"
        try:
            deps = [int(x) for x in json.loads(raw)]
        except Exception:
            deps = []
        return cls(
            id=row["id"],
            goal_id=row["goal_id"],
            description=row["description"],
            status=row["status"],
            owner=row["owner"] or "either",
            depends_on=deps,
            result=row["result"],
            sort_order=row["sort_order"] or 0,
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


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
    priority: int = 5
    success_criteria: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    subtasks: List[SubTask] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row, subtasks: Optional[List[SubTask]] = None) -> "Goal":
        criteria: List[str] = []
        notes: List[str] = []
        try:
            criteria = json.loads(row["success_criteria"] or "[]")
        except Exception:
            criteria = []
        try:
            notes = json.loads(row["notes"] or "[]")
        except Exception:
            notes = []
        return cls(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            created_at=row["created_at"],
            deadline=row["deadline"],
            status=row["status"],
            progress_pct=int(row["progress_pct"] or 0),
            last_update=row["last_update"],
            last_checkin=row["last_checkin"],
            priority=int(row["priority"] or 5) if "priority" in row.keys() else 5,
            success_criteria=criteria if isinstance(criteria, list) else [],
            notes=notes if isinstance(notes, list) else [],
            subtasks=subtasks or [],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_int(value, default: int = 0) -> int:
    """Coerce tool args to a small non-negative int safe for SQLite INTEGER.

    Gemini Live sometimes sends floats, numeric strings, or garbage. Anything
    outside 0..2**63-1 is rejected so we never hit OverflowError in SQLite.
    """
    if value is None or value == "":
        return default
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, float):
            value = int(value)
        elif isinstance(value, str):
            value = int(float(value.strip()))  # handles "1", "1.0"
        else:
            value = int(value)
        if value < 0 or value > 2**62:  # stay well inside signed 64-bit
            return default
        return value
    except (TypeError, ValueError, OverflowError):
        return default




def _parse_deadline(text: str) -> Optional[str]:
    """Best-effort natural-ish date parsing. Returns ISO date or None."""
    text = (text or "").strip().lower()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except Exception:
            pass
    today = datetime.now().date()
    if "tomorrow" in text:
        return (today + timedelta(days=1)).isoformat()
    if "next week" in text or "in a week" in text:
        return (today + timedelta(days=7)).isoformat()
    if "in 2 weeks" in text or "in two weeks" in text:
        return (today + timedelta(days=14)).isoformat()
    if "in a month" in text or "next month" in text:
        return (today + timedelta(days=30)).isoformat()
    if "friday" in text:
        days = (4 - today.weekday()) % 7
        if days == 0:
            days = 7
        return (today + timedelta(days=days)).isoformat()
    m = re.search(r"in\s+(\d+)\s+days?", text)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"in\s+(\d+)\s+weeks?", text)
    if m:
        return (today + timedelta(days=7 * int(m.group(1)))).isoformat()
    return None


def _load_subtasks(goal_id: int) -> List[SubTask]:
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM subtasks WHERE goal_id = ? ORDER BY sort_order, id",
            (goal_id,),
        )
        rows = cur.fetchall()
    return [SubTask.from_row(r) for r in rows]


def _recompute_progress(goal_id: int) -> int:
    """If subtasks exist, progress = % of done subtasks. Else keep existing."""
    subs = _load_subtasks(goal_id)
    if not subs:
        with _cursor() as cur:
            cur.execute("SELECT progress_pct FROM goals WHERE id = ?", (goal_id,))
            row = cur.fetchone()
        return int(row["progress_pct"]) if row else 0
    done = sum(1 for s in subs if s.status == "done")
    pct = int(round(100.0 * done / len(subs)))
    with _cursor() as cur:
        cur.execute(
            "UPDATE goals SET progress_pct = ?, last_update = ? WHERE id = ?",
            (pct, _now_iso(), goal_id),
        )
    return pct


def _sync_world_model(goal: Optional[Goal] = None) -> None:
    """Push a short active-goal string into the World Model for prompt injection."""
    try:
        from core.world_model import world
        if goal is None:
            with _cursor() as cur:
                cur.execute(
                    "SELECT * FROM goals WHERE status = 'active' "
                    "ORDER BY priority DESC, id ASC LIMIT 1"
                )
                row = cur.fetchone()
            if not row:
                world.update_user(active_goal=None)
                return
            goal = Goal.from_row(row)
        pct = goal.progress_pct
        deadline_bit = f", due {goal.deadline}" if goal.deadline else ""
        world.update_user(
            active_goal=f"#{goal.id} {goal.title} ({pct}%{deadline_bit})"
        )
    except Exception as exc:
        logger.debug("[goal_tracker] world_model sync skipped: %s", exc)


def _goal_summary_line(goal: Goal) -> str:
    subs = goal.subtasks or _load_subtasks(goal.id)
    if subs:
        done = sum(1 for s in subs if s.status == "done")
        return (
            f"#{goal.id} [{goal.status}] {goal.title} — "
            f"{goal.progress_pct}% ({done}/{len(subs)} subtasks)"
            + (f", due {goal.deadline}" if goal.deadline else "")
        )
    return (
        f"#{goal.id} [{goal.status}] {goal.title} — {goal.progress_pct}%"
        + (f", due {goal.deadline}" if goal.deadline else "")
    )


# ---------------------------------------------------------------------------
# Public tool entry point
# ---------------------------------------------------------------------------

def goal_tracker(action: str = "list", **kwargs) -> str:
    """
    Main dispatch for the goal_tracker tool.

    Supported actions
    -----------------
    create          title, description?, deadline?, priority?, subtasks? (list of str)
    get / status    id
    list            status? (active|paused|done|abandoned|all)
    update          id, note?, progress_pct?
    add_subtask     id (goal), description, owner? (gama|user|either)
    update_subtask  subtask_id, status?, description?, result?
    complete_subtask subtask_id
    break_down      id  — create a few starter subtasks from the goal title/desc
    advance         id? — next ready GAMA-owned subtask (or across all active goals)
    checkin         id
    complete / pause / resume / abandon / delete
    history         id
    """
    action = (action or "list").strip().lower()

    if action in ("create", "new", "add"):
        return _create(
            title=kwargs.get("title") or kwargs.get("goal") or "",
            description=kwargs.get("description") or kwargs.get("desc") or "",
            deadline=kwargs.get("deadline") or kwargs.get("due") or "",
            priority=kwargs.get("priority"),
            subtasks=kwargs.get("subtasks"),
        )
    if action in ("get", "status", "show", "detail"):
        return _get_rich(_safe_int(kwargs.get("id")))
    if action in ("list", "ls", "all"):
        return _list(status=kwargs.get("status") or "active")
    if action in ("update", "progress"):
        return _update(
            goal_id=_safe_int(kwargs.get("id")),
            note=kwargs.get("note") or kwargs.get("text") or "",
            progress_pct=kwargs.get("progress_pct") if kwargs.get("progress_pct") is not None
            else kwargs.get("progress"),
        )
    if action in ("add_subtask", "subtask_add", "add_task"):
        return _add_subtask(
            goal_id=_safe_int(kwargs.get("id") or kwargs.get("goal_id")),
            description=kwargs.get("description") or kwargs.get("text") or kwargs.get("subtask") or "",
            owner=kwargs.get("owner") or "either",
        )
    if action in ("update_subtask", "subtask_update"):
        return _update_subtask(
            subtask_id=_safe_int(kwargs.get("subtask_id") or kwargs.get("id")),
            status=kwargs.get("status"),
            description=kwargs.get("description"),
            result=kwargs.get("result"),
        )
    if action in ("complete_subtask", "subtask_done", "finish_subtask"):
        return _update_subtask(
            subtask_id=_safe_int(kwargs.get("subtask_id") or kwargs.get("id")),
            status="done",
            result=kwargs.get("result"),
        )
    if action in ("break_down", "breakdown", "decompose"):
        return _break_down(_safe_int(kwargs.get("id")))
    if action in ("advance", "next", "next_step"):
        gid = _safe_int(kwargs.get("id"), default=0)
        return _advance(gid if gid > 0 else None)
    if action in ("checkin", "check_in", "touch"):
        return _checkin(_safe_int(kwargs.get("id")))
    if action in ("complete", "done", "finish"):
        return _set_status(_safe_int(kwargs.get("id")), "done")
    if action == "pause":
        return _set_status(_safe_int(kwargs.get("id")), "paused")
    if action == "resume":
        return _set_status(_safe_int(kwargs.get("id")), "active")
    if action in ("abandon", "cancel"):
        return _set_status(_safe_int(kwargs.get("id")), "abandoned")
    if action in ("delete", "remove"):
        return _delete(_safe_int(kwargs.get("id")))
    if action == "history":
        return _history(_safe_int(kwargs.get("id")))

    return (
        "Unknown goal action. Use: create, get, list, update, add_subtask, "
        "update_subtask, complete_subtask, break_down, advance, checkin, "
        "complete, pause, resume, abandon, history, delete."
    )


# ---------------------------------------------------------------------------
# CRUD + rich operations
# ---------------------------------------------------------------------------

def _create(
    title: str,
    description: str = "",
    deadline: str = "",
    priority: Any = None,
    subtasks: Any = None,
) -> str:
    title = (title or "").strip()
    if not title:
        return "What's the goal?"
    now = _now_iso()
    deadline_iso = _parse_deadline(deadline) if deadline else None
    try:
        pri = max(1, min(10, int(priority))) if priority is not None else 5
    except Exception:
        pri = 5

    with _cursor() as cur:
        cur.execute(
            "INSERT INTO goals (title, description, created_at, deadline, "
            "status, progress_pct, last_update, last_checkin, priority) "
            "VALUES (?, ?, ?, ?, 'active', 0, ?, ?, ?)",
            (title, description or "", now, deadline_iso, now, now, pri),
        )
        gid = cur.lastrowid

    created_subs = 0
    if subtasks:
        items: List[str] = []
        if isinstance(subtasks, str):
            items = [s.strip() for s in subtasks.split(";") if s.strip()]
        elif isinstance(subtasks, list):
            for s in subtasks:
                if isinstance(s, str) and s.strip():
                    items.append(s.strip())
                elif isinstance(s, dict) and s.get("description"):
                    items.append(str(s["description"]).strip())
        for i, desc in enumerate(items):
            _add_subtask(gid, desc, owner="either", sort_order=i, quiet=True)
            created_subs += 1
        if created_subs:
            _recompute_progress(gid)

    goal = _get(gid)
    _sync_world_model(goal)

    tail = f" — targeting {deadline_iso}." if deadline_iso else "."
    if deadline and not deadline_iso:
        tail = f" (couldn't parse deadline '{deadline}' — give me a clearer date if you want one set.)"
    extra = f" {created_subs} starter subtasks added." if created_subs else ""
    return f"Goal #{gid} created: {title}{tail}{extra}"


def _get(goal_id: int) -> Optional[Goal]:
    if not goal_id or goal_id < 1 or goal_id > 2**62:
        return None
    with _cursor() as cur:
        cur.execute("SELECT * FROM goals WHERE id = ?", (goal_id,))
        row = cur.fetchone()
    if not row:
        return None
    return Goal.from_row(row, subtasks=_load_subtasks(goal_id))


def _get_rich(goal_id: int) -> str:
    if not goal_id:
        return "Which goal? Pass a valid id (use list to see active goals)."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    lines = [
        f"Goal #{goal.id}: {goal.title}",
        f"  Status: {goal.status}  |  Progress: {goal.progress_pct}%  |  Priority: {goal.priority}",
    ]
    if goal.deadline:
        lines.append(f"  Deadline: {goal.deadline}")
    if goal.description:
        lines.append(f"  Description: {goal.description}")
    if goal.success_criteria:
        lines.append("  Success criteria:")
        for c in goal.success_criteria:
            lines.append(f"    - {c}")
    subs = goal.subtasks
    if subs:
        lines.append(f"  Subtasks ({sum(1 for s in subs if s.status == 'done')}/{len(subs)} done):")
        for s in subs:
            owner_tag = f"[{s.owner}]" if s.owner != "either" else ""
            dep = f" (depends on {s.depends_on})" if s.depends_on else ""
            res = f" → {s.result}" if s.result else ""
            lines.append(f"    #{s.id} [{s.status}] {owner_tag} {s.description}{dep}{res}")
    else:
        lines.append("  (no subtasks yet — use break_down or add_subtask)")
    return "\n".join(lines)


def _list(status: str = "active") -> str:
    status = (status or "active").lower()
    with _cursor() as cur:
        if status == "all":
            cur.execute("SELECT * FROM goals ORDER BY status, priority DESC, id")
        else:
            cur.execute(
                "SELECT * FROM goals WHERE status = ? ORDER BY priority DESC, id",
                (status,),
            )
        rows = cur.fetchall()
    if not rows:
        return f"No {status} goals."
    lines = []
    for row in rows:
        g = Goal.from_row(row, subtasks=_load_subtasks(row["id"]))
        lines.append(_goal_summary_line(g))
    return "\n".join(lines)


def _update(goal_id: int, note: str, progress_pct: Any) -> str:
    if not goal_id:
        return "Which goal? Pass a valid id."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    now = _now_iso()
    with _cursor() as cur:
        if progress_pct is not None:
            try:
                pct = max(0, min(100, int(progress_pct)))
            except Exception:
                pct = goal.progress_pct
            cur.execute(
                "UPDATE goals SET progress_pct = ?, last_update = ?, last_checkin = ? WHERE id = ?",
                (pct, now, now, goal_id),
            )
        else:
            cur.execute(
                "UPDATE goals SET last_update = ?, last_checkin = ? WHERE id = ?",
                (now, now, goal_id),
            )
        if note:
            cur.execute(
                "INSERT INTO goal_updates (goal_id, note, at) VALUES (?, ?, ?)",
                (goal_id, note, now),
            )
    goal = _get(goal_id)
    _sync_world_model(goal)
    return f"Updated goal #{goal_id}: {goal.title} ({goal.progress_pct}%)."


def _add_subtask(
    goal_id: int,
    description: str,
    owner: str = "either",
    sort_order: Optional[int] = None,
    quiet: bool = False,
) -> str:
    description = (description or "").strip()
    if not description:
        return "What should the subtask be?"
    if not goal_id:
        return "Which goal? Pass a valid id."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    owner = (owner or "either").lower()
    if owner not in ("gama", "user", "either"):
        owner = "either"
    now = _now_iso()
    if sort_order is None:
        existing = _load_subtasks(goal_id)
        sort_order = (max((s.sort_order for s in existing), default=-1) + 1)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO subtasks (goal_id, description, status, owner, depends_on, "
            "sort_order, created_at, updated_at) VALUES (?, ?, 'pending', ?, '[]', ?, ?, ?)",
            (goal_id, description, owner, sort_order, now, now),
        )
        sid = cur.lastrowid
    _recompute_progress(goal_id)
    _sync_world_model(_get(goal_id))
    if quiet:
        return f"subtask #{sid}"
    return f"Added subtask #{sid} to goal #{goal_id}: {description} [{owner}]."


def _update_subtask(
    subtask_id: int,
    status: Optional[str] = None,
    description: Optional[str] = None,
    result: Optional[str] = None,
) -> str:
    if not subtask_id or subtask_id < 1 or subtask_id > 2**62:
        return "Which subtask? Pass a valid subtask_id."
    with _cursor() as cur:
        cur.execute("SELECT * FROM subtasks WHERE id = ?", (subtask_id,))
        row = cur.fetchone()
    if not row:
        return f"No subtask with id {subtask_id}."
    st = SubTask.from_row(row)
    now = _now_iso()
    new_status = st.status
    if status:
        status = status.lower().strip()
        if status in ("pending", "in_progress", "blocked", "done", "cancelled"):
            new_status = status
        elif status in ("complete", "finished", "finish"):
            new_status = "done"
    new_desc = description.strip() if description else st.description
    new_result = result if result is not None else st.result

    with _cursor() as cur:
        cur.execute(
            "UPDATE subtasks SET status = ?, description = ?, result = ?, updated_at = ? WHERE id = ?",
            (new_status, new_desc, new_result, now, subtask_id),
        )
    pct = _recompute_progress(st.goal_id)
    _sync_world_model(_get(st.goal_id))
    return (
        f"Subtask #{subtask_id} → {new_status}. "
        f"Goal #{st.goal_id} now at {pct}%."
    )


def _break_down(goal_id: int) -> str:
    """Create a few sensible starter subtasks from title + description."""
    if not goal_id:
        return "Which goal should I break down? Pass id from list."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    if goal.subtasks:
        return (
            f"Goal #{goal_id} already has {len(goal.subtasks)} subtasks. "
            "Add more with add_subtask if needed."
        )

    title = goal.title.lower()
    desc = (goal.description or "").lower()
    text = f"{title} {desc}"
    starters: List[Tuple[str, str]] = []

    starters.append(("Clarify exact success criteria and constraints", "user"))
    starters.append(("Identify the first concrete deliverable", "either"))

    if any(k in text for k in ("code", "implement", "pipeline", "feature", "bug", "refactor", "vision", "model")):
        starters.append(("Scaffold project structure / files", "gama"))
        starters.append(("Implement core logic", "either"))
        starters.append(("Write or run basic tests", "gama"))
        starters.append(("Review and polish", "user"))
    elif any(k in text for k in ("write", "essay", "report", "paper", "document")):
        starters.append(("Outline structure", "either"))
        starters.append(("Draft first version", "either"))
        starters.append(("Edit and finalize", "user"))
    elif any(k in text for k in ("learn", "study", "course", "exam")):
        starters.append(("List key topics to cover", "either"))
        starters.append(("Schedule study blocks", "user"))
        starters.append(("Review and self-test", "user"))
    else:
        starters.append(("Break the work into the next 2–3 concrete steps", "either"))
        starters.append(("Execute the first step", "either"))
        starters.append(("Review progress and adjust", "user"))

    created = []
    for i, (desc, owner) in enumerate(starters):
        msg = _add_subtask(goal_id, desc, owner=owner, sort_order=i, quiet=True)
        created.append(msg)

    _sync_world_model(_get(goal_id))
    return (
        f"Broke down goal #{goal_id} into {len(created)} starter subtasks. "
        f"Use get id={goal_id} to see them, or advance to start the next GAMA-owned step."
    )


def _advance(goal_id: Optional[int] = None) -> str:
    """
    Return the next ready GAMA-owned (or either) pending subtask.
    Ready = pending + all depends_on are done.
    """
    goals: List[Goal] = []
    if goal_id is not None:
        g = _get(goal_id)
        if g is None:
            return f"No goal with id {goal_id}."
        if g.status != "active":
            return f"Goal #{goal_id} is {g.status}, not active."
        goals = [g]
    else:
        with _cursor() as cur:
            cur.execute(
                "SELECT * FROM goals WHERE status = 'active' ORDER BY priority DESC, id"
            )
            rows = cur.fetchall()
        goals = [Goal.from_row(r, subtasks=_load_subtasks(r["id"])) for r in rows]

    if not goals:
        return "No active goals."

    for g in goals:
        done_ids = {s.id for s in g.subtasks if s.status == "done"}
        candidates = [
            s for s in g.subtasks
            if s.status == "pending"
            and s.owner in ("gama", "either")
            and all(d in done_ids for d in s.depends_on)
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda s: (s.sort_order, s.id))
        nxt = candidates[0]
        _update_subtask(nxt.id, status="in_progress")
        return (
            f"Next step for goal #{g.id} ({g.title}): "
            f"subtask #{nxt.id} — {nxt.description} "
            f"[owner={nxt.owner}]. "
            "Execute it, then call complete_subtask when finished."
        )

    return (
        "No ready GAMA-owned subtasks right now. "
        "Either wait on user-owned steps or add new subtasks."
    )


def _checkin(goal_id: int) -> str:
    if not goal_id:
        return "Which goal? Pass a valid id."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    now = _now_iso()
    with _cursor() as cur:
        cur.execute("UPDATE goals SET last_checkin = ? WHERE id = ?", (now, goal_id))
    return f"Noted — goal #{goal_id} ({goal.title}) checked in on."


def _set_status(goal_id: int, status: str) -> str:
    if not goal_id:
        return "Which goal? Pass a valid id."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    now = _now_iso()
    with _cursor() as cur:
        cur.execute(
            "UPDATE goals SET status = ?, last_update = ? WHERE id = ?",
            (status, now, goal_id),
        )
        if status == "done":
            cur.execute(
                "UPDATE goals SET progress_pct = 100, last_checkin = ? WHERE id = ?",
                (now, goal_id),
            )
    if status == "active":
        _sync_world_model(_get(goal_id))
    else:
        _sync_world_model(None)
    return f"Goal #{goal_id} ({goal.title}) marked {status}."


def _delete(goal_id: int) -> str:
    if not goal_id:
        return "Which goal? Pass a valid id."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    with _cursor() as cur:
        cur.execute("DELETE FROM subtasks WHERE goal_id = ?", (goal_id,))
        cur.execute("DELETE FROM goal_updates WHERE goal_id = ?", (goal_id,))
        cur.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    _sync_world_model(None)
    return f"Deleted goal #{goal_id} ({goal.title})."


def _history(goal_id: int) -> str:
    if not goal_id:
        return "Which goal? Pass a valid id."
    goal = _get(goal_id)
    if goal is None:
        return f"No goal with id {goal_id}."
    with _cursor() as cur:
        cur.execute(
            "SELECT note, at FROM goal_updates WHERE goal_id = ? ORDER BY id DESC LIMIT 15",
            (goal_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return f"No update history for goal #{goal_id} yet."
    lines = [f"History for #{goal_id} ({goal.title}):"]
    for r in rows:
        lines.append(f"  [{r['at']}] {r['note']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Proactive watcher
# ---------------------------------------------------------------------------

def _due_for_checkin(goal: Goal) -> Optional[str]:
    now = datetime.now()
    try:
        last = datetime.fromisoformat(goal.last_checkin) if goal.last_checkin else None
    except Exception:
        last = None
    stale = (last is None) or ((now - last).total_seconds() >= _STALE_CHECKIN_S)

    deadline_soon = False
    if goal.deadline:
        try:
            dl = datetime.fromisoformat(goal.deadline)
            if len(goal.deadline) <= 10:
                dl = dl.replace(hour=23, minute=59)
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
                goal = Goal.from_row(row, subtasks=_load_subtasks(row["id"]))
                reason = _due_for_checkin(goal)
                if reason:
                    try:
                        from state_engine.arbitrator import arbitrator
                        from state_engine.user_state import PriorityLevel
                        subs_bit = ""
                        if goal.subtasks:
                            done = sum(1 for s in goal.subtasks if s.status == "done")
                            subs_bit = f" ({done}/{len(goal.subtasks)} subtasks)"
                        arbitrator.dispatch(
                            title="Goal check-in",
                            message=(
                                f'"{goal.title}" — {reason}. '
                                f"You're at {goal.progress_pct}%{subs_bit}. "
                                "Want to give me an update?"
                            ),
                            priority=PriorityLevel.P3_PROACTIVE,
                            category="goal",
                            speak=True,
                        )
                    except Exception as exc:
                        logger.warning(f"[goal_tracker] Could not announce check-in: {exc}")
                    with _cursor() as cur2:
                        cur2.execute(
                            "UPDATE goals SET last_checkin = ? WHERE id = ?",
                            (_now_iso(), goal.id),
                        )
        except Exception as exc:
            logger.error(f"[goal_tracker] Watch loop error: {exc}")
        _watcher_stop.wait(_POLL_INTERVAL_S)
    logger.info("[goal_tracker] Watcher stopped.")


def start_goal_watcher() -> None:
    """Start the background proactive check-in thread. Idempotent."""
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watch_loop, name="gama-goal-watcher", daemon=True
    )
    _watcher_thread.start()


def stop_goal_watcher() -> None:
    _watcher_stop.set()


__all__ = ["goal_tracker", "start_goal_watcher", "stop_goal_watcher", "Goal", "SubTask"]
