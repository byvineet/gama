"""
voice/full_duplex_manager.py — Full-Duplex Voice Manager
=============================================================
Top-level piece the spec asks for: the thing that makes GAMA feel
like it can listen, think, execute, speak and monitor all at once.

It does NOT reimplement any of that — Listening lives in
voice/audio_coordinator.py + voice/pipeline.py, Speaking lives in
voice/speech_manager.py + voice/tts_engine.py, Execution lives in
core/task_queue.py, and narrating execution as speech lives in
voice/execution_narrator.py. This module is the thin coordination
layer on top that implements the specific behaviors the spec calls
out as cross-cutting:

  - Immediate Acknowledgement: speak the ack line the instant intent
    is understood, without waiting on execution — `acknowledge()` is
    fire-and-forget and returns immediately.
  - Interruptible Conversation: answer "what are you doing?" / "how
    much is left?" etc. straight from core.task_queue (zero LLM calls,
    per the existing describe_summary() design) instead of routing
    through the planner.
  - Dynamic Task Modification: "skip duplicates" / "ignore PDFs" /
    "pause after this file" modify the *running* task in place via
    core.task_queue.modify() instead of the planner cancelling and
    re-planning from scratch.
  - Barge-in resume: after an interruption is handled, automatically
    resume the task that was running before, if it wasn't the thing
    the user interrupted about.

Context & Memory Integration: this module holds no duplicate state.
Every answer is read live from core.task_queue / state_engine on each
call — never cached here — so it can't drift from what's actually
running.
"""

from __future__ import annotations

import re
from typing import Optional

from utils.logger import get_logger
from voice import speech_manager
from voice.speech_manager import Priority

log = get_logger(__name__)


# Lightweight local intent matching for the handful of utterances the
# spec calls out explicitly ("Pause.", "Resume.", "Stop.", "Skip this.",
# status questions...). Zero LLM calls — these are answered/actioned
# directly against core.task_queue. Anything that doesn't match falls
# through (returns None) so the normal planner/Gemini path handles it.
_STATUS_PATTERNS = (
    re.compile(r"\bwhat are you doing\b", re.I),
    re.compile(r"\bhow much (is|has|do you have) left\b", re.I),
    re.compile(r"\bare you (still )?working\b", re.I),
    re.compile(r"\bwhat did you finish\b", re.I),
    re.compile(r"\bwhat are you waiting (for|on)\b", re.I),
    re.compile(r"\bwhat'?s (the )?status\b", re.I),
    re.compile(r"\bhow long (is|will it|does it)\b", re.I),
)
_PAUSE_PATTERNS = (
    re.compile(r"^\s*pause\.?\s*$", re.I),
    re.compile(r"\bpause (that|this|it)\.?\s*$", re.I),
    re.compile(r"^\s*hold on\.?\s*$", re.I),
    re.compile(r"^\s*wait\.?\s*$", re.I),
)
_RESUME_PATTERNS = (
    re.compile(r"^\s*resume\.?\s*$", re.I),
    re.compile(r"\bresume (that|this|it)\.?\s*$", re.I),
    re.compile(r"^\s*continue\.?\s*$", re.I),
    re.compile(r"^\s*go ahead\.?\s*$", re.I),
    re.compile(r"\bkeep going\.?\s*$", re.I),
)
_STOP_PATTERNS = (
    re.compile(r"^\s*stop\.?\s*$", re.I),
    re.compile(r"\bcancel (that|this|it)\.?\s*$", re.I),
    re.compile(r"\bstop (that|this|it|doing that)\.?\s*$", re.I),
    re.compile(r"\babort (that|this|it)\.?\s*$", re.I),
    re.compile(r"\bcancel it\.?\s*$", re.I),
    re.compile(r"\bdon'?t (do|proceed|continue) (that|this|it)\.?\s*$", re.I),
    re.compile(r"\bforget (it|that)\.?\s*$", re.I),
    re.compile(r"\bnever mind\.?\s*$", re.I),
)


class FullDuplexVoiceManager:
    """See module docstring. One instance per process."""

    # ── Immediate Acknowledgement ─────────────────────────────────
    def acknowledge(self, text: str, *, priority: "Priority | int" = Priority.ACK) -> None:
        """Speak `text` right now, non-blocking. Call this the moment
        intent is understood — BEFORE kicking off execution — so
        speech and execution start together instead of speech waiting
        on execution to finish."""
        speech_manager.say(text, priority=priority, kind="ack", ttl_s=8.0)

    # ── Interruptible Conversation ─────────────────────────────────
    def try_answer_status_question(self, utterance: str) -> Optional[str]:
        """If `utterance` is a direct question about ongoing work,
        answer it straight from core.task_queue and return the text
        (already spoken at QUESTION priority). Returns None if this
        isn't a status question, so the caller can fall through to the
        normal command/planner path — running work is never cancelled
        just because the user asked about it."""
        if not any(p.search(utterance) for p in _STATUS_PATTERNS):
            return None
        answer = self._describe_current_work()
        speech_manager.say(answer, priority=Priority.QUESTION, kind="status")
        return answer

    def _describe_current_work(self) -> str:
        try:
            from core.task_queue import task_queue
            return task_queue.describe_summary()
        except Exception:
            log.exception("FullDuplexVoiceManager: describe_summary() failed")
            return "I'm not sure right now — let me check."

    # ── Barge-in command shortcuts (Pause/Resume/Stop) ──────────────
    def try_handle_control_command(self, utterance: str, task_id: Optional[str] = None) -> Optional[str]:
        """Handle the small set of direct task-control utterances the
        spec calls out ("Pause.", "Resume.", "Stop.") without a planner
        round-trip. Returns the spoken confirmation, or None if this
        utterance isn't one of those — caller should fall through to
        the normal command path."""
        try:
            from core.task_queue import task_queue
        except Exception:
            return None

        tid = task_id or task_queue.current_task_id()
        if tid is None:
            return None

        if any(p.match(utterance) for p in _PAUSE_PATTERNS):
            if task_queue.pause(tid):
                reply = "Paused."
                speech_manager.say(reply, priority=Priority.INTERRUPT, kind="control")
                return reply
        elif any(p.match(utterance) for p in _RESUME_PATTERNS):
            if task_queue.resume(tid):
                reply = "Resuming."
                speech_manager.say(reply, priority=Priority.INTERRUPT, kind="control")
                return reply
        elif any(p.match(utterance) for p in _STOP_PATTERNS):
            if task_queue.cancel(tid):
                reply = "Stopped."
                speech_manager.say(reply, priority=Priority.INTERRUPT, kind="control")
                return reply
        return None

    # ── Dynamic Task Modification ────────────────────────────────────
    def modify_current_task(self, task_id: Optional[str] = None, **modifiers) -> bool:
        """"Skip duplicate files.", "Ignore PDFs.", "Move only images."
        — apply live modifiers to the running task instead of
        cancelling and re-planning it. `modifiers` are arbitrary
        key/value pairs the task's own fn knows how to interpret
        (see core.task_queue.Task.modifiers)."""
        try:
            from core.task_queue import task_queue
        except Exception:
            return False
        tid = task_id or task_queue.current_task_id()
        if tid is None:
            return False
        return task_queue.modify(tid, **modifiers)

    # ── Barge-in resume ──────────────────────────────────────────────
    def resume_after_interruption(self, interrupted_about_task_id: Optional[str] = None) -> None:
        """Call once an interruption has been fully handled (question
        answered / control command applied). If the interruption
        wasn't itself about pausing/stopping a task, whatever was
        running keeps running uninterrupted — there's nothing to
        "resume" here since execution never blocked on speech in the
        first place (see core philosophy: voice never blocks
        execution). This is a no-op placeholder kept for symmetry /
        future use (e.g. re-arming barge-in) so call sites don't need
        to special-case "nothing to do here."""
        return


_manager: Optional[FullDuplexVoiceManager] = None


def get_manager() -> FullDuplexVoiceManager:
    global _manager
    if _manager is None:
        _manager = FullDuplexVoiceManager()
    return _manager


__all__ = ["FullDuplexVoiceManager", "get_manager"]
