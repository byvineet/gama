"""
context_engine/working_memory.py — Working Memory (Gama 2.0 Core
Intelligence Layer, spec section 3 "Working Memory" + section 2's
pronoun-resolution requirement).

Distinct from:
    - memory/memory_manager.py (Short-Term/profile JSON — durable
      preferences, not "what am I doing right now").
    - memory/long_term.py       (Long-Term semantic recall — facts learned
      over time, not the active task's context).

Working Memory answers one question: "what does 'it'/'that'/'them'/'the
previous one' refer to, right now?" It tracks a small set of named slots
(current goal/task/file/project/website/person/folder/app) plus a short
recency stack of concrete entities mentioned or acted on, so a follow-up
like "summarize it" → "email it" → "delete it" resolves naturally without
Gemini needing to ask "summarize what?".

This is intentionally NOT the Blackboard from spec section 9 — the
existing state_engine.StateManager already plays that role for system
state (primary/activity/mood/tasks). Working Memory is scoped to
*conversational* context only, and is fused into the prompt fresh every
turn (see main.py's `_build_config`) rather than through the 5-minute
context cache, since it changes far more often than profile/desktop
context.

Thread-safe (RLock) since tool execution can happen from more than one
thread in principle (fast-intent path vs Gemini tool-call path).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional
from collections import deque

from utils.logger import get_logger

log = get_logger(__name__)

# The slots the spec names explicitly for Working Memory.
SLOT_NAMES = ("goal", "task", "file", "project", "website", "person", "folder", "app", "song")

# Reference words this module can resolve to the most recent matching
# entity. Kept simple/local (no LLM call) per "Zero unnecessary AI calls".
_REFERENCE_WORDS = {"it", "that", "them", "this", "the previous one", "the last one"}

_MAX_RECENCY = 12


@dataclass
class _SlotEntry:
    value: str
    updated_at: float = field(default_factory=time.time)


@dataclass
class _RecencyEntry:
    kind: str
    value: str
    ts: float = field(default_factory=time.time)


class WorkingMemory:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._slots: Dict[str, _SlotEntry] = {}
        self._recency: Deque[_RecencyEntry] = deque(maxlen=_MAX_RECENCY)

    # -- writes --------------------------------------------------------------
    def set_slot(self, name: str, value: str) -> None:
        """Set a named working-memory slot (e.g. 'file', 'project').
        Unknown slot names are still accepted (open vocabulary), but only
        SLOT_NAMES are guaranteed to render in as_prompt_block()'s labels."""
        if not value:
            return
        with self._lock:
            self._slots[name] = _SlotEntry(value=value)
        self.remember_entity(name, value)
        self._publish(name, value)

    def get_slot(self, name: str) -> Optional[str]:
        with self._lock:
            entry = self._slots.get(name)
            return entry.value if entry else None

    def remember_entity(self, kind: str, value: str) -> None:
        """Push a concrete entity onto the recency stack for pronoun
        resolution, independent of whether it also occupies a named slot."""
        if not value:
            return
        with self._lock:
            self._recency.append(_RecencyEntry(kind=kind, value=value))

    def clear_task(self) -> None:
        """Called when a task/goal is completed or abandoned — clears
        task-scoped slots but keeps longer-lived ones (project/person)."""
        with self._lock:
            for name in ("goal", "task", "file"):
                self._slots.pop(name, None)

    def clear_all(self) -> None:
        with self._lock:
            self._slots.clear()
            self._recency.clear()

    # -- reads / resolution ----------------------------------------------
    def resolve(self, reference_word: str, kind: Optional[str] = None) -> Optional[str]:
        """Resolve a pronoun/reference to the most recent matching entity.

        `kind` optionally narrows the search (e.g. resolve("it", kind="file")
        only considers recency entries tagged 'file'). Without a kind, the
        single most recent entity of any kind is returned — good enough for
        the common "open X, then do Y to it" pattern this module targets.
        """
        word = (reference_word or "").strip().lower()
        if word not in _REFERENCE_WORDS and word != "":
            # Still allow resolution even for words we don't recognize as
            # references — callers may already know they want "the file",
            # in which case `kind` alone drives the lookup.
            pass
        with self._lock:
            candidates = list(self._recency)
        if kind:
            candidates = [c for c in candidates if c.kind == kind]
        if not candidates:
            return None
        return candidates[-1].value

    def is_reference_word(self, word: str) -> bool:
        return (word or "").strip().lower() in _REFERENCE_WORDS

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return {name: entry.value for name, entry in self._slots.items()}

    def as_prompt_block(self) -> str:
        """Small block injected fresh every turn (not cached) so 'it'/
        'that'/'them' resolve against what's *currently* true, not a
        5-minute-old snapshot."""
        with self._lock:
            slots = {name: entry.value for name, entry in self._slots.items() if name in SLOT_NAMES}
            recent = list(self._recency)[-5:]
        if not slots and not recent:
            return ""
        lines = ["[WORKING MEMORY]"]
        if slots:
            for name in SLOT_NAMES:
                if name in slots:
                    lines.append(f"  current_{name}: {slots[name]}")
        if recent:
            lines.append("  recently referenced (most recent last, for resolving \"it\"/\"that\"/\"them\"):")
            for r in recent:
                lines.append(f"    - {r.kind}: {r.value}")
        return "\n".join(lines)

    # -- event publishing ---------------------------------------------------
    def _publish(self, slot: str, value: str) -> None:
        try:
            from state_engine import event_bus
            event_bus.publish("WorkingMemoryUpdated", slot=slot, value=value)
        except Exception:
            log.debug("[WorkingMemory] publish skipped", exc_info=True)


# Process-wide singleton.
working_memory = WorkingMemory()

__all__ = ["WorkingMemory", "working_memory", "SLOT_NAMES"]
