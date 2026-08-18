"""
voice/vad.py — Silero VAD (ONNX Runtime)
==========================================
Voice-activity detection gate that sits in front of the rest of the
pipeline. Nothing downstream (Whisper, WeSpeaker) should ever run on
silence — this module is the single source of truth for "is someone
speaking right now".

Uses the official Silero VAD ONNX export (`silero_vad.onnx`, ~1.8 MB)
via onnxruntime. No PyTorch. Loaded once as a process-wide singleton
and reused for the life of the app — never re-instantiate per call.

Model file:
    models/vad/silero_vad.onnx
    Get it with: python scripts/download_models.py --vad
    (see MODELS.md for manual download / conversion instructions)

Design notes:
    - Silero VAD is stateful (it keeps an internal LSTM/GRU state
      across chunks for temporal context), so this wrapper keeps that
      state alive between `process_chunk()` calls and only resets it
      via `reset()` at utterance boundaries. Sharing one session
      across calls but resetting state per-utterance is what keeps
      this both fast (no reload) and correct (no cross-utterance
      state bleed).
    - Chunk size is fixed by the model: 512 samples @16kHz (32ms) or
      256 @8kHz. We standardize on 16kHz/512 everywhere in Gama.
    - This is a *gate*, not a segmenter: `SpeechSegmenter` below turns
      a stream of per-chunk probabilities into "utterance started" /
      "utterance ended" events with hangover padding, which is what
      the rest of the pipeline actually consumes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "vad" / "silero_vad.onnx"

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # Silero VAD's fixed window size at 16kHz (32ms)
CHUNK_MS = (CHUNK_SAMPLES / SAMPLE_RATE) * 1000.0

DEFAULT_THRESHOLD = 0.5          # per-chunk speech probability cutoff
DEFAULT_MIN_SPEECH_MS = 150      # ignore blips shorter than this
DEFAULT_HANGOVER_MS = 500        # keep "speaking" this long after last positive chunk
DEFAULT_MAX_SILENCE_LEAD_MS = 6000  # give up waiting for speech after this long


class VadUnavailable(RuntimeError):
    """Raised when the ONNX model file / onnxruntime isn't available.
    Callers should catch this and fail safe (e.g. fall back to an
    energy-based trim, or surface a clear setup error) rather than
    silently skipping voice-activity gating."""


class _SileroSession:
    """Process-wide singleton around one onnxruntime InferenceSession.
    Thread-safe: the session itself is stateless per call except for
    the explicit (h, c) state arrays we pass in/out ourselves, so
    multiple logical VAD "instances" (see SileroVAD below) can safely
    share the same underlying session concurrently.
    """

    _instance: "Optional[_SileroSession]" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.available = False
        self.session = None
        self._load()

    def _load(self) -> None:
        if not MODEL_PATH.exists():
            log.warning(
                f"Silero VAD model not found at {MODEL_PATH}. "
                f"Run scripts/download_models.py --vad (see MODELS.md)."
            )
            return
        try:
            import onnxruntime as ort
        except Exception as exc:
            log.error(f"onnxruntime not installed ({exc}); VAD unavailable.")
            return

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_mem_pattern = True
        so.enable_mem_reuse = True
        so.log_severity_level = 3  # errors only — no noisy ORT logging
        so.intra_op_num_threads = 1  # VAD is tiny; extra threads add overhead, not speed

        try:
            self.session = ort.InferenceSession(
                str(MODEL_PATH), sess_options=so, providers=["CPUExecutionProvider"]
            )
            self.available = True
            log.info("Silero VAD (ONNX) loaded.")
        except Exception as exc:
            log.error(f"Failed to load Silero VAD ONNX model: {exc}")

    @classmethod
    def instance(cls) -> "_SileroSession":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


class SileroVAD:
    """Lightweight per-stream VAD state. Cheap to construct — create
    one per active microphone stream / conversation; it borrows the
    shared ONNX session but keeps its own recurrent state so multiple
    concurrent streams never interfere with each other."""

    def __init__(self, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._sess = _SileroSession.instance()
        self.threshold = threshold
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    @property
    def available(self) -> bool:
        return self._sess.available

    def reset(self) -> None:
        self._h[:] = 0.0
        self._c[:] = 0.0

    def process_chunk(self, chunk_int16: np.ndarray) -> float:
        """Returns speech probability (0..1) for one 512-sample chunk.
        Target latency: <20ms (see MODELS.md performance notes)."""
        if not self._sess.available:
            raise VadUnavailable("Silero VAD model/session not loaded.")
        if chunk_int16.size != CHUNK_SAMPLES:
            padded = np.zeros(CHUNK_SAMPLES, dtype=np.int16)
            n = min(CHUNK_SAMPLES, chunk_int16.size)
            padded[:n] = chunk_int16[:n]
            chunk_int16 = padded

        audio = (chunk_int16.astype(np.float32) / 32768.0)[None, :]
        inputs = {
            "input": audio,
            "h": self._h,
            "c": self._c,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
        }
        names = {i.name for i in self._sess.session.get_inputs()}
        # Older Silero ONNX exports use a single "state" tensor instead
        # of separate h/c — support both without branching call sites.
        if "state" in names and "h" not in names:
            if not hasattr(self, "_state"):
                self._state = np.zeros((2, 1, 128), dtype=np.float32)
            inputs = {"input": audio, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
            out, new_state = self._sess.session.run(None, inputs)
            self._state = new_state
            return float(out.squeeze())

        out, new_h, new_c = self._sess.session.run(None, inputs)
        self._h, self._c = new_h, new_c
        return float(out.squeeze())


@dataclass
class SpeechSegment:
    started_at: float
    ended_at: float
    audio: np.ndarray  # concatenated int16 PCM for the whole utterance


class SpeechSegmenter:
    """Turns a stream of raw PCM chunks into discrete speech segments.

    Feed it audio with `feed()`. It emits nothing until speech starts,
    then buffers until `hangover_ms` of consecutive silence confirms
    the utterance ended, then hands the whole utterance to
    `on_segment(SpeechSegment)`. This is what the pipeline hands off
    to Whisper + WeSpeaker for parallel processing — one buffer,
    reused for both.
    """

    def __init__(
        self,
        on_segment: Callable[[SpeechSegment], None],
        threshold: float = DEFAULT_THRESHOLD,
        min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
        hangover_ms: int = DEFAULT_HANGOVER_MS,
    ) -> None:
        self._vad = SileroVAD(threshold=threshold)
        self._on_segment = on_segment
        self._min_speech_ms = min_speech_ms
        self._hangover_ms = hangover_ms
        self._speaking = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._buf: list[np.ndarray] = []
        self._start_ts = 0.0
        self._leftover = np.zeros(0, dtype=np.int16)

    def reset(self) -> None:
        self._vad.reset()
        self._speaking = False
        self._speech_ms = self._silence_ms = 0.0
        self._buf.clear()
        self._leftover = np.zeros(0, dtype=np.int16)

    def feed(self, pcm_int16: np.ndarray) -> None:
        """Feed an arbitrary-length int16 chunk; internally re-chunked
        to Silero's fixed 512-sample window."""
        combined = np.concatenate([self._leftover, pcm_int16]) if self._leftover.size else pcm_int16
        n_chunks = combined.size // CHUNK_SAMPLES
        for i in range(n_chunks):
            chunk = combined[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES]
            self._feed_chunk(chunk)
        rem = n_chunks * CHUNK_SAMPLES
        self._leftover = combined[rem:]

    def _feed_chunk(self, chunk: np.ndarray) -> None:
        prob = self._vad.process_chunk(chunk)
        is_speech = prob >= self._vad.threshold

        if is_speech:
            if not self._speaking:
                self._speaking = True
                self._speech_ms = 0.0
                self._buf = []
                self._start_ts = time.monotonic()
            self._speech_ms += CHUNK_MS
            self._silence_ms = 0.0
            self._buf.append(chunk)
        elif self._speaking:
            self._silence_ms += CHUNK_MS
            self._buf.append(chunk)  # keep short trailing silence for natural endpointing
            if self._silence_ms >= self._hangover_ms:
                if self._speech_ms >= self._min_speech_ms:
                    audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.int16)
                    self._on_segment(SpeechSegment(self._start_ts, time.monotonic(), audio))
                self._speaking = False
                self._buf = []
                self._speech_ms = self._silence_ms = 0.0

    @property
    def is_speaking(self) -> bool:
        return self._speaking


__all__ = [
    "SileroVAD", "SpeechSegmenter", "SpeechSegment", "VadUnavailable",
    "SAMPLE_RATE", "CHUNK_SAMPLES", "CHUNK_MS",
    "DEFAULT_THRESHOLD", "DEFAULT_MIN_SPEECH_MS", "DEFAULT_HANGOVER_MS",
]
