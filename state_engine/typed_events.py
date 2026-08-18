"""
state_engine/typed_events.py — Typed Event Definitions (Phase 3)
==================================================================
Defines all event types used throughout Gama for type safety and documentation.

Events are organized by category:
- User Events: USER_SPEECH, WAKE_DETECTED, etc.
- Session Events: SESSION_STARTED, SESSION_ENDED, etc.
- Task Events: TASK_STARTED, TASK_COMPLETED, etc.
- Speech Events: SPEECH_STARTED, SPEECH_FINISHED, SPEECH_INTERRUPTED
- System Events: ECHO_DETECTED, ERROR_OCCURRED, etc.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    """
    All event types in the system.
    
    Naming convention: NOUN_VERB (past tense)
    Examples: USER_SPEECH_DETECTED, TASK_COMPLETED, SESSION_STARTED
    """
    
    # ── User Events ──────────────────────────────────────────────
    USER_SPEECH_DETECTED = "UserSpeechDetected"
    USER_SPEECH_RECOGNIZED = "UserSpeechRecognized"
    WAKE_WORD_DETECTED = "WakeWordDetected"
    SLEEP_WORD_DETECTED = "SleepWordDetected"
    USER_INTERRUPTED = "UserInterrupted"
    
    # ── Session Events ───────────────────────────────────────────
    SESSION_STARTED = "SessionStarted"
    SESSION_ENDED = "SessionEnded"
    SESSION_TIMEOUT = "SessionTimeout"
    SLEEP_ENTERED = "SleepEntered"
    SLEEP_EXITED = "SleepExited"
    
    # ── Task Events ──────────────────────────────────────────────
    TASK_SUBMITTED = "TaskSubmitted"
    TASK_STARTED = "TaskStarted"
    TASK_PROGRESS_CHANGED = "TaskProgressChanged"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    TASK_PAUSED = "TaskPaused"
    TASK_RESUMED = "TaskResumed"
    
    # ── Speech Events ────────────────────────────────────────────
    SPEECH_QUEUED = "SpeechQueued"
    SPEECH_STARTED = "SpeechStarted"
    SPEECH_COMPLETED = "SpeechCompleted"
    SPEECH_INTERRUPTED = "SpeechInterrupted"
    SPEECH_EXPIRED = "SpeechExpired"
    
    # ── Tool Events ──────────────────────────────────────────────
    TOOL_CALLED = "ToolCalled"
    TOOL_EXECUTED = "ToolExecuted"
    TOOL_FAILED = "ToolFailed"
    
    # ── System Events ────────────────────────────────────────────
    ECHO_DETECTED = "EchoDetected"
    ERROR_OCCURRED = "ErrorOccurred"
    WARNING_ISSUED = "WarningIssued"
    STATE_CHANGED = "StateChanged"
    
    # ── Attention Events (Phase 4) ───────────────────────────────
    ATTENTION_MODE_CHANGED = "AttentionModeChanged"
    PASSIVE_MODE_ENTERED = "PassiveModeEntered"
    ENGAGED_MODE_ENTERED = "EngagedModeEntered"
    TASK_MONITORING_ENTERED = "TaskMonitoringEntered"
    
    # ── Context Events (Phase 7) ─────────────────────────────────
    CONTEXT_UPDATED = "ContextUpdated"
    CONTEXT_BUFFER_FILLED = "ContextBufferFilled"
    CONTEXT_INFERRED = "ContextInferred"


@dataclass
class EventData:
    """Base class for event data"""
    pass


@dataclass
class UserSpeechData(EventData):
    """Data for user speech events"""
    text: str
    confidence: float = 1.0
    speaker: Optional[str] = None
    verified: bool = False
    duration_ms: Optional[float] = None


@dataclass
class TaskEventData(EventData):
    """Data for task events"""
    task_id: str
    task_name: str
    status: Optional[str] = None
    progress: Optional[float] = None
    error: Optional[str] = None
    result: Any = None


@dataclass
class SpeechEventData(EventData):
    """Data for speech events"""
    text: str
    priority: int = 0
    kind: str = "generic"
    duration_ms: Optional[float] = None


@dataclass
class SessionEventData(EventData):
    """Data for session events"""
    session_id: Optional[str] = None
    reason: Optional[str] = None
    duration_s: Optional[float] = None


@dataclass
class AttentionEventData(EventData):
    """Data for attention mode events"""
    mode: str  # "passive", "engaged", "task_monitoring"
    previous_mode: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ErrorEventData(EventData):
    """Data for error events"""
    error_type: str
    message: str
    traceback: Optional[str] = None
    recoverable: bool = True


# Event type to data class mapping
EVENT_DATA_TYPES = {
    EventType.USER_SPEECH_DETECTED: UserSpeechData,
    EventType.USER_SPEECH_RECOGNIZED: UserSpeechData,
    EventType.TASK_STARTED: TaskEventData,
    EventType.TASK_COMPLETED: TaskEventData,
    EventType.TASK_FAILED: TaskEventData,
    EventType.SPEECH_STARTED: SpeechEventData,
    EventType.SPEECH_COMPLETED: SpeechEventData,
    EventType.SESSION_STARTED: SessionEventData,
    EventType.SESSION_ENDED: SessionEventData,
    EventType.ATTENTION_MODE_CHANGED: AttentionEventData,
    EventType.ERROR_OCCURRED: ErrorEventData,
}


# Event priority levels (for filtering/routing)
class EventPriority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


# Event categories for filtering
class EventCategory(Enum):
    USER = "user"
    SESSION = "session"
    TASK = "task"
    SPEECH = "speech"
    TOOL = "tool"
    SYSTEM = "system"
    ATTENTION = "attention"
    CONTEXT = "context"


# Map event types to categories
EVENT_CATEGORIES = {
    EventType.USER_SPEECH_DETECTED: EventCategory.USER,
    EventType.USER_SPEECH_RECOGNIZED: EventCategory.USER,
    EventType.WAKE_WORD_DETECTED: EventCategory.USER,
    EventType.SLEEP_WORD_DETECTED: EventCategory.USER,
    EventType.USER_INTERRUPTED: EventCategory.USER,
    
    EventType.SESSION_STARTED: EventCategory.SESSION,
    EventType.SESSION_ENDED: EventCategory.SESSION,
    EventType.SESSION_TIMEOUT: EventCategory.SESSION,
    EventType.SLEEP_ENTERED: EventCategory.SESSION,
    EventType.SLEEP_EXITED: EventCategory.SESSION,
    
    EventType.TASK_SUBMITTED: EventCategory.TASK,
    EventType.TASK_STARTED: EventCategory.TASK,
    EventType.TASK_PROGRESS_CHANGED: EventCategory.TASK,
    EventType.TASK_COMPLETED: EventCategory.TASK,
    EventType.TASK_FAILED: EventCategory.TASK,
    EventType.TASK_CANCELLED: EventCategory.TASK,
    EventType.TASK_PAUSED: EventCategory.TASK,
    EventType.TASK_RESUMED: EventCategory.TASK,
    
    EventType.SPEECH_QUEUED: EventCategory.SPEECH,
    EventType.SPEECH_STARTED: EventCategory.SPEECH,
    EventType.SPEECH_COMPLETED: EventCategory.SPEECH,
    EventType.SPEECH_INTERRUPTED: EventCategory.SPEECH,
    EventType.SPEECH_EXPIRED: EventCategory.SPEECH,
    
    EventType.TOOL_CALLED: EventCategory.TOOL,
    EventType.TOOL_EXECUTED: EventCategory.TOOL,
    EventType.TOOL_FAILED: EventCategory.TOOL,
    
    EventType.ECHO_DETECTED: EventCategory.SYSTEM,
    EventType.ERROR_OCCURRED: EventCategory.SYSTEM,
    EventType.WARNING_ISSUED: EventCategory.SYSTEM,
    EventType.STATE_CHANGED: EventCategory.SYSTEM,
}


def get_event_category(event_type: EventType) -> EventCategory:
    """Get the category for an event type"""
    return EVENT_CATEGORIES.get(event_type, EventCategory.SYSTEM)


def is_critical_event(event_type: EventType) -> bool:
    """Check if an event type is critical (should never be dropped)"""
    critical_events = {
        EventType.ERROR_OCCURRED,
        EventType.TASK_FAILED,
        EventType.SESSION_ENDED,
        EventType.WAKE_WORD_DETECTED,
    }
    return event_type in critical_events


__all__ = [
    "EventType",
    "EventData",
    "UserSpeechData",
    "TaskEventData",
    "SpeechEventData",
    "SessionEventData",
    "AttentionEventData",
    "ErrorEventData",
    "EventPriority",
    "EventCategory",
    "EVENT_DATA_TYPES",
    "EVENT_CATEGORIES",
    "get_event_category",
    "is_critical_event",
]
