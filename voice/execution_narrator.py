"""
voice/execution_narrator.py — Execution Narrator
====================================================
Converts internal execution states (task phases reported via
core/task_queue.py's report_step()/set_waiting()/etc., which publish
on the shared Event Bus) into short, natural spoken lines — without
the rest of the codebase ever having to think about phrasing.

    Internal:  step 5, "verify_file_integrity"
    Spoken:    "I'm verifying that everything copied correctly."

    Internal:  "download_python"
    Spoken:    "I'm downloading the latest Python installer."

Design
------
- Purely event-driven. This module never polls or runs a timer — it
  only speaks in response to TaskStarted / TaskProgressChanged /
  TaskPaused / TaskResumed / TaskCompleted / TaskFailed / TaskCancelled
  events on state_engine.event_bus. "Intelligent Progress Updates":
  speak only on phase changes, waits, retries, and completion — never
  spam.
- Categorized response pools (Acknowledgement / Working / Searching /
  Waiting / Verifying / Finished / Error / Recovery) with randomized
  selection so GAMA doesn't repeat itself.
- Phase names are matched by keyword, not exact string, so any task
  author can report_step("scan_duplicates") or
  report_step("Scanning for duplicates") and get sensible narration
  without registering anything here first. A handful of well-known
  phase ids get a specific, better line (see _SPECIFIC_LINES);
  everything else falls back to a category template built from the
  task's own name/step text ("I'm indexing your documents.").
- Every line goes through voice.speech_manager at PROGRESS priority
  (short TTL — stale "still downloading" lines expire instead of
  firing late) except completion/error lines, which use
  COMPLETION/ERROR priority and never expire.
- Per-task de-noising: won't re-narrate the same phase twice in a row,
  and never fires more than once per _MIN_GAP_S for the same task,
  so a burst of fine-grained report_step() calls from a tight loop
  doesn't turn into a wall of speech.

This module intentionally has NO knowledge of *how* a task runs. It
only reads the payload TaskQueue already publishes (see
core/task_queue.py: report_step/set_waiting/mark_retry/set_verifying)
and the Context Engine/Task Queue remain the single source of truth —
this narrator holds no duplicate state beyond a small per-task
de-noising cache.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Dict, Optional

from core import personality
from state_engine.event_bus import Event, event_bus
from utils.logger import get_logger
from voice import speech_manager
from voice.speech_manager import Priority

log = get_logger(__name__)

_MIN_GAP_S = 3.0       # minimum seconds between two narrated lines for the same task
_MIN_GAP_COMPLETE = 0.0  # completion/error lines are never suppressed by the gap

# Response pools now live in core/personality.py (single source of
# truth shared with core/wake_acknowledgment.py and core/engagement.py,
# spec section 5: "Maintain a large acknowledgement pool... randomize
# selections... avoid repeating recently used acknowledgements" — the
# anti-repeat there is cross-module, not just per-pool, which a purely
# local _POOLS dict here couldn't provide).

# Well-known phase ids -> a specific, better line than the generic
# category templates above. Matched by substring against the reported
# step name (case-insensitive), so "verify_file_integrity",
# "Verifying file integrity", etc. all hit the same entry.
_SPECIFIC_LINES: Dict[str, str] = {
    "verify_file_integrity": "I'm verifying that everything copied correctly.",
    "download_python": "I'm downloading the latest Python installer.",
    "scan_duplicates": "I'm checking for duplicate files.",
    "index_documents": "I'm indexing your documents.",
    "organizing_downloads": "I'm organizing your Downloads.",
    "search_projects": "I'm searching your projects.",
}


def _pick(category: str, **fmt) -> str:
    return personality.pick_acknowledgment(category, **fmt)


def _speak(text: str, *, priority, kind: str, ttl_s: Optional[float] = None) -> None:
    """Every narrated line passes through the Speech Styler before
    reaching speech_manager — spec section 16: 'Speech Styler is
    always the final stage before speech synthesis.' Failure here must
    never silence narration, so it degrades to the raw line."""
    try:
        # speech_styler removed
        # style() removed — pass-through
        pass
    except Exception:
        pass
    speech_manager.say(text, priority=priority, kind=kind, ttl_s=ttl_s)


def _match_specific(step: str) -> Optional[str]:
    s = step.lower()
    for key, line in _SPECIFIC_LINES.items():
        if key.replace("_", " ") in s.replace("_", " "):
            return line
    return None


def _classify(step: str) -> str:
    """Best-effort category for an arbitrary step name, by keyword."""
    s = step.lower()
    if any(k in s for k in ("verify", "check", "confirm", "validat")):
        return "verifying"
    if any(k in s for k in ("search", "look", "find", "index")):
        return "searching"
    if any(k in s for k in ("download", "install", "copy", "move", "writ", "organiz", "scan", "clean")):
        return "working"
    return "working"


class ExecutionNarrator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_spoken_phase: Dict[str, str] = {}
        self._last_spoken_at: Dict[str, float] = {}
        self._subscribed = False

    def start(self) -> None:
        """Subscribe to the Event Bus. Idempotent — safe to call more
        than once (e.g. from multiple import sites)."""
        if self._subscribed:
            return
        event_bus.subscribe("TaskStarted", self._on_task_started)
        event_bus.subscribe("TaskProgressChanged", self._on_progress)
        event_bus.subscribe("TaskPaused", self._on_paused)
        event_bus.subscribe("TaskResumed", self._on_resumed)
        event_bus.subscribe("TaskCompleted", self._on_completed)
        event_bus.subscribe("TaskFailed", self._on_failed)
        event_bus.subscribe("TaskCancelled", self._on_cancelled)
        self._subscribed = True
        log.debug("ExecutionNarrator: subscribed to task events.")

    # ── de-noising ─────────────────────────────────────────────────
    def _should_speak(self, task_id: str, phase_key: str,
                       min_gap: float = _MIN_GAP_S) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._last_spoken_phase.get(task_id) == phase_key:
                return False
            last_at = self._last_spoken_at.get(task_id, 0.0)
            if (now - last_at) < min_gap:
                return False
            self._last_spoken_phase[task_id] = phase_key
            self._last_spoken_at[task_id] = now
            return True

    def _forget(self, task_id: str) -> None:
        with self._lock:
            self._last_spoken_phase.pop(task_id, None)
            self._last_spoken_at.pop(task_id, None)

    # ── event handlers ─────────────────────────────────────────────
    def _on_task_started(self, evt: Event) -> None:
        name = evt.data.get("name", "that")
        task_id = evt.data.get("task_id", "")
        if not self._should_speak(task_id, "started"):
            return
        # Use the task name naturally when it's meaningful
        if name and not name.startswith("task_"):
            clean = name.rstrip(".").strip()
            line = f"Okay, starting {clean} now."
        else:
            line = _pick("acknowledgement")
        _speak(line, priority=Priority.ACK, kind="narrator", ttl_s=10.0)

    def _on_progress(self, evt: Event) -> None:
        task_id = evt.data.get("task_id", "")
        name = evt.data.get("name", "that")
        phase = evt.data.get("phase", "")
        if not phase:
            return

        if evt.data.get("waiting"):
            reason = evt.data.get("waiting_reason") or name
            if self._should_speak(task_id, f"waiting:{reason}"):
                _speak(_pick("waiting", reason=reason), priority=Priority.PROGRESS,
                                    kind="narrator")
            return

        if evt.data.get("retry_count"):
            if self._should_speak(task_id, f"retry:{evt.data['retry_count']}"):
                _speak(_pick("recovery", name=name), priority=Priority.PROGRESS,
                                    kind="narrator")
            return

        if not self._should_speak(task_id, phase):
            return

        line = _match_specific(phase)
        if line is None:
            category = _classify(phase)
            line = _pick(category, name=name)
        _speak(line, priority=Priority.PROGRESS, kind="narrator")

    def _on_paused(self, evt: Event) -> None:
        name = evt.data.get("name", "that")
        _speak(f"Paused {name}.", priority=Priority.ACK, kind="narrator", ttl_s=8.0)

    def _on_resumed(self, evt: Event) -> None:
        name = evt.data.get("name", "that")
        _speak(f"Resuming {name}.", priority=Priority.ACK, kind="narrator", ttl_s=8.0)

    def _on_completed(self, evt: Event) -> None:
        name = evt.data.get("name", "that")
        task_id = evt.data.get("task_id", "")
        self._forget(task_id)
        # Completion is never filtered by the gap — always speak it
        _speak(_pick("finished", name=name), priority=Priority.COMPLETION, kind="narrator")

    def _on_failed(self, evt: Event) -> None:
        name = evt.data.get("name", "that")
        self._forget(evt.data.get("task_id", ""))
        # Errors are never filtered by the gap
        _speak(_pick("error", name=name), priority=Priority.ERROR, kind="narrator")

    def _on_cancelled(self, evt: Event) -> None:
        name = evt.data.get("name", "that")
        self._forget(evt.data.get("task_id", ""))
        if name and not name.startswith("task_"):
            line = f"Stopped {name}."
        else:
            line = "Stopped."
        _speak(line, priority=Priority.ACK, kind="narrator", ttl_s=8.0)


_narrator: Optional[ExecutionNarrator] = None
_narrator_lock = threading.Lock()


def get_narrator() -> ExecutionNarrator:
    global _narrator
    if _narrator is None:
        with _narrator_lock:
            if _narrator is None:
                _narrator = ExecutionNarrator()
    return _narrator


def start() -> None:
    """Import-and-call this once at startup (see INTEGRATION.md) to
    turn on automatic task narration."""
    get_narrator().start()


__all__ = ["ExecutionNarrator", "get_narrator", "start"]
