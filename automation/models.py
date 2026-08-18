"""
automation/models.py — shared data model for the automation engine.

Kept dependency-free (dataclasses + typing only) so both the registry
and every provider can import it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ExecutionMethod(str, Enum):
    """Priority ladder from the spec — fastest/most reliable first.
    Providers self-report which rung they used so the engine/telemetry
    can see whether a slow fallback path is being hit too often."""
    NATIVE_API = "native_api"
    WINRT = "winrt"
    POWERSHELL = "powershell"
    CLI = "cli"
    ACCESSIBILITY = "accessibility"
    UI_AUTOMATION = "ui_automation"
    COMPUTER_VISION = "computer_vision"
    OCR = "ocr"
    INPUT_SIMULATION = "input_simulation"


@dataclass
class ActionResult:
    """Return value of every capability action."""
    ok: bool
    message: str
    method: ExecutionMethod = ExecutionMethod.NATIVE_API
    data: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0


# A capability action: takes free-form kwargs, returns ActionResult.
ActionFn = Callable[..., ActionResult]
# Optional verifier: takes the same kwargs + the ActionResult, returns (ok, detail).
VerifyFn = Callable[..., Tuple[bool, str]]


@dataclass
class Capability:
    """One registered unit of automation (e.g. 'window.move').

    name:        dotted id, "<module>.<action>" e.g. "window.snap"
    run:         the callable that performs the action
    verify:      optional post-condition check
    cost:        relative execution cost, 0 (near-free) .. 10 (slow/heavy)
    speed_ms:    rough expected latency, used by the planner to order steps
    permissions: e.g. {"filesystem", "process", "network"} — informational,
                 lets a future confirmation layer flag risky steps
    description: human-readable, used for keyword matching in goal parsing
    keywords:    extra trigger words for the naive goal→capability matcher
    """
    name: str
    run: ActionFn
    verify: Optional[VerifyFn] = None
    cost: int = 1
    speed_ms: int = 50
    permissions: Tuple[str, ...] = ()
    description: str = ""
    keywords: Tuple[str, ...] = ()
    destructive: bool = False
    # True for anything Gama's existing confirmation-code gate already
    # covers (shutdown/restart/sleep/hibernate/lock, permanent delete).
    # The engine checks this BEFORE running the capability — see
    # automation/engine.py::run(). Keeps this package from becoming a
    # side-door around actions/confirmation.py's DESTRUCTIVE_ACTIONS gate.


@dataclass
class PlanStepSpec:
    """One resolved step, ready to be handed to core.planner.PlanStep."""
    description: str
    capability: str
    kwargs: Dict[str, Any] = field(default_factory=dict)
    critical: bool = True
    retryable: bool = True


@dataclass
class AutomationPlan:
    goal: str
    steps: List[PlanStepSpec] = field(default_factory=list)
