"""
core/notification_router.py — Smart notification routing
========================================================
Phase 3: route alerts by context.

Rules (defaults, overridable via data/notification_routing.json):
  - If user is in a meeting / DND → queue non-critical
  - If urgent → Telegram (when configured) + desktop
  - If asleep / deep sleep → critical only (Telegram if possible)
  - Otherwise → desktop toast (and Telegram if preferred)
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

log = get_logger(__name__)
_lock = threading.RLock()


@dataclass
class QueuedNote:
    title: str
    message: str
    kind: str
    priority: str
    ts: float = field(default_factory=time.time)


_queue: Deque[QueuedNote] = deque(maxlen=100)
_cfg_cache: Optional[Dict[str, Any]] = None
_cfg_mtime: float = 0.0


def _data_dir() -> Path:
    try:
        from utils.paths import get_base_dir
        DATA_DIR = get_base_dir()
        return Path(DATA_DIR)
    except Exception:
        import os
        env = (os.environ.get("GAMA_DATA") or "").strip()
        return Path(env) if env else Path.home() / ".gama"


def _cfg_path() -> Path:
    return _data_dir() / "notification_routing.json"


def _load_cfg() -> Dict[str, Any]:
    global _cfg_cache, _cfg_mtime
    path = _cfg_path()
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
        if _cfg_cache is not None and mtime == _cfg_mtime:
            return _cfg_cache
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _cfg_cache = json.load(f)
        else:
            _cfg_cache = {
                "prefer_telegram_for_urgent": True,
                "queue_when_meeting": True,
                "queue_when_dnd": True,
                "critical_only_when_asleep": True,
                "also_desktop_for_urgent": True,
            }
        _cfg_mtime = mtime
        return _cfg_cache or {}
    except Exception:
        return {
            "prefer_telegram_for_urgent": True,
            "queue_when_meeting": True,
            "queue_when_dnd": True,
            "critical_only_when_asleep": True,
            "also_desktop_for_urgent": True,
        }


def _context() -> Dict[str, Any]:
    """Best-effort user context from runtime / desktop."""
    ctx = {
        "asleep": False,
        "meeting": False,
        "dnd": False,
        "active_app": "",
    }
    try:
        from core.tool_dispatch import get_active_assistant
        asst = get_active_assistant()
        if asst is not None:
            if not getattr(asst, "_awake", True):
                ctx["asleep"] = True
            try:
                if getattr(asst, "_runtime", None) is not None:
                    if getattr(asst._runtime, "is_deep_sleep", False):
                        ctx["asleep"] = True
            except Exception:
                pass
    except Exception:
        pass
    try:
        from actions.desktop_context import desktop_context
        snap = str(desktop_context(action="active_window") or "").lower()
        ctx["active_app"] = snap[:120]
        meeting_kw = ("zoom", "meet.google", "teams", "webex", "skype")
        if any(k in snap for k in meeting_kw):
            ctx["meeting"] = True
    except Exception:
        pass
    try:
        from state_engine.user_settings import UserSettings
        # soft probe — may not exist
    except Exception:
        pass
    return ctx


def _send_desktop(title: str, message: str, kind: str) -> bool:
    try:
        from actions.desktop_notify import notify
        notify(title=title, message=message, kind=kind)
        return True
    except Exception as exc:
        log.debug("desktop notify failed: %s", exc)
        return False


def _send_telegram(message: str, kind: str) -> bool:
    try:
        from actions.telegram_sender import send_telegram_alert
        return bool(send_telegram_alert(message, kind=kind, force=True))
    except Exception as exc:
        log.debug("telegram notify failed: %s", exc)
        return False


def route_notification(
    title: str,
    message: str = "",
    *,
    kind: str = "info",
    priority: str = "normal",
) -> str:
    """
    Route a notification.

    priority: low | normal | high | urgent | critical
    """
    title = (title or "GAMA").strip()
    message = (message or "").strip()
    priority = (priority or "normal").lower().strip()
    kind = (kind or "info").lower().strip()
    cfg = _load_cfg()
    ctx = _context()

    # Critical / urgent always try to reach the user
    if priority in ("critical", "urgent", "high"):
        delivered = []
        if cfg.get("prefer_telegram_for_urgent", True):
            if _send_telegram(f"{title}: {message}", kind=kind):
                delivered.append("telegram")
        if cfg.get("also_desktop_for_urgent", True) or not delivered:
            if _send_desktop(title, message, kind):
                delivered.append("desktop")
        if delivered:
            return f"Delivered via {', '.join(delivered)} (priority={priority})."
        return "Failed to deliver urgent notification."

    # Asleep → queue unless critical (already handled)
    if ctx.get("asleep") and cfg.get("critical_only_when_asleep", True):
        with _lock:
            _queue.append(QueuedNote(title, message, kind, priority))
        return "Queued (user appears asleep). Will deliver on wake."

    # Meeting / DND → queue non-critical
    if ctx.get("meeting") and cfg.get("queue_when_meeting", True):
        with _lock:
            _queue.append(QueuedNote(title, message, kind, priority))
        return "Queued (meeting detected)."

    # Default: desktop
    if _send_desktop(title, message, kind):
        return "Delivered via desktop."
    # Fallback telegram
    if _send_telegram(f"{title}: {message}", kind=kind):
        return "Delivered via telegram (desktop failed)."
    return "Notification could not be delivered."


def flush_queue() -> str:
    """Deliver all queued notifications (e.g. after wake)."""
    with _lock:
        items = list(_queue)
        _queue.clear()
    if not items:
        return "Notification queue empty."
    ok = 0
    for n in items:
        if _send_desktop(n.title, n.message, n.kind):
            ok += 1
    return f"Flushed {ok}/{len(items)} queued notification(s)."


def queue_status() -> str:
    with _lock:
        n = len(_queue)
        if not n:
            return "Queue empty."
        preview = list(_queue)[-5:]
    lines = [f"{n} queued:"]
    for q in preview:
        lines.append(f"- [{q.priority}] {q.title}: {q.message[:60]}")
    return "\n".join(lines)


def notification_router(action: str = "status", **kwargs) -> str:
    action = (action or "status").lower().strip().replace("-", "_")
    if action in ("notify", "send", "route"):
        return route_notification(
            kwargs.get("title") or "GAMA",
            kwargs.get("message") or kwargs.get("text") or "",
            kind=kwargs.get("kind") or "info",
            priority=kwargs.get("priority") or "normal",
        )
    if action in ("flush", "drain", "deliver_queued"):
        return flush_queue()
    if action in ("status", "queue", "list"):
        return queue_status()
    if action in ("context",):
        return json.dumps(_context(), indent=2)
    return "Unknown notification_router action. Use: notify, flush, status, context."


__all__ = [
    "route_notification",
    "flush_queue",
    "notification_router",
]
