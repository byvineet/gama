"""
core/protocols/actions.py — Pluggable action handlers for Protocol steps
================================================================================
Every ActionType maps to a handler function of shape:

    handler(step: ProtocolStep, ctx: dict) -> str

`ctx` carries at least {"parameters": {...}} resolved by the executor.
Handlers should raise an exception on failure (the executor's retry /
fallback / skip / ask_user / abort machinery takes it from there) and
return a short human-readable result string on success.

Handlers deliberately delegate to Gama's existing action modules instead of
reimplementing app-launching, browser control, media control, etc. — this
keeps the Protocol engine a thin orchestration layer, not a duplicate
automation stack. New action types can be added by writing one function and
registering it in `_register_default_handlers`, without touching the
executor.
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

from core.protocols.models import ProtocolStep, ActionType

log = get_logger(__name__)
logger = log  # back-compat alias
ActionHandler = Callable[["ProtocolStep", Dict[str, Any]], str]


class ActionHandlerRegistry:
    """Maps ActionType -> handler. Extensible at runtime so plugins/custom
    tools can register new action types without editing this file."""

    def __init__(self) -> None:
        self._handlers: Dict[str, ActionHandler] = {}
        self._register_default_handlers()

    def register(self, action_type: str, handler: ActionHandler) -> None:
        self._handlers[action_type] = handler

    def execute(self, step: ProtocolStep, context: Dict[str, Any]) -> str:
        handler = self._handlers.get(step.action_type)
        if handler is None:
            raise ValueError(f"No handler registered for action type '{step.action_type}'")
        return handler(step, context)

    def _register_default_handlers(self) -> None:
        self.register(ActionType.OPEN_APP.value, _handle_open_app)
        self.register(ActionType.CLOSE_APP.value, _handle_close_app)
        self.register(ActionType.OPEN_FOLDER.value, _handle_open_folder)
        self.register(ActionType.OPEN_FILE.value, _handle_open_file)
        self.register(ActionType.TERMINAL.value, _handle_terminal)
        self.register(ActionType.KEYBOARD.value, _handle_keyboard)
        self.register(ActionType.TYPE_TEXT.value, _handle_type_text)
        self.register(ActionType.MOUSE.value, _handle_mouse)
        self.register(ActionType.BROWSER.value, _handle_browser)
        self.register(ActionType.WEB_SEARCH.value, _handle_web_search)
        self.register(ActionType.MEDIA_PLAY.value, _handle_media_play)
        self.register(ActionType.MEDIA_PAUSE.value, _handle_media_pause)
        self.register(ActionType.MEDIA_CONTROL.value, _handle_media_control)
        self.register(ActionType.VOLUME.value, _handle_volume)
        self.register(ActionType.BRIGHTNESS.value, _handle_brightness)
        self.register(ActionType.NOTIFICATION.value, _handle_notification)
        self.register(ActionType.CLIPBOARD.value, _handle_clipboard)
        self.register(ActionType.WAIT.value, _handle_wait)
        self.register(ActionType.WAIT_PROCESS.value, _handle_wait_process)
        self.register(ActionType.ASK_USER.value, _handle_ask_user)
        self.register(ActionType.SPEAK.value, _handle_speak)
        self.register(ActionType.AI_PROMPT.value, _handle_ai_prompt)
        self.register(ActionType.PLUGIN.value, _handle_plugin)
        self.register(ActionType.TOOL.value, _handle_tool_execution)
        # NOTE: ActionType.CALL_PROTOCOL is handled directly by the executor
        # (it needs access to call_stack / recursion protection), not here.


# ----------------------------------------------------------------------
# Individual handlers — thin wrappers around existing action modules
# ----------------------------------------------------------------------

def _handle_open_app(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.open_app import open_app
    return open_app(step.target, new_window=bool(step.params.get("new_window", False)))


def _handle_close_app(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.process_manager import process_manager
    return process_manager("close", name_or_title=step.target)


def _handle_open_folder(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    path = os.path.expanduser(step.target)
    if not os.path.isdir(path):
        # Legacy/loosely-typed step data sometimes stores an app name here
        # (e.g. "Explorer") rather than an actual folder path — try opening
        # it as an app before giving up.
        try:
            from actions.open_app import open_app
            return open_app(step.target)
        except Exception:
            pass
        raise FileNotFoundError(f"Folder not found: {path}")
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return f"Opened folder {path}"


def _handle_open_file(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    path = os.path.expanduser(step.target)
    if not os.path.isfile(path):
        try:
            from actions.open_app import open_app
            return open_app(step.target)
        except Exception:
            pass
        raise FileNotFoundError(f"File not found: {path}")
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return f"Opened file {path}"


def _handle_terminal(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.terminal import terminal_command
    return terminal_command("run", command=step.target, cwd=step.params.get("cwd", ""))


def _handle_keyboard(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.ui_automation import ui_automation
    return ui_automation("hotkey", keys=step.target)


# Phrasing like "write an email about the delay" / "write a short poem about
# autumn" wants generated content typed out, not the literal words "an email
# about the delay" typed into the active window. Phrasing like "write hello
# world" or "write my name is X" wants the literal text typed as-is. This
# distinguishes the two so both "write ___" use cases actually do something
# useful instead of silently doing nothing (the old behavior: falling
# through to AI_PROMPT, which generated text and then just discarded it).
_GENERATIVE_WRITE_RE = re.compile(
    r"^(?:an?|the|my|our|some)\s+\w+.*\b(about|regarding|on|for)\b|"
    r"^(?:an?|the)\s+(email|message|note|letter|poem|story|summary|reply|"
    r"response|paragraph|essay|script|caption|tweet|post)\b",
    re.I,
)


def _handle_type_text(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.keyboard_actions import keyboard_actions
    text = step.target or ""
    if _GENERATIVE_WRITE_RE.match(text.strip()):
        from actions.utilities import _gemini_generate
        generated = _gemini_generate(f"Write the following, respond with ONLY the requested "
                                      f"text and nothing else (no preamble, no quotes): {text}")
        if not generated.startswith("Error:"):
            text = generated.strip()
    keyboard_actions("type", text=text)
    return f"Typed: {text[:80]}"


def _handle_mouse(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.mouse_actions import mouse_actions
    return mouse_actions(step.params.get("mouse_action", "click"), **step.params)


def _handle_browser(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.browser_control import browser_control
    return browser_control("open", url=step.target)


def _handle_web_search(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.browser_control import browser_control
    return browser_control("search", query=step.target)


def _handle_media_play(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.media_controller import media_controller
    if step.target:
        return media_controller("play", song=step.target)
    return media_controller("play")


def _handle_media_pause(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.media_controller import media_controller
    return media_controller("pause")


def _handle_media_control(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.media_controller import media_controller
    return media_controller(step.target or "next")


def _handle_volume(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.media_controller import media_controller
    level = step.params.get("level", 50)
    return media_controller("volume", level=level)


def _handle_brightness(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.computer_settings import computer_settings
    level = step.params.get("level", 50)
    return computer_settings("brightness", value=str(level))


def _handle_notification(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.desktop_notify import notify
    notify("Protocol", step.target)
    return f"Notified: {step.target}"


def _handle_clipboard(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.clipboard import clipboard
    return clipboard("write", text=step.target)


def _handle_wait(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    seconds = float(step.params.get("seconds", 1.0))
    time.sleep(max(0.0, min(seconds, 3600.0)))
    return f"Waited {seconds}s"


def _handle_wait_process(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    import psutil  # already a dependency elsewhere in the project
    target = step.target.lower()
    timeout = float(step.params.get("timeout_secs", 30.0))
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in psutil.process_iter(["name"]):
            try:
                if target in (p.info.get("name") or "").lower():
                    return f"{step.target} is running"
            except Exception:
                continue
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for process '{step.target}'")


def _handle_ask_user(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    # The executor surfaces this back to the conversation layer via its
    # status listeners; here we just record the prompt so it's visible
    # in logs/history even if no UI is attached.
    return f"[ask_user] {step.target}"


def _handle_speak(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    try:
        from core.audio_controller import speak  # type: ignore
        speak(step.target)
    except Exception:
        logger.info(f"[protocols.actions] (speak, no TTS available) {step.target}")
    return step.target


def _handle_ai_prompt(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from actions.utilities import _gemini_generate
    result = _gemini_generate(step.target)
    if result.startswith("Error:"):
        raise RuntimeError(result)
    return result


def _handle_plugin(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    plugin_name = step.params.get("plugin") or step.target
    try:
        from actions.dynamic_tool_creator import run_custom_tool  # type: ignore
        return run_custom_tool(plugin_name, **step.params)
    except Exception as exc:
        raise RuntimeError(f"Plugin '{plugin_name}' failed: {exc}")


def _handle_tool_execution(step: ProtocolStep, ctx: Dict[str, Any]) -> str:
    from core.tool_dispatch import tool_registry
    tool_name = step.params.get("tool") or step.target
    entry = tool_registry.get_entry(tool_name)
    if entry is None:
        raise ValueError(f"Unknown tool '{tool_name}'")
    return entry.handler(step.params)


action_handler_registry = ActionHandlerRegistry()

__all__ = ["ActionHandlerRegistry", "action_handler_registry", "ActionHandler"]
