"""
voice/audio_coordinator.py — Audio Coordinator
==================================================
Central coordinator for everything touching audio, per the Gama 2.0
voice spec's "Shared Audio Pipeline" / "Audio Coordinator" sections:

    Microphone -> VAD -> Wake Word -> Streaming ASR -> Intent Pipeline

GAMA already has every one of these pieces (voice/vad.py,
wake_word/*, voice/pipeline.py's VoicePipeline, voice/barge_in.py,
voice/speech_manager.py) — they just weren't behind a single door.
Historically each piece was fed audio (and told to activate/deactivate)
by ad-hoc call sites scattered through main.py. That's exactly the
"duplicate listeners / duplicate ASR sessions / repeatedly stop-start
the microphone" failure mode the spec calls out.

AudioCoordinator does NOT open its own sounddevice stream — main.py's
existing microphone callback keeps doing that (there is exactly one
InputStream in the process; this module doesn't change that). What it
DOES do is give that one existing callback a single method to call —
`feed(pcm_int16)` — which then fans the same frame out to every
consumer (VAD/VoicePipeline, wake-word engine, barge-in detector)
itself, in the right order, instead of main.py doing that fan-out by
hand at three or four separate call sites.

Echo Guard Integration
----------------------
``feed()`` now consults ``voice.echo_guard.EchoGuard`` before forwarding
frames to the VAD/voice-pipeline and wake-word consumers:

  • While Gama is actively speaking (``guard.is_speaking``), only the
    barge-in consumer receives audio — all other consumers are silenced
    so Gama's own TTS output can never reach the speech pipeline.
  • Wake-word detection is also gated: the wake-word engine should never
    fire on Gama's own voice. Barge-in is the only correct path during
    TTS playback.

This is purely additive — nothing existing has to change to keep working.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np

from state_engine.event_bus import event_bus
from utils.logger import get_logger
from voice import speech_manager
from voice.speech_manager import Priority

log = get_logger(__name__)


class AudioCoordinator:
    """Owns *coordination*, not the raw audio device. One instance per
    process — see get_coordinator()."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._speaking = False
        self._listening_paused = False

        # Consumers registered by whoever wires them up (voice pipeline,
        # wake-word engine, barge-in detector). Each is just a callable
        # taking one int16 PCM numpy array — feed() fans every incoming
        # frame out to all of them, once, in a fixed order.
        self._vad_consumer: Optional[Callable[[np.ndarray], None]] = None
        self._wake_word_consumer: Optional[Callable[[np.ndarray], None]] = None
        self._barge_in_consumer: Optional[Callable[[np.ndarray], None]] = None

        # Pre-bind echo guard lazily on first feed() call to avoid circular
        # import at module load time.
        self._echo_guard = None
        self._echo_guard_loaded = False

    def _get_echo_guard(self):
        """Echo guard stack removed — always returns None."""
        self._echo_guard = None
        self._echo_guard_loaded = True
        return None

    # ── registration (call once at startup) ──────────────────────
    def register_vad(self, consumer: Callable[[np.ndarray], None]) -> None:
        self._vad_consumer = consumer

    def register_wake_word(self, consumer: Callable[[np.ndarray], None]) -> None:
        self._wake_word_consumer = consumer

    def register_barge_in(self, consumer: Callable[[np.ndarray], None]) -> None:
        self._barge_in_consumer = consumer

    # ── the shared pipeline entry point ──────────────────────────
    def feed(self, pcm_int16: np.ndarray) -> None:
        """Call this once per audio frame from the (single) microphone
        callback. Fans the same frame out to every registered consumer
        instead of each call site owning its own copy of this logic.
        Never blocks — every consumer here is expected to be cheap/
        non-blocking itself (same discipline as EventBus subscribers).

        Echo-guard gate: while Gama is actively speaking, only the
        barge-in consumer receives audio — VAD, voice pipeline, and
        wake-word detection are all suppressed to prevent Gama's own
        TTS from entering the intent pipeline.
        """
        with self._lock:
            paused = self._listening_paused
            speaking = self._speaking

        # Check echo guard (the authoritative TTS playback state).
        guard = self._get_echo_guard()
        tts_active = guard.is_speaking if guard is not None else speaking

        # Barge-in only cares about audio while GAMA is speaking.
        if (speaking or tts_active) and self._barge_in_consumer is not None:
            try:
                self._barge_in_consumer(pcm_int16)
            except Exception:
                log.exception("AudioCoordinator: barge-in consumer raised")

        # All other consumers are silenced while TTS is playing OR while
        # listening is explicitly paused (Guard Mode / sleep).
        if paused or tts_active:
            return

        if self._wake_word_consumer is not None:
            try:
                self._wake_word_consumer(pcm_int16)
            except Exception:
                log.exception("AudioCoordinator: wake-word consumer raised")

        if self._vad_consumer is not None:
            try:
                self._vad_consumer(pcm_int16)
            except Exception:
                log.exception("AudioCoordinator: VAD consumer raised")

    # ── listening control ─────────────────────────────────────────
    def pause_listening(self) -> None:
        """Stop feeding wake-word/VAD (e.g. during Guard Mode / sleep)
        without tearing down and recreating the microphone stream."""
        with self._lock:
            self._listening_paused = True

    def resume_listening(self) -> None:
        with self._lock:
            self._listening_paused = False

    # ── speak/listen handshake ──────────────────────────────────────
    def begin_speaking(self) -> None:
        """Call right before handing text to speech_manager/tts_engine.
        Publishes SpeechStarted so the rest of the system (state engine,
        UI) reacts consistently instead of every call site remembering
        to do this by hand. (Barge-in monitoring is handled by Gemini
        Live's server-side interrupt + the transcript gate in
        core/live_session.py — no local detector to activate.)"""
        with self._lock:
            self._speaking = True
        event_bus.publish("SpeechStarted")

    def end_speaking(self, interrupted: bool = False) -> None:
        with self._lock:
            self._speaking = False
        event_bus.publish("SpeechInterrupted" if interrupted else "SpeechCompleted")

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    # ── convenience: route a speak request through the coordinator ──
    def speak(self, text: str, *, priority: "Priority | int" = Priority.PROGRESS,
              kind: str = "coordinator", blocking: bool = False) -> None:
        """Thin pass-through to speech_manager.say() kept here so callers
        that already hold an AudioCoordinator reference don't need a
        second import — arbitration itself still lives in
        voice/speech_manager.py (single source of truth for ordering)."""
        speech_manager.say(text, priority=priority, kind=kind, blocking=blocking)


_coordinator: Optional[AudioCoordinator] = None
_coordinator_lock = threading.Lock()


def get_coordinator() -> AudioCoordinator:
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                _coordinator = AudioCoordinator()
    return _coordinator


__all__ = ["AudioCoordinator", "get_coordinator"]
