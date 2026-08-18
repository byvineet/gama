"""
security/verification_pipeline.py — Multi-Factor Verification Orchestration
==============================================================================
Given a TrustLevel, runs exactly the combination of factors that level
requires and returns one combined verdict:

    SAFE        -> allow immediately, no factors run.
    NORMAL      -> allow immediately, no factors run (caller logs it).
    SENSITIVE   -> voice verification only.
    DESTRUCTIVE -> voice AND verbal confirmation, both required.

Any missing/failed factor at SENSITIVE/DESTRUCTIVE level means deny —
this pipeline never partially executes a command.

Author: Gama Security Upgrade
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from security import authentication as auth
from security.trust_levels import TrustLevel, describe
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class PipelineResult:
    allowed: bool
    level: TrustLevel
    factors: List[auth.FactorResult] = field(default_factory=list)
    message: str = ""
    latency_ms: float = 0.0


def run(level: TrustLevel, *, recent_pcm: Optional[bytes] = None,
        expected_name: Optional[str] = None, confirmation_code: Optional[str] = None,
        verbal_confirmed: bool = False, transcript: Optional[str] = None,
        transcript_age_s: Optional[float] = None,
        verbal_intent: Optional[bool] = None) -> PipelineResult:
    t0 = time.monotonic()

    if level == TrustLevel.SAFE:
        return PipelineResult(True, level, [], "", 0.0)

    if level == TrustLevel.NORMAL:
        return PipelineResult(True, level, [], "", round((time.monotonic() - t0) * 1000, 1))

    if level == TrustLevel.SENSITIVE:
        voice = auth.check_voice(recent_pcm, expected_name)
        latency = round((time.monotonic() - t0) * 1000, 1)
        if voice.passed:
            return PipelineResult(True, level, [voice], "", latency)
        return PipelineResult(
            False, level, [voice],
            f"I couldn't verify your voice for this ({voice.detail}). "
            f"Sensitive actions are blocked for unverified speakers.",
            latency,
        )

    if level == TrustLevel.DESTRUCTIVE:
        verbal = auth.check_verbal_confirmation(
            verbal_confirmed, transcript=transcript, transcript_age_s=transcript_age_s,
            intent=verbal_intent,
        )

        try:
            from state_engine import user_settings
            voice_verification_wanted = user_settings.get_voice_verification_enabled()
        except Exception:
            voice_verification_wanted = True

        if voice_verification_wanted:
            voice = auth.check_voice(recent_pcm, expected_name)
            factors = [voice, verbal]
            latency = round((time.monotonic() - t0) * 1000, 1)

            failed = [f for f in factors if not f.passed]
            if not failed:
                return PipelineResult(True, level, factors, "", latency)

            reasons = "; ".join(f"{f.factor}: {f.detail}" for f in failed)
            return PipelineResult(
                False, level, factors,
                f"This is a destructive action and requires voice + verbal "
                f"confirmation, both of which must succeed. Blocked because: {reasons}.",
                latency,
            )

        # Voice verification opted out — the confirmation code (checked
        # separately, in security_manager.authorize) is now the mandatory
        # second factor in place of voice, alongside verbal confirmation.
        factors = [verbal]
        latency = round((time.monotonic() - t0) * 1000, 1)
        if not verbal.passed:
            return PipelineResult(
                False, level, factors,
                f"This is a destructive action and requires verbal confirmation "
                f"plus your confirmation code (voice verification is off). "
                f"Blocked because: {verbal.factor}: {verbal.detail}.",
                latency,
            )
        return PipelineResult(True, level, factors, "", latency)

    # Should never happen given TrustLevel's four members, but fail safe.
    return PipelineResult(False, level, [], f"Unknown trust level: {describe(level)}", 0.0)


__all__ = ["PipelineResult", "run"]
