"""
actions/ui_automation.py — Gama Accessibility-First UI Automation
====================================================================
Windows UI Automation (UIA) is the *primary* way Gama interacts with
on-screen controls. It reads the actual accessibility tree exposed by
an application (button/text/menu objects, their names, their bounding
rectangles) instead of guessing coordinates from a screenshot, so:

  - It survives DPI scaling, window resizing, and theme changes.
  - It can find "the Save button" by its accessible name even if it
    moved, instead of a hard-coded (x, y).
  - It's much cheaper than a screenshot + vision-model round trip.

Computer vision (actions/screen_agent.py, Gemini Vision) is kept as
the FALLBACK for apps that don't expose a usable UIA tree — games,
canvas-based web apps, custom-drawn UI, etc. See `resolve_click_target`
in actions/computer_agent.py for how the two are chained together.

Built on `pywinauto` (already a dependency, Windows-only), using its
`uia` backend specifically — the `win32` backend only understands
classic Win32 controls, `uia` also understands WPF/UWP/Electron/modern
apps (VS Code, Spotify, Settings, Edge/Chrome's own chrome).

Everything here is best-effort and never raises out to the caller —
every public function returns None / False / a plain string on
failure so a missing accessibility tree just means "fall back to CV",
never a crash.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import platform
import time
from typing import List, Optional, Tuple

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()

# Cache the Desktop() object — constructing it repeatedly is not free
# (COM init on Windows) and it's stateless enough to reuse across calls.
_desktop = None


def uia_available() -> bool:
    """Cheap capability check — Windows + pywinauto importable."""
    if _OS != "Windows":
        return False
    try:
        import pywinauto  # noqa: F401
        return True
    except Exception:
        return False


def _get_desktop():
    global _desktop
    if _desktop is None:
        from pywinauto import Desktop
        _desktop = Desktop(backend="uia")
    return _desktop


def _find_window(title_substr: str = "", timeout: float = 3.0):
    """Best-effort find of a top-level window whose title contains
    `title_substr` (case-insensitive). Empty string -> foreground window."""
    if not uia_available():
        return None
    try:
        desktop = _get_desktop()
        if not title_substr:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            return desktop.window(handle=hwnd)
        deadline = time.time() + timeout
        needle = title_substr.lower()
        while time.time() < deadline:
            for w in desktop.windows():
                try:
                    t = (w.window_text() or "").lower()
                    if needle in t:
                        return w
                except Exception:
                    continue
            time.sleep(0.2)
        return None
    except Exception as exc:
        logger.debug(f"ui_automation._find_window failed: {exc}")
        return None


def list_controls(window_title: str = "", max_items: int = 40) -> List[str]:
    """Return a short human-readable list of clickable/interactable
    controls in a window — used both for debugging and so Gama can
    reason about 'what's on screen' without a screenshot."""
    win = _find_window(window_title)
    if win is None:
        return []
    out: List[str] = []
    try:
        for ctrl in win.descendants():
            try:
                name = (ctrl.window_text() or "").strip()
                ctype = ctrl.friendly_class_name()
                if name and ctype in (
                    "Button", "MenuItem", "TabItem", "ListItem", "Edit",
                    "CheckBox", "RadioButton", "Hyperlink", "Text",
                ):
                    out.append(f"{ctype}: {name}")
                    if len(out) >= max_items:
                        break
            except Exception:
                continue
    except Exception as exc:
        logger.debug(f"ui_automation.list_controls failed: {exc}")
    return out


def find_element(window_title: str = "", text: str = "",
                  control_type: str = "") -> Optional[Tuple[int, int, int, int]]:
    """Find an element by (partial, case-insensitive) accessible name,
    optionally scoped by control type ('Button', 'Edit', ...).
    Returns its screen rectangle (left, top, right, bottom) or None."""
    win = _find_window(window_title)
    if win is None or not text:
        return None
    needle = text.lower()
    try:
        candidates = win.descendants()
        # Prefer an exact (case-insensitive) name match, then fall back
        # to substring match — mirrors how a human reads a label.
        exact, partial = None, None
        for ctrl in candidates:
            try:
                if control_type and ctrl.friendly_class_name() != control_type:
                    continue
                name = (ctrl.window_text() or "").strip()
                if not name:
                    continue
                if name.lower() == needle and exact is None:
                    exact = ctrl
                elif needle in name.lower() and partial is None:
                    partial = ctrl
            except Exception:
                continue
        target = exact or partial
        if target is None:
            return None
        rect = target.rectangle()
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as exc:
        logger.debug(f"ui_automation.find_element failed: {exc}")
        return None


def click_element(window_title: str = "", text: str = "",
                   control_type: str = "", double: bool = False) -> bool:
    """Locate an element via UIA and invoke/click it directly through
    the accessibility API (not pyautogui coordinates) when possible —
    this works even if the window is partially off-screen or occluded.
    Falls back to a coordinate click at the element's center if the
    control doesn't support a direct invoke pattern."""
    win = _find_window(window_title)
    if win is None or not text:
        return False
    needle = text.lower()
    try:
        for ctrl in win.descendants():
            try:
                if control_type and ctrl.friendly_class_name() != control_type:
                    continue
                name = (ctrl.window_text() or "").strip().lower()
                if name and (name == needle or needle in name):
                    ctrl.set_focus()
                    if double:
                        ctrl.double_click_input()
                    else:
                        try:
                            ctrl.invoke()  # accessibility-native, no coordinates
                        except Exception:
                            ctrl.click_input()  # fallback: click at its center
                    return True
            except Exception:
                continue
        return False
    except Exception as exc:
        logger.debug(f"ui_automation.click_element failed: {exc}")
        return False


def type_into_element(window_title: str = "", text: str = "",
                       value: str = "", control_type: str = "Edit") -> bool:
    """Focus a text/edit control by name and type into it via UIA
    (set_focus + type_keys), which is more reliable than a blind
    keyboard.type() because we know the right control has focus first."""
    win = _find_window(window_title)
    if win is None:
        return False
    needle = (text or "").lower()
    try:
        for ctrl in win.descendants():
            try:
                if ctrl.friendly_class_name() != control_type:
                    continue
                name = (ctrl.window_text() or "").strip().lower()
                if not needle or needle in name:
                    ctrl.set_focus()
                    ctrl.type_keys(value, with_spaces=True, pause=0.02)
                    return True
            except Exception:
                continue
        return False
    except Exception as exc:
        logger.debug(f"ui_automation.type_into_element failed: {exc}")
        return False


def window_exists(title_substr: str) -> bool:
    return _find_window(title_substr, timeout=1.0) is not None


def ui_automation(action: str = "list", **kwargs) -> str:
    """Tool entrypoint (matches the actions/*.py `(action, **kwargs)`
    convention). Exposed mainly for debugging / direct voice commands
    like 'what buttons are on this window'."""
    action = (action or "list").lower().strip()
    if not uia_available():
        return "Accessibility automation isn't available on this system (needs Windows + pywinauto)."

    window_title = kwargs.get("window_title", "") or kwargs.get("window", "")

    if action == "list":
        items = list_controls(window_title)
        return "\n".join(items) if items else "No labeled controls found in that window."
    if action == "click":
        ok = click_element(window_title, kwargs.get("text", ""),
                            kwargs.get("control_type", ""), bool(kwargs.get("double", False)))
        return f"Clicked '{kwargs.get('text', '')}'." if ok else f"Couldn't find a control named '{kwargs.get('text', '')}'."
    if action == "type":
        ok = type_into_element(window_title, kwargs.get("text", ""), kwargs.get("value", ""),
                                kwargs.get("control_type", "Edit"))
        return "Typed successfully." if ok else "Couldn't find a matching text field."
    if action == "exists":
        return "Yes." if window_exists(window_title) else "No."
    return f"Unknown ui_automation action: {action}. Use: list, click, type, exists."


__all__ = [
    "uia_available", "list_controls", "find_element", "click_element",
    "type_into_element", "window_exists", "ui_automation",
]
