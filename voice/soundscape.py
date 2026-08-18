"""
voice/soundscape.py — Cybernetic HUD Audio Feedback Engine
===========================================================
Generates and plays tactical audio feedback (chimes, pings, warning pulses)
for GAMA's own UI events — trimmed down to the ones GAMA actually uses:
success, alert, failure, plus reminder/alarm/timer for actions/reminder.py.
(Dropped ack/processing/notification — nothing ever triggered them.)

By default sounds are synthesized on-demand via winsound.Beep — no external
files needed. Each event "kind" can optionally be overridden with a custom
sound file (wav/mp3/etc — anything the OS default player or winsound can
play) via `set_custom_sound()` / the `sound_action` tool, so the user isn't
stuck with plain beeps if they'd rather hear their own alert tone, chime,
or clip. Overrides persist to config/custom_sounds.json across restarts.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from utils.paths import user_data_path

log = get_logger(__name__)
logger = log  # back-compat alias
_ENABLED = True

# Every kind of feedback GAMA can play. Kept as a flat set of well-known
# names so tool callers ("set custom sound for alerts to X") have a fixed,
# discoverable vocabulary instead of arbitrary free text.
_KNOWN_KINDS = ("success", "alert", "failure", "reminder", "alarm", "timer")

_CONFIG_PATH = user_data_path("config/custom_sounds.json")
_config_lock = threading.Lock()
_custom_sounds: Dict[str, str] = {}
_loaded = False


def _load_config() -> None:
    global _loaded
    if _loaded:
        return
    with _config_lock:
        if _loaded:
            return
        try:
            if _CONFIG_PATH.exists():
                data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _custom_sounds.update({k: v for k, v in data.items() if isinstance(v, str)})
        except Exception as exc:
            logger.debug(f"[Soundscape] Failed to load custom sound config: {exc}")
        _loaded = True


def _save_config() -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(_custom_sounds, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[Soundscape] Failed to save custom sound config: {exc}")


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _play_file(path: str) -> bool:
    """Best-effort playback of an arbitrary sound file. Returns True if a
    playback method was attempted successfully (fire-and-forget — this
    doesn't guarantee audio was actually heard, just that nothing raised)."""
    p = Path(path)
    if not p.exists():
        logger.warning(f"[Soundscape] Custom sound file not found: {path}")
        return False
    try:
        if sys.platform == "win32":
            if p.suffix.lower() == ".wav":
                import winsound
                winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
                return True
            # Non-WAV (mp3, etc.) — winsound can't do it; hand off to the
            # user's default player. Fire-and-forget, doesn't block.
            os.startfile(str(p))  # noqa: S606
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["afplay", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        subprocess.Popen(["paplay", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as exc:
        logger.debug(f"[Soundscape] Custom sound playback failed for {path}: {exc}")
        return False


def _play_beep_sequence(tones: list[tuple[int, int]]) -> None:
    """Built-in synthesized fallback tone sequence."""
    if sys.platform == "win32":
        try:
            import winsound
            for freq, duration in tones:
                winsound.Beep(max(37, min(32767, int(freq))), int(duration))
        except Exception as exc:
            logger.debug(f"[Soundscape] winsound beep failed: {exc}")


def _play_kind(kind: str, tones: list[tuple[int, int]]) -> None:
    """Play whatever's configured for `kind` — a custom file if the user set
    one, otherwise the built-in synthesized beep sequence. Always runs on a
    background thread so the caller (UI/voice loop) is never blocked."""
    def _worker():
        if not _ENABLED:
            return
        _load_config()
        custom = _custom_sounds.get(kind)
        if custom and _play_file(custom):
            return
        _play_beep_sequence(tones)

    threading.Thread(target=_worker, daemon=True, name="SoundscapeSFX").start()


def play_success() -> None:
    """Harmonic tri-chord completion ping."""
    _play_kind("success", [(523, 30), (659, 30), (784, 80)])


def play_alert() -> None:
    """Subdued warning pulse for Sentinel alerts."""
    _play_kind("alert", [(220, 80), (180, 120)])


def play_failure() -> None:
    """Descending low-tone failure buzz."""
    _play_kind("failure", [(400, 100), (300, 100), (200, 160)])


def play_kind(kind: str) -> bool:
    """Play the sound for an arbitrary known kind (used by reminder/alarm/
    timer, event_voice, and by the test tool). Returns False if `kind`
    isn't recognized."""
    default_tones = {
        "success": [(523, 30), (659, 30), (784, 80)],
        "alert": [(220, 80), (180, 120)],
        "failure": [(400, 100), (300, 100), (200, 160)],
        "reminder": [(800, 180), (1000, 180), (1200, 180)],
        "alarm": [(1000, 200), (1200, 200), (800, 200)] * 2,
        "timer": [(800, 180), (1000, 180), (1200, 180)],
    }
    kind = (kind or "").lower().strip()
    if kind not in default_tones:
        return False
    _play_kind(kind, default_tones[kind])
    return True


def set_enabled(enabled: bool) -> None:
    global _ENABLED
    _ENABLED = bool(enabled)


# ---------------------------------------------------------------------------
# Custom sound configuration
# ---------------------------------------------------------------------------

def set_custom_sound(kind: str, path: str) -> str:
    kind = (kind or "").lower().strip()
    if kind not in _KNOWN_KINDS:
        return (f"Unknown sound kind '{kind}'. Use one of: {', '.join(_KNOWN_KINDS)}.")
    p = Path(path).expanduser()
    if not p.exists():
        return f"Couldn't find a file at '{path}'."
    _load_config()
    with _config_lock:
        _custom_sounds[kind] = str(p)
        _save_config()
    return f"Custom sound for '{kind}' set to {p.name}."


def clear_custom_sound(kind: str) -> str:
    kind = (kind or "").lower().strip()
    _load_config()
    with _config_lock:
        if kind in _custom_sounds:
            del _custom_sounds[kind]
            _save_config()
            return f"Reverted '{kind}' back to the default beep."
    return f"No custom sound was set for '{kind}'."


def list_custom_sounds() -> Dict[str, str]:
    _load_config()
    with _config_lock:
        return dict(_custom_sounds)


# ---------------------------------------------------------------------------
# Tool entrypoint
# ---------------------------------------------------------------------------

def sound_action(action: str = "status", **kwargs) -> str:
    """Tool entrypoint for testing and configuring GAMA's UI/alert sounds.

    Actions:
      test    (kind)         — play a sound right now, e.g. kind='alert'
      set     (kind, path)   — use a custom sound file for that kind from now on
      clear   (kind)         — revert a kind back to the default synthesized beep
      list                   — show which kinds have a custom sound configured
      status                 — whether sound feedback is currently enabled
      enable / disable       — turn all GAMA UI/alert sounds on or off
    """
    action = (action or "status").lower().strip()

    if action == "test":
        kind = (kwargs.get("kind") or "success").lower().strip()
        if not play_kind(kind):
            return f"Unknown sound kind '{kind}'. Use one of: {', '.join(_KNOWN_KINDS)}."
        return f"Playing test sound for '{kind}'."

    if action == "set":
        kind = kwargs.get("kind", "")
        path = kwargs.get("path", "")
        if not kind or not path:
            return "Give me both a kind (e.g. 'alert') and a file path."
        return set_custom_sound(kind, path)

    if action == "clear":
        kind = kwargs.get("kind", "")
        if not kind:
            return "Which sound kind should I revert to the default beep?"
        return clear_custom_sound(kind)

    if action == "list":
        sounds = list_custom_sounds()
        if not sounds:
            return "No custom sounds configured — everything's using the default beeps."
        return "Custom sounds: " + ", ".join(f"{k}={Path(v).name}" for k, v in sounds.items())

    if action in ("enable", "on", "resume"):
        set_enabled(True)
        return "Sound feedback is on."
    if action in ("disable", "off", "mute"):
        set_enabled(False)
        return "Sound feedback is off."

    if action == "status":
        sounds = list_custom_sounds()
        return (f"Sound feedback is {'on' if _ENABLED else 'off'}. "
                f"{len(sounds)} custom sound(s) configured.")

    return "Unknown sound action. Use: test, set, clear, list, status, enable, disable."


__all__ = [
    "play_success", "play_alert", "play_failure", "play_kind",
    "set_enabled", "set_custom_sound", "clear_custom_sound", "list_custom_sounds",
    "sound_action",
]
