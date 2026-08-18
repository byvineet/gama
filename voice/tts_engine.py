"""
voice/tts_engine.py — GAMA's voice model (Gemini native TTS, single backend)
=======================================================================
GAMA speaks with a single voice backend: Gemini's native text-to-speech
model (gemini-2.5-flash-preview-tts), called via a plain (non-Live)
`generate_content` request with response_modalities=["AUDIO"]:
  • Voice model  — a genuine Gemini neural TTS voice. Picked via the
    TTS_VOICE env var (default "Charon" — see Gemini API docs for the
    full prebuilt-voice catalog).
  • Requires the same Gemini API key already used for Live/routing, and
    requires internet — there is no offline fallback. If the request
    fails (no internet, no key, API error), GAMA is silent for that
    utterance rather than falling back to a different engine; the
    failure is logged and the next utterance retries normally.

Concurrency
-----------
A single dedicated worker thread owns synthesis + the sounddevice
stream — both are not safely reentrant. A Python queue serialises requests.
stop() drains the queue and calls sd.stop() to cut current playback
immediately.

Echo Guard Integration
----------------------
Every call to speak_exact() notifies voice.echo_guard so the mic pipeline
knows Gama is speaking and can suppress its own TTS from entering the
intent pipeline.  The guard is called in the worker thread (on_tts_start
before sd.play, on_tts_end after sd.wait) so the state is always accurate.

Device Hot-Swap
---------------
voice.device_monitor watches the default output device; when it changes,
it calls sd.stop() which causes sd.wait() to return immediately, ending
the current playback.  The next queued item is then synthesised and played
through the new default device automatically (sd.play() always uses the
current default at the moment of the call).
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Callable, Optional

import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)

# ── Voice model configuration ───────────────────────────────────────────────
# TTS_VOICE / TTS_MODEL come from .env.
_GEMINI_TTS_VOICE = os.environ.get("TTS_VOICE", "Charon").strip() or "Charon"
_GEMINI_TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-2.5-flash-preview-tts").strip() or "gemini-2.5-flash-preview-tts"
_TTS_SAMPLE_RATE = 24000  # Gemini TTS always returns 24kHz 16-bit mono PCM

# ── Internal queue sentinel ───────────────────────────────────────────────────
_STOP_SENTINEL = object()

# ── Module state ──────────────────────────────────────────────────────────────
_speak_queue: queue.Queue = queue.Queue()
_worker: Optional[threading.Thread] = None
_stop_flag   = threading.Event()
_initialized = False
_init_lock   = threading.Lock()

# ── Generation counter ──────────────────────────────────────────────────────
# Gemini TTS synthesis is a single blocking network call (no streaming, no
# cancellable request) that can take hundreds of ms to a few seconds. Without
# this counter, a barge-in mid-synthesis would have to wait for that network
# round-trip to finish before the worker could return to listening — a real
# violation of the 100-300ms interruption budget. stop() bumps `_generation`;
# the worker thread compares its captured generation after the network call
# returns and silently discards a stale response instead of playing it or
# blocking further utterances behind it.
_generation = 0
_generation_lock = threading.Lock()


def _bump_generation() -> int:
    global _generation
    with _generation_lock:
        _generation += 1
        return _generation


def _current_generation() -> int:
    with _generation_lock:
        return _generation

# ── Output device tracking ─────────────────────────────────────────────────────
# When the system default output device changes (e.g. Bluetooth connects),
# the device monitor fires _on_output_device_changed which resolves the new
# device index and stores it here. _synthesize_and_play passes it explicitly to
# sd.play() so PortAudio doesn't use a stale cached device mapping.
_current_output_device: Optional[int] = None   # None → let sounddevice pick
_device_lock = threading.Lock()

# ── Speaker RMS tracking (adaptive barge-in in main.py) ──────────────────────
_speaker_rms: float = 0.0
_speaker_rms_lock = threading.Lock()


def get_speaker_rms() -> float:
    """RMS amplitude (0.0–1.0) of Gemini TTS currently being played.
    Returns 0.0 when nothing is playing.  Thread-safe."""
    with _speaker_rms_lock:
        return _speaker_rms


def _set_speaker_rms(val: float) -> None:
    global _speaker_rms
    with _speaker_rms_lock:
        _speaker_rms = val


# ── Speaking-rate scale (wired to emotion detector) ───────────────────────────
# 1.0 = normal speed. >1.0 = slower (e.g. 1.15 for tired user). <1.0 = faster.
# Historical note: this used to feed Piper's length_scale directly;
# pyttsx3 fallback maps it to words-per-minute (baseline 170 wpm).
_speaking_rate_scale: float = 1.0
_rate_scale_lock = threading.Lock()


def set_speaking_rate_scale(scale: float) -> None:
    """Adjust TTS speaking rate.

    scale < 1.0  → faster speech  (e.g. 0.9 when user sounds rushed)
    scale = 1.0  → normal (default)
    scale > 1.0  → slower/softer  (e.g. 1.15 when user sounds tired)

    Takes effect on the next utterance.  Thread-safe, non-blocking.
    """
    global _speaking_rate_scale
    scale = max(0.5, min(2.0, float(scale)))  # clamp to sane range
    with _rate_scale_lock:
        _speaking_rate_scale = scale


def get_speaking_rate_scale() -> float:
    with _rate_scale_lock:
        return _speaking_rate_scale


# ── Echo guard helpers ────────────────────────────────────────────────────────

def _echo_on_start(*_a, **_k) -> None:
    """No-op — echo_guard removed."""
    return
def _echo_on_end(*_a, **_k) -> None:
    """No-op — echo_guard removed."""
    return
# ── Public API ────────────────────────────────────────────────────────────────

def warmup() -> None:
    """Start the TTS worker thread (voice model is synthesized fresh
    per-utterance, so there's nothing to preload here).

    Call once at startup, off the UI thread. Safe to call multiple times.
    """
    global _worker, _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True

    _worker = threading.Thread(target=_worker_loop, name="gama-tts-worker", daemon=True)
    _worker.start()

    # Register with device monitor so output-device changes interrupt the
    # current playback cleanly (next sd.play() will use the new device).
    try:
        from voice.device_monitor import get_monitor
        mon = get_monitor()
        mon.on_output_change(_on_output_device_changed)
        mon.start()
        log.debug("[tts] Registered with device monitor for hot-swap.")
    except Exception as exc:
        log.debug(f"[tts] Device monitor registration skipped: {exc}")


def speak_exact(text: str, *, kind: str = "generic",
                on_done: Optional[Callable[[], None]] = None) -> None:
    """Queue *text* for voice-model synthesis and playback.

    Returns immediately.  ``on_done`` fires after the OS audio buffer
    drains so blocking callers (speech_manager blocking=True) are safe.
    """
    if not text:
        if on_done is not None:
            threading.Thread(target=_fire, args=(on_done,), daemon=True).start()
        return
    # Defensive self-start: normally main.py calls warmup() once at startup,
    # but any caller that reaches speak_exact() before that (or from a code
    # path that skips the usual bootstrap, e.g. a script or a future
    # refactor) would otherwise queue silently forever with nothing
    # consuming _speak_queue — the caller sees no error, just a hang until
    # its own timeout. Mirrors the lazy-start pattern already used by
    # core/task_queue.py's _ensure_workers().
    if not _initialized:
        warmup()
    try:
        from voice import soundscape
        if kind == "result":
            soundscape.play_success()
    except Exception:
        pass
    _speak_queue.put_nowait((text, kind, on_done))


def stop(kind_filter: Optional[str] = None) -> None:
    """Stop current playback and discard pending items.

    ``kind_filter`` is accepted for API compatibility but ignored — there
    is a single audio stream with no per-kind queue.

    Also invalidates any synthesis request currently in flight (waiting
    on the Gemini TTS network call) so the worker doesn't sit blocked
    behind a response that's about to be thrown away — see
    ``_generation`` above.
    """
    _bump_generation()
    _drain_queue()
    _stop_flag.set()
    # Stop sounddevice stream immediately.
    try:
        import sounddevice as sd
        sd.stop()
    except Exception:
        pass
    # Always mark echo guard as ended so mic isn't left blocked.
    _echo_on_end()


# ── Device hot-swap ───────────────────────────────────────────────────────────

def _on_output_device_changed(evt) -> None:
    """Called by device_monitor when the default output device changes.

    1. Stops in-progress playback immediately (sd.wait() returns early).
    2. Resolves the new device's index so the NEXT sd.play() uses it
       explicitly — avoids PortAudio serving the old cached device even
       after Windows has switched the default (common with Bluetooth).
    """
    global _current_output_device
    log.info(
        f"[tts] Output device changed: {evt.old_name!r} → {evt.new_name!r} "
        "— interrupting current playback and switching device."
    )
    try:
        import sounddevice as sd
        sd.stop()   # causes sd.wait() in _synthesize_and_play to return
    except Exception:
        pass

    # Resolve the new device index by name so we don't rely on PortAudio's
    # potentially stale "default" mapping.
    try:
        import sounddevice as sd
        new_name_lower = (evt.new_name or "").lower()
        for i, dev in enumerate(sd.query_devices()):
            dev_name = dev.get("name", "").lower()
            if dev.get("max_output_channels", 0) > 0 and (
                dev_name == new_name_lower or new_name_lower in dev_name
            ):
                with _device_lock:
                    _current_output_device = i
                log.info(f"[tts] Gemini TTS will use device index {i}: {dev['name']!r}")
                return
        # No exact match — fall back to system default (None)
        with _device_lock:
            _current_output_device = None
        log.info("[tts] Device not found by name — using system default.")
    except Exception as exc:
        log.debug(f"[tts] Device index lookup failed: {exc}")
        with _device_lock:
            _current_output_device = None


# ── Internal ──────────────────────────────────────────────────────────────────

def _drain_queue() -> None:
    while True:
        try:
            item = _speak_queue.get_nowait()
            if item is _STOP_SENTINEL:
                continue
            _, _, on_done = item
            if on_done is not None:
                threading.Thread(target=_fire, args=(on_done,), daemon=True).start()
        except queue.Empty:
            break


def _worker_loop() -> None:
    """Worker thread: synthesizes each utterance with GAMA's Gemini TTS
    voice model and plays it via sounddevice. There is no offline
    fallback — if Gemini TTS can't be reached this turn, the utterance
    is logged and dropped rather than spoken through a different engine."""
    while True:
        try:
            item = _speak_queue.get()   # blocks
        except Exception:
            continue

        if item is _STOP_SENTINEL or item is None:
            continue

        text, kind, on_done = item
        log.debug(f"[tts] Speaking ({kind}): {text[:80]}")

        my_gen = _current_generation()
        result_holder: dict = {}

        def _do_synth_and_play():
            result_holder["ok"] = _synthesize_and_play_voice_model(text, my_gen)

        synth_thread = threading.Thread(
            target=_do_synth_and_play, name="gama-tts-synth", daemon=True
        )
        synth_thread.start()

        # Poll instead of a single blocking join(): the moment stop() bumps
        # the generation counter (e.g. from a barge-in), the worker gives up
        # waiting and moves straight to _fire(on_done) / the next queue item
        # rather than sitting blocked on the in-flight network call. The
        # synth thread itself checks the generation before ever touching
        # sd.play(), so a late response is discarded quietly.
        while synth_thread.is_alive():
            if _current_generation() != my_gen:
                break
            synth_thread.join(timeout=0.02)

        if synth_thread.is_alive():
            log.debug("[tts] Synthesis superseded by newer stop() — not waiting on network reply.")
        elif not result_holder.get("ok", False):
            log.warning(f"[tts/silent] Gemini TTS unreachable this turn — dropped: {text[:80]}")

        _fire(on_done)


# ── Voice model synthesis (Gemini native TTS) ─────────────────────────────

_genai_client = None
_genai_client_lock = threading.Lock()


def _get_genai_client():
    """Lazily build a dedicated google-genai client for one-shot TTS
    calls, using the same API key Live/routing already use. Cached after
    first successful creation; a failure (e.g. key missing) is retried
    on the next call rather than cached, in case config changes at
    runtime (e.g. the user adds a key after startup)."""
    global _genai_client
    with _genai_client_lock:
        if _genai_client is not None:
            return _genai_client
        try:
            from google import genai
            from core.config_manager import config as _cfg
            api_key = _cfg.gemini_key()
            if not api_key:
                return None
            _genai_client = genai.Client(
                api_key=api_key,
                http_options={"api_version": "v1beta"},
            )
            return _genai_client
        except Exception as exc:
            log.debug(f"[tts] Gemini client init failed: {exc}")
            return None


def _synth_voice_model(text: str) -> tuple:
    """Synthesize *text* with GAMA's Gemini TTS voice model. Returns
    (int16 PCM np.ndarray, sample_rate), or (None, 0) if the request
    couldn't be completed (e.g. no internet, no API key, API error)."""
    try:
        from google.genai import types

        client = _get_genai_client()
        if client is None:
            log.debug("[tts] No Gemini client available (missing API key?).")
            return None, 0

        # Gemini TTS has no numeric rate knob — it's controllable via a
        # natural-language style prefix instead (e.g. "Say slowly: ...").
        # Map the existing 0.5-2.0 speaking-rate scale onto that prefix so
        # callers that adjust rate (e.g. for a detected-tired user) still
        # have an effect.
        _scale = get_speaking_rate_scale()
        if _scale >= 1.15:
            _prompt = f"Say slowly and calmly: {text}"
        elif _scale <= 0.9:
            _prompt = f"Say quickly: {text}"
        else:
            _prompt = text

        response = client.models.generate_content(
            model=_GEMINI_TTS_MODEL,
            contents=_prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=_GEMINI_TTS_VOICE
                        )
                    )
                ),
            ),
        )

        pcm_bytes = response.candidates[0].content.parts[0].inline_data.data
        if not pcm_bytes:
            return None, 0

        data = np.frombuffer(pcm_bytes, dtype=np.int16)
        return data, _TTS_SAMPLE_RATE
    except Exception as exc:
        log.warning(f"[tts] Gemini TTS synthesis failed: {exc}")
        return None, 0


def _synthesize_and_play_voice_model(text: str, generation: Optional[int] = None) -> bool:
    """Synthesize *text* with the voice model and play via sounddevice.
    Returns True if it played (or genuinely attempted to), False if the
    voice model was unreachable and the caller should use the offline
    fallback instead.

    ``generation`` — if given, checked against the live generation
    counter right after the (blocking) network call returns and again
    just before sd.play(). A mismatch means stop() fired while this
    request was in flight; the response is discarded silently instead
    of being played over whatever the user barged in with.

    Echo guard on_tts_start is called before sd.play() and on_tts_end is
    called in the finally block — even if playback is interrupted by
    sd.stop() or a device change — so the mic pipeline is never left in
    a permanently-blocked state.
    """
    import sounddevice as sd  # type: ignore

    _stop_flag.clear()
    started = False
    try:
        audio, sample_rate = _synth_voice_model(text)
        if generation is not None and _current_generation() != generation:
            log.debug("[tts] Discarding synthesis result — superseded by a newer stop().")
            return True  # genuinely attempted — don't also fire the fallback
        if audio is None or len(audio) == 0:
            return False
        if _stop_flag.is_set():
            return True  # genuinely attempted — don't also fire the fallback

        _set_speaker_rms(
            float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) / 32768.0
        )

        # Feed AEC reverse stream BEFORE playback.
        try:
            from voice.aec import get_processor as _get_aec
            _aec = _get_aec()
            if _aec.available:
                _aec.feed_reverse(audio, sample_rate)
        except Exception as _aec_exc:
            log.debug(f"[tts] AEC reverse feed skipped: {_aec_exc}")

        if generation is not None and _current_generation() != generation:
            log.debug("[tts] Discarding synthesis result — superseded just before playback.")
            return True

        # Notify echo guard — BEFORE sd.play() so the mic is gated from
        # the very first sample that leaves the speaker.
        _echo_on_start(text)
        started = True

        with _device_lock:
            out_dev = _current_output_device   # None → system default
        sd.play(audio, samplerate=sample_rate, device=out_dev)
        sd.wait()
        return True

    except Exception as exc:
        log.warning(f"[tts] Voice-model playback error: {exc}")
        return False
    finally:
        _set_speaker_rms(0.0)
        _stop_flag.clear()
        # Always notify guard that TTS ended — even on error or interrupt.
        if started:
            _echo_on_end()


def _fire(on_done: Optional[Callable[[], None]]) -> None:
    if on_done is None:
        return
    try:
        on_done()
    except Exception:
        log.exception("[tts] on_done callback raised")


__all__ = ["speak_exact", "warmup", "stop", "get_speaker_rms"]
