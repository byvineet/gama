"""
actions/user_settings_action.py — voice-facing wrapper around
state_engine/user_settings.py.

Registered as the `user_settings` tool so the user can say things like:

    "Set humor to 80%"
    "Set talkativeness to 40 percent"
    "Set honesty to high"          ← legacy: maps to 80%
    "Turn off professionality"     ← maps to 20%
    "Enable proactive suggestions"
    "Disable wake greetings"
    "Turn off voice verification"
    "Turn barge-in on" / "Turn interruption on"
    "Turn barge-in off" / "Turn interruption off"
    "Set listening sensitivity to 70%"
    "Increase listening sensitivity"
    "Decrease listening sensitivity"

Author: Gama / Vineet Machchal
"""

from __future__ import annotations

from state_engine import user_settings as _settings

_TRUTHY = {"on", "enable", "enabled", "true", "yes", "start"}
_FALSY  = {"off", "disable", "disabled", "false", "no", "stop"}


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return bool(value)


def user_settings(action: str, **kwargs) -> str:
    action = (action or "").strip().lower()

    # ── Personality traits ────────────────────────────────────────────────

    if action in ("set_personality", "set_trait", "personality"):
        trait = kwargs.get("trait") or kwargs.get("name")
        level = kwargs.get("level") or kwargs.get("value") or kwargs.get("percent")
        if not trait or level is None:
            return (
                "ERROR: Need both a trait (humor / professionality / honesty / talkativeness) "
                "and a value (0–100% or low / medium / high)."
            )
        return _settings.set_personality_trait(trait, level)

    if action in ("get_personality", "show_personality"):
        p = _settings.get_personality()
        return ("Current personality: " +
                ", ".join(f"{k}={v}%" for k, v in p.items()))

    # ── Proactive suggestions ─────────────────────────────────────────────

    if action in ("proactive_suggestions", "set_proactive_suggestions"):
        # Proactive suggestions were removed from Gama (answer-only policy) —
        # acknowledge the setting instead of crashing on a missing module.
        return "DONE: proactive suggestions are permanently off (answer-only policy)."

    # ── Wake greeting ──────────────────────────────────────────────────────

    if action in ("wake_greeting", "set_wake_greeting"):
        enabled = _coerce_bool(kwargs.get("enabled", kwargs.get("value", True)))
        return _settings.set_wake_greeting_enabled(enabled)

    # ── Voice verification ─────────────────────────────────────────────────

    if action in ("voice_verification", "set_voice_verification"):
        enabled = _coerce_bool(kwargs.get("enabled", kwargs.get("value", True)))
        return _settings.set_voice_verification_enabled(enabled)

    # ── Barge-in toggle ────────────────────────────────────────────────────

    if action in ("barge_in", "set_barge_in", "barge-in", "interruption", "set_interruption"):
        enabled = _coerce_bool(kwargs.get("enabled", kwargs.get("value", True)))
        return _settings.set_barge_in_enabled(enabled)

    # ── Listening sensitivity ─────────────────────────────────────────────

    if action in ("listening_sensitivity", "set_sensitivity", "sensitivity"):
        value = kwargs.get("value") or kwargs.get("percent") or kwargs.get("level")
        if value is None:
            current = _settings.get_listening_sensitivity()
            return f"Current listening sensitivity is {current}%."
        return _settings.set_listening_sensitivity(value)

    if action in ("increase_sensitivity", "sensitivity_up"):
        return _settings.set_listening_sensitivity("up")

    if action in ("decrease_sensitivity", "sensitivity_down"):
        return _settings.set_listening_sensitivity("down")

    # ── Status dump ───────────────────────────────────────────────────────

    if action in ("status", "list", "show"):
        data   = _settings.get_all()
        p      = _settings.get_personality()
        sens   = _settings.get_listening_sensitivity()
        return (
            f"Proactive suggestions: {'on' if data.get('proactive_suggestions_enabled') else 'off'}. "
            f"Wake greeting: {'on' if data.get('wake_greeting_enabled') else 'off'}. "
            f"Voice verification: {'on' if data.get('voice_verification_enabled') else 'off'}. "
            f"Interruption: {'on' if data.get('barge_in_enabled', True) else 'off'}. "
            f"Listening sensitivity: {sens}%. "
            f"Personality: " + ", ".join(f"{k}={v}%" for k, v in p.items()) + "."
        )

    return (
        f"ERROR: Unknown user_settings action '{action}'. Valid actions: "
        "set_personality, get_personality, proactive_suggestions, "
        "wake_greeting, voice_verification, barge_in, "
        "listening_sensitivity, increase_sensitivity, decrease_sensitivity, status."
    )


__all__ = ["user_settings"]
