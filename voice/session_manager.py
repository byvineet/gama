"""
voice/session_manager.py — Conversation Session Manager (JARVIS-style)
========================================================================
Sits on top of the existing wake-word / VAD / speaker-verification /
echo-guard stack and removes the need to repeat the wake word for every
follow-up command, while still rejecting self-talk, human-to-human
conversation, background noise and Gama's own TTS echo.

State machine
-------------

    PASSIVE  --isolated wake phrase only--------------------->  ACTIVE
    ACTIVE   --valid directed interaction------------------->  ACTIVE (timer reset)
    ACTIVE   --adaptive timeout / explicit sleep------------->  PASSIVE

Passive Mode
    Every utterance is still fed through VAD + speaker verification
    (unchanged — that happens upstream in voice/pipeline.py and
    wake_word/listener.py). While PASSIVE, an utterance can ONLY
    activate a session if it is an isolated match of one of the exactly
    two supported wake phrases ("gama" / "wake up gama") — see
    wake_word/engines/vosk_engine.py's exact-whole-utterance-equality
    check. Command-shaped or high-confidence text with no isolated wake
    phrase ("Gama, open Chrome", "Can Gama open Chrome?") is always
    ignored while PASSIVE; DIRECT_COMMAND_CONFIDENCE_THRESHOLD is kept
    only for callers/tests that want to inspect classifier confidence,
    it no longer gates activation on its own.

Active Session
    Once activated, every subsequent utterance is evaluated by the
    IntentClassifier using the additional context an active session
    provides (recent turn history, session speaker, elapsed time since
    last turn) at the lower ACTIVE_SESSION_CONFIDENCE_THRESHOLD. Any
    utterance that passes resets the adaptive timeout. Utterances that
    are classified as self-talk / human-to-human / unknown are ignored
    but do NOT end the session by themselves — only the timeout does.

Adaptive Timeout
    Base window is TIMEOUT_MIN_S–TIMEOUT_MAX_S (10-15s). The window is
    nudged within (and briefly beyond) that range based on conversation
    flow signals:
      * Gama just asked a question              -> extend toward the max
        (+ a short grace window) since a reply is expected.
      * The user's utterance was itself a question directed at Gama
        (mid-thought, more likely to continue)  -> extend toward the max.
      * A short, complete command ("thanks", "stop", "turn it off")
        -> shrink toward the min, conversation has clearly wrapped up.
      * Consecutive directed turns in quick succession -> keep extending
        (active back-and-forth), reset counter after a passive rejection.

Echo Protection
    This module never receives Gama's own TTS output in the first
    place if voice/echo_guard.py's should_block()/is_speaking gates are
    respected upstream (voice/pipeline.py and main.py already call
    those before handing text here). As a defence-in-depth measure,
    classify() also accepts an `is_echo` flag callers can pass through
    from echo_guard, and immediately returns UNKNOWN/0.0 without
    touching session state if it is set.

This module is intentionally dependency-light (no ML calls) so it can
run synchronously on every utterance without adding latency; the
speaker verification and semantic heavy-lifting continue to happen in
voice/pipeline.py and core/fast_intent.py / Gemini.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional

from utils.logger import get_logger

log = get_logger(__name__)


# ── Adaptive timeout tuning ────────────────────────────────────────────────
TIMEOUT_MIN_S: float = 10.0
TIMEOUT_MAX_S: float = 15.0
# Extra grace given when Gama just asked a question and is awaiting a reply.
QUESTION_GRACE_S: float = 5.0

# ── Confidence thresholds ──────────────────────────────────────────────────
# No wake word to anchor on yet — require near-certainty before letting an
# utterance activate a session on its own ("Gama, turn off the lights").
DIRECT_COMMAND_CONFIDENCE_THRESHOLD: float = 0.80
# Inside an active session we already know the user is engaged with Gama,
# so the bar for "this follow-up was meant for me" is lower.
ACTIVE_SESSION_CONFIDENCE_THRESHOLD: float = 0.45

# How many recent turns to keep for context-aware classification.
HISTORY_MAXLEN: int = 8


class Intent(Enum):
    DIRECTED = "directed_to_gama"
    SELF_TALK = "self_talk"
    HUMAN_TO_HUMAN = "human_to_human"
    UNKNOWN = "unknown"


class SessionState(Enum):
    PASSIVE = "passive"
    ACTIVE = "active"


@dataclass
class IntentResult:
    intent: Intent
    confidence: float
    reason: str = ""


@dataclass
class Turn:
    text: str
    intent: Intent
    confidence: float
    speaker: Optional[str]
    ts: float = field(default_factory=time.monotonic)


# ── Lightweight linguistic signals for intent classification ──────────────
# These are cheap, explainable heuristics meant to run in front of (not
# instead of) semantic/LLM classification. `core/fast_intent.py` and Gemini
# itself remain the source of truth for *what* to do; this only decides
# *whether Gama was being spoken to at all*.

_WAKE_NAME_RE = re.compile(r"\b(gama|jarvis|hey gama|okay gama)\b", re.IGNORECASE)

_IMPERATIVE_VERBS = (
    "open", "close", "play", "pause", "stop", "resume", "turn", "set", "start",
    "send", "call", "text", "email", "search", "find", "show", "tell", "remind",
    "schedule", "cancel", "delete", "create", "add", "remove", "mute", "unmute",
    "increase", "decrease", "volume", "shutdown", "restart", "lock", "launch",
    "check", "what's", "what is", "who is", "how", "why", "when", "where",
)

_ADDRESS_PRONOUNS = ("you", "your", "yourself")

_SELF_TALK_MARKERS = (
    "hmm", "let me think", "where did i", "i think i", "note to self",
    "i should", "i need to remember", "okay so", "let's see",
)

_HUMAN_TO_HUMAN_MARKERS = (
    "he said", "she said", "did you hear", "what do you think", "babe",
    "honey", "bro,", "dude,", "mom,", "dad,",
)

_THIRD_PERSON_ABOUT_GAMA = re.compile(r"\bgama (is|was|has|does|said)\b", re.IGNORECASE)


def _score_heuristic(text: str) -> IntentResult:
    """Cheap rule-based first pass. Returns a confidence in [0, 1] that the
    utterance is DIRECTED at Gama, or the best-guess alternative intent.
    """
    t = text.strip().lower()
    if not t:
        return IntentResult(Intent.UNKNOWN, 0.0, "empty transcript")

    # Explicit wake name anywhere in the utterance -> near-certain.
    if _WAKE_NAME_RE.search(t):
        return IntentResult(Intent.DIRECTED, 0.95, "wake name present")

    # Talking *about* Gama in third person is the opposite signal.
    if _THIRD_PERSON_ABOUT_GAMA.search(t):
        return IntentResult(Intent.HUMAN_TO_HUMAN, 0.75, "third-person reference to Gama")

    for marker in _HUMAN_TO_HUMAN_MARKERS:
        if marker in t:
            return IntentResult(Intent.HUMAN_TO_HUMAN, 0.7, f"human-to-human marker '{marker}'")

    for marker in _SELF_TALK_MARKERS:
        if t.startswith(marker) or f" {marker}" in t:
            return IntentResult(Intent.SELF_TALK, 0.65, f"self-talk marker '{marker}'")

    starts_imperative = t.split(" ", 1)[0] in _IMPERATIVE_VERBS or any(
        t.startswith(v) for v in _IMPERATIVE_VERBS
    )
    has_address_pronoun = any(f" {p}" in f" {t} " for p in _ADDRESS_PRONOUNS)
    is_question = t.endswith("?") or t.startswith(("what", "who", "how", "why", "when", "where", "can you", "could you"))

    score = 0.15  # baseline uncertainty
    reasons = []
    if starts_imperative:
        score += 0.35
        reasons.append("imperative phrasing")
    if has_address_pronoun:
        score += 0.15
        reasons.append("addresses 'you'")
    if is_question:
        score += 0.2
        reasons.append("question form")
    # Very short utterances ("okay", "cool", "nice") are ambiguous by default.
    if len(t.split()) <= 1:
        score -= 0.2

    score = max(0.0, min(1.0, score))
    if score >= 0.5:
        return IntentResult(Intent.DIRECTED, score, ", ".join(reasons) or "heuristic")
    return IntentResult(Intent.UNKNOWN, score, "no strong directed signal")


class ConversationSessionManager:
    """Owns the PASSIVE/ACTIVE state machine, the adaptive inactivity
    timer, and intent classification for whether an utterance should be
    treated as a command to Gama.

    Thread-safe: `feed_wake()`, `evaluate()`, `end_session()` and the
    property getters may all be called from different threads (mic
    callback thread, pipeline worker threads, asyncio loop).

    This class does not own a clock/timer thread itself — the adaptive
    timeout is exposed via `current_timeout_s()` / `seconds_remaining()`
    and `is_expired()` so callers can drive it from whatever scheduling
    primitive they already use (main.py uses an asyncio task via
    `_schedule_auto_sleep`); this avoids a second competing timer.
    """

    def __init__(
        self,
        on_session_start: Optional[callable] = None,
        on_session_end: Optional[callable] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._state = SessionState.PASSIVE
        self._session_speaker: Optional[str] = None
        self._session_started_at: float = 0.0
        self._last_activity_at: float = 0.0
        self._current_timeout_s: float = TIMEOUT_MIN_S
        self._awaiting_reply: bool = False  # Gama just asked a question
        self._consecutive_directed_turns: int = 0
        self._history: Deque[Turn] = deque(maxlen=HISTORY_MAXLEN)
        self._on_session_start = on_session_start
        self._on_session_end = on_session_end

    # ── state introspection ────────────────────────────────────────────
    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        return self.state is SessionState.ACTIVE

    @property
    def session_speaker(self) -> Optional[str]:
        with self._lock:
            return self._session_speaker

    def current_timeout_s(self) -> float:
        with self._lock:
            return self._current_timeout_s

    def seconds_remaining(self) -> float:
        with self._lock:
            if self._state is not SessionState.ACTIVE:
                return 0.0
            elapsed = time.monotonic() - self._last_activity_at
            return max(0.0, self._current_timeout_s - elapsed)

    def is_expired(self) -> bool:
        with self._lock:
            return self._state is SessionState.ACTIVE and self.seconds_remaining() <= 0.0

    # ── session lifecycle ──────────────────────────────────────────────
    def start_session(self, speaker: Optional[str] = None, reason: str = "wake word") -> None:
        """Enter ACTIVE state (wake word fired, or a direct command was
        classified with high enough confidence to activate on its own)."""
        with self._lock:
            was_active = self._state is SessionState.ACTIVE
            self._state = SessionState.ACTIVE
            self._session_speaker = speaker
            now = time.monotonic()
            self._session_started_at = now
            self._last_activity_at = now
            self._current_timeout_s = TIMEOUT_MIN_S
            self._consecutive_directed_turns = 0
            if not was_active:
                self._history.clear()
        log.info(f"[session] ACTIVE ({reason}), speaker={speaker}")
        if self._on_session_start and not was_active:
            try:
                self._on_session_start(reason)
            except Exception:
                log.debug("[session] on_session_start callback failed", exc_info=True)

    def end_session(self, reason: str = "") -> None:
        with self._lock:
            was_active = self._state is SessionState.ACTIVE
            self._state = SessionState.PASSIVE
            self._session_speaker = None
            self._awaiting_reply = False
            self._consecutive_directed_turns = 0
        if was_active:
            log.info(f"[session] PASSIVE ({reason or 'timeout'})")
            if self._on_session_end:
                try:
                    self._on_session_end(reason)
                except Exception:
                    log.debug("[session] on_session_end callback failed", exc_info=True)

    def note_gama_asked_question(self, asked: bool = True) -> None:
        """Call this when Gama's own reply ends in a question, so the
        adaptive timeout extends to give the user time to answer."""
        with self._lock:
            self._awaiting_reply = asked

    # ── intent classification ──────────────────────────────────────────
    def classify(
        self,
        text: str,
        *,
        speaker_verified: bool = False,
        speaker: Optional[str] = None,
        confidence_boost: float = 0.0,
        is_echo: bool = False,
    ) -> IntentResult:
        """Classify one utterance. Does NOT mutate session state — call
        `evaluate()` for the full "should Gama respond, and do the
        bookkeeping" flow. Exposed separately so callers (e.g. a debug
        panel) can inspect classification without side effects.

        `confidence_boost` lets callers fold in signals this module
        doesn't have direct access to (e.g. a semantic similarity score
        from Gemini/embeddings, or contextual continuation logic in
        core/fast_intent.py) without duplicating the heuristics here.
        """
        # ── Echo Protection ─────────────────────────────────────────────
        # Never treat Gama's own TTS (or anything already flagged as an
        # echo by voice/echo_guard.py) as directed speech.
        if is_echo:
            return IntentResult(Intent.UNKNOWN, 0.0, "echo — dropped")

        result = _score_heuristic(text)
        confidence = max(0.0, min(1.0, result.confidence + confidence_boost))

        with self._lock:
            active = self._state is SessionState.ACTIVE
            awaiting_reply = self._awaiting_reply
            same_speaker = (
                speaker is not None
                and self._session_speaker is not None
                and speaker == self._session_speaker
            )

        # Session context nudges the score, it never invents a DIRECTED
        # verdict out of a clear SELF_TALK/HUMAN_TO_HUMAN read.
        if result.intent in (Intent.DIRECTED, Intent.UNKNOWN):
            if active and awaiting_reply:
                confidence += 0.25  # Gama is actively waiting on this person
            elif active:
                confidence += 0.10  # in-session utterances get some benefit of the doubt
            if active and same_speaker:
                confidence += 0.10
            if not speaker_verified and not active:
                # Unverified speaker with no active session — do not let a
                # bystander activate Gama on heuristic confidence alone.
                confidence -= 0.25
            confidence = max(0.0, min(1.0, confidence))
            if confidence >= 0.5 and result.intent is Intent.UNKNOWN:
                result = IntentResult(Intent.DIRECTED, confidence, result.reason + " + session context")
            else:
                result = IntentResult(result.intent, confidence, result.reason)

        return result

    def evaluate(
        self,
        text: str,
        *,
        speaker_verified: bool = False,
        speaker: Optional[str] = None,
        confidence_boost: float = 0.0,
        is_echo: bool = False,
        wake_word_present: bool = False,
    ) -> IntentResult:
        """Full decision: classify the utterance, decide whether it
        should activate/extend a session, and update all session state
        accordingly. Returns the IntentResult; callers should only act
        on it (route to fast-intent/Gemini) when
        `result.intent is Intent.DIRECTED`.
        """
        if is_echo:
            return IntentResult(Intent.UNKNOWN, 0.0, "echo — dropped")

        if wake_word_present:
            # Wake word always wins outright — no ambiguity to resolve.
            self.start_session(speaker=speaker, reason="wake word")
            self._record_turn(text, Intent.DIRECTED, 1.0, speaker)
            self._touch(text_was_question=text.strip().endswith("?"))
            return IntentResult(Intent.DIRECTED, 1.0, "wake word")

        result = self.classify(
            text,
            speaker_verified=speaker_verified,
            speaker=speaker,
            confidence_boost=confidence_boost,
        )

        with self._lock:
            active = self._state is SessionState.ACTIVE

        if active:
            # Inside an Active Conversation Session, follow-ups don't need
            # the wake phrase — only the adaptive timeout ends the session.
            threshold = ACTIVE_SESSION_CONFIDENCE_THRESHOLD
            directed = result.intent is Intent.DIRECTED and result.confidence >= threshold
        else:
            # Strict isolated wake-phrase gate: while PASSIVE, nothing —
            # no matter how "command-shaped" or high-confidence — may
            # activate a session on its own. Only an isolated match of one
            # of the exactly-two supported wake phrases (routed in via
            # `wake_word_present` above, before this method is reached)
            # may open a session. This intentionally closes the old
            # "high-confidence direct command" bypass, since a phrase like
            # "Gama, open Chrome" or "Can Gama open Chrome?" must be
            # ignored and leave Gama in Passive Mode.
            directed = False

        if directed:
            with self._lock:
                self._consecutive_directed_turns += 1
            self._record_turn(text, Intent.DIRECTED, result.confidence, speaker)
            self._touch(text_was_question=text.strip().endswith("?"))
            return IntentResult(Intent.DIRECTED, result.confidence, result.reason)

        # Not directed at Gama (either genuinely classified otherwise, or
        # DIRECTED-leaning but below this context's threshold) — normalize
        # to UNKNOWN/original non-directed intent so callers can safely
        # branch on `.intent == Intent.DIRECTED` alone without separately
        # re-checking confidence against a threshold they don't have.
        final_intent = result.intent if result.intent is not Intent.DIRECTED else Intent.UNKNOWN
        result = IntentResult(final_intent, result.confidence, result.reason)

        # Log for context but don't touch the timer and don't end an
        # active session (per spec: only the adaptive timeout ends a
        # session; a stray aside mid-conversation shouldn't kill it).
        self._record_turn(text, result.intent, result.confidence, speaker)
        with self._lock:
            self._consecutive_directed_turns = 0
        log.debug(
            f"[session] not directed at Gama: intent={result.intent.value} "
            f"confidence={result.confidence:.2f} (threshold={threshold:.2f}) text={text!r}"
        )
        return result

    # ── internal ─────────────────────────────────────────────────────
    def _record_turn(self, text: str, intent: Intent, confidence: float, speaker: Optional[str]) -> None:
        with self._lock:
            self._history.append(Turn(text=text, intent=intent, confidence=confidence, speaker=speaker))

    def _touch(self, text_was_question: bool = False) -> None:
        """Reset the inactivity timer after a valid, directed interaction,
        adapting the window based on conversation flow."""
        with self._lock:
            self._last_activity_at = time.monotonic()
            timeout = TIMEOUT_MIN_S

            if self._awaiting_reply:
                timeout = TIMEOUT_MAX_S + QUESTION_GRACE_S
            elif text_was_question:
                # User's own utterance trailed off into a question — likely
                # mid-thought, give them the fuller window.
                timeout = TIMEOUT_MAX_S
            elif self._consecutive_directed_turns >= 2:
                # Active back-and-forth in progress — keep the longer window.
                timeout = TIMEOUT_MAX_S
            else:
                timeout = TIMEOUT_MIN_S

            self._current_timeout_s = timeout
            # Each new user turn implicitly answers any pending question.
            self._awaiting_reply = False

        log.debug(f"[session] activity — timeout window now {timeout:.1f}s")


# ── process-wide singleton (mirrors security/trusted_session.py's pattern)
_MANAGER: Optional[ConversationSessionManager] = None
_MANAGER_LOCK = threading.Lock()


def get_session_manager() -> ConversationSessionManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ConversationSessionManager()
        return _MANAGER


__all__ = [
    "ConversationSessionManager",
    "Intent",
    "IntentResult",
    "SessionState",
    "get_session_manager",
    "TIMEOUT_MIN_S",
    "TIMEOUT_MAX_S",
    "DIRECT_COMMAND_CONFIDENCE_THRESHOLD",
    "ACTIVE_SESSION_CONFIDENCE_THRESHOLD",
]
