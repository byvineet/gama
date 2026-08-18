"""
core/session_manager.py — Gemini Live Session Lifecycle Manager
===============================================================
Extracted from GamaAssistant.run() to isolate session reconnection logic.

The old reconnect loop used a flat ``await asyncio.sleep(1.5)`` with no
backoff and no retry limit — this hammers the Gemini endpoint at full speed
on persistent outages and burns through rate-limit quota.

This module provides:

  • ``ReconnectPolicy``  — configurable backoff parameters
  • ``SessionManager``   — tracks attempt count, computes next delay,
                           emits log lines, and enforces a max-delay cap

Usage (in GamaAssistant.run())::

    from core.session_manager import session_manager

    # Inside the reconnect loop, replace:
    #   await asyncio.sleep(1.5)
    # with:
    #   await session_manager.wait_before_reconnect()

    # Reset the attempt counter when a session connects successfully:
    #   session_manager.reset()

Author : Vineet Machchal
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Reconnect policy
# ---------------------------------------------------------------------------

@dataclass
class ReconnectPolicy:
    """
    Exponential-backoff reconnect configuration.

    Delay formula: ``min(max_delay, base_delay * multiplier ** attempt)``
    where ``attempt`` is 0-indexed (first reconnect = attempt 0).

    Defaults mirror the previous flat 1.5s but cap at 30s after ~5 tries:
      attempt 0 →  1.5s
      attempt 1 →  3.0s
      attempt 2 →  6.0s
      attempt 3 → 12.0s
      attempt 4 → 24.0s
      attempt 5+ → 30.0s (capped)
    """
    base_delay: float = 1.5           # seconds for first reconnect
    multiplier: float = 2.0           # growth factor per attempt
    max_delay: float = 30.0           # hard cap (seconds)
    jitter: float = 0.5               # ± random jitter (seconds) to spread load
    max_attempts: Optional[int] = None  # None = unlimited retries


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

class SessionManager:
    """
    Tracks Gemini Live reconnection state and enforces exponential backoff.

    Call ``reset()`` whenever a session connects successfully so the attempt
    counter resets and the next disconnect starts from the base delay again.

    Call ``wait_before_reconnect()`` at the bottom of the reconnect loop
    (replacing the flat ``asyncio.sleep``).
    """

    def __init__(self, policy: Optional[ReconnectPolicy] = None) -> None:
        self._policy = policy or ReconnectPolicy()
        self._attempt: int = 0
        self._last_connect_ts: float = 0.0
        self._last_disconnect_ts: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Call when a session connects successfully to reset backoff."""
        if self._attempt > 0:
            log.info(
                f"[SessionManager] Connection established after "
                f"{self._attempt} attempt(s). Resetting backoff."
            )
        self._attempt = 0
        self._last_connect_ts = time.monotonic()

    def record_disconnect(self) -> None:
        """Call when a session drops to timestamp the disconnect."""
        self._last_disconnect_ts = time.monotonic()

    async def wait_before_reconnect(self) -> bool:
        """
        Wait for the next reconnect delay (with exponential backoff).

        Returns:
            True  — should reconnect now
            False — max_attempts reached; caller should give up
        """
        policy = self._policy

        # Check retry limit
        if policy.max_attempts is not None and self._attempt >= policy.max_attempts:
            log.error(
                f"[SessionManager] Max reconnect attempts "
                f"({policy.max_attempts}) reached — giving up."
            )
            return False

        delay = self._next_delay()
        self._attempt += 1

        log.info(
            f"[SessionManager] Reconnecting in {delay:.1f}s "
            f"(attempt #{self._attempt}"
            + (f" of {policy.max_attempts}" if policy.max_attempts else "")
            + ")…"
        )

        await asyncio.sleep(delay)
        return True

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def is_first_attempt(self) -> bool:
        return self._attempt == 0

    def status(self) -> dict:
        return {
            "attempt": self._attempt,
            "last_connect": self._last_connect_ts,
            "last_disconnect": self._last_disconnect_ts,
            "next_delay_s": self._next_delay(),
            "max_attempts": self._policy.max_attempts,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _next_delay(self) -> float:
        """Compute backoff delay for the current attempt."""
        import random
        p = self._policy
        raw = p.base_delay * (p.multiplier ** self._attempt)
        capped = min(raw, p.max_delay)
        jitter = random.uniform(-p.jitter, p.jitter) if p.jitter > 0 else 0.0
        return max(0.1, capped + jitter)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

session_manager = SessionManager()


__all__ = [
    "ReconnectPolicy",
    "SessionManager",
    "session_manager",
]
