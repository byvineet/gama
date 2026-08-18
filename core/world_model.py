"""
core/world_model.py — JARVIS World Model (Central Brain)
=========================================================
Phase 2 of the JARVIS intelligence architecture.

The World Model is the single source of truth for the entire assistant.
Every subsystem reads from and writes to this model instead of maintaining
isolated state. It represents the current reality of:

  • User     — preferences, habits, focus, active goal, current intent
  • Computer — running apps, active window, clipboard, files, system stats
  • Tasks    — active, completed, progress, dependencies, confidence
  • Conversation — topic, references, pending questions

Design principles:
  - Event-driven: writes publish events to the EventBus
  - Incremental: only update what changed
  - Confidence-scored: every field carries a 0.0–1.0 confidence
  - TTL-aware: temporary state auto-expires
  - Thread-safe: RLock throughout
  - Zero-latency reads (dict copy)
  - Unified API: get/set/update/snapshot

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Field wrapper — carries confidence + expiry alongside the value
# ---------------------------------------------------------------------------

@dataclass
class WorldField:
    """A single field in the World Model with metadata."""
    value: Any
    confidence: float = 1.0          # 0.0 (unknown) → 1.0 (certain)
    source: str = ""                  # which module wrote this
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None  # None = never expires

    def is_alive(self) -> bool:
        if self.expires_at is None:
            return True
        return time.time() < self.expires_at

    def age(self) -> float:
        return time.time() - self.updated_at


# ---------------------------------------------------------------------------
# Section snapshots — plain dataclasses for fast read-only access
# ---------------------------------------------------------------------------

@dataclass
class UserState:
    name: str = "Vineet"
    preferences: Dict[str, Any] = field(default_factory=dict)
    habits: Dict[str, Any] = field(default_factory=dict)
    active_goal: Optional[str] = None
    focus_app: Optional[str] = None
    current_intent: Optional[str] = None
    trust_level: str = "trusted"           # trusted / guest / unverified
    session_mood: str = "neutral"


@dataclass
class ComputerState:
    active_app: Optional[str] = None
    active_window_title: Optional[str] = None
    clipboard: Optional[str] = None
    selected_files: List[str] = field(default_factory=list)
    current_folder: Optional[str] = None
    browser_url: Optional[str] = None
    browser_tab: Optional[str] = None
    media_playing: Optional[str] = None
    audio_device: Optional[str] = None
    cpu_percent: Optional[float] = None
    ram_percent: Optional[float] = None
    battery_percent: Optional[int] = None
    battery_plugged: Optional[bool] = None
    network_online: bool = True
    running_apps: List[str] = field(default_factory=list)
    session_mode: str = "idle"


@dataclass
class TaskEntry:
    task_id: str
    description: str
    status: str = "pending"       # pending / running / completed / failed
    progress: float = 0.0         # 0.0 – 1.0
    confidence: float = 1.0
    dependencies: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


@dataclass
class ConversationState:
    topic: Optional[str] = None
    references: Dict[str, Any] = field(default_factory=dict)  # "it", "that file", etc.
    pending_questions: List[str] = field(default_factory=list)
    last_user_message: Optional[str] = None
    last_assistant_message: Optional[str] = None
    turn_count: int = 0
    # Added for the Conversation Context Manager layer
    # (core/conversation_context.py) — spec section 3 asks for
    # "current task / previous task / previous command / pending
    # operations" explicitly. current task lives on TaskEntry via
    # active_tasks(); these two track what came immediately before it.
    current_task_id: Optional[str] = None
    previous_task_id: Optional[str] = None
    previous_command: Optional[str] = None
    pending_operations: List[str] = field(default_factory=list)


@dataclass
class WorldSnapshot:
    """Immutable snapshot of the World Model at a point in time."""
    user: UserState
    computer: ComputerState
    tasks: Dict[str, TaskEntry]
    conversation: ConversationState
    timestamp: float = field(default_factory=time.time)

    def as_prompt_block(self) -> str:
        """Compact sensory + thread context for ACTIVE turns only.

        Inject useful environment (window, clipboard, file, browser, tasks).
        Never include CPU/GPU/RAM clutter. Never narrate this block aloud
        unless the user asks what you see / what you're doing.
        """
        lines = ["[WORLD — sensory & thread; do not narrate unless asked]"]
        u, c, cv = self.user, self.computer, self.conversation

        if u.active_goal:
            lines.append(f"  goal: {u.active_goal}")
        if u.current_intent:
            lines.append(f"  intent: {u.current_intent}")

        # Sensory layer — what Gama can "see"
        if c.active_app or c.active_window_title:
            app = c.active_app or ""
            win = c.active_window_title or ""
            lines.append(f"  screen: {app} — {win}".strip(" —"))
        if c.clipboard:
            clip = (c.clipboard[:100] + "…") if len(c.clipboard) > 100 else c.clipboard
            lines.append(f"  clipboard: {clip}")
        if c.current_folder:
            lines.append(f"  folder: {c.current_folder}")
        if c.browser_tab or c.browser_url:
            lines.append(f"  browser: {c.browser_tab or c.browser_url}")
        if c.media_playing:
            lines.append(f"  media: {c.media_playing}")
        if c.session_mode and c.session_mode != "idle":
            lines.append(f"  mode: {c.session_mode}")

        # Thread / tasks
        active_tasks = [t for t in self.tasks.values() if t.status == "running"]
        if active_tasks:
            for t in active_tasks[:3]:
                lines.append(f"  task: {t.description} ({t.progress:.0%})")
        if cv.previous_command:
            lines.append(f"  previous_command: {cv.previous_command}")
        if cv.pending_operations:
            lines.append(f"  pending: {', '.join(cv.pending_operations[:3])}")
        if cv.topic:
            lines.append(f"  topic: {cv.topic}")
        if cv.references:
            for k, v in list(cv.references.items())[:3]:
                lines.append(f"  ref[{k}]: {v}")

        if len(lines) == 1:
            return ""
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# World Model — the central brain
# ---------------------------------------------------------------------------

class WorldModel:
    """
    Process-wide singleton. Thread-safe, event-driven, zero-latency reads.

    Usage::

        from core.world_model import world

        # Read
        snap = world.snapshot()
        app  = world.get("computer.active_app")

        # Write
        world.set("computer.active_app", "chrome.exe", confidence=0.95, source="desktop_tracker")
        world.update_computer(active_app="chrome.exe", cpu_percent=42.1)

        # Tasks
        world.add_task("t1", "Open VS Code")
        world.update_task("t1", status="running", progress=0.5)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # Flat key→WorldField store for fine-grained access
        self._fields: Dict[str, WorldField] = {}

        # Structured sections (kept in sync via update_* helpers)
        self._user = UserState()
        self._computer = ComputerState()
        self._tasks: Dict[str, TaskEntry] = {}
        self._conversation = ConversationState()

        # Timeline of recent events (last 200)
        self._timeline: List[Dict[str, Any]] = []
        self._timeline_lock = threading.Lock()

        # Lazy EventBus import to avoid circular imports
        self._bus = None

    # ── EventBus ────────────────────────────────────────────────────────────

    def _emit(self, event_name: str, **data: Any) -> None:
        try:
            if self._bus is None:
                from state_engine.event_bus import event_bus
                self._bus = event_bus
            self._bus.publish(event_name, **data)
        except Exception:
            pass  # EventBus failure must never crash a caller

    # ── Timeline ────────────────────────────────────────────────────────────

    def _record(self, key: str, value: Any, source: str) -> None:
        entry = {"ts": time.time(), "key": key, "value": value, "source": source}
        with self._timeline_lock:
            self._timeline.append(entry)
            if len(self._timeline) > 200:
                self._timeline = self._timeline[-200:]

    def recent_events(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._timeline_lock:
            return list(self._timeline[-n:])

    # ── Low-level set/get ────────────────────────────────────────────────────

    def set(
        self,
        key: str,
        value: Any,
        confidence: float = 1.0,
        source: str = "",
        ttl: Optional[float] = None,
    ) -> None:
        """Set a named field. key uses dot notation: 'computer.active_app'."""
        expires_at = (time.time() + ttl) if ttl is not None else None
        with self._lock:
            self._fields[key] = WorldField(
                value=value,
                confidence=confidence,
                source=source,
                expires_at=expires_at,
            )
        self._record(key, value, source)
        self._emit("WorldModelUpdated", key=key, value=value, source=source)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a field value, returning default if missing or expired."""
        with self._lock:
            f = self._fields.get(key)
        if f is None or not f.is_alive():
            return default
        return f.value

    def get_field(self, key: str) -> Optional[WorldField]:
        """Get the full WorldField (value + metadata)."""
        with self._lock:
            f = self._fields.get(key)
        if f is None or not f.is_alive():
            return None
        return f

    def expire_old(self) -> None:
        """Remove expired TTL fields. Call periodically."""
        with self._lock:
            expired = [k for k, f in self._fields.items() if not f.is_alive()]
            for k in expired:
                del self._fields[k]

    # ── Structured update helpers ────────────────────────────────────────────

    def update_user(self, **kwargs: Any) -> None:
        """Update UserState fields by keyword. e.g. update_user(active_goal='play music')."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._user, k):
                    setattr(self._user, k, v)
                    self._fields[f"user.{k}"] = WorldField(value=v, source="user_update")
        if kwargs:
            self._emit("WorldModelUpdated", section="user", fields=list(kwargs.keys()))

    def update_computer(self, **kwargs: Any) -> None:
        """Update ComputerState fields by keyword."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._computer, k):
                    setattr(self._computer, k, v)
                    self._fields[f"computer.{k}"] = WorldField(value=v, source="computer_update")
        if kwargs:
            self._emit("WorldModelUpdated", section="computer", fields=list(kwargs.keys()))

    def update_conversation(self, **kwargs: Any) -> None:
        """Update ConversationState fields by keyword."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._conversation, k):
                    setattr(self._conversation, k, v)
                    self._fields[f"conv.{k}"] = WorldField(value=v, source="conv_update")
            if "turn_count" not in kwargs:
                self._conversation.turn_count += 1
                self._fields["conv.turn_count"] = WorldField(
                    value=self._conversation.turn_count, source="conv_update"
                )
        if kwargs:
            self._emit("WorldModelUpdated", section="conversation", fields=list(kwargs.keys()))

    def set_current_task(self, task_id: Optional[str]) -> None:
        """Rotate current_task_id -> previous_task_id and set the new
        current one. Pass None to just clear the current slot (task
        finished/abandoned) while still remembering it as 'previous'."""
        with self._lock:
            if self._conversation.current_task_id != task_id:
                self._conversation.previous_task_id = self._conversation.current_task_id
            self._conversation.current_task_id = task_id
        self._emit("WorldModelUpdated", section="conversation", fields=["current_task_id"])

    def set_previous_command(self, command: str) -> None:
        with self._lock:
            self._conversation.previous_command = command
        self._emit("WorldModelUpdated", section="conversation", fields=["previous_command"])

    def add_pending_operation(self, op: str) -> None:
        with self._lock:
            if op not in self._conversation.pending_operations:
                self._conversation.pending_operations.append(op)
        self._emit("WorldModelUpdated", section="conversation", fields=["pending_operations"])

    def clear_pending_operation(self, op: str) -> None:
        with self._lock:
            if op in self._conversation.pending_operations:
                self._conversation.pending_operations.remove(op)
        self._emit("WorldModelUpdated", section="conversation", fields=["pending_operations"])

    def set_reference(self, ref_key: str, value: Any, ttl: float = 300.0) -> None:
        """Store a conversation reference ('it', 'that file', etc.) with TTL."""
        with self._lock:
            self._conversation.references[ref_key] = value
        self.set(f"conv.ref.{ref_key}", value, ttl=ttl, source="reference")

    def resolve_reference(self, ref_key: str) -> Optional[Any]:
        """Resolve a pronoun / reference ('it', 'this file', etc.)."""
        with self._lock:
            return self._conversation.references.get(ref_key)

    # ── Task management ──────────────────────────────────────────────────────

    def add_task(
        self,
        task_id: str,
        description: str,
        confidence: float = 1.0,
        dependencies: Optional[List[str]] = None,
    ) -> TaskEntry:
        entry = TaskEntry(
            task_id=task_id,
            description=description,
            confidence=confidence,
            dependencies=dependencies or [],
        )
        with self._lock:
            self._tasks[task_id] = entry
        self._emit("TaskAdded", task_id=task_id, description=description)
        return entry

    def update_task(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            for k, v in kwargs.items():
                if hasattr(t, k):
                    setattr(t, k, v)
            if kwargs.get("status") == "completed":
                t.completed_at = time.time()
        self._emit("TaskUpdated", task_id=task_id, fields=list(kwargs.keys()))

    def get_task(self, task_id: str) -> Optional[TaskEntry]:
        with self._lock:
            return self._tasks.get(task_id)

    def active_tasks(self) -> List[TaskEntry]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status in ("pending", "running")]

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self) -> WorldSnapshot:
        """Return an immutable snapshot of the current world state."""
        with self._lock:
            import copy
            return WorldSnapshot(
                user=copy.copy(self._user),
                computer=copy.copy(self._computer),
                tasks=dict(self._tasks),
                conversation=copy.copy(self._conversation),
            )

    # ── Feed from context snapshot ───────────────────────────────────────────

    def sync_from_context(self) -> None:
        """
        Pull the latest ContextSnapshot into the World Model.
        Call from the context engine's refresh cycle.
        """
        try:
            from context_engine.context_snapshot import get_snapshot
            cs = get_snapshot()
            self.update_computer(
                active_app=cs.active_app,
                active_window_title=cs.active_window_title,
                browser_tab=cs.browser_tab,
                current_folder=cs.current_folder,
                media_playing=cs.media_playing,
                network_online=cs.network_online,
                cpu_percent=cs.cpu_percent,
                ram_percent=cs.ram_percent,
                battery_percent=cs.battery_percent,
                battery_plugged=cs.battery_plugged,
                clipboard=cs.clipboard,
                session_mode=cs.session_mode,
            )
        except Exception:
            pass

    # ── Context block for LLM ────────────────────────────────────────────────

    def as_prompt_block(self) -> str:
        return self.snapshot().as_prompt_block()

    def tasks_and_goal_prompt_block(self) -> str:
        """
        Lean prompt block: active tasks + user goal/intent only.

        Deliberately omits computer stats (active app, clipboard, CPU/RAM,
        battery, etc.) and conversation references — those are already
        injected into the Gemini prompt via actions.desktop_context
        .summarize_for_prompt() and context_engine.working_memory, which
        are the existing single sources for that data. Duplicating them
        here would bloat the prompt and risk showing two slightly-out-
        of-sync readings of the same field. This block only carries what
        nothing else currently surfaces: goal/intent + active task
        progress from the World Model's task tracker.
        """
        snap = self.snapshot()
        u = snap.user
        active = [t for t in snap.tasks.values() if t.status in ("pending", "running")]
        if not u.active_goal and not u.current_intent and not active:
            return ""
        lines = ["[ACTIVE GOAL / TASKS]"]
        if u.active_goal:
            lines.append(f"  goal: {u.active_goal}")
        if u.current_intent:
            lines.append(f"  intent: {u.current_intent}")
        for t in active:
            lines.append(f"  task[{t.task_id}]: {t.description} — {t.status} ({t.progress:.0%})")
        return "\n".join(lines)


# Process-wide singleton
world = WorldModel()

__all__ = [
    "WorldModel", "WorldField", "WorldSnapshot",
    "UserState", "ComputerState", "TaskEntry", "ConversationState",
    "world",
]
