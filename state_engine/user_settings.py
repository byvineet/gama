"""
state_engine/user_settings.py — Gama User-Tunable Settings
============================================================
Small JSON-backed store for the handful of things the user can tweak
by voice at runtime:

    * personality traits — humor / professionality / honesty / talkativeness
      (0–100 percentage, multiples of 10 preferred; "low"/"medium"/"high"
      are accepted for backward compatibility and mapped to 30/50/80.)
    * proactive_suggestions_enabled — off by default.
    * wake_greeting_enabled — off by default.
    * voice_verification_enabled — on by default. When off, the local voice
      pipeline skips WeSpeaker verification on every utterance (and
      DESTRUCTIVE tools also skip the voice factor).
    * barge_in_enabled — on by default. When off, Gama does not listen
      while speaking (no interruption). Toggle with "turn barge-in /
      interruption on/off".
    * listening_sensitivity — integer 10–100 (default 50). Controls how
      sensitive the mic detection is; higher = picks up softer sounds.
      Expressed as a percentage: 10% barely hears loud speech, 100%
      catches near-whispers. Configurable by voice:
        "set listening sensitivity to 70%"
        "increase listening sensitivity"
        "decrease listening sensitivity"

Author: Gama / Vineet Machchal
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Union

from utils.logger import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()

_SETTINGS_PATH = Path.home() / ".gama" / "user_settings.json"

# ---------------------------------------------------------------------------
# Personality — percentage scale (0–100).  Low/medium/high kept for compat.
# ---------------------------------------------------------------------------
_TRAITS = ("humor", "professionality", "honesty", "talkativeness")

# Map legacy string levels to percentage equivalents.
_LEGACY_LEVEL_MAP: dict[str, int] = {
    "low": 30,
    "medium": 50,
    "high": 80,
}

_DEFAULTS: Dict[str, Any] = {
    "proactive_suggestions_enabled": False,
    "notifications_enabled": False,
    "wake_greeting_enabled": False,
    "voice_verification_enabled": True,
    "barge_in_enabled": True,
    "listening_sensitivity": 50,   # 10–100 integer
    "personality": {
        "humor": 50,
        "professionality": 50,
        "honesty": 80,
        "talkativeness": 50,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_pct(value: Any) -> int:
    """Coerce a trait value to an integer percentage (0–100).
    Accepts int, '80', '80%', 'high', 'low', 'medium'."""
    if isinstance(value, int):
        return max(0, min(100, value))
    s = str(value).strip().lower().rstrip("%")
    if s in _LEGACY_LEVEL_MAP:
        return _LEGACY_LEVEL_MAP[s]
    try:
        return max(0, min(100, int(s)))
    except (ValueError, TypeError):
        return 50  # safe default on parse failure


def _load() -> Dict[str, Any]:
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            merged = json.loads(json.dumps(_DEFAULTS))  # deep copy
            merged.update({k: v for k, v in data.items() if k != "personality"})
            if isinstance(data.get("personality"), dict):
                for trait, val in data["personality"].items():
                    merged["personality"][trait] = _to_pct(val)
            # Migrate old string traits that were saved before this refactor.
            for trait in _TRAITS:
                merged["personality"][trait] = _to_pct(merged["personality"].get(trait, 50))
            return merged
    except Exception as exc:
        log.warning(f"Could not read user_settings.json, using defaults: {exc}")
    return json.loads(json.dumps(_DEFAULTS))


def _save(data: Dict[str, Any]) -> None:
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.error(f"Could not write user_settings.json: {exc}")


def get_all() -> Dict[str, Any]:
    with _LOCK:
        return _load()


# ---------------------------------------------------------------------------
# Proactive suggestions
# ---------------------------------------------------------------------------

def get_proactive_suggestions_enabled() -> bool:
    return bool(get_all().get("proactive_suggestions_enabled", False))


def set_proactive_suggestions_enabled(enabled: bool) -> str:
    with _LOCK:
        data = _load()
        data["proactive_suggestions_enabled"] = bool(enabled)
        _save(data)
    return ("DONE: Proactive suggestions are now enabled." if enabled
            else "DONE: Proactive suggestions are now disabled.")


# ---------------------------------------------------------------------------
# Notifications (unified: Instagram push + system alerts)
# ---------------------------------------------------------------------------

def get_notifications_enabled() -> bool:
    """Whether the unified notification watcher is armed."""
    return bool(get_all().get("notifications_enabled", False))


def set_notifications_enabled(enabled: bool) -> str:
    with _LOCK:
        data = _load()
        data["notifications_enabled"] = bool(enabled)
        _save(data)
    return ("DONE: Notifications are now enabled." if enabled
            else "DONE: Notifications are now disabled.")


# ---------------------------------------------------------------------------
# Wake greeting
# ---------------------------------------------------------------------------

def get_wake_greeting_enabled() -> bool:
    return bool(get_all().get("wake_greeting_enabled", False))


def set_wake_greeting_enabled(enabled: bool) -> str:
    with _LOCK:
        data = _load()
        data["wake_greeting_enabled"] = bool(enabled)
        _save(data)
    return ("DONE: I'll greet you when I wake up from now on." if enabled
            else "DONE: I'll just say I'm awake from now on, no greeting.")


# ---------------------------------------------------------------------------
# Voice verification opt-out (destructive actions only)
# ---------------------------------------------------------------------------

def get_voice_verification_enabled() -> bool:
    return bool(get_all().get("voice_verification_enabled", True))


def set_voice_verification_enabled(enabled: bool) -> str:
    with _LOCK:
        data = _load()
        data["voice_verification_enabled"] = bool(enabled)
        _save(data)
    if enabled:
        return "DONE: Voice verification is back on for destructive actions."
    from actions.confirmation import is_code_set
    if not is_code_set():
        return ("DONE: Voice verification is off, but you have no confirmation "
                "code set yet — set one now, or destructive actions will be blocked.")
    return "DONE: Voice verification is off. Destructive actions will use your confirmation code instead."


# ---------------------------------------------------------------------------
# Barge-in toggle
# ---------------------------------------------------------------------------

def get_barge_in_enabled() -> bool:
    """Whether the user can interrupt Gama mid-speech."""
    return bool(get_all().get("barge_in_enabled", True))


def set_barge_in_enabled(enabled: bool) -> str:
    with _LOCK:
        data = _load()
        data["barge_in_enabled"] = bool(enabled)
        _save(data)
    # Propagate to the actual real-time interrupt-detection path in
    # main.py's mic audio callback (voice/barge_in.py's detector above is
    # a separate, currently-unused legacy path — this is the one that
    # matters for live behavior).
    try:
        from core.tool_dispatch import _ACTIVE_ASSISTANT
        if _ACTIVE_ASSISTANT is not None:
            _ACTIVE_ASSISTANT.set_barge_in_enabled(enabled)
    except Exception as exc:
        log.debug(f"Could not propagate barge-in toggle to running assistant: {exc}")
    return ("DONE: Interruption is now enabled — you can interrupt me mid-sentence." if enabled
            else "DONE: Interruption is off — I won't listen while I'm speaking.")


# ---------------------------------------------------------------------------
# Listening sensitivity  (10–100 integer percentage)
# ---------------------------------------------------------------------------

def get_listening_sensitivity() -> int:
    """Return listening sensitivity as integer 10–100 (default 50)."""
    return max(10, min(100, int(get_all().get("listening_sensitivity", 50))))


def set_listening_sensitivity(pct: Union[int, str]) -> str:
    """Set listening sensitivity to `pct` percent (10–100).

    Also accepts relative adjustments: '+10', '-10', 'up', 'down'.
    Maps to microphone energy threshold and VAD sensitivity.
    """
    with _LOCK:
        data = _load()
        current = max(10, min(100, int(data.get("listening_sensitivity", 50))))
        s = str(pct).strip().lower().rstrip("%")
        # Relative adjustments
        if s in ("up", "higher", "more", "increase", "+"):
            new = min(100, current + 10)
        elif s in ("down", "lower", "less", "decrease", "-"):
            new = max(10, current - 10)
        elif s.startswith("+"):
            try:
                new = min(100, current + int(s[1:]))
            except ValueError:
                new = min(100, current + 10)
        elif s.startswith("-"):
            try:
                new = max(10, current - int(s[1:]))
            except ValueError:
                new = max(10, current - 10)
        else:
            try:
                new = max(10, min(100, int(float(s))))
            except (ValueError, TypeError):
                return f"ERROR: Could not parse sensitivity value '{pct}'. Use a number like 50 or 70%."
        data["listening_sensitivity"] = new
        _save(data)

    descriptor = _sensitivity_descriptor(new)
    return f"DONE: Listening sensitivity set to {new}% ({descriptor})."


def _sensitivity_descriptor(pct: int) -> str:
    if pct <= 20:
        return "very low — only loud speech triggers me"
    if pct <= 40:
        return "low"
    if pct <= 60:
        return "medium"
    if pct <= 80:
        return "high"
    return "very high — I'll catch even soft speech"


def sensitivity_to_energy_threshold(pct: int) -> float:
    """Convert sensitivity percentage to RMS energy threshold.

    Higher sensitivity → lower threshold (picks up softer sounds).
      10%  → 0.020 (need to speak loudly)
      50%  → 0.010 (default)
     100%  → 0.003 (catches near-whispers)
    """
    pct = max(10, min(100, pct))
    return round(0.020 - ((pct - 10) / 90.0) * 0.017, 5)


def sensitivity_to_vad_threshold(pct: int) -> float:
    """Convert sensitivity percentage to Silero VAD probability threshold.

    Higher sensitivity → lower threshold (more chunks pass as 'speech').
      10%  → 0.70
      50%  → 0.45 (default, close to Silero's native 0.5)
     100%  → 0.20
    """
    pct = max(10, min(100, pct))
    return round(0.70 - ((pct - 10) / 90.0) * 0.50, 3)


# ---------------------------------------------------------------------------
# Personality traits  (0–100 percentage)
# ---------------------------------------------------------------------------

def get_personality() -> Dict[str, int]:
    p = get_all().get("personality", _DEFAULTS["personality"])
    return {t: _to_pct(p.get(t, _DEFAULTS["personality"].get(t, 50))) for t in _TRAITS}


def set_personality_trait(trait: str, value: Union[str, int]) -> str:
    """Set a personality trait to a percentage value (0–100).

    Accepts: integer, '80', '80%', or legacy 'low'/'medium'/'high'.
    """
    trait = (trait or "").strip().lower()
    if trait not in _TRAITS:
        return f"ERROR: Unknown personality trait '{trait}'. Valid traits: {', '.join(_TRAITS)}."

    pct = _to_pct(value)
    with _LOCK:
        data = _load()
        data.setdefault("personality", dict(_DEFAULTS["personality"]))
        data["personality"][trait] = pct
        _save(data)
    return f"DONE: {trait.capitalize()} set to {pct}%."


def personality_prompt_fragment() -> str:
    """Short fragment describing current personality dial settings for the
    system prompt so Gemini's tone reflects user preferences."""
    p = get_personality()

    def _desc(pct: int, trait: str) -> str:
        """Map pct to a natural-language qualifier for the prompt."""
        if pct <= 20:
            return f"very low {trait}"
        if pct <= 40:
            return f"low {trait}"
        if pct <= 60:
            return f"moderate {trait}"
        if pct <= 80:
            return f"high {trait}"
        return f"very high {trait}"

    lines = [_desc(v, t) for t, v in p.items()]
    return (
        f"Personality dials (user-configured, 0–100%): "
        f"humor={p.get('humor', 50)}%, "
        f"professionality={p.get('professionality', 50)}%, "
        f"honesty={p.get('honesty', 80)}%, "
        f"talkativeness={p.get('talkativeness', 50)}%. "
        f"({', '.join(lines)}.) "
        "Adjust tone, word-choice, brevity, and candour to match these dials precisely."
    )


__all__ = [
    "get_all",
    "get_proactive_suggestions_enabled", "set_proactive_suggestions_enabled",
    "get_notifications_enabled", "set_notifications_enabled",
    "get_wake_greeting_enabled", "set_wake_greeting_enabled",
    "get_voice_verification_enabled", "set_voice_verification_enabled",
    "get_barge_in_enabled", "set_barge_in_enabled",
    "get_listening_sensitivity", "set_listening_sensitivity",
    "sensitivity_to_energy_threshold", "sensitivity_to_vad_threshold",
    "get_personality", "set_personality_trait", "personality_prompt_fragment",
    "_TRAITS",
]
