"""
context_engine — glue between the three subsystems the spec calls out:

  1. Desktop Context Engine  (actions/desktop_context.py — unchanged core,
     now also publishes diff-based events + registers as a background task)
  2. Vision Engine           (actions/screen_processor.py — unchanged core,
     now reports ActivityState + publishes ScreenshotCaptured/VisionCompleted)
  3. Reasoning Engine        (this package's reasoning.py)

Why not rewrite desktop_context.py / screen_processor.py wholesale?
Both already implement the spec's core design goals correctly:
  - Desktop Context Engine: cheap local OS polling + caching, no
    screenshots/OCR/continuous AI, diff-friendly snapshot dict.
  - Vision Engine: capture -> analyze -> return -> release, activates
    only on an explicit tool call, never retains images to disk.
This package adds the missing piece — Event Bus publishing and
State Manager reporting — without touching their working internals.

`publish_context_event` is the one shared helper both engines use so
event names/shapes stay consistent; it degrades to a no-op if
state_engine isn't importable (e.g. a unit test importing these
modules in isolation) so neither engine ever depends hard on it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def publish_context_event(event_name: str, **data) -> None:
    """Best-effort publish onto the shared state_engine Event Bus.
    Desktop Context / Vision / Meeting Watch / System Monitor all call
    this instead of importing state_engine directly at module scope,
    so none of them hard-fail if state_engine isn't present."""
    try:
        from state_engine import state
        state.emit(event_name, **data)
    except Exception:
        logger.debug(f"context_engine: publish_context_event('{event_name}') skipped", exc_info=True)


def set_activity(activity_name: str) -> None:
    """activity_name is the .name of a state_engine.ActivityState member,
    e.g. 'ANALYZING_SCREEN'. String-based so callers don't need a hard
    import of ActivityState at module scope."""
    try:
        from state_engine import state, ActivityState
        state.set_activity(ActivityState[activity_name])
    except Exception:
        logger.debug(f"context_engine: set_activity('{activity_name}') skipped", exc_info=True)


def register_background_task(task_id: str, name: str, detail: str = "") -> None:
    try:
        from state_engine import state
        state.tasks.start(task_id, name, detail)
    except Exception:
        logger.debug(f"context_engine: register_background_task('{task_id}') skipped", exc_info=True)


def update_background_task(task_id: str, detail: str = "") -> None:
    try:
        from state_engine import state
        state.tasks.update(task_id, detail=detail)
    except Exception:
        pass


def complete_background_task(task_id: str, ok: bool = True, detail: str = "") -> None:
    try:
        from state_engine import state
        state.tasks.complete(task_id, ok=ok, detail=detail)
    except Exception:
        pass


from .reasoning import ReasoningEngine, get_reasoning_engine, fuse_context  # noqa: E402
from .working_memory import WorkingMemory, working_memory, SLOT_NAMES  # noqa: E402
from .context_snapshot import (  # noqa: E402
    ContextSnapshot, get_snapshot, query_context, refresh_snapshot, SESSION_MODES,
)

__all__ = [
    "publish_context_event",
    "set_activity",
    "register_background_task",
    "update_background_task",
    "complete_background_task",
    "ReasoningEngine",
    "get_reasoning_engine",
    "fuse_context",
    "WorkingMemory",
    "working_memory",
    "SLOT_NAMES",
    "ContextSnapshot",
    "get_snapshot",
    "query_context",
    "refresh_snapshot",
    "SESSION_MODES",
]
