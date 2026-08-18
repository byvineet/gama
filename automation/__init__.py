"""
automation/ — Gama 2.0 Universal Automation Engine
====================================================
Goal-driven Windows automation layer. Does NOT replace core/planner.py
(the deterministic step-runner) — it sits in front of it. This package
turns a natural-language goal into a `core.planner.Plan` by:

    goal → CapabilityRegistry lookup → AutomationEngine.build_plan()
         → core.planner.execute(plan) → verification → events → memory

See automation/engine.py for the entry point (`automation_engine`,
a process-wide singleton) and automation/registry.py for how
capabilities/providers register themselves.

Author: Vineet Machchal
"""

from automation.engine import automation_engine

__all__ = ["automation_engine"]
