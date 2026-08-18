"""
voice/speech_manager.py — Centralized scripted-speech arbitration
======================================================================
Why this exists
----------------
Gemini Live's own audio output remains GAMA's normal conversational
voice — that never changes. But Gemini Live cannot speak anything new
while a tool call is in flight (the turn is blocked until the function
response goes back), so enrollment prompts, countdowns, "still working"
acknowledgements, reminders and system announcements all have to go out
through a separate engine (voice/tts_engine.py, Gemini native TTS,
requires internet) instead.

Previously every call site (main.py's ack line, voice/face enrollment's
on_speak callbacks) pushed straight into voice.tts_engine's raw FIFO
queue with no coordination between them. That caused exactly the bugs
this module fixes:
  - A "still working" ack queued just before the real result landed
    would still be sitting in the FIFO and play AFTER the result.
  - Enrollment intro lines queued at the very start could still be
    sitting unplayed minutes later if the enrollment UI moved on to
    later steps without waiting for speech to drain, so "enrollment is
    about to begin" could be heard after enrollment had already ended.
  - Nothing suppressed duplicate/rapid acks.

SpeechManager is now the ONLY thing allowed to touch
voice.tts_engine's queue. Every caller — enrollment flows, reminders,
system announcements, processing acknowledgements — goes through
`speech_manager.say(...)`. It:

  - Orders requests by priority (RESULT/PROMPT speech can pre-empt and
    flush lower-priority ACK lines that haven't started playing yet, so
    a stale ack can never play after the thing it was acknowledging).
  - Supports interruption/cancellation (`cancel_all()`, used when
    enrollment is retried/cancelled, and `cancel(kind=...)` to drop
    just one category, e.g. pending ACKs, without touching prompts).
  - Suppresses duplicate lines queued within a short window (fixes
    repeated acknowledgements firing when nothing new was actually
    asked).
  - Offers a `say(..., blocking=True)` mode so callers that need audio
    to genuinely finish before advancing (enrollment step sequencing)
    can request that, instead of the old fire-and-forget pattern that
    let the UI race ahead of the audio.

This module deliberately does NOT touch Gemini Live's own audio path —
conversational replies keep coming out of the Live session exactly as
before. The offline engine stays what it always had to be (the only
option during an in-flight tool call), but it is now the single,
consistently-ordered voice for every scripted line, not several
uncoordinated callers hitting the same FIFO.

Gama 2.0 update — full priority ladder + expiry
------------------------------------------------
The JARVIS-style voice engine (see voice/execution_narrator.py and
voice/full_duplex_manager.py) needs a finer-grained priority order
than the original four tiers, per spec:

    Emergency Stop > User interruption > Direct questions > Errors >
    Task acknowledgements > Progress updates > Completion >
    Background suggestions

That's implemented below as `Priority`. The original four names
(ACK/REMINDER/PROMPT/RESULT) are kept as aliases pointing at the new
scale so every existing call site keeps working unchanged.

Low-priority chatter (PROGRESS/SUGGESTION) is also the stuff most
likely to go stale — a "still downloading" line queued 20 seconds ago
is worthless once the download has since finished. Those tiers now
carry a short TTL; `say()` opportunistically purges anything that
aged out before it ever got a chance to play, so old low-priority
speech "automatically expires" instead of queuing up and firing late.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

# Single speaker: Gemini Live via SpeechAuthority. Offline TTS is no longer
# a parallel voice — scripted lines are injected into the Live session.
try:
    from core.speech_authority import speech_authority, SpeakPriority
except Exception:  # pragma: no cover
    speech_authority = None
    SpeakPriority = None


class Priority(IntEnum):
    """Higher value = more important. A higher-priority item queued
    while a lower-priority one is still waiting (not yet started)
    flushes the lower-priority one instead of stacking behind it.

    Full ladder (spec order, low -> high):
        SUGGESTION < COMPLETION < PROGRESS < ACK < ERROR < QUESTION
        < INTERRUPT < EMERGENCY
    """
    SUGGESTION = 0       # background suggestions ("you could also...")
    COMPLETION = 1       # "Done, sir." / task-finished lines
    PROGRESS = 2         # event-driven "I'm downloading..." narration
    ACK = 3              # task acknowledgements ("On it.", "still working")
    ERROR = 4            # error/failure narration
    QUESTION = 5         # direct answers to user questions ("what's left?")
    INTERRUPT = 6        # user-interruption handling ("Pause.", "Stop.")
    EMERGENCY = 7        # emergency stop — always wins

    # ── legacy aliases — pre-existing call sites are untouched ──
    REMINDER = 1         # old REMINDER tier ~= COMPLETION-ish background line
    PROMPT = 5           # old PROMPT tier (enrollment/guidance) ~= QUESTION
    RESULT = 6           # old RESULT tier (final spoken results) ~= INTERRUPT


# Tiers whose queued-but-unplayed items go stale quickly and should be
# dropped rather than spoken late. Anything not listed never expires.
_DEFAULT_TTL_S: dict[int, float] = {
    Priority.SUGGESTION: 25.0,
    Priority.PROGRESS: 12.0,
}


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    text: str = field(compare=False)
    kind: str = field(compare=False)
    created_at: float = field(compare=False, default_factory=time.monotonic)
    ttl_s: Optional[float] = field(compare=False, default=None)

    @property
    def expired(self) -> bool:
        return self.ttl_s is not None and (time.monotonic() - self.created_at) > self.ttl_s


class SpeechManager:
    """Single arbitration point for all offline scripted speech."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: list[_Item] = []   # queued, not yet handed to tts_engine
        self._seq = 0
        self._last_text: str = ""
        self._last_ts: float = 0.0
        # Increased from 2.5 → 5.0 s to suppress rapid duplicate fires
        # from two code paths (e.g. session audio + local TTS ack) that
        # arrive for the same text within a single turn.
        self._dup_window_s = 5.0
        # Track what is *actively playing* right now so we never re-queue
        # the identical line while the previous play is still ongoing.
        self._active_text: str = ""
        self._active_seq: int = -1
        self._speaking_done = threading.Event()
        self._speaking_done.set()

    # ── public API ──────────────────────────────────────────────
    def say(self, text: str, *, priority: "Priority | int" = Priority.PROMPT,
            kind: str = "generic", dedup: bool = True, blocking: bool = False,
            ttl_s: Optional[float] = None) -> None:
        """Speak `text`. Higher-priority requests flush any
        lower-priority items still waiting to start (so a stale ACK
        never plays after a RESULT that has already been queued).

        ttl_s: if the item is still unplayed after this many seconds
        it is dropped instead of spoken late (defaults come from
        `_DEFAULT_TTL_S` by priority tier — PROGRESS/SUGGESTION lines
        expire automatically per spec, "older low-priority speech
        should automatically expire"). Pass ttl_s=0 to disable expiry
        for a normally-expiring tier.

        blocking=True waits for this specific line to finish playing
        before returning — used by enrollment flows that need the
        audio and the on-screen step to stay in lockstep instead of
        the UI racing ahead of speech.
        """
        if not text:
            return
        text = text.strip()
        if not text:
            return

        priority = int(priority)
        if ttl_s is None:
            ttl_s = _DEFAULT_TTL_S.get(priority)
        elif ttl_s <= 0:
            ttl_s = None

        now = time.monotonic()
        with self._lock:
            self._purge_expired_nolock()

            if dedup and text == self._last_text and (now - self._last_ts) < self._dup_window_s:
                log.debug(f"SpeechManager: suppressed duplicate (dedup window): {text!r}")
                return

            # Also suppress if this exact text is currently playing (active
            # play not yet finished) — prevents double-speak when two code
            # paths race to queue the same line within a single turn.
            if dedup and text == self._active_text and self._active_seq >= 0:
                log.debug(f"SpeechManager: suppressed duplicate (currently playing): {text!r}")
                return

            # A higher (or equal) priority item flushes any *lower*
            # priority items still waiting — e.g. a RESULT arriving
            # cancels a not-yet-started ACK so it can never play late.
            self._pending = [it for it in self._pending if it.priority > priority]

            self._seq += 1
            item = _Item(priority=priority, seq=self._seq, text=text, kind=kind, ttl_s=ttl_s)
            self._pending.append(item)
            self._pending.sort(key=lambda it: (-it.priority, it.seq))
            self._last_text, self._last_ts = text, now
            # Mark as active immediately so concurrent callers see it.
            self._active_text = text
            self._active_seq = item.seq

            done_event = threading.Event()
            self._speaking_done = done_event if blocking else self._speaking_done

        # Route through SpeechAuthority → Gemini Live only (single speaker).
        # Offline TTS is intentionally not used as a second voice.
        if speech_authority is not None:
            try:
                # Map legacy Priority → SpeakPriority roughly by value
                speech_authority.say(text, priority=priority, kind=kind, dedup=False)
            except Exception as exc:
                log.debug(f"SpeechManager→SpeechAuthority failed: {exc}")
        else:
            log.warning("SpeechAuthority unavailable — scripted speech dropped (no offline fallback).")

        # Mark done immediately for blocking callers; actual audio is async
        # via Gemini. Enrollment should prefer short lines.
        self._on_spoken(item, done_event if blocking else None)
        if blocking:
            done_event.wait(timeout=0.05)

    def _purge_expired_nolock(self) -> None:
        """Drop pending items that aged out before they got a chance
        to play. Caller must hold self._lock."""
        self._pending = [it for it in self._pending if not it.expired]

    def cancel(self, kind: Optional[str] = None) -> None:
        """Drop queued-but-not-yet-started items."""
        with self._lock:
            if kind is None:
                self._pending.clear()
            else:
                self._pending = [it for it in self._pending if it.kind != kind]
        if speech_authority is not None:
            try:
                if kind is None:
                    speech_authority.cancel_all()
            except Exception:
                pass

    def cancel_all(self) -> None:
        """Full stop of scripted queue."""
        with self._lock:
            self._pending.clear()
            self._last_text = ""
            self._active_text = ""
            self._active_seq = -1
        if speech_authority is not None:
            try:
                speech_authority.cancel_all()
            except Exception:
                pass

    # ── internal ────────────────────────────────────────────────
    def _on_spoken(self, item: _Item, done_event: Optional[threading.Event]) -> None:
        with self._lock:
            self._pending = [it for it in self._pending if it.seq != item.seq]
            # Clear active tracking when this item finishes.
            if self._active_seq == item.seq:
                self._active_text = ""
                self._active_seq = -1
        if done_event is not None:
            done_event.set()


_manager: Optional[SpeechManager] = None
_manager_lock = threading.Lock()


def get_manager() -> SpeechManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = SpeechManager()
    return _manager


# ── Convenience module-level wrappers (mirrors tts_engine's style) ──
def say(text: str, *, priority: "Priority | int" = Priority.PROMPT,
        kind: str = "generic", dedup: bool = True, blocking: bool = False,
        ttl_s: Optional[float] = None) -> None:
    get_manager().say(text, priority=priority, kind=kind, dedup=dedup,
                       blocking=blocking, ttl_s=ttl_s)


def cancel(kind: Optional[str] = None) -> None:
    get_manager().cancel(kind)


def cancel_all() -> None:
    get_manager().cancel_all()


__all__ = ["SpeechManager", "Priority", "get_manager", "say", "cancel", "cancel_all"]
