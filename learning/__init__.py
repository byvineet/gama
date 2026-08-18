"""
learning/ — Gama's local usage-habit tracking (spec section 2).

Modules:
  habit_tracker.py         — records raw usage events (app focus,
                              downloads, commands) cheaply, batched to
                              disk. Self-subscribes to the shared event
                              bus once habit_tracker.init() runs.
  routine_analyzer.py       — turns raw events into confidence-scored
                              (day-type, hour) routines, distinguishing
                              one-off actions from real recurring
                              habits, decaying stale ones.

NOTE: recommendation_engine.py and workspace_planner.py (the proactive
"you usually have X open — want me to set that up?" suggestion/auto-prep
flow) were removed — they announced these workspace-prep prompts
unprompted, and main.py no longer wires them in. habit_tracker and
routine_analyzer remain as the underlying, passive data layer in case a
future feature wants opt-in access to the collected routines, but
nothing currently reads from them.

Everything is local-only (SQLite under learning/habits.db), no network
calls, no telemetry — matches the memory subsystem's design.
"""

from __future__ import annotations

# Phase 6 JARVIS: passive workflow learning
from learning.workflow_learner import workflow_learner, WorkflowLearner, WorkflowPattern

__all__: list[str] = ["workflow_learner", "WorkflowLearner", "WorkflowPattern"]
