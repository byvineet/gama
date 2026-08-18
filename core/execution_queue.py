"""
core/execution_queue.py — Execution Queue with Verification & Recovery
======================================================================
Wraps every tool execution with two cross-cutting concerns the old
if-elif dispatcher lacked:

  1. Post-execution result verification
  2. Retry with exponential backoff + circuit-breaker feedback

The capability gate (ConfidenceScorer) is deliberately *not* in this
module — it is called in _execute_single_tool_call where the Gemini
session is available so a "please confirm" message can be sent back.
This module handles pure execution reliability after the gate passes.

The queue is synchronous — designed to run inside ``asyncio.to_thread``
so it never blocks the event loop.

Author : Vineet Machchal
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from utils.logger import get_logger
from utils import perf as _perf

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Failure / transient patterns
# ---------------------------------------------------------------------------

_FAILURE_PREFIXES = (
    "Tool failed",
    "Error:",
    "ERROR:",
    "Exception:",
    "BLOCKED:",
    "NOT EXECUTED:",
    "Unknown tool",
)

_TRANSIENT_KEYWORDS = (
    "timeout",
    "timed out",
    "connection refused",
    "connection reset",
    "temporarily unavailable",
    "rate limit",
    "try again",
)


# ---------------------------------------------------------------------------
# Result verifier
# ---------------------------------------------------------------------------

class ToolVerifier:
    """
    Lightweight post-execution result checker.

    Checks the result string for known failure patterns and optionally
    calls a per-tool custom verifier registered in ToolRegistry.
    """

    @staticmethod
    def verify(
        name: str,
        args: dict,
        result: str,
        custom_verifier: Optional[Callable[[str, dict, str], tuple]] = None,
    ) -> tuple:
        """
        Verify a tool result.

        Returns:
            (ok: bool, detail: str, transient: bool)
            transient=True means the failure may resolve on retry.
        """
        if isinstance(result, str):
            result_lower = result.lower()
            for prefix in _FAILURE_PREFIXES:
                if result.startswith(prefix):
                    transient = any(kw in result_lower for kw in _TRANSIENT_KEYWORDS)
                    return False, result, transient

        if custom_verifier is not None:
            try:
                ok, detail = custom_verifier(name, args, result)
                return ok, detail, False
            except Exception as exc:
                log.debug(f"[ToolVerifier] Custom verifier for '{name}' raised: {exc}")

        return True, result, False


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Full record of one tool execution attempt."""
    tool: str
    result: str
    success: bool
    retried: bool = False
    verification_detail: str = ""
    duration_ms: float = 0.0
    attempts: int = 1


# ---------------------------------------------------------------------------
# Execution Queue
# ---------------------------------------------------------------------------

class ExecutionQueue:
    """
    Wraps tool execution with verify → retry → record-outcome.

    The pre-execution confidence gate is the caller's responsibility
    (_execute_single_tool_call) so the Gemini session can receive a
    properly-formed tool response on block.

    Usage::

        result = exec_queue.run(
            name="delete_file",
            args={"path": "/tmp/foo.txt"},
            executor_fn=_execute_tool_impl,
        )
        return result.result   # str for Gemini
    """

    #: Max automatic retries on transient failure (1 = try twice total)
    MAX_RETRIES: int = 1
    #: Pause between retries (seconds)
    RETRY_BACKOFF: float = 0.4

    def run(
        self,
        name: str,
        args: dict,
        executor_fn: Callable[[str, dict], str],
        custom_verifier: Optional[Callable] = None,
    ) -> ExecutionResult:
        """
        Execute a tool with verify + retry + outcome recording.

        Args:
            name:            Tool name.
            args:            Tool arguments dict.
            executor_fn:     Callable(name, args) → str — the tool handler.
            custom_verifier: Optional (name, args, result) → (ok, str).

        Returns:
            ExecutionResult — never raises.
        """
        t0 = time.perf_counter()
        result_str = ""
        success = False
        transient = False
        timed_out = False
        attempts = 0

        _perf.reset_llm_call_count()

        for attempt in range(self.MAX_RETRIES + 1):
            attempts = attempt + 1
            try:
                result_str = executor_fn(name, args)
            except Exception as exc:
                result_str = f"Tool failed: {exc}"
                log.error(
                    f"[ExecQueue] '{name}' raised on attempt {attempts}: {exc}",
                    exc_info=True,
                )
                transient = True
                if any(kw in str(exc).lower() for kw in ("timeout", "timed out", "deadline")):
                    timed_out = True
            else:
                ok, detail, transient = ToolVerifier.verify(
                    name, args, result_str, custom_verifier
                )
                if ok:
                    success = True
                    result_str = detail
                    break
                else:
                    result_str = detail
                    if any(kw in detail.lower() for kw in ("timeout", "timed out", "deadline")):
                        timed_out = True
                    log.warning(
                        f"[ExecQueue] '{name}' verification failed "
                        f"(attempt {attempts}/{self.MAX_RETRIES + 1}): {detail!r}"
                    )

            if not transient or attempt >= self.MAX_RETRIES:
                break

            log.info(
                f"[ExecQueue] '{name}' transient failure — "
                f"retrying in {self.RETRY_BACKOFF}s…"
            )
            time.sleep(self.RETRY_BACKOFF)

        duration_ms = (time.perf_counter() - t0) * 1000.0
        llm_calls = _perf.get_llm_call_count()

        # Feed outcome into ConfidenceScorer + CircuitBreaker
        self._record_outcome(name, success)
        # Feed into per-tool P50/P95/P99 + outcome metrics (perf audit)
        _perf.record_tool_outcome(
            name, duration_ms, success,
            retried=attempts > 1, timed_out=timed_out, llm_calls=llm_calls,
        )

        return ExecutionResult(
            tool=name,
            result=result_str,
            success=success,
            retried=attempts > 1,
            verification_detail=result_str if not success else "",
            duration_ms=round(duration_ms, 1),
            attempts=attempts,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _record_outcome(name: str, success: bool) -> None:
        """Feed outcome into ConfidenceScorer and CircuitBreaker (both non-fatal)."""
        try:
            from core.confidence import confidence_scorer
            confidence_scorer.record_outcome(name, success)
        except Exception:
            pass
        try:
            from core.capability_manager import circuit_breaker
            if success:
                circuit_breaker.record_success(name)
            else:
                circuit_breaker.record_failure(name)
        except Exception:
            pass


# Process-wide singleton
exec_queue = ExecutionQueue()


__all__ = [
    "ToolVerifier",
    "ExecutionResult",
    "ExecutionQueue",
    "exec_queue",
]
