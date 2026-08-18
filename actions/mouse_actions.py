"""
actions/mouse_actions.py — Gama Mouse Control
===============================================
Mouse automation: move, click, double-click, right-click, scroll, drag.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
from typing import Optional

log = get_logger(__name__)
logger = log  # back-compat alias
def mouse_actions(action: str = "click", **kwargs) -> str:
    """Mouse automation for any active window."""
    action = (action or "click").lower().strip()

    if action == "move":
        return _move(int(kwargs.get("x", 0)), int(kwargs.get("y", 0)),
                     float(kwargs.get("duration", 0.3)))
    if action == "move_relative":
        return _move_rel(int(kwargs.get("dx", 0)), int(kwargs.get("dy", 0)))
    if action == "click":
        return _click(
            _int_or_none(kwargs.get("x")),
            _int_or_none(kwargs.get("y")),
            kwargs.get("button", "left"),
            int(kwargs.get("clicks", 1)),
        )
    if action == "double_click":
        return _click(
            _int_or_none(kwargs.get("x")),
            _int_or_none(kwargs.get("y")),
            "left", 2,
        )
    if action == "right_click":
        return _click(
            _int_or_none(kwargs.get("x")),
            _int_or_none(kwargs.get("y")),
            "right", 1,
        )
    if action == "scroll":
        return _scroll(int(kwargs.get("amount", -300)))
    if action == "drag":
        return _drag(
            int(kwargs.get("x", 0)), int(kwargs.get("y", 0)),
            float(kwargs.get("duration", 0.5)),
            kwargs.get("button", "left"),
        )
    if action == "position":
        return _position()
    if action == "screen_size":
        return _screen_size()
    return f"Unknown mouse action: {action}. Use: move, move_relative, click, double_click, right_click, scroll, drag, position, screen_size."


def _int_or_none(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except Exception:
        return None


def _get_ag():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui
    except Exception as exc:
        logger.error(f"pyautogui unavailable: {exc}")
        return None


def _move(x: int, y: int, duration: float = 0.3) -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        ag.moveTo(x, y, duration=duration)
        return f"Moved to ({x}, {y})."
    except Exception as exc:
        return f"Move failed: {exc}"


def _move_rel(dx: int, dy: int) -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        ag.moveRel(dx, dy, duration=0.2)
        return f"Moved by ({dx}, {dy})."
    except Exception as exc:
        return f"Move failed: {exc}"


def _click(x: Optional[int], y: Optional[int], button: str = "left",
           clicks: int = 1) -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        ag.click(x=x, y=y, clicks=clicks, button=button)
        return f"Clicked {button} ({clicks}x) at ({x}, {y})."
    except Exception as exc:
        return f"Click failed: {exc}"


def _scroll(amount: int) -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        ag.scroll(amount)
        return f"Scrolled {amount}."
    except Exception as exc:
        return f"Scroll failed: {exc}"


def _drag(x: int, y: int, duration: float = 0.5, button: str = "left") -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        ag.dragTo(x, y, duration=duration, button=button)
        return f"Dragged to ({x}, {y})."
    except Exception as exc:
        return f"Drag failed: {exc}"


def _position() -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        x, y = ag.position()
        return f"Mouse at ({x}, {y})."
    except Exception as exc:
        return f"Position failed: {exc}"


def _screen_size() -> str:
    ag = _get_ag()
    if ag is None:
        return "Mouse unavailable."
    try:
        w, h = ag.size()
        return f"Screen size: {w} x {h}."
    except Exception as exc:
        return f"Screen size failed: {exc}"


__all__ = ["mouse_actions"]
