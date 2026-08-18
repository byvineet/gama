"""
core/jarvis_bootstrap.py — JARVIS Architecture Bootstrap
=========================================================
Single entry point that initializes all 7 phases of the JARVIS
intelligence architecture. Call ``bootstrap_jarvis()`` once at
startup (after logging is configured, before the main assistant loop).

Integrates with the existing GamaAssistant startup without modifying
main.py's internal logic — just add one call near the top of
GamaAssistant.__init__ or run():

    from core.jarvis_bootstrap import bootstrap_jarvis
    bootstrap_jarvis()

Each phase is initialized independently and gracefully degrades if
a module fails to import (e.g. on non-Windows systems during dev).

Phase summary:
  1. Reliability     — HealthMonitor + ConfidenceScorer
  2. World Model     — WorldModel singleton wired to EventBus
  3. Context Engine  — ContextAwarenessEngine started (refreshes World Model)
  4. Goal Planner    — GoalPlanner ready (wraps existing Planner)
  5. Layered Memory  — Five-layer memory initialized
  6. Workflow Learner — Passive learning, session end hook registered
  7. Proactive Engine — Background suggestions started

Author : Vineet Machchal
"""

from __future__ import annotations

import atexit
import time
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

_bootstrapped = False


def bootstrap_jarvis(
    *,
    enable_health_monitor: bool = True,
    enable_context_awareness: bool = True,
    enable_proactive_engine: bool = True,
    enable_workflow_learner: bool = True,
    health_poll_interval: float = 30.0,
    context_refresh_interval: float = 4.0,
    proactive_poll_interval: float = 12.0,
) -> bool:
    """
    Initialize the full JARVIS intelligence stack.

    Returns True if bootstrap succeeded (even partially), False on total failure.
    Call once at GamaAssistant startup.

    Args:
        enable_health_monitor:     Phase 1 — module health monitoring
        enable_context_awareness:  Phase 3 — context sensing + World Model sync
        enable_proactive_engine:   Phase 7 — proactive suggestions
        enable_workflow_learner:   Phase 6 — passive workflow learning
        health_poll_interval:      seconds between health checks
        context_refresh_interval:  seconds between context refreshes
        proactive_poll_interval:   seconds between proactive rule evaluations
    """
    global _bootstrapped
    if _bootstrapped:
        log.debug("[JARVIS] Already bootstrapped — skipping.")
        return True

    t0 = time.perf_counter()
    log.info("[JARVIS] Bootstrapping intelligence architecture…")
    ok_count = 0

    # ── Phase 1: Reliability — Health Monitor ────────────────────────────────
    if enable_health_monitor:
        try:
            from core.health_monitor import health_monitor
            _register_default_health_probes(health_monitor, health_poll_interval)
            health_monitor.start()
            log.info("[JARVIS] ✓ Phase 1 — Health Monitor started.")
            ok_count += 1
        except Exception as exc:
            log.warning(f"[JARVIS] Phase 1 (Health Monitor) failed: {exc}")

    # ── Phase 2: World Model ─────────────────────────────────────────────────
    try:
        from core.world_model import world
        # Seed basic user identity from config
        try:
            from core.config_manager import config as _cfg
            user_name = _cfg.user_name
            world.update_user(name=user_name, trust_level="trusted")
        except Exception:
            world.update_user(name="Vineet", trust_level="trusted")
        log.info("[JARVIS] ✓ Phase 2 — World Model ready.")
        ok_count += 1
    except Exception as exc:
        log.warning(f"[JARVIS] Phase 2 (World Model) failed: {exc}")

    # ── Phase 3: Context Awareness Engine ───────────────────────────────────
    if enable_context_awareness:
        try:
            from context_engine.context_awareness import context_awareness
            context_awareness._interval = context_refresh_interval
            context_awareness.start()
            log.info("[JARVIS] ✓ Phase 3 — Context Awareness Engine started.")
            ok_count += 1
        except Exception as exc:
            log.warning(f"[JARVIS] Phase 3 (Context Awareness) failed: {exc}")


    # ── Phase 5: Layered Memory ──────────────────────────────────────────────
    try:
        from memory.layered_memory import layered_memory
        # Sync graph preferences into World Model
        layered_memory.sync_to_world()
        log.info("[JARVIS] ✓ Phase 5 — Layered Memory ready.")
        ok_count += 1
    except Exception as exc:
        log.warning(f"[JARVIS] Phase 5 (Layered Memory) failed: {exc}")

    # ── Phase 6: Workflow Learner ────────────────────────────────────────────
    if enable_workflow_learner:
        try:
            from learning.workflow_learner import workflow_learner
            # Register session-end hook so patterns persist when GAMA closes
            atexit.register(workflow_learner.end_session)
            log.info("[JARVIS] ✓ Phase 6 — Workflow Learner active.")
            ok_count += 1
        except Exception as exc:
            log.warning(f"[JARVIS] Phase 6 (Workflow Learner) failed: {exc}")

    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        f"[JARVIS] Bootstrap complete: {ok_count}/6 phases active in {elapsed_ms:.0f}ms."
    )
    _bootstrapped = ok_count > 0
    return _bootstrapped


# ---------------------------------------------------------------------------
# Default health probes
# ---------------------------------------------------------------------------

def _register_default_health_probes(health_monitor, poll_interval: float) -> None:
    """Register lightweight probes for each critical subsystem."""

    def _probe_memory():
        from memory.layered_memory import layered_memory  # noqa
        return True

    def _probe_context():
        from context_engine.context_snapshot import get_snapshot
        snap = get_snapshot()
        return snap is not None

    def _probe_state_engine():
        from state_engine.event_bus import event_bus  # noqa
        return True

    def _probe_world_model():
        from core.world_model import world
        snap = world.snapshot()
        return snap is not None

    registrations = [
        ("memory",       _probe_memory,       None,  poll_interval),
        ("context",      _probe_context,       None,  poll_interval),
        ("state_engine", _probe_state_engine,  None,  poll_interval * 2),
        ("world_model",  _probe_world_model,   None,  poll_interval),
    ]

    # Voice engine probe (best-effort — may not exist on non-Windows)
    try:
        def _probe_voice():
            from voice import tts_engine
            worker = getattr(tts_engine, "_worker", None)
            # The worker thread is only spawned lazily on the first
            # speak_exact() call — module imported but idle (no thread
            # yet) is healthy, not a failure.
            return worker is None or worker.is_alive()

        registrations.append(("tts", _probe_voice, None, poll_interval))
    except Exception:
        pass

    for name, probe, restart_fn, interval in registrations:
        health_monitor.register(
            name=name,
            probe=probe,
            restart_fn=restart_fn,
            poll_interval=interval,
        )


# ---------------------------------------------------------------------------
# Proactive suggestion delivery
# ---------------------------------------------------------------------------

def _speak_suggestion(suggestion) -> None:
    """Deliver a proactive suggestion via GAMA's voice output (best-effort)."""
    try:
        # Use the state engine event bus so the UI can show a toast
        # The main loop will pick it up via the existing notification system
        from state_engine.event_bus import event_bus
        event_bus.publish(
            "GamaNotification",
            text=suggestion.message,
            priority=suggestion.priority,
            rule_id=suggestion.rule_id,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Convenience helpers for main.py integration
# ---------------------------------------------------------------------------

def get_world_context_block() -> str:
    """
    Return a compact World Model prompt block for injection into LLM context.
    Safe to call at any time — returns "" if World Model isn't ready.
    """
    try:
        from core.world_model import world
        return world.as_prompt_block()
    except Exception:
        return ""


def resolve_command_context(command: str) -> dict:
    """
    Resolve vague references in a command and return enriched context.
    Safe to call at any time — returns {} if not ready.
    """
    try:
        from context_engine.context_awareness import context_awareness
        return context_awareness.get_context_for_command(command)
    except Exception:
        return {}


def score_and_check(action: str, intent_clarity: float = 0.9) -> tuple:
    """
    Score an action and return (should_execute: bool, score: ConfidenceScore).
    Safe to call at any time.
    """
    try:
        from core.confidence import score_action
        from core.world_model import world
        snap = world.snapshot()
        trust = snap.user.trust_level
        cs = score_action(action, intent_clarity=intent_clarity, trust_level=trust)
        return cs.should_execute, cs
    except Exception:
        return True, None  # fail-open: execute if confidence scoring fails


def record_action_outcome(action: str, success: bool) -> None:
    """Record the outcome of an executed action for adaptive confidence scoring."""
    try:
        from core.confidence import confidence_scorer
        confidence_scorer.record_outcome(action, success)
    except Exception:
        pass


def record_workflow_action(action: str) -> None:
    """Record an action for workflow learning."""
    try:
        from learning.workflow_learner import workflow_learner
        workflow_learner.record(action)
    except Exception:
        pass


__all__ = [
    "bootstrap_jarvis",
    "get_world_context_block",
    "resolve_command_context",
    "score_and_check",
    "record_action_outcome",
    "record_workflow_action",
]
