"""
Gama - Logging Setup (production-optimised)
============================================
• File handler: INFO level — captures everything worth reviewing.
• Console handler: WARNING by default (near-zero idle noise).
  Set GAMA_DEBUG=1 to promote console to INFO for development.

Author : Vineet Machchal
"""

from __future__ import annotations

import logging

import re as _re_emoji
_EMOJI_RE = _re_emoji.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0000200D]"
)

class EmojiStripFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _EMOJI_RE.sub("", record.msg)
            if record.args and isinstance(record.args, tuple):
                record.args = tuple(
                    _EMOJI_RE.sub("", a) if isinstance(a, str) else a for a in record.args
                )
        except Exception:
            pass
        return True


import collections
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from pathlib import Path
import queue
from typing import Any, Dict, List, Optional

_BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR  = _BASE_DIR / "logs"
LOG_FILE  = LOGS_DIR  / "gama.log"

_LOG_FORMAT  = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_initialized = False

# Module-level reference so the listener is never garbage-collected.
_queue_listener: "QueueListener | None" = None

# ── In-Memory Ring Buffer for Real-Time Error Inspection ──────────────────
_ERROR_BUFFER_MAX = 100
_recent_errors_lock = threading.Lock()
_recent_errors: collections.deque[Dict[str, Any]] = collections.deque(maxlen=_ERROR_BUFFER_MAX)


class RecentErrorLogHandler(logging.Handler):
    """Captures WARNING, ERROR, and CRITICAL log records into an in-memory buffer."""
    def __init__(self, level: int = logging.WARNING):
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc_text = ""
            if record.exc_info:
                import traceback
                exc_text = "".join(traceback.format_exception(*record.exc_info))

            entry = {
                "timestamp": record.created,
                "time_str": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "traceback": exc_text,
                "filename": record.filename,
                "lineno": record.lineno,
            }
            with _recent_errors_lock:
                _recent_errors.append(entry)
        except Exception:
            pass


def get_recent_errors(limit: int = 20, min_level: str = "ERROR") -> List[Dict[str, Any]]:
    """Retrieve recent errors/warnings directly from in-memory ring buffer (0 ms latency)."""
    min_lvl_int = getattr(logging, min_level.upper(), logging.ERROR)
    with _recent_errors_lock:
        items = list(_recent_errors)

    filtered = []
    for item in items:
        lvl_int = getattr(logging, item["level"].upper(), logging.INFO)
        if lvl_int >= min_lvl_int:
            filtered.append(dict(item))

    return filtered[-limit:]


def get_log_tail(lines: int = 50, max_bytes: int = 256 * 1024, min_level: Optional[str] = None) -> List[str]:
    """Efficiently read the last N lines from gama.log without loading large files into memory.

    Uses backward seek from EOF, reading at most max_bytes.
    """
    if not LOG_FILE.exists():
        return []
    try:
        min_level_str = min_level.upper().strip() if min_level else None
        target_lines: List[str] = []
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return []

            read_size = min(file_size, max_bytes)
            f.seek(file_size - read_size, os.SEEK_SET)
            block = f.read(read_size).decode("utf-8", errors="replace")
            all_lines = block.splitlines()
            if read_size < file_size and len(all_lines) > 1:
                all_lines = all_lines[1:]

            if min_level_str:
                for line in all_lines:
                    if f"| {min_level_str}" in line or (min_level_str == "ERROR" and "| CRITICAL" in line):
                        target_lines.append(line)
            else:
                target_lines = all_lines

            return target_lines[-lines:]
    except Exception as exc:
        return [f"Error reading log tail: {exc}"]


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with a QueueHandler front-end so all log writes
    in hot paths (tool execution, audio callback, event loop) are non-blocking
    enqueue operations (~0 ms) rather than synchronous file I/O (~5-20 ms on
    Windows with AV scanning).  The QueueListener drains to the real handlers
    on a dedicated background thread."""
    global _initialized, _queue_listener
    if _initialized:
        return

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    file_level    = getattr(logging, level.upper(), logging.INFO)
    debug_mode    = os.environ.get("GAMA_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    console_level = logging.INFO if debug_mode else logging.INFO

    root = logging.getLogger()
    root.setLevel(min(file_level, console_level))

    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # ── Real (blocking) handlers — only seen by the background listener thread
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024,
        backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(file_level)

    try:
        from rich.logging import RichHandler
        ch = RichHandler(rich_tracebacks=True, show_path=False)
        ch.setFormatter(logging.Formatter("%(message)s", datefmt=_DATE_FORMAT))
    except ImportError:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
    ch.setLevel(console_level)

    # In-memory error capture handler (synchronous, 0 ms memory append)
    eh = RecentErrorLogHandler(level=logging.WARNING)
    root.addHandler(eh)

    # ── Async queue front-end — what the root logger actually calls for I/O
    log_queue: queue.Queue = queue.Queue(maxsize=-1)   # unbounded; never drops
    queue_handler = QueueHandler(log_queue)
    root.addHandler(queue_handler)

    # ── Background listener — drains the queue to the real blocking handlers
    _queue_listener = QueueListener(
        log_queue, fh, ch,
        respect_handler_level=True,
    )
    _queue_listener.start()

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    if not _initialized:
        setup_logging()
    return logging.getLogger(name)


def flush_and_stop_logging() -> None:
    """Best-effort synchronous flush for fatal shutdown paths."""
    try:
        listener = globals().get("_queue_listener")
        if listener is not None:
            listener.stop()
    except Exception:
        pass

    try:
        import logging as _logging
        for _handler in _logging.getLogger().handlers:
            try:
                _handler.flush()
            except Exception:
                pass
    except Exception:
        pass


__all__ = [
    "setup_logging",
    "get_logger",
    "get_recent_errors",
    "get_log_tail",
    "flush_and_stop_logging",
    "LOG_FILE",
]
