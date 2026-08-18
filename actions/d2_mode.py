"""
actions/d2_mode.py — Enter / exit Gama D2 spatial interface
===========================================================
D2 is a secondary presentation mode. It must never activate automatically.
Only explicit user requests route here.

Examples:
  "Gama, switch to D2."
  "Enter the spatial interface."
  "Return to Nexus."
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("gama.d2_mode")

def _stop_nexus_gestures() -> None:
    """Ensure backend gesture camera is off (avoids dual capture / leftover preview)."""
    # gesture_control removed; D2 manages its own camera lifecycle.
    return



def _push_d2(action: str, **extra: Any) -> bool:
    try:
        from core.web_bridge import broadcast_sync
        payload: Dict[str, Any] = {"action": action, **extra}
        broadcast_sync({"type": "d2", "data": payload})
        return True
    except Exception as exc:
        log.debug("d2 push failed: %s", exc)
        return False


def d2_mode(
    action: str = "status",
    tasks: Optional[List[dict]] = None,
    reminders: Optional[List[dict]] = None,
    items: Optional[List[dict]] = None,
    value: float = 0.0,
    state: str = "idle",
    **kwargs: Any,
) -> str:
    """
    Control the D2 spatial interface.

    action:
      enter | exit | status | show_tasks | show_reminders | show_news |
      visualize_cpu | visualize_ram | clear | set_state
    """
    act = (action or "status").strip().lower()

    if act in ("enter", "open", "show", "enable", "start", "switch"):
        # Free the backend camera — D2 uses browser MediaPipe instead
        _stop_nexus_gestures()
        # Mutual exclusion with H1
        try:
            from core.web_bridge import broadcast_sync
            broadcast_sync({"type": "h1", "data": {"action": "exit"}})
        except Exception:
            pass
        ok = _push_d2("enter")
        return (
            "Entering D2 interface, sir."
            if ok
            else "Could not reach the D2 interface."
        )

    if act in ("exit", "close", "hide", "disable", "stop", "leave", "nexus", "return"):
        ok = _push_d2("exit")
        # Stop any residual gesture/camera scanning after leaving D2
        _stop_nexus_gestures()
        return (
            "Returning to Nexus, sir."
            if ok
            else "Could not exit D2."
        )

    if act == "show_tasks":
        _push_d2("show_tasks", tasks=tasks or kwargs.get("data") or [])
        return "Tasks are on D2, sir."

    if act == "show_reminders":
        _push_d2("show_reminders", reminders=reminders or kwargs.get("data") or [])
        return "Reminders are on D2, sir."

    if act == "show_news":
        _push_d2("show_news", items=items or kwargs.get("data") or [])
        return "News cards are on D2, sir."

    if act in ("visualize_cpu", "cpu"):
        _push_d2("visualize_cpu", value=float(value or kwargs.get("cpu") or 0))
        return "CPU visualization on D2."

    if act in ("visualize_ram", "ram", "memory"):
        _push_d2("visualize_ram", value=float(value or kwargs.get("ram") or 0))
        return "Memory visualization on D2."

    if act == "clear":
        _push_d2("clear")
        return "D2 content cleared."

    if act == "set_state":
        _push_d2("set_state", state=state)
        return f"D2 state set to {state}."

    if act == "status":
        return (
            "D2 is Gama's secondary card/orb interface (separate from H1). "
            "Say 'switch to D2' to enter, or 'return to Nexus' to exit. "
            "It does not activate automatically."
        )

    return (
        "Unknown D2 action. Use: enter, exit, show_tasks, show_reminders, "
        "show_news, visualize_cpu, visualize_ram, clear."
    )


def switch_to_d2(**kwargs: Any) -> str:
    return d2_mode(action="enter", **kwargs)


def exit_d2(**kwargs: Any) -> str:
    return d2_mode(action="exit", **kwargs)
