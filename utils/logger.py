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


import os
import sys
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from pathlib import Path
import queue

_BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR  = _BASE_DIR / "logs"
LOG_FILE  = LOGS_DIR  / "gama.log"

_LOG_FORMAT  = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_initialized = False

# Module-level reference so the listener is never garbage-collected.
_queue_listener: "QueueListener | None" = None


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

    # ── Async queue front-end — what the root logger actually calls
    log_queue: queue.Queue = queue.Queue(maxsize=-1)   # unbounded; never drops
    queue_handler = QueueHandler(log_queue)
    root.addHandler(queue_handler)

    # ── Background listener — drains the queue to the real handlers
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


__all__ = ["setup_logging", "get_logger"]


# ---------------------------------------------------------------------------
# Crash-safe logging
# ---------------------------------------------------------------------------
def flush_and_stop_logging() -> None:
    """Best-effort synchronous flush for fatal shutdown paths.

    Normal application logging is asynchronous. Fatal paths must flush the
    QueueListener before process termination so the last exception cannot be
    lost when sys.exit() occurs immediately afterward.
    """
    try:
        listener = globals().get("_listener")
        if listener is not None:
            listener.stop()
    except Exception:
        pass

    # Flush all handlers attached to the root logger.
    try:
        import logging as _logging
        for _handler in _logging.getLogger().handlers:
            try:
                _handler.flush()
            except Exception:
                pass
    except Exception:
        pass
