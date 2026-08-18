"""
state_engine/enums.py — the vocabulary of GAMA's State Engine.

Primary states are mutually exclusive ("what mode is GAMA in").
Activity states describe what's happening *within* a primary state and
may change frequently. Mood is purely cosmetic (orb/UI personality) and
must never be read by logic — only by rendering code.

Both PrimaryState and ActivityState are plain str Enums so they can be
sent straight over Qt signals, JSON-serialized into the timeline/debug
panel, and compared with `==` against a raw string message from a
websocket without extra plumbing.
"""

from __future__ import annotations

from enum import Enum


class PrimaryState(str, Enum):
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    VERIFYING_VOICE = "VERIFYING_VOICE"
    PROCESSING = "PROCESSING"
    THINKING = "THINKING"
    PLANNING = "PLANNING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    WAITING = "WAITING"
    EXECUTING = "EXECUTING"
    SLEEPING = "SLEEPING"
    MEETING_MODE = "MEETING_MODE"
    ERROR = "ERROR"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class ActivityState(str, Enum):
    NONE = "NONE"

    # System
    INITIALIZING = "INITIALIZING"
    LOADING_MODEL = "LOADING_MODEL"
    CLEANING_MEMORY = "CLEANING_MEMORY"

    # Understanding
    LISTENING_FOR_WAKE_WORD = "LISTENING_FOR_WAKE_WORD"
    TRANSCRIBING_AUDIO = "TRANSCRIBING_AUDIO"
    PARSING_INTENT = "PARSING_INTENT"
    ANALYZING_CONTEXT = "ANALYZING_CONTEXT"
    THINKING = "THINKING"
    PLANNING = "PLANNING"

    # Desktop
    READING_SCREEN = "READING_SCREEN"
    ANALYZING_SCREEN = "ANALYZING_SCREEN"
    MONITORING_DESKTOP = "MONITORING_DESKTOP"
    READING_CLIPBOARD = "READING_CLIPBOARD"

    # Browser
    SEARCHING_WEB = "SEARCHING_WEB"
    READING_WEB_PAGE = "READING_WEB_PAGE"
    SUMMARIZING_WEB = "SUMMARIZING_WEB"
    OPENING_BROWSER = "OPENING_BROWSER"
    CONTROLLING_BROWSER = "CONTROLLING_BROWSER"

    # Files
    OPENING_FILE = "OPENING_FILE"
    WRITING_FILE = "WRITING_FILE"
    COPYING_FILES = "COPYING_FILES"
    MOVING_FILES = "MOVING_FILES"
    DELETING_FILE = "DELETING_FILE"

    # Coding
    ANALYZING_PROJECT = "ANALYZING_PROJECT"
    READING_CODE = "READING_CODE"
    WRITING_CODE = "WRITING_CODE"
    EDITING_CODE = "EDITING_CODE"
    RUNNING_TESTS = "RUNNING_TESTS"
    FIXING_ERRORS = "FIXING_ERRORS"

    # Vision
    ANALYZING_IMAGE = "ANALYZING_IMAGE"
    LOOKING_CAMERA = "LOOKING_CAMERA"
    # Voice
    VERIFYING_SPEAKER = "VERIFYING_SPEAKER"
    RECORDING_AUDIO = "RECORDING_AUDIO"

    # Memory
    STORING_MEMORY = "STORING_MEMORY"
    RECALLING_MEMORY = "RECALLING_MEMORY"
    LEARNING = "LEARNING"

    # General
    EXECUTING_COMMAND = "EXECUTING_COMMAND"
    WAITING_FOR_APPLICATION = "WAITING_FOR_APPLICATION"
    DOWNLOADING = "DOWNLOADING"
    UPDATING = "UPDATING"


class MoodState(str, Enum):
    """Cosmetic only — orb color/glow/pulse/particles. Never branch
    application logic on mood; if you need that, it belongs in
    PrimaryState or ActivityState instead."""
    CALM = "CALM"
    NORMAL = "NORMAL"
    FOCUSED = "FOCUSED"
    THINKING = "THINKING"
    HAPPY = "HAPPY"
    EXCITED = "EXCITED"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    SECURITY = "SECURITY"
    ERROR = "ERROR"


class TaskStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # Added for core/task_queue.py's richer Task Queue (queued-but-not-started,
    # and user/system-cancelled). Additive only — BackgroundTaskRegistry's
    # existing RUNNING/PAUSED/COMPLETED/FAILED usages are unaffected.
    QUEUED = "QUEUED"
    CANCELLED = "CANCELLED"


# Human-readable status text shown in the UI, keyed by ActivityState
# (falls back to PrimaryState text when activity is NONE). Centralized
# here so copy changes never require touching emitting code.
ACTIVITY_STATUS_TEXT: dict[ActivityState, str] = {
    ActivityState.NONE: "",
    ActivityState.INITIALIZING: "Starting up...",
    ActivityState.LOADING_MODEL: "Loading model...",
    ActivityState.CLEANING_MEMORY: "Tidying memory...",
    ActivityState.LISTENING_FOR_WAKE_WORD: "Listening for wake word...",
    ActivityState.TRANSCRIBING_AUDIO: "Listening...",
    ActivityState.PARSING_INTENT: "Understanding request...",
    ActivityState.ANALYZING_CONTEXT: "Thinking it through...",
    ActivityState.THINKING: "Thinking...",
    ActivityState.PLANNING: "Planning response...",
    ActivityState.READING_SCREEN: "Reading the screen...",
    ActivityState.ANALYZING_SCREEN: "Analyzing what's on screen...",
    ActivityState.MONITORING_DESKTOP: "Keeping an eye on the desktop...",
    ActivityState.READING_CLIPBOARD: "Reading clipboard...",
    ActivityState.SEARCHING_WEB: "Searching the web...",
    ActivityState.READING_WEB_PAGE: "Reading a page...",
    ActivityState.SUMMARIZING_WEB: "Reading documentation...",
    ActivityState.OPENING_BROWSER: "Opening the browser...",
    ActivityState.CONTROLLING_BROWSER: "Working in the browser...",
    ActivityState.OPENING_FILE: "Opening file...",
    ActivityState.WRITING_FILE: "Writing file...",
    ActivityState.COPYING_FILES: "Copying files...",
    ActivityState.MOVING_FILES: "Moving files...",
    ActivityState.DELETING_FILE: "Deleting file...",
    ActivityState.ANALYZING_PROJECT: "Analyzing project...",
    ActivityState.READING_CODE: "Reading code...",
    ActivityState.WRITING_CODE: "Writing code...",
    ActivityState.EDITING_CODE: "Editing code...",
    ActivityState.RUNNING_TESTS: "Running tests...",
    ActivityState.FIXING_ERRORS: "Fixing errors...",
    ActivityState.ANALYZING_IMAGE: "Analyzing image...",
    ActivityState.LOOKING_CAMERA: "Looking through the camera...",
    ActivityState.VERIFYING_SPEAKER: "Verifying owner...",
    ActivityState.RECORDING_AUDIO: "Recording audio...",
    ActivityState.STORING_MEMORY: "Remembering that...",
    ActivityState.RECALLING_MEMORY: "Recalling...",
    ActivityState.LEARNING: "Learning...",
    ActivityState.EXECUTING_COMMAND: "Launching application...",
    ActivityState.WAITING_FOR_APPLICATION: "Waiting for response...",
    ActivityState.DOWNLOADING: "Downloading...",
    ActivityState.UPDATING: "Updating...",
}

PRIMARY_STATUS_TEXT: dict[PrimaryState, str] = {
    PrimaryState.OFFLINE: "Offline",
    PrimaryState.STARTING: "Starting up...",
    PrimaryState.INITIALIZING: "Initializing...",
    PrimaryState.READY: "Ready",
    PrimaryState.IDLE: "Idle",
    PrimaryState.LISTENING: "Listening...",
    PrimaryState.VERIFYING_VOICE: "Verifying voice...",
    PrimaryState.PROCESSING: "Processing...",
    PrimaryState.THINKING: "Thinking...",
    PrimaryState.PLANNING: "Planning...",
    PrimaryState.SPEAKING: "Speaking...",
    PrimaryState.INTERRUPTED: "Interrupted",
    PrimaryState.WAITING: "Waiting...",
    PrimaryState.EXECUTING: "Executing...",
    PrimaryState.SLEEPING: "Sleeping",
    PrimaryState.MEETING_MODE: "In a meeting",
    PrimaryState.ERROR: "Something went wrong",
    PrimaryState.ERROR_RECOVERY: "Recovering...",
    PrimaryState.SHUTTING_DOWN: "Shutting down...",
}
