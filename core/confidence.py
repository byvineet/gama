"""
core/confidence.py — Confidence Scoring for Action Execution
=============================================================
Phase 1 of the JARVIS reliability architecture.

Every intent is scored before execution:

  High   (score ≥ 0.75) → Execute immediately
  Medium (score ≥ 0.40) → Execute if safe / low-risk
  Low    (score <  0.40) → Ask user before proceeding

Confidence is computed from:
  • Intent clarity        — how certain the parser is about what was asked
  • Action risk           — destructive actions get lower base confidence
  • User trust level      — trusted users raise the threshold for execution
  • Context availability  — resolving "that file" only when context is clear
  • Past success rate     — actions that have failed recently score lower

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------

class ConfidenceLevel(str, Enum):
    HIGH   = "high"    # ≥ 0.75 — execute immediately
    MEDIUM = "medium"  # ≥ 0.40 — execute if safe
    LOW    = "low"     # <  0.40 — ask user

    @classmethod
    def from_score(cls, score: float) -> "ConfidenceLevel":
        if score >= 0.75:
            return cls.HIGH
        if score >= 0.40:
            return cls.MEDIUM
        return cls.LOW


# ---------------------------------------------------------------------------
# Action risk categories
# ---------------------------------------------------------------------------

class ActionRisk(str, Enum):
    """How dangerous is this action if done wrong?"""
    SAFE        = "safe"        # read-only, reversible, low impact
    LOW         = "low"         # small side effects, easy to undo
    MEDIUM      = "medium"      # moderate impact, hard to undo (e.g. send email)
    HIGH        = "high"        # irreversible or high-impact (delete files, payments)
    DESTRUCTIVE = "destructive" # no undo at all (format drive, factory reset)

    @property
    def penalty(self) -> float:
        """Score reduction applied at each risk level."""
        return {
            "safe":        0.00,
            "low":         0.05,
            "medium":      0.15,
            "high":        0.30,
            "destructive": 0.50,
        }[self.value]


# ---------------------------------------------------------------------------
# Confidence score
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceScore:
    score: float
    level: ConfidenceLevel
    action: str
    risk: ActionRisk
    reasons: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def should_ask_user(self) -> bool:
        return self.level == ConfidenceLevel.LOW

    @property
    def should_execute(self) -> bool:
        return self.level in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)

    def __str__(self) -> str:
        return (
            f"ConfidenceScore(action={self.action!r}, score={self.score:.2f}, "
            f"level={self.level.value}, risk={self.risk.value})"
        )


# ---------------------------------------------------------------------------
# Confidence scorer
# ---------------------------------------------------------------------------

class ConfidenceScorer:
    """
    Computes a confidence score for an action before execution.

    Usage::

        scorer = ConfidenceScorer()

        score = scorer.score(
            action="delete_file",
            risk=ActionRisk.HIGH,
            intent_clarity=0.9,         # from LLM / fast-intent
            context_resolved=True,      # "that file" was resolved
            trust_level="trusted",
        )

        if score.should_ask_user:
            gama.ask("Are you sure you want to delete that file?")
        else:
            execute_action()
            scorer.record_outcome(action="delete_file", success=True)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Track success/failure per action for adaptive scoring
        self._outcomes: Dict[str, list] = {}  # action → [(ts, success), ...]
        self._window = 3600.0  # look back 1 hour for success rate

    def score(
        self,
        action: str,
        risk: ActionRisk = ActionRisk.SAFE,
        intent_clarity: float = 1.0,
        context_resolved: bool = True,
        trust_level: str = "trusted",
        extra_penalty: float = 0.0,
    ) -> ConfidenceScore:
        """
        Compute a confidence score.

        Args:
            action:           Name of the action being scored.
            risk:             ActionRisk level.
            intent_clarity:   0.0–1.0 from the intent parser. Default 1.0.
            context_resolved: True if all context refs ("that file") are known.
            trust_level:      "trusted" / "guest" / "unverified".
            extra_penalty:    Additional custom penalty (0.0–1.0).
        """
        reasons = []

        # Base score = intent clarity (what user meant)
        base = max(0.0, min(1.0, intent_clarity))
        reasons.append(f"intent_clarity={base:.2f}")

        # Risk penalty
        rp = risk.penalty
        if rp > 0:
            base -= rp
            reasons.append(f"risk_penalty=-{rp:.2f} ({risk.value})")

        # Context resolution penalty
        if not context_resolved:
            base -= 0.20
            reasons.append("context_unresolved=-0.20")

        # Trust level adjustment
        trust_bonus = {"trusted": 0.05, "guest": 0.00, "unverified": -0.15}.get(trust_level, 0.0)
        if trust_bonus != 0:
            base += trust_bonus
            reasons.append(f"trust_{trust_level}={trust_bonus:+.2f}")

        # Past success rate adjustment
        sr = self._success_rate(action)
        if sr is not None:
            if sr < 0.5:
                penalty = (0.5 - sr) * 0.3  # up to -0.15 for 0% success rate
                base -= penalty
                reasons.append(f"past_success={sr:.0%}, penalty=-{penalty:.2f}")
            elif sr > 0.9:
                base += 0.05
                reasons.append(f"past_success={sr:.0%}, bonus=+0.05")

        # Extra custom penalty
        if extra_penalty > 0:
            base -= extra_penalty
            reasons.append(f"extra_penalty=-{extra_penalty:.2f}")

        score = max(0.0, min(1.0, base))
        level = ConfidenceLevel.from_score(score)

        return ConfidenceScore(
            score=score,
            level=level,
            action=action,
            risk=risk,
            reasons=reasons,
        )

    def record_outcome(self, action: str, success: bool) -> None:
        """Record the outcome of an executed action for adaptive scoring."""
        with self._lock:
            if action not in self._outcomes:
                self._outcomes[action] = []
            self._outcomes[action].append((time.time(), success))
            # Trim old entries
            cutoff = time.time() - self._window
            self._outcomes[action] = [
                (ts, s) for (ts, s) in self._outcomes[action] if ts > cutoff
            ]

    def _success_rate(self, action: str) -> Optional[float]:
        """Return recent success rate for an action, or None if no data."""
        with self._lock:
            cutoff = time.time() - self._window
            entries = [
                s for (ts, s) in self._outcomes.get(action, []) if ts > cutoff
            ]
        if len(entries) < 3:  # not enough data
            return None
        return sum(entries) / len(entries)

    def report(self) -> Dict[str, dict]:
        """Return success rates for all tracked actions."""
        with self._lock:
            result = {}
            for action, entries in self._outcomes.items():
                cutoff = time.time() - self._window
                recent = [s for (ts, s) in entries if ts > cutoff]
                if recent:
                    result[action] = {
                        "success_rate": sum(recent) / len(recent),
                        "sample_count": len(recent),
                    }
            return result


# ---------------------------------------------------------------------------
# Convenience functions using the process-wide scorer
# ---------------------------------------------------------------------------

# Risk classification for common action types
ACTION_RISK_MAP: Dict[str, ActionRisk] = {
    # Safe reads / reversible
    "get_weather": ActionRisk.SAFE,
    "weather_report": ActionRisk.SAFE,
    "web_search": ActionRisk.SAFE,
    "edge_search": ActionRisk.SAFE,
    "get_time": ActionRisk.SAFE,
    "get_system_info": ActionRisk.SAFE,
    "system_info": ActionRisk.SAFE,
    "read_file": ActionRisk.SAFE,
    "take_screenshot": ActionRisk.SAFE,
    "screen_process": ActionRisk.SAFE,
    "desktop_context": ActionRisk.SAFE,
    "recall_memory": ActionRisk.SAFE,
    # Low risk — auto-run
    "open_app": ActionRisk.LOW,
    "play_music": ActionRisk.LOW,
    "set_volume": ActionRisk.LOW,
    "set_brightness": ActionRisk.LOW,
    "set_reminder": ActionRisk.LOW,
    "create_folder": ActionRisk.LOW,
    "copy_file": ActionRisk.LOW,
    "sleep": ActionRisk.LOW,
    "lock": ActionRisk.LOW,
    # Medium — outbound / costly (short confirm)
    "send_email": ActionRisk.MEDIUM,
    "email_action": ActionRisk.MEDIUM,
    "send_message": ActionRisk.MEDIUM,
    "whatsapp": ActionRisk.MEDIUM,
    "send_whatsapp": ActionRisk.MEDIUM,
    "post_instagram": ActionRisk.MEDIUM,
    "write_file": ActionRisk.MEDIUM,
    "move_file": ActionRisk.MEDIUM,
    "run_script": ActionRisk.MEDIUM,
    "system_settings": ActionRisk.MEDIUM,
    # High
    "delete_file": ActionRisk.HIGH,
    "terminate_process": ActionRisk.HIGH,
    "clear_clipboard": ActionRisk.LOW,  # reversible
    # Destructive — code required
    "shutdown": ActionRisk.DESTRUCTIVE,
    "restart": ActionRisk.DESTRUCTIVE,
    "reboot": ActionRisk.DESTRUCTIVE,
    "uninstall_app": ActionRisk.DESTRUCTIVE,
    "format_drive": ActionRisk.DESTRUCTIVE,
    "factory_reset": ActionRisk.DESTRUCTIVE,
    "wipe_memory": ActionRisk.DESTRUCTIVE,
    "empty_recycle_bin": ActionRisk.DESTRUCTIVE,
}


def classify_risk(action: str) -> ActionRisk:
    """Return the risk level for a known action, defaulting to LOW."""
    return ACTION_RISK_MAP.get(action.lower(), ActionRisk.LOW)


# Process-wide singleton scorer
confidence_scorer = ConfidenceScorer()


def score_action(
    action: str,
    intent_clarity: float = 1.0,
    context_resolved: bool = True,
    trust_level: str = "trusted",
) -> ConfidenceScore:
    """Convenience wrapper for the process-wide scorer."""
    risk = classify_risk(action)
    return confidence_scorer.score(
        action=action,
        risk=risk,
        intent_clarity=intent_clarity,
        context_resolved=context_resolved,
        trust_level=trust_level,
    )


__all__ = [
    "ConfidenceLevel", "ConfidenceScore", "ConfidenceScorer",
    "ActionRisk", "ACTION_RISK_MAP",
    "classify_risk", "score_action", "confidence_scorer",
]
