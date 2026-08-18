"""
core/audio_stream.py — Mic callback, realtime send, playback — extracted from GamaAssistant (Phase 1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger(__name__)

# Mirrored from main.py / interrupt_calibration to avoid circular imports
# after Phase-1 extraction.
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 512  # overridden by PerfMode preferred_chunk_size when set
CHANNELS = 1

def _resolve_chunk_size() -> int:
    try:
        from utils.performance_mode import perf as _perf
        preferred = _perf.preferred_chunk_size
        if preferred and int(preferred) > 0:
            return int(preferred)
    except Exception:
        pass
    return CHUNK_SIZE

# Interrupt / barge-in calibration constants (and helpers). Prefer the
# shared module so runtime profile updates via apply_interrupt_calibration
# stay in sync; fall back to a safe default if the module is unavailable.
try:
    from core.interrupt_calibration import *  # noqa: F401, F403
    from core.interrupt_calibration import POST_SPEECH_GATE_SECONDS  # noqa: F401
except Exception:
    POST_SPEECH_GATE_SECONDS = 0.03

# Cached web-bridge amplitude pusher — resolved on first mic callback so the
# real-time path never repeats the import machinery per chunk.
_push_amplitude = None


class AudioStreamController:
    """Mic callback, realtime send, playback — extracted from GamaAssistant (Phase 1)."""

    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    async def _listen_audio(self):
        """Mic callback → out_queue. Matches Mark XXXIX-OR exactly."""
        asst = self._asst
        import sounddevice as sd
        import numpy as _np_audio  # imported once here; captured in callback closure
        loop = asyncio.get_event_loop()
        log.info("Mic started")
        asst.ui.write_log(f'<span style="color:#00d4ff">Mic starting...</span>')

        # Pre-bind TTS RMS getter so the real-time callback never does an
        # import or raises an ImportError on the audio thread.
        try:
            from voice import tts_engine as _tts_mod_cb
            _get_tts_rms = _tts_mod_cb.get_speaker_rms
        except Exception:
            _get_tts_rms = lambda: 0.0  # noqa: E731

        def callback(indata, frames, time_info, status):
            # --- WebRTC AEC: echo-cancel before any downstream consumer ---
            # Apply AEC3 + NS + AGC to the raw mic frame.  Both the
            # processor (asst._aec) and numpy (_np_audio) are pre-fetched
            # above so no import overhead occurs on the real-time thread.
            # process() is a sub-millisecond no-op passthrough when AEC
            # is unavailable.
            raw_pcm = indata.reshape(-1).view(_np_audio.int16)
            if asst._aec is not None:
                try:
                    clean_pcm = asst._aec.process(raw_pcm)
                except Exception:
                    clean_pcm = raw_pcm  # passthrough on unexpected error
            else:
                clean_pcm = raw_pcm
            data = clean_pcm.tobytes()

            # --- Rolling buffer for voice verification (cheap, local) ---
            # Uses AEC-cleaned audio so speaker embeddings aren't
            # contaminated by Gama's own TTS playback.
            _vbl = getattr(asst, "_voice_buffer_lock", None)
            if _vbl is not None:
                with _vbl:
                    asst._voice_buffer.extend(data)
                    if len(asst._voice_buffer) > asst._voice_buffer_max_bytes:
                        del asst._voice_buffer[:len(asst._voice_buffer) - asst._voice_buffer_max_bytes]
            else:
                asst._voice_buffer.extend(data)
                if len(asst._voice_buffer) > asst._voice_buffer_max_bytes:
                    del asst._voice_buffer[:len(asst._voice_buffer) - asst._voice_buffer_max_bytes]

            # --- Prosody tracking (feature 4: emotional/tone detection) ---
            # O(n) numpy stats only — see voice/emotion_detector.py. Never
            # blocks or raises into this real-time callback.
            # Fast mode (thin_mic_callback): skip — saves work every chunk.
            _thin = False
            try:
                _p = getattr(asst, "_perf", None)
                if _p is not None and getattr(_p, "thin_mic_callback", False):
                    _thin = True
            except Exception:
                pass
            if not _thin and asst._emotion_detector is not None:
                try:
                    asst._emotion_detector.feed_frame(clean_pcm)
                except Exception:
                    pass

            # Live mic amplitude → React waveform (noise-gated).
            # Background room noise must not drive the HUD; only real speech.
            try:
                _arr = clean_pcm.astype(_np_audio.float32)
                _mic_rms = float((_arr * _arr).mean() ** 0.5) / 32768.0
                asst._last_mic_rms = _mic_rms
                # Adaptive-ish noise floor: ignore quiet ambient (typical
                # room noise sits well below ~0.02–0.04 after AEC).
                _NOISE_FLOOR = 0.035
                _mic_lvl = max(0.0, _mic_rms - _NOISE_FLOOR) / (0.22)  # speech peak ~0.25
                _mic_lvl = min(1.0, _mic_lvl)
                # Soft knee: suppress residual noise further
                if _mic_lvl < 0.08:
                    _mic_lvl = 0.0
                # Track human speech energy so Active Window does not expire mid-utterance.
                # When Gama is speaking, only count clearly louder mic energy (barge-in).
                try:
                    _speaking_now = bool(getattr(asst, "_speaking", False))
                    if _mic_lvl >= 0.12 and (not _speaking_now or _mic_lvl >= 0.28):
                        asst._last_user_voice_ts = time.monotonic()
                except Exception:
                    pass
                global _push_amplitude
                if _push_amplitude is None:
                    from core.web_bridge import push_amplitude as _pa
                    _push_amplitude = _pa
                if getattr(asst, "_speaking", False):
                    _tts = float(getattr(asst, "_gemini_speaker_rms", 0.0) or 0.0)
                    _tts_lvl = min(1.0, max(0.0, _tts - 0.01) * 2.8)
                    _push_amplitude(max(_tts_lvl, _mic_lvl * 0.85))
                else:
                    _push_amplitude(_mic_lvl)
            except Exception:
                pass

            # --- Local wake detection (always runs once loaded, cheap) ---
            if asst._wake_listener is not None and asst._wake_listener.available:
                label = asst._wake_listener.feed(data)
                if label:
                    loop.call_soon_threadsafe(asst._on_wake_engine_label, label)

            with asst._speaking_lock:
                gama_speaking = asst._speaking

            # Only forward audio to Gemini once locally awake AND speaker-
            # verified. _wake_verifying stays True between "wake word heard"
            # and "verification done" so a false-positive wake (e.g. Hindi
            # phonemes matching the wake phrase) never sends user audio to
            # Gemini before we know who is speaking.
            # Layer 2 (post-speech gate): keep mic → Gemini closed until
            # room reverb has decayed after Gama finishes speaking.
            # Read silence timestamp under the same lock used in
            # _set_speaking so we never see stale data after a transition.
            with asst._speaking_lock:
                _since_silence = time.monotonic() - asst._last_speaking_end_ts
            _post_speech_ok = _since_silence >= POST_SPEECH_GATE_SECONDS
            # Only forward to Gemini when a live session actually exists —
            # in offline mode (asst.session is None / out_queue not yet
            # created) this must no-op rather than crash, since the mic
            # now runs independently of the Gemini connection lifecycle.
            # Stream to Gemini in OBSERVE + ACTIVE (silent understanding in
            # Observe). DEEP_SLEEP stops streaming entirely. Speaking is
            # gated separately via runtime.may_speak in _receive_audio.
            _rt = getattr(asst, "_runtime", None)
            _may_stream = True
            if _rt is not None:
                _may_stream = _rt.may_stream_to_gemini
            # Full-duplex when barge-in/interruption is ON: stream mic audio
            # even while Gama is speaking so Gemini can detect user speech
            # and we flush playback immediately.
            # When barge-in is OFF: do NOT listen while Gama is speaking —
            # drop mic frames until speech ends (half-duplex).
            # Keep security gates (wake verify, announcing-while-asleep,
            # muted, enrolling) and runtime stream permission.
            # Flag lives on the assistant (not this controller).
            _barge_ok = bool(getattr(asst, "_barge_in_enabled", True))
            # Fast continuous-listen: while awake, do not block streaming on
            # wake_verifying (stay conversational once active).
            _wake_block = asst._wake_verifying
            try:
                _p = getattr(asst, "_perf", None)
                if (
                    _p is not None
                    and getattr(_p, "continuous_listen_when_awake", False)
                    and asst._awake
                ):
                    _wake_block = False
            except Exception:
                pass
            if (asst.session is not None and asst.out_queue is not None
                    and _may_stream
                    and (asst._awake or (_rt is not None and _rt.is_observe))
                    and not _wake_block
                    and not asst._announcing_while_asleep
                    and not asst.ui._canvas.muted
                    and not asst._enrolling and _post_speech_ok
                    and (_barge_ok or not gama_speaking)):
                def _safe_put(item):
                    try:
                        asst.out_queue.put_nowait(item)
                    except Exception:
                        pass  # queue full — drop this chunk rather than crash

                try:
                    loop.call_soon_threadsafe(_safe_put, {"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"})
                except Exception:
                    pass  # loop already closed/shutting down

        # ── Device hot-swap restart loop ─────────────────────────────────────
        # _mic_restart_event is set by device_monitor when the default input
        # device changes.  We break out of the inner wait loop, which closes
        # the current sd.InputStream cleanly, then immediately reopen a new
        # one on the updated default device — no application restart needed.
        _chunk = _resolve_chunk_size()
        log.info(f"Mic capture blocksize={_chunk} samples")
        while asst._running:
            try:
                with sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=_chunk,
                    latency="low",
                    callback=callback,
                ):
                    log.info("🎤 Mic stream open")
                    asst.ui.write_log(
                        f'<span style="color:#00ff88">Mic live — speak now!</span>'
                    )
                    while asst._running:
                        if asst._mic_restart_event.is_set():
                            asst._mic_restart_event.clear()
                            log.info("[mic] Default input device changed — restarting stream.")
                            asst.ui.write_log(
                                '<span style="color:#5ab8cc">'
                                '🎤 Mic device changed — reconnecting…</span>'
                            )
                            # Reset AEC: the new mic may have different
                            # latency characteristics; stale state would
                            # produce bad echo cancellation.
                            if asst._aec is not None:
                                try:
                                    asst._aec.reset()
                                except Exception:
                                    pass
                            break   # exits inner loop → closes InputStream
                        await asyncio.sleep(0.1)
            except Exception as exc:
                log.error(f"❌ Mic: {exc}")
                asst.ui.write_log(
                    f'<span style="color:#ff3355">Mic error: {exc} — retrying…</span>'
                )
                # Brief pause so we don't spin-loop if the device is
                # temporarily unavailable (e.g. mid-unplug of a USB mic).
                await asyncio.sleep(2.0)
            if not asst._running:
                break

    # ---------------------------------------------------------------
    # Local wake word / interrupt handling
    # ---------------------------------------------------------------

    async def _send_realtime(self):
        """Pull audio from out_queue and send to Gemini.

        NOTE: `send_realtime_input(media=...)` sends the audio wrapped in
        the legacy `realtime_input.media_chunks` field, which newer Live
        models (Gemini 3.1 Flash Live) reject outright — the server closes
        the socket with 1007 "media_chunks is deprecated. Use audio,
        video, or text instead." We already build `msg` as a Blob-shaped
        dict ({"data": ..., "mime_type": "audio/pcm;rate=..."}), so we just
        need to route it through the `audio=` kwarg instead, which the SDK
        sends as `realtime_input.audio` — the non-deprecated field.
        """
        asst = self._asst
        while asst._running:
            try:
                msg = await asst.out_queue.get()
                if asst.session is None:
                    # Session already torn down — stop this task so TaskGroup
                    # can rebuild. Do not spam errors.
                    log.info("[send_realtime] session is None — exiting send loop.")
                    return
                await asst.session.send_realtime_input(audio=msg)
            except Exception as exc:
                err_s = str(exc).lower()
                _fatal = any(
                    m in err_s
                    for m in (
                        "1008", "1011", "aborted", "closed",
                        "policy violation", "connectionclosed",
                        "internal error", "websocket",
                    )
                )
                if _fatal:
                    log.warning(
                        f"[send_realtime] connection dead ({exc}) — "
                        "exiting send loop for reconnect."
                    )
                    # Raise so the TaskGroup / run() reconnect path wakes up
                    raise
                log.error(f"send_realtime: {exc}")
                await asyncio.sleep(0.1)

    async def _play_audio(self):
        """Play audio from audio_in_queue → speakers.
        Matches Mark XLVII exactly — uses asyncio.wait_for with timeout
        and _turn_done_event to know when Gama is done speaking.

        Device hot-swap: when the system default output device changes
        (e.g. Bluetooth connects) _output_device_changed is set by the
        device monitor callback. The inner loop detects it on the next 25 ms
        tick, exits cleanly, and this outer loop reopens a fresh
        RawOutputStream on the new device — so Gemini audio always follows
        the same device the OS and every other app use."""
        asst = self._asst
        import sounddevice as sd
        log.info("Play started")

        while asst._running:
            # Clear the flag BEFORE opening the stream so any change that
            # fires while we're constructing the new stream is not missed.
            asst._output_device_changed.clear()

            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=256,          # 256 @ 24 kHz = ~10 ms — lower output latency
                latency="low",
            )
            stream.start()
            log.info(f"RawOutputStream started ({RECEIVE_SAMPLE_RATE}Hz)")
            with asst._live_out_stream_lock:
                asst._live_out_stream = stream

            try:
                while asst._running and not asst._output_device_changed.is_set():
                    try:
                        chunk = await asyncio.wait_for(
                            asst.audio_in_queue.get(),
                            timeout=0.025,   # 25 ms poll — faster turn-end detection
                        )
                    except asyncio.TimeoutError:
                        # No audio for 25 ms — if turn is done and queue empty,
                        # Gama is finished speaking
                        if (
                            asst._turn_done_event
                            and asst._turn_done_event.is_set()
                            and asst.audio_in_queue.empty()
                        ):
                            asst._set_speaking(False)
                            asst._turn_done_event.clear()
                            if asst._shutdown_pending:
                                log.info("Shutdown pending: quitting application.")
                                asst.stop()
                                try:
                                    from PySide6.QtWidgets import QApplication
                                    from PySide6.QtCore import QMetaObject, Qt
                                    app = QApplication.instance()
                                    if app:
                                        QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)
                                except Exception:
                                    # Headless / no Qt — process will exit via asst.stop()
                                    pass
                        continue

                    asst._set_speaking(True)
                    # Track live speaker RMS for adaptive barge-in (Layer 1).
                    try:
                        import numpy as _np_play
                        _carr = _np_play.frombuffer(chunk, dtype=_np_play.int16)
                        asst._gemini_speaker_rms = float(
                            _np_play.sqrt(_np_play.mean(_carr.astype(_np_play.float32) ** 2))
                        ) / 32768.0
                        # Live speech level → React waveform (gentle scale)
                        try:
                            from core.web_bridge import push_amplitude
                            _raw = float(asst._gemini_speaker_rms or 0.0)
                            _lvl = min(1.0, max(0.0, _raw - 0.01) * 2.8)
                            push_amplitude(_lvl)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            asst._audio_out_executor, stream.write, chunk)
                    except Exception as exc:
                        log.debug(f"[play] stream.write error (device changed?): {exc}")
                        break  # exit inner loop; outer loop reopens stream

            except Exception as exc:
                log.error(f"❌ Play: {exc}")
            finally:
                asst._set_speaking(False)
                with asst._live_out_stream_lock:
                    if asst._live_out_stream is stream:
                        asst._live_out_stream = None
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

            if asst._output_device_changed.is_set() and asst._running:
                log.info("[_play_audio] Output device changed — reopening audio stream.")
                await asyncio.sleep(0.15)  # brief settle so the new device is ready


    def _set_speaking(self, value: bool, interrupted: bool = False):
        asst = self._asst
        with asst._speaking_lock:
            prev = asst._speaking
            asst._speaking = value
            if prev and not value:
                # Stamp silence time INSIDE the lock so the mic callback
                # always sees _speaking=False together with a current
                # silence timestamp — never an old stamp with the new state.
                # This keeps Layer 2 (post-speech gate) atomic.
                asst._last_speaking_end_ts = time.monotonic()
                asst._gemini_speaker_rms = 0.0
        asst.ui.set_speaking(value)
        # Mirror speaking state to the React HUD (web bridge).
        try:
            from core.web_bridge import push_state
            push_state(speaking=bool(value), awake=bool(getattr(asst, "_awake", False)))
        except Exception:
            pass
        # Every completed speech turn in ACTIVE resets the Active Window.
        if prev and not value and asst._awake:
            try:
                asst._runtime.on_interaction("speech finished")
            except Exception:
                pass

        # Gama 2.0: publish SpeechStarted/SpeechCompleted/SpeechInterrupted
        # on the shared Event Bus (spec: "Event Bus Integration"). This is
        # purely additive bookkeeping — it does NOT drive the existing
        # adaptive-RMS barge-in gate above (that logic is unchanged), it
        # just makes the transition visible to anything else that
        # subscribes (execution_narrator, a future debug panel, etc.).
        if prev != value:
            try:
                coord = getattr(asst, "_audio_coordinator", None)
                if coord is not None:
                    if value:
                        coord.begin_speaking()
                    else:
                        coord.end_speaking(interrupted=interrupted)
            except Exception:
                log.debug("AudioCoordinator speaking-state hook failed (non-fatal)", exc_info=True)

        if value:
            asst.ui.set_state("SPEAKING")
            # GAMA started speaking — pause silence timer; do not expire mid-sentence
            if asst._loop:
                asst._loop.call_soon_threadsafe(asst._cancel_auto_sleep)
            try:
                if getattr(asst, "_runtime", None) is not None:
                    asst._runtime.pause_active_deadline()
            except Exception:
                pass
        else:
            if asst._awake:
                # Idle-and-armed → LISTENING so HUD matches input transcription
                # (READY/WAITING were confusing when the mic is open).
                asst.ui.set_state("LISTENING")
                # Silence resumed — restart silence-only Active Window countdown
                try:
                    if getattr(asst, "_runtime", None) is not None:
                        asst._runtime.resume_active_deadline()
                except Exception:
                    pass
                if asst._loop and not asst._announcing_while_asleep:
                    asst._loop.call_soon_threadsafe(asst._schedule_auto_sleep)
            else:
                # Not ACTIVE. Trailing audio drain must not flash the wrong
                # UI state: OBSERVE stays IDLE; only DEEP_SLEEP is SLEEPING.
                try:
                    rt = getattr(asst, "_runtime", None)
                    if rt is not None and rt.is_deep_sleep:
                        asst.ui.set_state("SLEEPING")
                    else:
                        asst.ui.set_state("IDLE")
                except Exception:
                    asst.ui.set_state("IDLE")

    # ── Conversation timeout helpers (run on the asyncio loop) ───────────────


__all__ = ["AudioStreamController"]
