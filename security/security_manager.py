"""
security/security_manager.py — Centralized Security Manager
===============================================================
The single chokepoint every tool call passes through before it runs.
Classifies the call's trust level (security/trust_levels.py), runs the
appropriate verification pipeline (security/verification_pipeline.py),
and logs every decision to a local-only audit trail.

This is a drop-in replacement for the previous actions/security.py
`authorize()` — same call signature, same (bool, message) return shape —
so main.py's tool dispatcher didn't need to change at all; only its
underlying decision logic got smarter (4 graduated trust levels + real
biometric MFA instead of a single sensitive/not-sensitive bit).

Author: Gama Security Upgrade
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Tuple

from security.trust_levels import TrustLevel, classify, describe
from security.verification_pipeline import run as run_pipeline
from security import trusted_session
from storage.user_profiles import DEFAULT_OWNER
from utils.logger import get_logger

log = get_logger(__name__)

SECURITY_DIR = Path.home() / ".gama" / "security"
SECURITY_DIR.mkdir(parents=True, exist_ok=True)
SECURITY_LOG = SECURITY_DIR / "security.log"


def _log_event(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **event}
    try:
        with open(SECURITY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as exc:
        log.error(f"Could not write security log: {exc}")
    log.info(f"[security] {event}")


def recent_security_events(limit: int = 20) -> list[dict]:
    if not SECURITY_LOG.exists():
        return []
    try:
        lines = SECURITY_LOG.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
    except Exception:
        return []


def is_sensitive(tool_name: str, args: dict) -> bool:
    """Backward-compatible helper: True for anything at SENSITIVE level
    or above (i.e. anything that needs at least voice verification)."""
    return classify(tool_name, args) >= TrustLevel.SENSITIVE


def classify_tool(tool_name: str, args: dict) -> TrustLevel:
    return classify(tool_name, args)


def authorize(tool_name: str, args: dict, recent_pcm: Optional[bytes] = None,
              confirmation_code: Optional[str] = None,
              verbal_confirmed: Optional[bool] = None,
              transcript: Optional[str] = None,
              transcript_age_s: Optional[float] = None,
              expected_name: str = DEFAULT_OWNER,
              verbal_intent: Optional[bool] = None) -> Tuple[bool, str]:
    """Decide whether this tool call is allowed to proceed.

    Returns (allowed, message) — message is empty when allowed, or a
    user-facing explanation of why it was blocked / what's still needed
    when not. This exact shape matches the previous actions/security.py
    API so callers don't need to change.

    `verbal_confirmed` may also be read from args["verbal_confirmed"] or
    args["confirmed"] if not passed explicitly, so callers that only
    have access to the raw tool args (rather than a separately-tracked
    transcript flag) still work without extra plumbing.
    """
    level = classify(tool_name, args)
    action = str(args.get("action", "")).lower().strip()

    if verbal_confirmed is None:
        verbal_confirmed = bool(args.get("verbal_confirmed") or args.get("confirmed", False))

    if level == TrustLevel.SAFE:
        return True, ""

    if level == TrustLevel.NORMAL:
        _log_event({"tool": tool_name, "action": action, "level": level.name, "decision": "allow"})
        return True, ""

    # --- Trusted session shortcut (spec section 3) ------------------
    # SENSITIVE-tier ("MEDIUM") commands accept a still-valid trusted
    # session (created once at wake-time speaker verification) instead
    # of re-running a fresh voice check on every single call.
    if level == TrustLevel.SENSITIVE and trusted_session.is_owner_trusted(expected_name):
        trusted_session.touch_session()  # sliding window: real usage extends it
        _log_event({"tool": tool_name, "action": action, "level": level.name,
                     "decision": "allow", "reason": "trusted_session"})
        return True, ""

    # NOTE: The DESTRUCTIVE trusted-session shortcut has been intentionally
    # removed.  Every DESTRUCTIVE-tier call must pass the full
    # voice+verbal+confirmation-code pipeline — no bypass, no window.

    result = run_pipeline(
        level,
        recent_pcm=recent_pcm,
        expected_name=expected_name,
        confirmation_code=confirmation_code,
        verbal_confirmed=verbal_confirmed,
        transcript=transcript,
        transcript_age_s=transcript_age_s,
        verbal_intent=verbal_intent,
    )

    event = {
        "tool": tool_name, "action": action, "level": level.name,
        "decision": "allow" if result.allowed else "deny",
        "latency_ms": result.latency_ms,
        "factors": [
            {"factor": f.factor, "passed": f.passed, "confidence": f.confidence, "detail": f.detail}
            for f in result.factors
        ],
    }
    if not result.allowed:
        event["reason"] = result.message
    _log_event(event)

    if not result.allowed:
        trusted_session.invalidate("failed authentication")

    # DESTRUCTIVE actions may still additionally require the shared
    # confirmation code for defense-in-depth, mirroring the previous
    # behavior of actions/security.py/actions/confirmation.py, but only
    # once the three biometric+verbal factors already passed — a code
    # never substitutes for a failed biometric factor.
    if result.allowed and level == TrustLevel.DESTRUCTIVE:
        from actions.confirmation import requires_confirmation, is_code_set, verify_confirmation_code
        try:
            from state_engine import user_settings
            voice_verification_wanted = user_settings.get_voice_verification_enabled()
        except Exception:
            voice_verification_wanted = True

        if not voice_verification_wanted and not is_code_set():
            # Voice verification opted out but no fallback code exists —
            # never allow a destructive action through on verbal alone.
            _log_event({"tool": tool_name, "action": action, "level": level.name,
                        "decision": "deny", "reason": "voice verification off, no confirmation code set"})
            return False, ("Voice verification is off and you haven't set a confirmation "
                            "code yet, so this destructive action can't proceed. Please set "
                            "a confirmation code first.")

        code_required = is_code_set() and (requires_confirmation(action) or not voice_verification_wanted)
        if code_required:
            if not confirmation_code:
                _log_event({"tool": tool_name, "action": action, "level": level.name,
                            "decision": "deny", "reason": "confirmation code missing"})
                return False, ("Voice and verbal confirmation checked out, but this "
                               "destructive action also needs your confirmation code.")
            if verify_confirmation_code(confirmation_code) != "VERIFIED":
                trusted_session.invalidate("failed confirmation code")
                _log_event({"tool": tool_name, "action": action, "level": level.name,
                            "decision": "deny", "reason": "wrong confirmation code"})
                return False, "The confirmation code was incorrect. Action cancelled."

    if not result.allowed:
        return False, result.message

    if level == TrustLevel.SENSITIVE:
        # A fresh voice check just succeeded — start/refresh the trusted
        # session so rapid follow-up SENSITIVE commands skip re-verification.
        trusted_session.create_session(expected_name, confidence=1.0)

    # NOTE: No destructive trusted session is created — every DESTRUCTIVE
    # action requires fresh proof, no caching.

    return True, ""


__all__ = [
    "authorize", "is_sensitive", "classify_tool",
    "recent_security_events", "TrustLevel", "describe",
]
