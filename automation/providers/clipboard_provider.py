"""
automation/providers/clipboard_provider.py — Clipboard Automation.

Text via pyperclip (already a dependency). Keeps a small in-process
ring buffer as lightweight "clipboard history" since Windows only
exposes its native clipboard history via UI, not an API.
"""

from __future__ import annotations

from collections import deque
from typing import Tuple

from utils.logger import get_logger
from automation.models import ActionResult, Capability
from automation.registry import registry

log = get_logger(__name__)

try:
    import pyperclip  # type: ignore
    _HAVE_PYPERCLIP = True
except Exception:
    _HAVE_PYPERCLIP = False

_history: deque = deque(maxlen=25)


def _write(text: str, **_) -> ActionResult:
    if not _HAVE_PYPERCLIP:
        return ActionResult(ok=False, message="pyperclip not available")
    try:
        pyperclip.copy(text)
        _history.appendleft(text)
        return ActionResult(ok=True, message="Copied to clipboard")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Clipboard write failed: {exc}")


def _verify_write(text: str, **_) -> Tuple[bool, str]:
    if not _HAVE_PYPERCLIP:
        return False, "no backend"
    current = pyperclip.paste()
    return (current == text), current[:40]


def _read(**_) -> ActionResult:
    if not _HAVE_PYPERCLIP:
        return ActionResult(ok=False, message="pyperclip not available")
    try:
        text = pyperclip.paste()
        return ActionResult(ok=True, message=text, data={"text": text})
    except Exception as exc:
        return ActionResult(ok=False, message=f"Clipboard read failed: {exc}")


def _history_list(**_) -> ActionResult:
    return ActionResult(ok=True, message=f"{len(_history)} item(s) in history",
                         data={"history": list(_history)})


def register() -> None:
    registry.register_many([
        Capability("clipboard.write", _write, verify=_verify_write, cost=0, speed_ms=5,
                   description="Write text to the clipboard", keywords=("copy",)),
        Capability("clipboard.read", _read, cost=0, speed_ms=5,
                   description="Read clipboard text", keywords=("paste", "clipboard")),
        Capability("clipboard.history", _history_list, cost=0, speed_ms=5,
                   description="List recent clipboard entries", keywords=("clipboard history",)),
    ])


register()
