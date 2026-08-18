"""
state_engine/manager.py — the State Manager: the single source of
truth for what GAMA is doing right now.

Design
------
Modules never touch the UI directly. Instead they either:

  1. Call state.set_primary(...) / set_activity(...) / set_mood(...)
     directly, or
  2. Publish a semantic event via state.emit("WakeWordDetected", ...)
     and let StateManager's internal event->state translation table
     do the transition for them (see _EVENT_TRANSITIONS below).

The UI (ui.py's GamaUI/GamaWindow) subscribes via state.subscribe(...)
and re-renders only on actual changes (set_* is a no-op if the value
is already current — "avoid unnecessary UI refreshes" from the spec).

Thread-safety
-------------
All mutation happens under a single RLock. Subscriber callbacks are
invoked outside the lock (copied list first) so a slow/misbehaving
subscriber can't deadlock a concurrent state update from another
thread (mic callback thread vs Qt thread vs asyncio loop are all
legitimate callers here).
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .background_tasks import BackgroundTaskRegistry
from .enums import (
    ACTIVITY_STATUS_TEXT,
    PRIMARY_STATUS_TEXT,
    ActivityState,
    MoodState,
    PrimaryState,
)
from .event_bus import Event, event_bus
from .timeline import StateTimeline

log = get_logger(__name__)
logger = log  # back-compat alias
StateListener = Callable[["StateSnapshot"], None]


@dataclass(frozen=True)
class StateSnapshot:
    primary: PrimaryState
    activity: ActivityState
    mood: MoodState
    status_text: str
    timestamp: float = field(default_factory=time.time)


# Events that map straight to a primary-state transition. Anything not
# listed here can still be emitted/observed (e.g. by a debug panel or
# a future plugin) without StateManager forcing a primary change —
# that keeps the event vocabulary open-ended without touching this
# file for every new event a module wants to publish.
_EVENT_TO_PRIMARY: Dict[str, PrimaryState] = {
    "WakeWordDetected": PrimaryState.LISTENING,
    "VoiceVerifyStarted": PrimaryState.VERIFYING_VOICE,
    "VoiceVerifyDone": PrimaryState.THINKING,
    "SpeechStarted": PrimaryState.SPEAKING,
    "SpeechFinished": PrimaryState.READY,
    "BargeInDetected": PrimaryState.INTERRUPTED,
    "BargeInHandled": PrimaryState.LISTENING,
    "ThinkingStarted": PrimaryState.THINKING,
    "PlanningStarted": PrimaryState.PLANNING,
    "PlanningFinished": PrimaryState.EXECUTING,
    "ThinkingFinished": PrimaryState.READY,
    "CommandExecuting": PrimaryState.EXECUTING,
    "CommandCompleted": PrimaryState.READY,
    "SleepEntered": PrimaryState.SLEEPING,
    "SleepExited": PrimaryState.READY,
    "ErrorOccurred": PrimaryState.ERROR,
    "ErrorRecovering": PrimaryState.ERROR_RECOVERY,
    "ErrorRecovered": PrimaryState.READY,
}

_EVENT_TO_ACTIVITY: Dict[str, ActivityState] = {
    "WakeWordDetected": ActivityState.NONE,
    "DownloadStarted": ActivityState.DOWNLOADING,
    "DownloadCompleted": ActivityState.NONE,
    "SpeakerVerified": ActivityState.VERIFYING_SPEAKER,
    "VoiceVerifyStarted": ActivityState.VERIFYING_SPEAKER,
}

# States where tool execution (side-effect actions) must not run.
# These guard deterministic rule: "Do not execute tools while still listening."
_TOOL_BLOCKED_STATES: frozenset[PrimaryState] = frozenset({
    PrimaryState.LISTENING,
    PrimaryState.VERIFYING_VOICE,
    PrimaryState.SLEEPING,
    PrimaryState.OFFLINE,
    PrimaryState.SHUTTING_DOWN,
})


class StateManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._primary = PrimaryState.OFFLINE
        self._activity = ActivityState.NONE
        self._mood = MoodState.NORMAL
        self._listeners: List[StateListener] = []
        self.timeline = StateTimeline()
        self.tasks = BackgroundTaskRegistry()
        self.bus = event_bus
        self.bus.subscribe("*", self._on_event)
        self._debug_enabled = False

        # Simple latency/perf counters for the debug panel.
        self._perf_lock = threading.RLock()
        self._thinking_started_at: Optional[float] = None
        self._thinking_durations: List[float] = []
        self._wake_latencies: List[float] = []
        self._command_durations: List[float] = []

    # -- subscription ----------------------------------------------------
    def subscribe(self, callback: StateListener) -> None:
        with self._lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback: StateListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _notify(self) -> None:
        snap = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(snap)
            except Exception:
                logger.exception("StateManager: a UI/debug listener raised — continuing")

    # -- primary / activity / mood ---------------------------------------
    def set_primary(self, state: PrimaryState, detail: str = "") -> None:
        with self._lock:
            if state == self._primary:
                return
            if state == PrimaryState.THINKING:
                with self._perf_lock:
                    self._thinking_started_at = time.time()
            elif self._primary == PrimaryState.THINKING and self._thinking_started_at:
                with self._perf_lock:
                    self._thinking_durations.append(time.time() - self._thinking_started_at)
                    self._thinking_started_at = None
            self._primary = state
        self.timeline.add("primary", state.value, detail)
        self._notify()

    def set_activity(self, activity: ActivityState, detail: str = "") -> None:
        with self._lock:
            if activity == self._activity:
                return
            self._activity = activity
        self.timeline.add("activity", activity.value, detail)
        self._notify()

    def set_mood(self, mood: MoodState) -> None:
        with self._lock:
            if mood == self._mood:
                return
            self._mood = mood
        self.timeline.add("mood", mood.value)
        self._notify()

    @contextmanager
    def activity(self, activity: ActivityState, detail: str = ""):
        """Convenience for modules: `with state.activity(SEARCHING_WEB):`
        automatically restores ActivityState.NONE afterward, even on
        exception, so callers can't forget to clear it."""
        self.set_activity(activity, detail)
        try:
            yield
        finally:
            self.set_activity(ActivityState.NONE)

    # -- events ------------------------------------------------------------
    def emit(self, event_name: str, **data) -> Event:
        """Publish a semantic event. StateManager translates known event
        names into primary/activity transitions (see _EVENT_TO_PRIMARY /
        _EVENT_TO_ACTIVITY above); unknown event names are still recorded
        on the timeline and broadcast to subscribers (debug panel, future
        plugins) — the vocabulary is intentionally open-ended."""
        return self.bus.publish(event_name, **data)

    def _on_event(self, evt: Event) -> None:
        detail = ", ".join(f"{k}={v}" for k, v in evt.data.items()) if evt.data else ""
        self.timeline.add("event", evt.name, detail)

        if evt.name == "WakeWordDetected":
            latency = evt.data.get("latency_sec")
            if latency is not None:
                with self._perf_lock:
                    self._wake_latencies.append(float(latency))
        if evt.name == "CommandCompleted":
            duration = evt.data.get("duration_sec")
            if duration is not None:
                with self._perf_lock:
                    self._command_durations.append(float(duration))

        primary = _EVENT_TO_PRIMARY.get(evt.name)
        if primary is not None:
            self.set_primary(primary, detail=evt.name)
        activity = _EVENT_TO_ACTIVITY.get(evt.name)
        if activity is not None:
            self.set_activity(activity, detail=evt.name)

    # -- snapshot / status text --------------------------------------------
    def snapshot(self) -> StateSnapshot:
        with self._lock:
            primary, activity, mood = self._primary, self._activity, self._mood
        text = ACTIVITY_STATUS_TEXT.get(activity, "") or PRIMARY_STATUS_TEXT.get(primary, primary.value.title())
        return StateSnapshot(primary=primary, activity=activity, mood=mood, status_text=text)

    @property
    def primary(self) -> PrimaryState:
        with self._lock:
            return self._primary

    @property
    def activity_state(self) -> ActivityState:
        with self._lock:
            return self._activity

    @property
    def mood(self) -> MoodState:
        with self._lock:
            return self._mood

    # -- debug / perf --------------------------------------------------
    def set_debug_enabled(self, enabled: bool) -> None:
        self._debug_enabled = enabled

    @property
    def debug_enabled(self) -> bool:
        return self._debug_enabled

    def perf_summary(self) -> dict:
        with self._perf_lock:
            def _avg(xs: List[float]) -> Optional[float]:
                return round(sum(xs) / len(xs), 3) if xs else None
            return {
                "avg_thinking_time_sec": _avg(self._thinking_durations[-50:]),
                "avg_wake_latency_sec": _avg(self._wake_latencies[-50:]),
                "avg_command_duration_sec": _avg(self._command_durations[-50:]),
                "sample_counts": {
                    "thinking": len(self._thinking_durations),
                    "wake": len(self._wake_latencies),
                    "command": len(self._command_durations),
                },
            }

    def is_tool_blocked(self) -> bool:
        """Return True when the current primary state should block tool
        execution. Enforces the spec rule: 'Do not execute tools while
        still listening.' Callers (main.py's _execute_tool) check this
        before running any side-effect action."""
        return self.primary in _TOOL_BLOCKED_STATES

    def debug_dump(self) -> dict:
        snap = self.snapshot()
        return {
            "primary": snap.primary.value,
            "activity": snap.activity.value,
            "mood": snap.mood.value,
            "status_text": snap.status_text,
            "background_tasks": [t.as_dict() for t in self.tasks.active()],
            "timeline": [str(e) for e in self.timeline.recent(limit=100)],
            "perf": self.perf_summary(),
        }


# Process-wide singleton. Every module (main.py, ui.py, actions/*,
# guard/*) imports this same instance rather than constructing its own
# StateManager, so there is exactly one source of truth per process.
state = StateManager()

__all__ = ["state", "StateManager", "StateSnapshot", "PrimaryState", "ActivityState", "MoodState"]
