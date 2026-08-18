"""
actions/security.py — Gama Sensitive-Action Security Gate (compatibility shim)
=================================================================================
This module used to contain the full security decision logic directly.
That logic has moved to the new, more capable `security/` package
(security/trust_levels.py, security/authentication.py,
security/verification_pipeline.py, security/security_manager.py), which
adds real ECAPA-TDNN voice verification,
and four graduated trust levels (SAFE/NORMAL/SENSITIVE/DESTRUCTIVE) with
proper multi-factor authentication for destructive actions.

This file remains as a thin compatibility layer so main.py's existing
`from actions import security as security_gate` / `security_gate.authorize(...)`
call site keeps working unchanged — the call signature and return shape
are identical to before.

Author: Vineet Machchal / Gama Security Upgrade
"""

from __future__ import annotations

from typing import Optional, Tuple

from security import security_manager

# Kept for any external code that still inspects this dict directly (it is
# now informational only — the canonical classification table lives in
# security/trust_levels.py and is what authorize() actually uses).
SENSITIVE_TOOLS: dict = {
    "computer_settings": {"shutdown", "restart", "reboot", "sleep", "lock", "sign_out"},
    "file_controller": {"delete", "empty_recycle_bin"},
    "terminal_command": True,
    "computer_agent": True,
    "startup_manager": {"add", "remove", "enable", "disable"},
    "process_manager": {"kill", "kill_all"},
    "game_updater": {"install"},
    "email_sender": {"setup"},
    "meeting_watch": {"start"},
}


def is_sensitive(tool_name: str, args: dict) -> bool:
    return security_manager.is_sensitive(tool_name, args)


def recent_security_events(limit: int = 20) -> list[dict]:
    return security_manager.recent_security_events(limit)


def authorize(tool_name: str, args: dict, recent_pcm: Optional[bytes],
              confirmation_code: Optional[str] = None,
              verbal_confirmed: Optional[bool] = None,
              transcript: Optional[str] = None,
              transcript_age_s: Optional[float] = None,
              verbal_intent: Optional[bool] = None) -> Tuple[bool, str]:
    """Decide whether this call is allowed to proceed. See
    security/security_manager.py for the full decision logic — this is
    just a stable-signature pass-through.

    `verbal_intent` is the pre-computed natural-language confirmation
    verdict (True/False/None) from
    security.authentication.classify_confirmation_intent() — see
    main.py's tool-dispatch site for where it's produced.
    """
    return security_manager.authorize(
        tool_name, args, recent_pcm=recent_pcm,
        confirmation_code=confirmation_code, verbal_confirmed=verbal_confirmed,
        transcript=transcript, transcript_age_s=transcript_age_s,
        verbal_intent=verbal_intent,
    )


__all__ = ["is_sensitive", "authorize", "recent_security_events", "SENSITIVE_TOOLS"]
