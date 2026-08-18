"""
learning/habit_tracker.py — Gama Habit Tracker (Part 5 improved)
=================================================================
Records meaningful behavior events (app focus changes, session types,
media preferences) as cheaply as possible so the routine analyzer can
later mine patterns out of them.

What's tracked (per spec Part 5 — only meaningful habits):
  - app_focus: which apps the user focuses (e.g. VS Code, Chrome, Spotify)
  - session: detected session type (studying, coding, gaming, browsing)
  - command: meaningful tool invocations (media play, search, etc.)
  - volume: volume level preferences by time of day
  - brightness: brightness preferences by time of day

What's NOT tracked:
  - Greetings, small talk, acknowledgements ("okay", "done", "thanks")
  - Random one-off conversations
  - Every single wake/sleep cycle

Design:
  - Events appended to in-memory buffer (O(1), no disk I/O on hot path).
  - Background thread flushes to SQLite every FLUSH_INTERVAL seconds.
  - Subscribes to EventBus — no new polling introduced.
  - Table is narrow: (kind, key, ts, weekday, hour).

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

log = get_logger(__name__)
logger = log  # back-compat alias
from utils.paths import user_data_path
_DB_PATH_P = user_data_path("learning/habits.db")
_DB_DIR = str(_DB_PATH_P.parent)
_DB_PATH = str(_DB_PATH_P)

FLUSH_INTERVAL = 30.0     # seconds between background flushes
FLUSH_SIZE = 50           # or flush early if buffer gets this big
RETENTION_DAYS = 120      # raw events older than this are pruned

_buffer_lock = threading.Lock()
_buffer: list[tuple[str, str, float, int, int]] = []  # kind, key, ts, weekday, hour
_flush_thread: Optional[threading.Thread] = None
_stop_flag = threading.Event()
_subscribed = False

# --- Noise filters: don't record these as habits ---
_NOISE_COMMANDS = {
    "okay", "ok", "yes", "no", "thanks", "thank you", "done", "sure",
    "alright", "right", "got it", "good", "great", "nice", "fine",
    "hi", "hello", "hey", "wake up", "go to sleep", "stop", "cancel",
    "status", "what", "help",
}

# Apps worth tracking (system processes / background apps are noise)
_NOISE_APPS = {
    "explorer.exe", "taskmgr.exe", "dwm.exe", "csrss.exe", "svchost.exe",
    "system", "registry", "idle", "antimalware service executable",
    "windows security", "gama.exe", "python.exe", "pythonw.exe",
}

# Meaningful session-type keywords for command tracking
_SESSION_KEYWORDS = {
    "studying", "coding", "gaming", "working", "browsing",
    "reading", "writing", "designing", "debugging",
}


def _connect() -> sqlite3.Connection:
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            ts REAL NOT NULL,
            weekday INTEGER NOT NULL,
            hour INTEGER NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind_key ON events(kind, key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    return conn


def _is_meaningful_app(app: str) -> bool:
    """Return True if this app is worth tracking as a habit."""
    if not app:
        return False
    a = app.lower().strip()
    if a in _NOISE_APPS:
        return False
    # Skip very short names (likely system processes)
    if len(a) < 3:
        return False
    return True


def _is_meaningful_command(cmd: str) -> bool:
    """Return True if this command is worth tracking as a habit."""
    if not cmd:
        return False
    c = cmd.lower().strip()
    # Skip one-word noise responses
    if c in _NOISE_COMMANDS:
        return False
    # Skip very short commands (likely noise)
    if len(c) < 4:
        return False
    # Skip pure questions with no meaningful action
    if c.startswith(("what", "who", "when", "where", "why", "how are")):
        return False
    return True


def record(kind: str, key: str, ts: Optional[float] = None) -> None:
    """Record one behavior event. Cheap: appends to memory only."""
    if not kind or not key:
        return
    ts = ts if ts is not None else time.time()
    dt = datetime.fromtimestamp(ts)
    with _buffer_lock:
        _buffer.append((kind, key, ts, dt.weekday(), dt.hour))
        should_flush = len(_buffer) >= FLUSH_SIZE
    if should_flush:
        _flush()


def record_session(session_type: str) -> None:
    """Record a detected session type (studying, coding, gaming, etc.)."""
    if session_type and session_type in _SESSION_KEYWORDS:
        record("session", session_type)


def record_volume_preference(level: int) -> None:
    """Record volume preference for current time of day."""
    # Bucket to nearest 10 to avoid noisy exact values
    bucketed = round(level / 10) * 10
    record("volume_pref", str(bucketed))


def record_brightness_preference(level: int) -> None:
    """Record brightness preference for current time of day."""
    bucketed = round(level / 10) * 10
    record("brightness_pref", str(bucketed))


def _flush() -> None:
    with _buffer_lock:
        if not _buffer:
            return
        rows = list(_buffer)
        _buffer.clear()
    try:
        conn = _connect()
        with conn:
            conn.executemany(
                "INSERT INTO events (kind, key, ts, weekday, hour) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        conn.close()
    except Exception:
        logger.debug("habit_tracker: flush failed, re-buffering %d rows", len(rows), exc_info=True)
        with _buffer_lock:
            _buffer[:0] = rows  # put back, try again next cycle


def _prune_old() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.close()
    except Exception:
        logger.debug("habit_tracker: prune failed", exc_info=True)


def _flush_loop() -> None:
    last_prune = 0.0
    while not _stop_flag.wait(FLUSH_INTERVAL):
        _flush()
        if time.time() - last_prune > 86400:  # once a day
            _prune_old()
            last_prune = time.time()
    _flush()  # final flush on shutdown


def _on_app_focused(event) -> None:
    app = event.data.get("app")
    if app and _is_meaningful_app(app):
        record("app_focus", str(app))


def _on_download_completed(event) -> None:
    name = event.data.get("filename") or event.data.get("name")
    if name:
        record("download", str(name))


def _on_command_executed(event) -> None:
    cmd = event.data.get("command") or event.data.get("action")
    if cmd and _is_meaningful_command(str(cmd)):
        record("command", str(cmd))


def _on_music_started(event) -> None:
    """Track music/media preferences."""
    app = event.data.get("app", "media")
    record("media_session", str(app))


def init() -> None:
    """Start the background flush thread and subscribe to the event bus.
    Safe to call more than once."""
    global _flush_thread, _subscribed
    if _flush_thread is None:
        _stop_flag.clear()
        _flush_thread = threading.Thread(target=_flush_loop, name="habit-tracker-flush", daemon=True)
        _flush_thread.start()

    if _subscribed:
        return
    try:
        from state_engine.event_bus import event_bus
        event_bus.subscribe("ApplicationFocused", _on_app_focused)
        event_bus.subscribe("DownloadCompleted", _on_download_completed)
        event_bus.subscribe("CommandExecuted", _on_command_executed)
        event_bus.subscribe("MusicStarted", _on_music_started)
        _subscribed = True
        logger.info("habit_tracker: subscribed to event bus.")
    except Exception:
        logger.debug("habit_tracker: event bus not available yet.", exc_info=True)


def shutdown() -> None:
    _stop_flag.set()
    if _flush_thread is not None:
        _flush_thread.join(timeout=2.0)


def query_recent(kind: str, key: str, limit: int = 50) -> list[float]:
    """Return recent timestamps for a given (kind, key), most recent first."""
    try:
        conn = _connect()
        cur = conn.execute(
            "SELECT ts FROM events WHERE kind=? AND key=? ORDER BY ts DESC LIMIT ?",
            (kind, key, limit),
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def db_path() -> str:
    return _DB_PATH


def forget_key(key: str) -> int:
    """Delete every learned event for a given key."""
    removed = 0
    with _buffer_lock:
        before = len(_buffer)
        _buffer[:] = [row for row in _buffer if row[1] != key]
        removed += before - len(_buffer)
    try:
        conn = _connect()
        with conn:
            cur = conn.execute("DELETE FROM events WHERE key=?", (key,))
            removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.close()
    except Exception:
        logger.debug("habit_tracker: forget_key failed", exc_info=True)
    return removed


def decay_sweep() -> int:
    """Age out stale raw events older than RETENTION_DAYS. Returns rows pruned."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        conn = _connect()
        with conn:
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            pruned = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.close()
        return pruned
    except Exception:
        logger.debug("habit_tracker: decay_sweep failed", exc_info=True)
        return 0


def event_count() -> int:
    try:
        conn = _connect()
        cur = conn.execute("SELECT COUNT(*) FROM events")
        n = cur.fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def distinct_keys(kind: Optional[str] = None) -> int:
    try:
        conn = _connect()
        if kind:
            cur = conn.execute("SELECT COUNT(DISTINCT key) FROM events WHERE kind=?", (kind,))
        else:
            cur = conn.execute("SELECT COUNT(DISTINCT key) FROM events")
        n = cur.fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


__all__ = ["init", "shutdown", "record", "record_session", "record_volume_preference",
           "record_brightness_preference", "query_recent", "db_path",
           "forget_key", "decay_sweep", "event_count", "distinct_keys"]
