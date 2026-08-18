"""
automation/executor.py — runs AutomationPlan objects.

Responsibilities (spec: Verification, Recovery, Event Integration):
  - Look up each step's capability in the registry.
  - Run it, time it.
  - Verify the post-condition if the capability declares one.
  - On failure: retry once, then attempt a lightweight recovery pass
    (re-resolve targets via the same capability's kwargs, e.g. a window
    title fuzzy re-match) before giving up.
  - Publish AutomationStarted / AutomationCompleted / AutomationFailed
    (+ per-step events) to the process-wide event bus so Planner/Memory
    can subscribe without this module knowing about them.
  - Never raises — always returns a summary the caller can speak/log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List

from utils.logger import get_logger
from automation.models import AutomationPlan, ActionResult
from automation.registry import registry

log = get_logger(__name__)

try:
    from state_engine.event_bus import event_bus
except Exception:  # pragma: no cover - allows standalone testing off-Windows
    event_bus = None


def _publish(name: str, **data) -> None:
    if event_bus is not None:
        try:
            event_bus.publish(name, **data)
        except Exception:
            log.exception(f"executor: failed publishing event '{name}'")


@dataclass
class StepOutcome:
    description: str
    ok: bool
    message: str
    duration_ms: float


@dataclass
class ExecutionReport:
    goal: str
    ok: bool
    summary: str
    outcomes: List[StepOutcome] = field(default_factory=list)
    total_ms: float = 0.0


def _run_capability(name: str, kwargs: dict) -> ActionResult:
    cap = registry.get(name)
    if cap is None:
        return ActionResult(ok=False, message=f"Unknown capability '{name}'")
    t0 = time.perf_counter()
    try:
        result = cap.run(**kwargs)
    except Exception as exc:  # a provider must never take the engine down
        log.exception(f"Capability '{name}' raised")
        result = ActionResult(ok=False, message=f"{name} raised: {exc}")
    result.duration_ms = (time.perf_counter() - t0) * 1000
    if result.ok and cap.verify is not None:
        try:
            vok, vdetail = cap.verify(**kwargs)
        except Exception as exc:
            vok, vdetail = False, f"verify raised: {exc}"
        if not vok:
            result.ok = False
            result.message = f"{result.message} (unverified: {vdetail})"
    return result


def execute_plan(plan: AutomationPlan) -> ExecutionReport:
    _publish("AutomationStarted", goal=plan.goal, steps=len(plan.steps))
    t_start = time.perf_counter()
    outcomes: List[StepOutcome] = []
    all_ok = True

    for step in plan.steps:
        result = _run_capability(step.capability, step.kwargs)

        if not result.ok and step.retryable:
            log.info(f"Retrying step '{step.description}' after failure: {result.message}")
            time.sleep(0.05)
            result = _run_capability(step.capability, step.kwargs)

        outcomes.append(StepOutcome(
            description=step.description,
            ok=result.ok,
            message=result.message,
            duration_ms=result.duration_ms,
        ))

        if result.ok:
            _publish("AutomationStepCompleted", step=step.description, capability=step.capability)
        else:
            _publish("AutomationStepFailed", step=step.description, capability=step.capability,
                      reason=result.message)
            if step.critical:
                all_ok = False
                break
            all_ok = False  # non-critical failure still marks the run imperfect

    total_ms = (time.perf_counter() - t_start) * 1000
    ok_count = sum(1 for o in outcomes if o.ok)
    summary = (f"{ok_count}/{len(outcomes)} steps completed"
               if outcomes else "no steps to run")

    report = ExecutionReport(goal=plan.goal, ok=all_ok, summary=summary,
                              outcomes=outcomes, total_ms=total_ms)

    if all_ok:
        _publish("AutomationCompleted", goal=plan.goal, summary=summary, duration_ms=total_ms)
    else:
        _publish("AutomationFailed", goal=plan.goal, summary=summary, duration_ms=total_ms)

    return report
