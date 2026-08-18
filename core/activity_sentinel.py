"""
core/activity_sentinel.py — Lightweight activity awareness
==========================================================
Occasionally samples desktop context. Project check-ins fire only when the
active project has had no update / memory touch for a long time — not on a
fixed short timer.

Design goals: cheap, never spam, respect DND / class / deep work windows.
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import threading
import time
from typing import Callable, Optional

log = get_logger(__name__)
# Tunables
_POLL_S = 5 * 60.0                 # look every 5 minutes (cheap)
_IDLE_SECONDS = 20 * 60            # same window this long before considering idle
_MIN_CHECKIN_GAP_S = 6 * 60 * 60   # hard floor between any spoken check-ins (6h)
_PROJECT_STALE_S = 18 * 60 * 60    # only ask about a project if no update in ~18h
_STARTUP_GRACE_S = 30 * 60         # no check-ins for first 30 min after boot

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_started = False
_last_checkin_ts = 0.0
_last_sig = ""
_sig_since = 0.0
_on_checkin: Optional[Callable[[str], None]] = None
_boot_ts = time.time()


def configure(on_checkin: Callable[[str], None] | None = None) -> None:
    global _on_checkin
    _on_checkin = on_checkin


def _class_in_session() -> bool:
    try:
        from actions.class_schedule import class_schedule
        text = str(class_schedule("now") or "").lower()
        if any(k in text for k in ("ongoing", "in progress", "current class", "right now")):
            return True
    except Exception:
        pass
    return False


def _important_window(title: str, app: str) -> bool:
    blob = f"{title} {app}".lower()
    markers = (
        "exam", "test", "leetcode", "codeforces", "visual studio", "pycharm",
        "intellij", "figma", "photoshop", "premiere", "after effects",
        "blender", "meeting", "zoom", "teams", "meet -", "classroom",
        "obs studio", "presentation", "powerpoint",
    )
    return any(m in blob for m in markers)


def _snapshot() -> dict:
    try:
        from actions.desktop_context import get_desktop_snapshot
        return get_desktop_snapshot() or {}
    except Exception:
        return {}


def _signature(snap: dict) -> str:
    app = str(snap.get("active_app") or snap.get("app") or "")
    title = str(snap.get("active_window") or snap.get("window_title") or "")
    return f"{app}|{title}".strip("|")


def _project_last_update_ts(proj: dict) -> float:
    """Most recent signal that the project is being actively worked on."""
    candidates = [
        float(proj.get("last_update_ts") or 0),
        float(proj.get("last_active_ts") or 0),
        float(proj.get("last_checkin_ts") or 0),
        float(proj.get("created_ts") or 0),
    ]
    # Also consult long-term memory touch if available
    try:
        from memory import long_term as lt
        name = str(proj.get("name") or "")
        if name:
            hits = lt.search(name, top_k=1, project=name)
            if hits:
                # MemoryHit may expose ts / last_access
                h0 = hits[0]
                for attr in ("last_access", "updated_ts", "ts", "created"):
                    val = getattr(h0, attr, None) if not isinstance(h0, dict) else h0.get(attr)
                    if val:
                        try:
                            candidates.append(float(val))
                        except (TypeError, ValueError):
                            pass
    except Exception:
        pass
    return max(candidates) if candidates else 0.0


def _maybe_checkin(snap: dict) -> None:
    global _last_checkin_ts, _last_sig, _sig_since

    if _on_checkin is None:
        return
    now = time.time()
    if now - _boot_ts < _STARTUP_GRACE_S:
        return
    if now - _last_checkin_ts < _MIN_CHECKIN_GAP_S:
        return

    try:
        from memory.project_context import is_dnd, get_active_project
        if is_dnd():
            return
    except Exception:
        return

    if _class_in_session():
        return

    sig = _signature(snap)
    if sig != _last_sig:
        _last_sig = sig
        _sig_since = now
        return

    if now - _sig_since < _IDLE_SECONDS:
        return

    title = str(snap.get("active_window") or snap.get("window_title") or "")
    app = str(snap.get("active_app") or snap.get("app") or "")
    if _important_window(title, app):
        return

    # Only project check-ins — and only when the project is long-stale.
    try:
        from memory.project_context import get_active_project, touch_checkin
        proj = get_active_project()
    except Exception:
        return

    if not proj or not proj.get("name"):
        return

    last_upd = _project_last_update_ts(proj)
    if last_upd and (now - last_upd) < _PROJECT_STALE_S:
        return  # recent activity / memory — stay quiet

    touch_checkin()
    msg = f"Sir, how's your project {proj['name']} going? Any update?"
    _last_checkin_ts = now
    try:
        _on_checkin(msg)
    except Exception as exc:
        log.debug("activity check-in callback failed: %s", exc)


def _loop() -> None:
    while not _stop.wait(_POLL_S):
        try:
            snap = _snapshot()
            if snap:
                _maybe_checkin(snap)
        except Exception as exc:
            log.debug("activity_sentinel tick failed: %s", exc)


def start() -> None:
    global _thread, _started
    if _started:
        return
    _started = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="gama-activity-sentinel", daemon=True)
    _thread.start()
    log.info("activity_sentinel started (stale-project check-ins only)")


def stop() -> None:
    global _started
    _stop.set()
    _started = False


__all__ = ["configure", "start", "stop"]
