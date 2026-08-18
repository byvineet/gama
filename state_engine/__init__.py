"""
state_engine — GAMA's centralized State Engine ("the brain").

    from state_engine import state, PrimaryState, ActivityState, MoodState

    state.set_primary(PrimaryState.LISTENING)
    state.set_activity(ActivityState.SEARCHING_WEB)
    state.set_mood(MoodState.FOCUSED)
    state.emit("WakeWordDetected", latency_sec=0.31)

    with state.activity(ActivityState.WRITING_CODE):
        ...  # ActivityState auto-restored to NONE after, even on exception

See manager.py for the full design notes.
"""

from .background_tasks import BackgroundTask, BackgroundTaskRegistry
from .enums import (
    ACTIVITY_STATUS_TEXT,
    PRIMARY_STATUS_TEXT,
    ActivityState,
    MoodState,
    PrimaryState,
    TaskStatus,
)
from .event_bus import Event, EventBus, event_bus
from .manager import StateManager, StateSnapshot, state
from .timeline import StateTimeline, TimelineEntry

__all__ = [
    "state",
    "StateManager",
    "StateSnapshot",
    "PrimaryState",
    "ActivityState",
    "MoodState",
    "TaskStatus",
    "ACTIVITY_STATUS_TEXT",
    "PRIMARY_STATUS_TEXT",
    "Event",
    "EventBus",
    "event_bus",
    "BackgroundTask",
    "BackgroundTaskRegistry",
    "StateTimeline",
    "TimelineEntry",
]
