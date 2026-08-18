"""
voice/event_voice.py — GAMA's own event voice (alert / success / failure)
===========================================================================
Speaks GAMA's own state changes — alerts, successes, failures — through
GAMA's voice model (voice.tts_engine, backed by Gemini native TTS — see that
module's docstring), each paired with its matching custom sound
(voice.soundscape / the sound_action tool).

Behaviour:
  * "alert" repeats "Alert, <reason>!" — sound + spoken line, once every
    few seconds — until stop_alert() is called (e.g. the user says
    "stop"). Only one alert loop runs at a time; calling speak_event()
    again with a new reason restarts the loop with the new line instead
    of stacking two loops on top of each other.
  * Everything else (success, failure, ...) plays its sound and speaks
    its line exactly once.

Author: Vineet Machchal
"""

from __future__ import annotations

import threading
from typing import Optional

from utils.logger import get_logger
from voice import soundscape, tts_engine
from voice.speech_manager import Priority, cancel as _sm_cancel, say as _sm_say

log = get_logger(__name__)

# Kinds that repeat until explicitly stopped, instead of speaking once.
_REPEATING_KINDS = {"alert"}
_REPEAT_INTERVAL_S = 4.0

_lock = threading.Lock()
_loop_thread: Optional[threading.Thread] = None
_loop_stop = threading.Event()
_active_kind: Optional[str] = None


def _loop_worker(kind: str, text: str) -> None:
    global _active_kind
    while not _loop_stop.is_set():
        soundscape.play_kind(kind)
        # blocking=True so two repeats of the line never overlap;
        # dedup=False so the (identical) text is allowed to repeat.
        _sm_say(text, priority=Priority.EMERGENCY, kind=kind, dedup=False, blocking=True)
        if _loop_stop.wait(timeout=_REPEAT_INTERVAL_S):
            break
    with _lock:
        if _active_kind == kind:
            _active_kind = None


def speak_event(kind: str, reason: str = "") -> None:
    """Speak + sound one of GAMA's own events.

    kind:   'alert' | 'success' | 'failure' (or any other soundscape kind).
    reason: short human reason, e.g. 'battery below 10 percent'.

    'alert' says "Alert, <reason>!" on repeat, every few seconds, until
    stop_alert() is called. Everything else speaks its line once.
    """
    global _loop_thread, _active_kind

    kind = (kind or "").lower().strip()
    if not kind:
        return
    label = kind.capitalize()
    reason = (reason or "").strip()
    text = f"{label}, {reason}!" if reason else f"{label}!"

    if kind in _REPEATING_KINDS:
        with _lock:
            _loop_stop.set()
            old = _loop_thread
        if old is not None and old.is_alive():
            old.join(timeout=1.5)
        with _lock:
            _loop_stop.clear()
            _active_kind = kind
            _loop_thread = threading.Thread(
                target=_loop_worker, args=(kind, text),
                daemon=True, name="GamaAlertLoop",
            )
            _loop_thread.start()
        return

    # One-shot event: sound + spoken line, exactly once.
    soundscape.play_kind(kind)
    priority = Priority.ERROR if kind == "failure" else Priority.COMPLETION
    _sm_say(text, priority=priority, kind=kind, dedup=False)


def stop_alert() -> str:
    """Stop whatever alert is currently looping. Safe to call even if
    nothing is looping. Wire this to the "stop"/"cancel" voice command."""
    global _active_kind
    with _lock:
        was_active = _active_kind is not None
        _loop_stop.set()
        _active_kind = None
    _sm_cancel(kind="alert")
    tts_engine.stop()
    return "Alert stopped." if was_active else "No alert is currently active."


def is_alert_active() -> bool:
    with _lock:
        return _active_kind is not None


def event_voice_action(action: str = "status", **kwargs) -> str:
    """Tool entrypoint.

    Actions:
      alert   (reason)  — start (or restart) the repeating alert voice
      success (reason)  — speak a one-off success line + sound
      failure (reason)  — speak a one-off failure line + sound
      stop               — stop a currently-looping alert
      status             — whether an alert is currently looping
    """
    action = (action or "status").lower().strip()
    reason = kwargs.get("reason", "")

    if action in ("alert", "success", "failure"):
        speak_event(action, reason)
        return f"Speaking '{action}'{f' — {reason}' if reason else ''}."

    if action in ("stop", "stop_alert", "cancel"):
        return stop_alert()

    if action == "status":
        return "An alert is currently looping." if is_alert_active() else "No alert is active."

    return "Unknown event_voice action. Use: alert, success, failure, stop, status."


__all__ = ["speak_event", "stop_alert", "is_alert_active", "event_voice_action"]
