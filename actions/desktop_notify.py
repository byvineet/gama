"""
actions/desktop_notify.py — Gama Desktop (OS) Notifications
=============================================================
Real, native OS toast notifications (Windows Action Center / macOS
Notification Center / Linux libnotify via `plyer`).

Scope — deliberately narrow:
  - Reminders, alarms, timers, and goal check-ins already get native
    desktop notifications through their own dedicated pipeline
    (actions/reminder.py's `_notify()` / `fire_notification()`, used
    directly by actions/goal_tracker.py) — that is unrelated to this
    module and is not touched here.
  - This module exists purely for ON-DEMAND notifications: the user
    explicitly asking "notify me on my desktop that X" / "pop up a
    notification when Y". It does NOT subscribe to task-completed,
    task-failed, battery, or proactive-suggestion events — those stay
    voice/HUD-only so the notification tray doesn't turn into a firehose.

Design goals:
  - Best-effort & optional dependency: `plyer` is already listed in
    requirements.txt. Import is lazy and wrapped — Gama must never
    crash or block because a notification backend is missing or a
    DBus/WinRT call hangs.
  - Fire-and-forget: every call runs on a short-lived daemon thread so
    a slow OS notification call can never stall the voice pipeline,
    audio loop, or Qt event loop.
  - Still rate-limited/de-duped per "kind" as a safety net, even
    though nothing auto-fires anymore — protects against a chatty
    caller or repeated identical requests.

Usage
-----
    from actions.desktop_notify import notify
    notify("Download finished", "report.pdf saved to Downloads", force=True)

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import ctypes
import logging
import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_APP_NAME = "Gama"
_APP_ID = "Gama"
_DEFAULT_TIMEOUT = 6          # seconds the toast stays visible
_DEFAULT_COOLDOWN = 10        # min seconds between two notifications of the same kind

# Register AppUserModelID on Windows so Action Center toast notifications work in executable builds
if platform.system() == "Windows":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_ID)
    except Exception as _e:
        logger.debug(f"Could not set AppUserModelID: {_e}")


@dataclass
class _NotifyState:
    enabled: bool = True
    last_fired: Dict[str, float] = field(default_factory=dict)
    icon_path: Optional[str] = None


_state = _NotifyState()
_lock = threading.Lock()


def _backend_send(title: str, message: str, timeout: int) -> None:
    """Actual OS call — always run off the calling thread by notify()."""
    # 1. Try PySide6 QSystemTrayIcon if active
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
        app = QApplication.instance()
        if app:
            tray_icons = app.findChildren(QSystemTrayIcon)
            if tray_icons:
                tray = tray_icons[0]
                tray.showMessage(title[:64] or _APP_NAME, message[:256] if message else "", QSystemTrayIcon.Information, timeout * 1000)
                return
    except Exception as exc:
        logger.debug(f"desktop_notify: PySide6 tray fallback notice failed ({exc})")

    # 2. Try plyer notification
    try:
        from plyer import notification as _plyer_notification
        _plyer_notification.notify(
            title=title[:64] or _APP_NAME,
            message=message[:256] if message else "",
            app_name=_APP_NAME,
            app_icon=_state.icon_path or "",
            timeout=timeout,
        )
        return
    except Exception as exc:
        logger.warning(f"desktop_notify: plyer backend unavailable/failed ({exc})")

    # 3. Windows-specific fallback if plyer itself isn't installed/working.
    try:
        if platform.system() == "Windows":
            from win10toast import ToastNotifier  # optional, not a hard dependency
            ToastNotifier().show_toast(
                title[:64] or _APP_NAME, message[:256] if message else "",
                duration=timeout, threaded=True,
            )
            return
    except Exception as exc:
        logger.warning(f"desktop_notify: win10toast fallback also failed ({exc}).")

    logger.warning(
        "desktop_notify: NO notification backend succeeded (tray/plyer/win10toast "
        "all unavailable) — this notification was silently dropped. If this is a "
        "PyInstaller build, check that plyer/win10toast were bundled (see Gama.spec)."
    )


def _ready(kind: str, cooldown: float) -> bool:
    with _lock:
        if not _state.enabled:
            return False
        last = _state.last_fired.get(kind, 0.0)
        if time.time() - last < cooldown:
            return False
        _state.last_fired[kind] = time.time()
        return True


def notify(title: str, message: str = "", kind: str = "manual",
           cooldown: float = _DEFAULT_COOLDOWN, force: bool = True) -> None:
    """Fire a desktop notification. Non-blocking, best-effort.

    `force=True` is the default here (unlike a general-purpose alert
    pipeline) because every caller of this module is, by design, either
    the user's own explicit request or another module that already
    decided this is important enough to show — so the rate limit is a
    safety net against accidental repeats, not a general throttle.
    """
    dedup_key = kind or title
    if not force and not _ready(dedup_key, cooldown):
        return
    if force:
        with _lock:
            _state.last_fired[dedup_key] = time.time()
    threading.Thread(
        target=_backend_send, args=(title, message, _DEFAULT_TIMEOUT),
        daemon=True, name="gama-desktop-notify",
    ).start()


def configure(icon_path: Optional[str] = None) -> None:
    """Set shared options (currently just the icon). No event-bus
    subscriptions are made — this module never auto-fires; see module
    docstring. Safe to call once at startup, kept for a stable API."""
    with _lock:
        if icon_path:
            _state.icon_path = icon_path


def set_enabled(enabled: bool) -> None:
    with _lock:
        _state.enabled = enabled


def desktop_notify(action: str = "status", **kwargs) -> str:
    """Tool entrypoint — matches the actions/*.py `(action, **kwargs)`
    convention. Only fires when the user explicitly asks:
        "Notify me on my desktop that the build is done."
        "Pop up a notification when this finishes."
        "Stop sending desktop notifications."
    Reminders, alarms, timers, and goal check-ins already notify on
    their own — this is not the pathway for those.
    """
    action = (action or "status").lower().strip()
    if action == "status":
        with _lock:
            return f"Desktop notifications are {'on' if _state.enabled else 'off'}."
    if action in ("disable", "off", "stop"):
        set_enabled(False)
        return "Okay, I'll stop sending desktop notifications."
    if action in ("enable", "on", "resume"):
        set_enabled(True)
        return "Desktop notifications are back on."
    if action == "send":
        title = kwargs.get("title") or "Gama"
        message = kwargs.get("message", "")
        notify(title, message, kind=kwargs.get("kind", "manual"), force=True)
        return "Sent."
    return "Unknown desktop_notify action. Use: status, enable, disable, send."


__all__ = ["configure", "notify", "set_enabled", "desktop_notify"]
