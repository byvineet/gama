"""
core/speech_authority.py — Single speaker for Gama (Gemini Live only)
=====================================================================
JARVIS rule: one voice. All acks, alerts, completions, reminders, and
wake lines go through Gemini 3.x Live audio — never offline Piper/gTTS
as a parallel speaker.

Gate rules
----------
- If Gama is speaking OR the user is speaking → queue is held (or dropped
  for low priority); nothing new is injected into the Live session.
- If neither is speaking → drain the priority queue into Gemini.
- Conversational replies from the Live model itself are the same speaker;
  this module only arbitrates *scripted* system lines so they never
  overlap the model or the user.

Priority (high → low)
---------------------
  EMERGENCY > INTERRUPT > QUESTION / RESULT > ERROR > ACK >
  PROGRESS > COMPLETION > REMINDER > SUGGESTION

Task completion lines ("Download completed, Sir.") use COMPLETION.
Critical failures use ERROR. Background Sentinel noise uses SUGGESTION
and is deferred until the channel is free.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

from utils.logger import get_logger

log = get_logger(__name__)


class SpeakPriority(IntEnum):
    SUGGESTION = 0
    COMPLETION = 1   # "Task completed, Sir."
    REMINDER = 2
    PROGRESS = 3
    ACK = 4          # "On it, Sir."
    ERROR = 5        # "Task failed, Sir."
    QUESTION = 6     # direct answer / result narration
    INTERRUPT = 7
    EMERGENCY = 8


_TTL_S = {
    SpeakPriority.SUGGESTION: 20.0,
    SpeakPriority.PROGRESS: 12.0,
    SpeakPriority.ACK: 8.0,
}


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    text: str = field(compare=False)
    kind: str = field(compare=False, default="generic")
    created: float = field(compare=False, default_factory=time.monotonic)
    ttl: Optional[float] = field(compare=False, default=None)

    @property
    def expired(self) -> bool:
        return self.ttl is not None and (time.monotonic() - self.created) > self.ttl


class SpeechAuthority:
    """Process-wide single-speaker arbiter (Gemini Live only)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queue: list[_Item] = []
        self._seq = 0
        self._last_text = ""
        self._last_ts = 0.0
        self._dup_window = 4.0

        # State probes — set by main / controllers
        self._is_gama_speaking: Callable[[], bool] = lambda: False
        self._is_user_speaking: Callable[[], bool] = lambda: False
        self._is_listening: Callable[[], bool] = lambda: False
        # Inject text into Gemini Live so it is spoken with the one voice
        self._speak_via_gemini: Optional[Callable[[str], None]] = None

        self._drain_scheduled = False
        self._worker_stop = threading.Event()
        self._worker = threading.Thread(
            target=self._drain_loop, name="gama-speech-authority", daemon=True
        )
        self._worker.start()

    # ── wiring ──────────────────────────────────────────────────────────
    def bind(
        self,
        *,
        is_gama_speaking: Callable[[], bool],
        is_user_speaking: Callable[[], bool],
        is_listening: Callable[[], bool],
        speak_via_gemini: Callable[[str], None],
    ) -> None:
        self._is_gama_speaking = is_gama_speaking
        self._is_user_speaking = is_user_speaking
        self._is_listening = is_listening
        self._speak_via_gemini = speak_via_gemini
        log.info("SpeechAuthority bound to Gemini Live (single speaker).")

    # ── channel busy? ───────────────────────────────────────────────────
    def channel_busy(self) -> bool:
        """True while Gama or the user is speaking — no acks/alerts."""
        try:
            if self._is_gama_speaking():
                return True
        except Exception:
            pass
        try:
            if self._is_user_speaking():
                return True
        except Exception:
            pass
        return False

    def can_inject(self) -> bool:
        """Allow scripted lines only when neither side is speaking."""
        return not self.channel_busy()

    # ── public API ──────────────────────────────────────────────────────
    def say(
        self,
        text: str,
        *,
        priority: SpeakPriority | int = SpeakPriority.ACK,
        kind: str = "generic",
        dedup: bool = True,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        priority = int(priority)
        ttl = _TTL_S.get(SpeakPriority(priority) if priority in SpeakPriority._value2member_map_ else SpeakPriority.ACK)
        # Map int priorities that aren't enum members
        if priority in _TTL_S:
            ttl = _TTL_S[priority]
        elif priority <= int(SpeakPriority.PROGRESS):
            ttl = 12.0
        else:
            ttl = None

        now = time.monotonic()
        with self._lock:
            if dedup and text == self._last_text and (now - self._last_ts) < self._dup_window:
                log.debug(f"SpeechAuthority: dedup suppressed {text!r}")
                return
            # Drop lower-priority waiting items
            self._queue = [it for it in self._queue if it.priority > priority]
            self._seq += 1
            self._queue.append(
                _Item(priority=priority, seq=self._seq, text=text, kind=kind, ttl=ttl)
            )
            self._queue.sort(key=lambda it: (-it.priority, it.seq))
            self._last_text, self._last_ts = text, now
        log.debug(f"SpeechAuthority: queued p={priority} kind={kind} {text[:80]!r}")

    def announce_task_completed(self, name: str) -> None:
        label = (name or "task").replace("_", " ").strip()
        self.say(f"{label} completed, Sir.", priority=SpeakPriority.COMPLETION, kind="task_done")

    def announce_task_failed(self, name: str, error: str = "") -> None:
        label = (name or "task").replace("_", " ").strip()
        msg = f"{label} failed, Sir."
        if error and len(error) < 60:
            msg = f"{label} failed, Sir: {error}"
        self.say(msg, priority=SpeakPriority.ERROR, kind="task_fail")

    def announce_task_cancelled(self, name: str) -> None:
        label = (name or "task").replace("_", " ").strip()
        self.say(f"{label} cancelled, Sir.", priority=SpeakPriority.ACK, kind="task_cancel")

    def cancel_below(self, priority: int) -> None:
        with self._lock:
            self._queue = [it for it in self._queue if it.priority >= priority]

    def cancel_all(self) -> None:
        with self._lock:
            self._queue.clear()

    # ── drain ───────────────────────────────────────────────────────────
    def _drain_loop(self) -> None:
        while not self._worker_stop.wait(0.25):
            try:
                self._try_drain_one()
            except Exception as exc:
                log.debug(f"SpeechAuthority drain: {exc}")

    def _try_drain_one(self) -> None:
        if self._speak_via_gemini is None:
            return
        if not self.can_inject():
            return
        item = None
        with self._lock:
            # purge expired
            self._queue = [it for it in self._queue if not it.expired]
            if not self._queue:
                return
            item = self._queue.pop(0)
        if item is None:
            return
        try:
            # Prefix so the model speaks this system line, not tools
            payload = (
                f'[SPEAK_ONLY] Say exactly this aloud and nothing else, '
                f'no tools: "{item.text}"'
            )
            self._speak_via_gemini(payload)
            log.info(f"SpeechAuthority → Gemini ({item.kind}): {item.text[:80]}")
        except Exception as exc:
            log.warning(f"SpeechAuthority speak failed: {exc}")

    def shutdown(self) -> None:
        self._worker_stop.set()
        self.cancel_all()


# Process singleton
speech_authority = SpeechAuthority()

__all__ = ["SpeechAuthority", "SpeakPriority", "speech_authority"]
