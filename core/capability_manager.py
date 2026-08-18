"""
core/capability_manager.py — Capability Gate for Tool Execution
===============================================================
Extends the ConfidenceScorer from Phase 1 of the JARVIS architecture into
every tool execution path — not just HIGH/DESTRUCTIVE tools.

Gate logic per risk tier
------------------------
  SAFE        → always execute (no scoring overhead)
  LOW         → always execute; circuit-broken tools are blocked
  MEDIUM      → score + execute if MEDIUM or HIGH confidence; LOW → ask user
  HIGH        → score + require HIGH confidence; MEDIUM → ask user
  DESTRUCTIVE → score + require HIGH confidence with extra penalty; else block

The gate is called from ExecutionQueue before any tool runs.  It returns a
GateResult (go, reason, score, ask_user) so the caller decides how to
respond to Gemini — either proceed, return a "please confirm" message, or
silently skip and let Gemini narrate.

Multi-step planning integration
--------------------------------
When the caller passes a list of tool calls (a batch from Gemini), the
CapabilityManager can route the sequence through GoalPlanner so mid-sequence
failures trigger verified rollback instead of leaving the desktop in a
partial state.

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.confidence import (
    ActionRisk,
    ConfidenceLevel,
    ConfidenceScore,
    confidence_scorer,
    classify_risk,
)
from utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Decision returned by CapabilityManager.gate()."""
    go: bool                              # True → execute the tool
    reason: str                           # human-readable explanation
    score: Optional[ConfidenceScore] = None
    ask_user: bool = False                # True → tell Gemini to ask for confirmation
    block_message: str = ""              # message to return to Gemini when blocked


# ---------------------------------------------------------------------------
# Per-tool circuit breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Per-tool sliding-window circuit breaker.

    Opens (blocks the tool) when failure_threshold failures occur within
    window_seconds.  Resets automatically after reset_after_seconds.
    Fails open by default — a broken circuit breaker never blocks tools.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        window_seconds: float = 60.0,
        reset_after_seconds: float = 120.0,
    ) -> None:
        self._threshold = failure_threshold
        self._window = window_seconds
        self._reset_after = reset_after_seconds
        self._failures: Dict[str, List[float]] = {}  # tool → [ts, ...]
        self._opened_at: Dict[str, float] = {}       # tool → ts when circuit opened
        self._lock = threading.RLock()

    def record_failure(self, tool: str) -> None:
        with self._lock:
            now = time.monotonic()
            if tool not in self._failures:
                self._failures[tool] = []
            self._failures[tool].append(now)
            # Trim old entries
            cutoff = now - self._window
            self._failures[tool] = [t for t in self._failures[tool] if t > cutoff]
            if len(self._failures[tool]) >= self._threshold:
                if tool not in self._opened_at:
                    self._opened_at[tool] = now
                    log.warning(
                        f"[CircuitBreaker] Circuit OPEN for tool '{tool}' "
                        f"({self._threshold} failures in {self._window:.0f}s). "
                        f"Resets in {self._reset_after:.0f}s."
                    )

    def record_success(self, tool: str) -> None:
        with self._lock:
            self._failures.pop(tool, None)
            self._opened_at.pop(tool, None)

    def is_open(self, tool: str) -> bool:
        """True if the circuit is open (tool should be blocked)."""
        with self._lock:
            opened = self._opened_at.get(tool)
            if opened is None:
                return False
            if time.monotonic() - opened >= self._reset_after:
                # Auto-reset
                self._failures.pop(tool, None)
                self._opened_at.pop(tool, None)
                log.info(f"[CircuitBreaker] Circuit RESET for tool '{tool}'.")
                return False
            return True

    def open_tools(self) -> List[str]:
        """Return names of all currently open (blocked) circuits."""
        with self._lock:
            now = time.monotonic()
            return [
                t for t, ts in self._opened_at.items()
                if now - ts < self._reset_after
            ]


# Process-wide circuit breaker
circuit_breaker = CircuitBreaker(
    failure_threshold=3,
    window_seconds=60.0,
    reset_after_seconds=120.0,
)


# ---------------------------------------------------------------------------
# Capability Manager
# ---------------------------------------------------------------------------

class CapabilityManager:
    """
    Gates tool execution with confidence scoring + circuit breaking.

    Replaces the narrow HIGH/DESTRUCTIVE-only gate in _execute_single_tool_call
    with a full-spectrum gate applied to every tool call before execution.

    Usage::

        result = capability_manager.gate("delete_file", args, intent_clarity=0.88)
        if not result.go:
            return result.block_message
        # ... proceed with execution
    """

    # Minimum confidence scores required per risk tier
    _MIN_SCORE: Dict[str, float] = {
        ActionRisk.SAFE.value:        0.0,   # always pass
        ActionRisk.LOW.value:         0.0,   # always pass (circuit breaker only)
        ActionRisk.MEDIUM.value:      0.40,  # MEDIUM confidence required
        ActionRisk.HIGH.value:        0.75,  # HIGH confidence required
        ActionRisk.DESTRUCTIVE.value: 0.80,  # HIGH + extra margin
    }

    # Extra scoring penalty applied at each tier (on top of the risk penalty
    # already inside ConfidenceScorer).
    _EXTRA_PENALTY: Dict[str, float] = {
        ActionRisk.SAFE.value:        0.00,
        ActionRisk.LOW.value:         0.00,
        ActionRisk.MEDIUM.value:      0.00,
        ActionRisk.HIGH.value:        0.00,
        ActionRisk.DESTRUCTIVE.value: 0.10,
    }

    def gate(
        self,
        tool_name: str,
        args: dict,
        intent_clarity: float = 0.88,
        trust_level: str = "trusted",
    ) -> GateResult:
        """
        Evaluate whether a tool should execute.

        Args:
            tool_name:       Name of the tool being called.
            args:            Tool arguments (for context; not modified).
            intent_clarity:  0.0–1.0 confidence from the intent source.
                             Gemini Live calls → 0.88, Flash-Lite → 0.80.
            trust_level:     User trust level from World Model.

        Returns:
            GateResult — always returns a result, never raises.
        """
        try:
            return self._evaluate(tool_name, args, intent_clarity, trust_level)
        except Exception as exc:
            log.debug(f"[CapabilityManager] gate() error (fail-open): {exc}")
            # Fail open — never block on scorer errors
            return GateResult(go=True, reason=f"gate error (fail-open): {exc}")

    def _evaluate(
        self,
        name: str,
        args: dict,
        intent_clarity: float,
        trust_level: str,
    ) -> GateResult:
        risk = classify_risk(name)

        # ── SAFE / LOW: circuit breaker only ────────────────────────────────
        if risk in (ActionRisk.SAFE, ActionRisk.LOW):
            if circuit_breaker.is_open(name):
                return GateResult(
                    go=False,
                    reason=f"circuit open — '{name}' has failed repeatedly",
                    ask_user=False,
                    block_message=(
                        f"The '{name}' tool has been temporarily disabled after "
                        f"repeated failures. Try again in a moment, or ask Sir "
                        f"to check the system."
                    ),
                )
            return GateResult(go=True, reason=f"risk={risk.value}, pass-through")

        # ── MEDIUM / HIGH / DESTRUCTIVE: score + threshold check ────────────
        extra_penalty = self._EXTRA_PENALTY.get(risk.value, 0.0)
        score = confidence_scorer.score(
            action=name,
            risk=risk,
            intent_clarity=intent_clarity,
            context_resolved=self._context_resolved(args),
            trust_level=trust_level,
            extra_penalty=extra_penalty,
        )

        min_score = self._MIN_SCORE.get(risk.value, 0.40)

        if circuit_breaker.is_open(name):
            return GateResult(
                go=False,
                reason=f"circuit open — '{name}' has failed repeatedly",
                score=score,
                ask_user=False,
                block_message=(
                    f"The '{name}' tool has been temporarily disabled after "
                    f"repeated failures. Please ask Sir to retry later."
                ),
            )

        if score.score < min_score:
            log.info(
                f"[CapabilityManager] Gating '{name}': "
                f"score={score.score:.2f} < min={min_score:.2f}, "
                f"risk={risk.value}, level={score.level.value}"
            )
            if score.level == ConfidenceLevel.LOW:
                return GateResult(
                    go=False,
                    reason=f"low confidence (score={score.score:.2f})",
                    score=score,
                    ask_user=True,
                    block_message=(
                        f"Confidence too low to run '{name}' automatically "
                        f"(risk={risk.value}, score={score.score:.2f}). "
                        "Please ask Sir to explicitly confirm this action before "
                        "proceeding — do NOT retry without his go-ahead."
                    ),
                )
            # MEDIUM confidence for a HIGH/DESTRUCTIVE tool → ask first
            return GateResult(
                go=False,
                reason=f"insufficient confidence (score={score.score:.2f}, need={min_score:.2f})",
                score=score,
                ask_user=True,
                block_message=(
                    f"This action ('{name}', risk={risk.value}) requires higher "
                    f"confidence before I proceed automatically "
                    f"(score={score.score:.2f}, need≥{min_score:.2f}). "
                    "Confirm with Sir before retrying."
                ),
            )

        return GateResult(
            go=True,
            reason=f"score={score.score:.2f} ≥ min={min_score:.2f}",
            score=score,
        )

    @staticmethod
    def _context_resolved(args: dict) -> bool:
        """
        Heuristic: are vague references ("it", "that", "this") still present
        in the args?  If so, context is likely unresolved.
        """
        vague = {"it", "that", "this", "there", "them", "those"}
        for v in args.values():
            if isinstance(v, str) and v.strip().lower() in vague:
                return False
        return True

    # ── Trust level resolution ────────────────────────────────────────────────

    @staticmethod
    def get_trust_level() -> str:
        """Pull trust level from World Model (defaults to 'trusted')."""
        try:
            from core.world_model import world
            snap = world.snapshot()
            return snap.user.trust_level
        except Exception:
            return "trusted"

    # ── Multi-step planning ───────────────────────────────────────────────────

