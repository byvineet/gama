"""
actions/keyboard_actions.py — Gama Keyboard Control
====================================================
Type text, press keys, send hotkeys into the ACTIVE window
(any app — not just browser).

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import time

log = get_logger(__name__)
logger = log  # back-compat alias
def keyboard_actions(action: str = "type", **kwargs) -> str:
    """Keyboard automation for any active window."""
    action = (action or "type").lower().strip()

    if action == "type":
        return _type(kwargs.get("text", ""), float(kwargs.get("interval", 0.02)))
    if action == "press":
        return _press(kwargs.get("key", "enter"))
    if action == "hotkey":
        keys = kwargs.get("keys", "")
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        return _hotkey(keys)
    if action == "hold":
        return _hold(kwargs.get("key", "shift"), float(kwargs.get("duration", 0.5)))
    if action == "copy":
        return _hotkey(["ctrl", "c"])
    if action == "paste":
        return _hotkey(["ctrl", "v"])
    if action == "cut":
        return _hotkey(["ctrl", "x"])
    if action == "select_all":
        return _hotkey(["ctrl", "a"])
    if action == "undo":
        return _hotkey(["ctrl", "z"])
    if action == "redo":
        return _hotkey(["ctrl", "y"])
    if action == "save":
        return _hotkey(["ctrl", "s"])
    if action == "find":
        return _hotkey(["ctrl", "f"])
    if action == "new_tab":
        return _hotkey(["ctrl", "t"])
    if action == "close_tab":
        return _hotkey(["ctrl", "w"])
    if action == "switch_window":
        return _hotkey(["alt", "tab"])
    return f"Unknown keyboard action: {action}. Use: type, press, hotkey, hold, copy, paste, cut, select_all, undo, redo, save, find, new_tab, close_tab, switch_window."


def _get_kb():
    try:
        from pynput.keyboard import Controller
        return Controller()
    except Exception as exc:
        logger.error(f"pynput unavailable: {exc}")
        return None


def _type(text: str, interval: float = 0.02) -> str:
    """Type text into the active window.

    Short strings use pynput character-by-character typing.
    Longer strings (>= 40 chars) are pasted via the clipboard so the
    Live session is not stalled for seconds (a known WebSocket 1011
    trigger when the user asks to open Notepad and write a paragraph).
    """
    if not text:
        return "What text should I type?"
    # Prefer paste for anything non-trivial — faster and does not block
    # the Live receive loop for multi-second stretches.
    if len(text) >= 40:
        try:
            return _paste_text(text)
        except Exception as exc:
            logger.warning(f"clipboard paste failed, falling back to type: {exc}")
    kb = _get_kb()
    if kb is None:
        return "Keyboard automation unavailable."
    try:
        # pynput Controller.type ignores interval; keep signature for callers.
        kb.type(text)
        return f"Typed {len(text)} characters."
    except Exception as exc:
        return f"Type failed: {exc}"


def _paste_text(text: str) -> str:
    """Put *text* on the clipboard and Ctrl+V into the active window."""
    # Prefer pyperclip; fall back to tkinter clipboard if needed.
    try:
        import pyperclip
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
        pyperclip.copy(text)
        time.sleep(0.05)
        result = _hotkey(["ctrl", "v"])
        time.sleep(0.05)
        # Best-effort restore of previous clipboard contents
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
        if "failed" in result.lower():
            return result
        return f"Pasted {len(text)} characters."
    except Exception:
        pass
    # tkinter fallback (always available on standard Windows Python)
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        time.sleep(0.05)
        result = _hotkey(["ctrl", "v"])
        try:
            root.destroy()
        except Exception:
            pass
        if "failed" in result.lower():
            return result
        return f"Pasted {len(text)} characters."
    except Exception as exc:
        return f"Paste failed: {exc}"


def _press(key: str) -> str:
    if not key:
        key = "enter"
    kb = _get_kb()
    if kb is None:
        return "Keyboard unavailable."
    try:
        from pynput.keyboard import Key
        key_map = {
            "enter": Key.enter, "return": Key.enter,
            "tab": Key.tab, "esc": Key.esc, "escape": Key.esc,
            "space": Key.space, "backspace": Key.backspace,
            "delete": Key.delete, "del": Key.delete,
            "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
            "home": Key.home, "end": Key.end,
            "page_up": Key.page_up, "page_down": Key.page_down,
            "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
            "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
            "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
        }
        k = key_map.get(key.lower(), key)
        kb.press(k)
        kb.release(k)
        return f"Pressed: {key}"
    except Exception as exc:
        return f"Press failed: {exc}"


def _hotkey(keys: list) -> str:
    if not keys:
        return "Which keys for the hotkey?"
    kb = _get_kb()
    if kb is None:
        return "Keyboard unavailable."
    try:
        from pynput.keyboard import Key
        key_map = {
            "ctrl": Key.ctrl_l, "control": Key.ctrl_l,
            "alt": Key.alt_l, "shift": Key.shift_l,
            "cmd": Key.cmd, "win": Key.cmd, "super": Key.cmd,
            "esc": Key.esc, "tab": Key.tab, "enter": Key.enter,
            "space": Key.space, "backspace": Key.backspace,
            "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
            "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
            "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
            "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
        }
        # Press all keys
        held = []
        for k in keys:
            k_lower = k.lower()
            if k_lower in key_map:
                kb.press(key_map[k_lower])
                held.append(key_map[k_lower])
            elif len(k) == 1:
                kb.press(k)
                held.append(k)
            else:
                # Try as Key attribute
                try:
                    key_obj = getattr(Key, k_lower, None)
                    if key_obj:
                        kb.press(key_obj)
                        held.append(key_obj)
                except Exception:
                    pass
        # Release in reverse order
        for k in reversed(held):
            kb.release(k)
        return f"Hotkey: {'+'.join(keys)}"
    except Exception as exc:
        return f"Hotkey failed: {exc}"


def _hold(key: str, duration: float = 0.5) -> str:
    if not key:
        key = "shift"
    kb = _get_kb()
    if kb is None:
        return "Keyboard unavailable."
    try:
        from pynput.keyboard import Key
        key_map = {
            "shift": Key.shift_l, "ctrl": Key.ctrl_l, "alt": Key.alt_l,
            "cmd": Key.cmd, "win": Key.cmd,
        }
        k = key_map.get(key.lower(), key)
        kb.press(k)
        time.sleep(duration)
        kb.release(k)
        return f"Held {key} for {duration}s"
    except Exception as exc:
        return f"Hold failed: {exc}"


__all__ = ["keyboard_actions"]
