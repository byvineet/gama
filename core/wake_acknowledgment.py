"""
core/wake_acknowledgment.py — JARVIS-style wake acknowledgments
=====================================================================
The instant-wake path (main.py's `_send_wake_ack`) previously always
spoke the same literal string, "I'm awake.", via local TTS. That's the
"generic response" this module replaces: a short pool of natural,
time-of-day- and system-state-aware acknowledgments, chosen so the same
line never plays twice in a row, spoken through local TTS (no Gemini
round trip) so it stays comfortably under the 2-3 second budget.

Usage (see main.py `_send_wake_ack`):

    from core.wake_acknowledgment import get_acknowledgment
    self._speak_exact(get_acknowledgment(), kind="result")
"""

from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Last line spoken, so we never repeat consecutively. Module-level is
# fine — there's exactly one Gama session per process, same assumption
# voice/echo_guard.py and security/trusted_session.py already make.
_last_line: Optional[str] = None
# NOTE: wake-ack lines are long, mood/time-specific sentences rather
# than the short generic acknowledgement pool core/personality.py
# shares with execution_narrator, so this module keeps its own local
# anti-repeat pool. Every line it returns still passes through the
# Speech Styler (voice/speech_styler.style()) at the call site in
# main.py before being spoken, so wording/personality stays consistent
# with the rest of the system regardless of which pool it came from.

# ---------------------------------------------------------------------
# Base pool — always eligible, time-agnostic.
# ---------------------------------------------------------------------
_BASE = [
    "At your service, Sir.",
    "I'm online, Sir. Ready when you are.",
    "All systems ready. How may I assist you?",
    "Welcome back, Sir.",
    "Yes, Sir?",
    "Standing by, Sir.",
    "Right here, Sir.",
    "Online and listening.",
]

# ---------------------------------------------------------------------
# Time-of-day variants.
# ---------------------------------------------------------------------
_MORNING = [
    "Good morning, Sir. Systems are fully operational.",
    "Morning, Sir. All systems green.",
    "Good morning. Ready to get started.",
]
_AFTERNOON = [
    "Good afternoon, Sir. How can I help?",
    "Afternoon, Sir. I'm listening.",
]
_EVENING = [
    "Good evening, Sir.",
    "Evening, Sir. What do you need?",
]
_NIGHT = [
    "Still up, Sir? I'm here.",
    "Late one, Sir. Go ahead.",
    "Here, Sir — even at this hour.",
]

# ---------------------------------------------------------------------
# System-state variants — only surfaced when the condition is notable,
# never as noise on an ordinary wake.
# ---------------------------------------------------------------------
_LOW_BATTERY = [
    "At your service, Sir — though I'd mention the battery's getting low.",
    "Online, Sir. Worth noting: battery is running low.",
]
_NO_NETWORK = [
    "I'm here, Sir, though we're offline at the moment.",
    "Online locally, Sir — no network connection right now.",
]
_UPDATE_READY = [
    "Ready, Sir. An update is available whenever you'd like it installed.",
]


def _time_of_day(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(IST)
    h = now.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"


def _gather_system_flags() -> dict:
    """Best-effort system-state snapshot. Every check is wrapped so a
    missing/optional dependency (e.g. no battery on a desktop) never
    breaks the wake ack — it just falls back to the base pool."""
    flags = {"low_battery": False, "no_network": False, "update_ready": False}

    try:
        import psutil
        batt = psutil.sensors_battery()
        if batt is not None and not batt.power_plugged and batt.percent <= 20:
            flags["low_battery"] = True
    except Exception:
        pass

    try:
        from core.internet_monitor import is_online
        flags["no_network"] = not is_online()
    except Exception:
        pass

    try:
        from actions.game_updater import update_available  # best-effort hook
        flags["update_ready"] = bool(update_available())
    except Exception:
        pass

    return flags


def get_acknowledgment(now: Optional[datetime] = None) -> str:
    """Pick a short wake acknowledgment. Adapts to time of day and
    notable system state, never repeats the immediately-previous line,
    and always resolves in-process (no network call) so it stays well
    under the 2-3 second wake budget.
    """
    global _last_line

    flags = _gather_system_flags()
    tod = _time_of_day(now)

    pool: list[str] = list(_BASE)
    pool += {"morning": _MORNING, "afternoon": _AFTERNOON,
             "evening": _EVENING, "night": _NIGHT}[tod]

    # System-state lines are appended (not a replacement) so they're in
    # rotation alongside normal acks rather than forced every time.
    if flags["low_battery"]:
        pool += _LOW_BATTERY
    if flags["no_network"]:
        pool += _NO_NETWORK
    if flags["update_ready"]:
        pool += _UPDATE_READY

    choices = [line for line in pool if line != _last_line] or pool
    line = random.choice(choices)
    _last_line = line
    log.debug(f"[wake-ack] tod={tod} flags={flags} -> {line!r}")
    return line


def reset() -> None:
    """Test/debug helper — clears the anti-repeat memory."""
    global _last_line
    _last_line = None


__all__ = ["get_acknowledgment", "reset"]
