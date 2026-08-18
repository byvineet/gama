"""
automation/providers/windows_provider.py — Window Automation.

Execution priority (per spec): native Win32 API (win32gui/win32con) first,
falling back to `pygetwindow` when pywin32 isn't installed (e.g. dev on
non-Windows). Window handles are cached per-process-id to satisfy the
"reuse window handles" requirement instead of re-enumerating every call.
"""

from __future__ import annotations

import sys
import time
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger
from automation.models import ActionResult, Capability, ExecutionMethod
from automation.registry import registry

log = get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"

try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    import win32process  # type: ignore
    _HAVE_WIN32 = _IS_WINDOWS
except Exception:
    _HAVE_WIN32 = False

try:
    import pygetwindow as gw  # type: ignore
    _HAVE_PYGETWINDOW = True
except Exception:
    _HAVE_PYGETWINDOW = False


# ── handle cache (title-substring -> last known hwnd) ───────────────────────
_handle_cache: Dict[str, int] = {}


def _enum_windows() -> List[Tuple[int, str]]:
    """List (hwnd, title) for every visible top-level window."""
    if not _HAVE_WIN32:
        return []
    out: List[Tuple[int, str]] = []

    def _cb(hwnd, _extra):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            out.append((hwnd, win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def _find_hwnd(title_substr: str) -> Optional[int]:
    title_l = title_substr.lower()
    cached = _handle_cache.get(title_l)
    if cached and _HAVE_WIN32 and win32gui.IsWindow(cached):
        return cached
    for hwnd, title in _enum_windows():
        if title_l in title.lower():
            _handle_cache[title_l] = hwnd
            return hwnd
    return None


# ── capability implementations ──────────────────────────────────────────────

def _focus(title: str, **_) -> ActionResult:
    if _HAVE_WIN32:
        hwnd = _find_hwnd(title)
        if not hwnd:
            return ActionResult(ok=False, message=f"No window matching '{title}'")
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return ActionResult(ok=True, message=f"Focused '{title}'", method=ExecutionMethod.NATIVE_API)
        except Exception as exc:
            return ActionResult(ok=False, message=f"Focus failed: {exc}")
    if _HAVE_PYGETWINDOW:
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return ActionResult(ok=False, message=f"No window matching '{title}'")
        matches[0].activate()
        return ActionResult(ok=True, message=f"Focused '{title}'", method=ExecutionMethod.ACCESSIBILITY)
    return ActionResult(ok=False, message="No window backend available")


def _verify_focus(title: str, **_) -> Tuple[bool, str]:
    if _HAVE_WIN32:
        fg = win32gui.GetForegroundWindow()
        active_title = win32gui.GetWindowText(fg)
        ok = title.lower() in active_title.lower()
        return ok, active_title
    return True, "unverified (no win32 backend)"


def _close(title: str, **_) -> ActionResult:
    if _HAVE_WIN32:
        hwnd = _find_hwnd(title)
        if not hwnd:
            return ActionResult(ok=False, message=f"No window matching '{title}'")
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return ActionResult(ok=True, message=f"Closed '{title}'")
    if _HAVE_PYGETWINDOW:
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return ActionResult(ok=False, message=f"No window matching '{title}'")
        matches[0].close()
        return ActionResult(ok=True, message=f"Closed '{title}'", method=ExecutionMethod.ACCESSIBILITY)
    return ActionResult(ok=False, message="No window backend available")


def _move(title: str, x: int, y: int, **_) -> ActionResult:
    if _HAVE_WIN32:
        hwnd = _find_hwnd(title)
        if not hwnd:
            return ActionResult(ok=False, message=f"No window matching '{title}'")
        _, _, w, h = win32gui.GetWindowRect(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win32gui.MoveWindow(hwnd, x, y, right - left, bottom - top, True)
        return ActionResult(ok=True, message=f"Moved '{title}' to ({x},{y})")
    if _HAVE_PYGETWINDOW:
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return ActionResult(ok=False, message=f"No window matching '{title}'")
        matches[0].moveTo(x, y)
        return ActionResult(ok=True, message=f"Moved '{title}' to ({x},{y})", method=ExecutionMethod.ACCESSIBILITY)
    return ActionResult(ok=False, message="No window backend available")


def _verify_position(title: str, x: int, y: int, **_) -> Tuple[bool, str]:
    if _HAVE_WIN32:
        hwnd = _find_hwnd(title)
        if not hwnd:
            return False, "window not found"
        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        ok = abs(left - x) <= 5 and abs(top - y) <= 5
        return ok, f"at ({left},{top})"
    return True, "unverified"


def _snap(title: str, side: str = "left", **_) -> ActionResult:
    """Snap to left/right/max half of the primary monitor."""
    if not _HAVE_WIN32:
        return ActionResult(ok=False, message="Snap requires win32 backend")
    hwnd = _find_hwnd(title)
    if not hwnd:
        return ActionResult(ok=False, message=f"No window matching '{title}'")
    try:
        import ctypes
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
    except Exception:
        screen_w, screen_h = 1920, 1080

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    half_w = screen_w // 2
    if side == "left":
        rect = (0, 0, half_w, screen_h)
    elif side == "right":
        rect = (half_w, 0, half_w, screen_h)
    elif side == "max":
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return ActionResult(ok=True, message=f"Maximized '{title}'")
    else:
        return ActionResult(ok=False, message=f"Unknown snap side '{side}'")

    win32gui.MoveWindow(hwnd, *rect, True)
    return ActionResult(ok=True, message=f"Snapped '{title}' {side}")


def _minimize(title: str, **_) -> ActionResult:
    if not _HAVE_WIN32:
        return ActionResult(ok=False, message="Requires win32 backend")
    hwnd = _find_hwnd(title)
    if not hwnd:
        return ActionResult(ok=False, message=f"No window matching '{title}'")
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return ActionResult(ok=True, message=f"Minimized '{title}'")


def _list_windows(**_) -> ActionResult:
    windows = _enum_windows() if _HAVE_WIN32 else (
        [(0, w.title) for w in gw.getAllWindows()] if _HAVE_PYGETWINDOW else []
    )
    titles = [t for _, t in windows if t.strip()]
    return ActionResult(ok=True, message=f"{len(titles)} windows open", data={"titles": titles})


def _close_all_except(keep: str, **_) -> ActionResult:
    if not _HAVE_WIN32:
        return ActionResult(ok=False, message="Requires win32 backend")
    keep_l = keep.lower()
    closed = []
    for hwnd, title in _enum_windows():
        if keep_l not in title.lower():
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed.append(title)
            except Exception:
                pass
    return ActionResult(ok=True, message=f"Closed {len(closed)} window(s), kept '{keep}'",
                         data={"closed": closed})


def _get_active_window(**_) -> ActionResult:
    if _HAVE_WIN32:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        return ActionResult(ok=True, message=title, data={"title": title, "hwnd": hwnd})
    return ActionResult(ok=False, message="Requires win32 backend")


def register() -> None:
    registry.register_many([
        Capability("window.focus", _focus, verify=_verify_focus, cost=1, speed_ms=20,
                   description="Bring a window to the foreground",
                   keywords=("focus", "switch to", "bring up")),
        Capability("window.close", _close, cost=1, speed_ms=20,
                   description="Close a window", keywords=("close", "quit")),
        Capability("window.move", _move, verify=_verify_position, cost=1, speed_ms=20,
                   description="Move a window to coordinates", keywords=("move window", "reposition window")),
        Capability("window.snap", _snap, cost=1, speed_ms=20,
                   description="Snap/maximize a window", keywords=("snap", "maximize", "left", "right")),
        Capability("window.minimize", _minimize, cost=1, speed_ms=20,
                   description="Minimize a window", keywords=("minimize",)),
        Capability("window.list", _list_windows, cost=1, speed_ms=30,
                   description="List open windows", keywords=("list windows", "what's open")),
        Capability("window.close_all_except", _close_all_except, cost=2, speed_ms=50,
                   description="Close every window except one",
                   keywords=("close every", "close all except")),
        Capability("window.active", _get_active_window, cost=0, speed_ms=5,
                   description="Get the currently focused window",
                   keywords=("active window", "current window")),
    ])


register()
