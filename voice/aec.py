"""
voice/aec.py — Acoustic Echo Cancellation (AEC3 + NS + AGC)
============================================================
Wraps the `aec-audio-processing` Python package (primary) with a
fallback to `webrtc-audio-processing` (secondary) and a silent
passthrough when neither is installed.

Public API:

    processor = AudioProcessor(sample_rate=16000, channels=1)

    # Feed what the speakers are about to play (call before sd.play):
    processor.feed_reverse(tts_pcm_int16, tts_sample_rate)

    # Process mic audio — returns clean audio, same length:
    clean = processor.process(mic_pcm_int16)

    # Reset internal streaming state:
    processor.reset()

aec-audio-processing API notes
--------------------------------
- Class: ``aec_audio_processing.AudioProcessor``
- Init:  ``AudioProcessor(enable_aec, enable_ns, enable_agc, enable_vad)``
- Setup: ``ap.set_stream_format(sr_in, ch_in, sr_out, ch_out)``
         ``ap.set_reverse_stream_format(sr, ch)``
         ``ap.set_stream_delay(ms)``
- Reverse: ``ap.analyze_reverse_stream(bytes)``  ← feed before process
- Capture: ``ap.process_stream(bytes)`` → bytes   ← int16 PCM in/out
- VAD:     ``ap.has_voice()`` → bool

Streaming contract
------------------
``process()`` uses two internal queues (_in_buf / _out_buf) so that:
  - Output length == input length on every call.
  - Every sample is processed exactly once, in order.
  - No sample is ever returned both raw and processed.
  - Max latency: one 10 ms frame (160 samples at 16 kHz).

Graceful fallback
-----------------
If the package is absent or init fails, ``available`` is False and
``process()`` is a no-op passthrough.  Gama continues normally.

Install:
    pip install aec-audio-processing

Author : Vineet Machchal (AEC integration)
"""

from __future__ import annotations

import collections
import threading
from typing import Optional

import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_VALID_RATES: frozenset[int] = frozenset({8000, 16000, 32000, 48000})
_REVERSE_BUFFER_MAX_S: float = 2.0
_DEFAULT_STREAM_DELAY_MS: int = 50   # ms hint for AEC convergence


# ── Backend loaders ───────────────────────────────────────────────────────────

def _load_aec_audio_processing(sample_rate: int, channels: int,
                                enable_aec: bool, enable_ns: bool,
                                enable_agc: bool, delay_ms: int):
    """Try aec-audio-processing (primary backend)."""
    try:
        from aec_audio_processing import AudioProcessor as _AP  # noqa: PLC0415
    except ImportError:
        return None, "aec_audio_processing not installed"

    try:
        ap = _AP(
            enable_aec=enable_aec,
            enable_ns=enable_ns,
            enable_agc=enable_agc,
            enable_vad=True,
        )
        ap.set_stream_format(sample_rate, channels, sample_rate, channels)
        ap.set_reverse_stream_format(sample_rate, channels)
        ap.set_stream_delay(delay_ms)
        return ap, "aec"
    except Exception as exc:
        return None, f"aec_audio_processing init failed: {exc}"


def _load_webrtc_audio_processing(sample_rate: int, channels: int,
                                   enable_aec: bool, enable_ns: bool,
                                   enable_agc: bool):
    """Try webrtc-audio-processing (fallback backend)."""
    try:
        import webrtc_audio_processing as _wap  # noqa: PLC0415
    except ImportError:
        return None, "webrtc_audio_processing not installed"

    session = None
    try:
        cfg_cls = getattr(_wap, "Config", None)
        apm_cls = getattr(_wap, "AudioProcessingModule", None)
        if cfg_cls is not None and apm_cls is not None:
            cfg = cfg_cls()
            if enable_aec and hasattr(cfg, "echo_canceller"):
                cfg.echo_canceller.enabled = True
            if enable_ns and hasattr(cfg, "noise_suppression"):
                cfg.noise_suppression.enabled = True
                if hasattr(cfg.noise_suppression, "level"):
                    cfg.noise_suppression.level = 1
            if enable_agc:
                for attr in ("gain_controller1", "gain_controller"):
                    gc = getattr(cfg, attr, None)
                    if gc is not None:
                        gc.enabled = True
                        break
            session = apm_cls(config=cfg)
        elif apm_cls is not None:
            kwargs: dict = {}
            if enable_aec:
                kwargs["enable_aec"] = True
            if enable_ns:
                kwargs["enable_ns"] = True
            if enable_agc:
                kwargs["enable_agc"] = True
            session = apm_cls(**kwargs)
    except Exception as exc:
        return None, f"webrtc_audio_processing init failed: {exc}"

    if session is None or not (hasattr(session, "process_stream") and
                                hasattr(session, "process_reverse_stream")):
        return None, "webrtc_audio_processing: missing required methods"

    return session, "webrtc"


# ── Resampler ─────────────────────────────────────────────────────────────────

def _resample_linear(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Fast linear resampling of int16 PCM — no scipy, CPU-friendly."""
    if src_rate == dst_rate or pcm.size == 0:
        return pcm
    src_len = pcm.size
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    indices = np.linspace(0, src_len - 1, dst_len)
    lo = indices.astype(np.int64)
    hi = np.minimum(lo + 1, src_len - 1)
    frac = (indices - lo).astype(np.float32)
    out = (pcm[lo].astype(np.float32) * (1.0 - frac) +
           pcm[hi].astype(np.float32) * frac)
    return np.clip(out, -32768, 32767).astype(np.int16)


# ── Reverse-stream ring buffer ────────────────────────────────────────────────

class ReverseBuffer:
    """Thread-safe ring buffer for TTS reverse-stream PCM.

    Push at any sample rate; ``pop_frame()`` resamples to the APM rate
    and returns exactly ``frame_n`` int16 samples, zero-padding if empty.
    """

    def __init__(self, apm_rate: int, frame_n: int,
                 max_s: float = _REVERSE_BUFFER_MAX_S) -> None:
        self._apm_rate = apm_rate
        self._frame_n = frame_n
        self._max_samples = int(max_s * apm_rate)
        self._buf: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def push(self, pcm: np.ndarray, src_rate: int) -> None:
        resampled = _resample_linear(pcm, src_rate, self._apm_rate)
        with self._lock:
            self._buf.extend(resampled.tolist())
            excess = len(self._buf) - self._max_samples
            for _ in range(max(0, excess)):
                self._buf.popleft()

    def pop_frame(self) -> np.ndarray:
        frame = np.zeros(self._frame_n, dtype=np.int16)
        with self._lock:
            for i in range(min(self._frame_n, len(self._buf))):
                frame[i] = self._buf.popleft()
        return frame

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


# ── Main public class ─────────────────────────────────────────────────────────

class AudioProcessor:
    """AEC3 + Noise Suppression + AGC processor.

    Backed by ``aec-audio-processing`` (preferred) or
    ``webrtc-audio-processing`` (fallback).  Passthrough when neither
    is available so Gama is never blocked by a missing package.

    Usage::

        ap = AudioProcessor(sample_rate=16000, channels=1)
        ap.feed_reverse(tts_pcm_int16, tts_sample_rate)   # before sd.play
        clean = ap.process(mic_pcm_int16)                  # in mic callback
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        enable_aec: bool = True,
        enable_ns: bool = True,
        enable_agc: bool = True,
        stream_delay_ms: int = _DEFAULT_STREAM_DELAY_MS,
    ) -> None:
        if sample_rate not in _VALID_RATES:
            log.warning(f"[aec] {sample_rate} Hz unsupported; using 16000.")
            sample_rate = 16000

        self.sample_rate = sample_rate
        self.channels = channels
        self._delay_ms = stream_delay_ms
        self._frame_n = sample_rate // 100   # 10 ms = 160 samples @ 16 kHz

        self._lock = threading.Lock()        # protects APM session calls
        self._stream_lock = threading.Lock() # protects _in_buf / _out_buf
        self._in_buf = np.zeros(0, dtype=np.int16)
        self._out_buf = np.zeros(0, dtype=np.int16)

        self.reverse_buffer = ReverseBuffer(sample_rate, self._frame_n)

        self._session = None
        self._api_style = ""
        self.available = False

        log.info("Initializing AEC…")

        # Try aec-audio-processing first (has prebuilt Windows wheels)
        session, style = _load_aec_audio_processing(
            sample_rate, channels, enable_aec, enable_ns, enable_agc, stream_delay_ms
        )
        if session is None:
            log.debug(f"[aec] aec-audio-processing unavailable: {style}")
            # Fall back to webrtc-audio-processing
            session, style = _load_webrtc_audio_processing(
                sample_rate, channels, enable_aec, enable_ns, enable_agc
            )

        if session is not None:
            self._session = session
            self._api_style = style
            self.available = True
            log.info(
                f"[aec] Audio Processing ready — backend={style}  "
                f"AEC3={enable_aec}  NS={enable_ns}  AGC={enable_agc}  "
                f"rate={sample_rate} Hz  delay={stream_delay_ms} ms"
            )
        else:
            log.warning(
                f"[aec] Unavailable ({style}). "
                f"Running passthrough — no echo cancellation.\n"
                f"       Install with: pip install aec-audio-processing"
            )

    # ── frame processing ─────────────────────────────────────────────────────

    def _process_frame_aec(self, mic: np.ndarray, rev: np.ndarray) -> np.ndarray:
        """aec-audio-processing backend: bytes in, bytes out."""
        try:
            with self._lock:
                self._session.analyze_reverse_stream(rev.tobytes())
                out_bytes = self._session.process_stream(mic.tobytes())
            if out_bytes and len(out_bytes) == self._frame_n * 2:
                return np.frombuffer(out_bytes, dtype=np.int16).copy()
            return mic
        except Exception as exc:
            log.debug(f"[aec] aec frame error: {exc}")
            return mic

    def _process_frame_webrtc(self, mic: np.ndarray, rev: np.ndarray) -> np.ndarray:
        """webrtc-audio-processing backend: float32 in, float32 out."""
        mic_f = mic.astype(np.float32) / 32768.0
        rev_f = rev.astype(np.float32) / 32768.0
        try:
            with self._lock:
                try:
                    self._session.process_reverse_stream(
                        rev_f, sample_rate=self.sample_rate,
                        num_channels=self.channels)
                    if hasattr(self._session, "set_stream_delay_ms"):
                        self._session.set_stream_delay_ms(self._delay_ms)
                    out = self._session.process_stream(
                        mic_f, sample_rate=self.sample_rate,
                        num_channels=self.channels)
                except TypeError:
                    self._session.process_reverse_stream(rev_f)
                    out = self._session.process_stream(mic_f)
            if out is None:
                return mic
            out_f = np.asarray(out, dtype=np.float32).flatten()[: self._frame_n]
            return np.clip(out_f * 32768.0, -32768, 32767).astype(np.int16)
        except Exception as exc:
            log.debug(f"[aec] webrtc frame error: {exc}")
            return mic

    def _process_one_frame(self, mic: np.ndarray, rev: np.ndarray) -> np.ndarray:
        if self._api_style == "aec":
            return self._process_frame_aec(mic, rev)
        return self._process_frame_webrtc(mic, rev)

    # ── public API ────────────────────────────────────────────────────────────

    def process(self, mic_int16: np.ndarray) -> np.ndarray:
        """Apply AEC + NS + AGC to a mic chunk of any length.

        Returns processed int16 audio of exactly ``len(mic_int16)``
        samples.  Every sample is processed exactly once, in order.

        Passthrough when APM is unavailable.  Safe to call from the
        sounddevice real-time callback (O(n), typically < 0.5 ms per
        32 ms chunk at 16 kHz).
        """
        if not self.available or self._session is None:
            return mic_int16

        n_in = len(mic_int16)

        with self._stream_lock:
            # 1. Append input
            self._in_buf = (
                np.concatenate([self._in_buf, mic_int16])
                if self._in_buf.size else mic_int16.copy()
            )

            # 2. Drain complete 10 ms frames into output buffer
            while self._in_buf.size >= self._frame_n:
                frame = self._in_buf[: self._frame_n]
                self._in_buf = self._in_buf[self._frame_n:]
                rev = self.reverse_buffer.pop_frame()
                processed = self._process_one_frame(frame, rev)
                self._out_buf = (
                    np.concatenate([self._out_buf, processed])
                    if self._out_buf.size else processed
                )

            # 3. Return exactly n_in samples (zero-pad only on first frame)
            if self._out_buf.size >= n_in:
                out = self._out_buf[:n_in].copy()
                self._out_buf = self._out_buf[n_in:]
            else:
                pad = np.zeros(n_in - self._out_buf.size, dtype=np.int16)
                out = np.concatenate([self._out_buf, pad])
                self._out_buf = np.zeros(0, dtype=np.int16)

        return out

    def feed_reverse(self, pcm_int16: np.ndarray, src_sample_rate: int) -> None:
        """Push TTS PCM into the reverse stream buffer.

        Call *before* sd.play() with the exact PCM that will be sent to
        the speakers.  Any sample rate is accepted; resampling to the APM
        rate happens internally.  Thread-safe.
        """
        if not self.available or pcm_int16.size == 0:
            return
        self.reverse_buffer.push(pcm_int16, src_sample_rate)

    def reset(self) -> None:
        """Reset internal streaming buffers (not the APM adaptive filter)."""
        with self._stream_lock:
            self._in_buf = np.zeros(0, dtype=np.int16)
            self._out_buf = np.zeros(0, dtype=np.int16)


# ── Process-wide singleton ────────────────────────────────────────────────────

_processor: Optional[AudioProcessor] = None
_processor_lock = threading.Lock()


def get_processor(
    sample_rate: int = 16000,
    channels: int = 1,
    enable_aec: bool | None = None,
    enable_ns: bool | None = None,
    enable_agc: bool | None = None,
) -> "AudioProcessor":
    """Return (or create) the process-wide :class:`AudioProcessor` singleton.

    When enable_* is None, values come from ``utils.performance_mode.perf``
    (Fast mode forces all off for lower mic-path latency).
    """
    global _processor
    if _processor is None:
        with _processor_lock:
            if _processor is None:
                if enable_aec is None or enable_ns is None or enable_agc is None:
                    try:
                        from utils.performance_mode import perf as _perf
                        if enable_aec is None:
                            enable_aec = _perf.aec_enabled
                        if enable_ns is None:
                            enable_ns = _perf.ns_enabled
                        if enable_agc is None:
                            enable_agc = _perf.agc_enabled
                    except Exception:
                        enable_aec = True if enable_aec is None else enable_aec
                        enable_ns = True if enable_ns is None else enable_ns
                        enable_agc = True if enable_agc is None else enable_agc
                _processor = AudioProcessor(
                    sample_rate=sample_rate,
                    channels=channels,
                    enable_aec=bool(enable_aec),
                    enable_ns=bool(enable_ns),
                    enable_agc=bool(enable_agc),
                )
    return _processor


__all__ = ["AudioProcessor", "ReverseBuffer", "get_processor"]
