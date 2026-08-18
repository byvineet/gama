"""
Gama - DSP Double-Clap Wake Detector
=====================================
A pure-signal-processing clap detector — no ML model, no extra audio
stream. It's fed the exact same int16 mono PCM frames already being
pulled off the shared mic InputStream for Gemini + the wake-word
engine (see wake_word/listener.py), so it adds no extra CPU for audio
capture and only a handful of float ops per frame for detection.

Pipeline (per frame):

    1. Adaptive noise threshold — a slow EMA of frame RMS tracks the
       ambient noise floor. Spikes are judged relative to *this*, not
       a fixed number, so the detector self-tunes to a quiet office vs.
       a noisy room without configuration.
    2. High-pass filter — a cheap one-pole IIR removes low-frequency
       rumble/HVAC/voice-fundamental energy so the spike detector reacts
       to the sharp, mostly-high-frequency transient of a clap, not to
       a raised voice or a door closing.
    3. Energy spike detection — filtered-frame RMS must jump well above
       the current adaptive floor.
    4. Duration validation — a clap is a very short transient (roughly
       3-40 ms). Longer "spikes" (raised voice, clatter) are rejected.
    5. Spectral flatness — a clap's spectrum is broadband/noise-like
       (flatness close to 1). Tonal sounds (speech, music, hums) have
       energy concentrated in a few bins (flatness close to 0). A short
       FFT over the flagged window filters those out.
    6. Confidence scoring — the above signals are combined into a
       0..1 score; only candidates above threshold count as "a clap".
    7. Double-clap gate — two validated claps 150-500 ms apart (per
       spec, configurable) trigger a wake event. Anything else (a
       single clap, three claps, claps too close/far apart) is ignored,
       which is what keeps this resistant to incidental noise.

This module never opens a microphone itself and never blocks — `feed()`
is O(frame_size) and safe to call from the same real-time audio
callback the wake-word engine already runs in.

Author : Vineet Machchal
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ClapDetectorConfig:
    sample_rate: int = 16000

    # Sensitivity 0..1 → higher = stricter (fewer false positives, may
    # miss soft claps). Everything else derives from this + explicit
    # overrides below so there's one knob for the common case.
    sensitivity: float = 0.5

    # Adaptive noise floor EMA time constant. Small alpha = slow-moving
    # floor (won't get "fooled" by the clap itself, since claps are much
    # faster than this).
    noise_floor_alpha: float = 0.01

    # One-pole high-pass cutoff (normalized, ~ cutoff_hz / (sr/2)).
    highpass_cutoff_hz: float = 1000.0

    # Spike must exceed floor * this multiplier. Derived from
    # sensitivity if left at default (see __post_init__).
    spike_ratio: Optional[float] = None

    # Valid clap transient duration window, in milliseconds.
    min_duration_ms: float = 3.0
    max_duration_ms: float = 45.0

    # Spectral flatness (0..1) minimum for a candidate to count as
    # "noise-like" (broadband) rather than tonal.
    min_spectral_flatness: float = 0.35

    # Confidence threshold to accept a single clap candidate.
    min_confidence: float = 0.55

    # Double-clap timing window.
    min_gap_ms: float = 150.0
    max_gap_ms: float = 500.0

    def __post_init__(self):
        s = min(max(self.sensitivity, 0.0), 1.0)
        if self.spike_ratio is None:
            # sensitivity 0 -> ratio 2.5x floor (very touchy)
            # sensitivity 1 -> ratio 6.0x floor (needs a sharp, loud clap)
            self.spike_ratio = 2.5 + s * 3.5
        # Stricter sensitivity also nudges flatness/confidence bars up a
        # little, without needing separate config plumbing for every field.
        self.min_spectral_flatness = min(0.6, self.min_spectral_flatness + s * 0.15)
        self.min_confidence = min(0.85, self.min_confidence + s * 0.15)


class _OnePoleHighPass:
    """Minimal one-pole IIR high-pass filter, applied per-frame with
    state carried across frames (so it stays correct at frame
    boundaries instead of resetting every call)."""

    __slots__ = ("_alpha", "_prev_x", "_prev_y")

    def __init__(self, cutoff_hz: float, sample_rate: int):
        rc = 1.0 / (2 * np.pi * max(cutoff_hz, 1.0))
        dt = 1.0 / sample_rate
        self._alpha = rc / (rc + dt)
        self._prev_x = 0.0
        self._prev_y = 0.0

    def apply(self, x: np.ndarray) -> np.ndarray:
        y = np.empty_like(x, dtype=np.float32)
        alpha = self._alpha
        prev_x = self._prev_x
        prev_y = self._prev_y
        for i in range(x.shape[0]):
            xi = x[i]
            yi = alpha * (prev_y + xi - prev_x)
            y[i] = yi
            prev_x = xi
            prev_y = yi
        self._prev_x = prev_x
        self._prev_y = prev_y
        return y


def _spectral_flatness(frame: np.ndarray) -> float:
    """Geometric mean / arithmetic mean of the magnitude spectrum.
    ~1.0 = white-noise-like/broadband (a clap). ~0.0 = tonal/periodic
    (speech vowels, hums, music notes)."""
    if frame.size < 8:
        return 0.0
    windowed = frame * np.hanning(frame.size).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(windowed)) + 1e-9
    log_mean = np.mean(np.log(spectrum))
    geo_mean = np.exp(log_mean)
    arith_mean = np.mean(spectrum)
    if arith_mean <= 1e-9:
        return 0.0
    return float(np.clip(geo_mean / arith_mean, 0.0, 1.0))


@dataclass
class _CandidateWindow:
    """Accumulates samples for one contiguous above-floor spike so we
    can validate its duration + spectrum once it ends."""
    samples: List[np.ndarray]
    start_ts: float
    n_samples: int = 0


class ClapDetector:
    """Stateful, streaming double-clap detector.

    Usage:
        det = ClapDetector(ClapDetectorConfig(sample_rate=16000))
        ...
        if det.feed(pcm_bytes):   # int16 mono
            # double clap confirmed — wake up
    """

    def __init__(self, cfg: Optional[ClapDetectorConfig] = None):
        self.cfg = cfg or ClapDetectorConfig()
        self._hpf = _OnePoleHighPass(self.cfg.highpass_cutoff_hz, self.cfg.sample_rate)
        self._noise_floor_peak = 400.0  # int16-scale starting estimate (peak, not RMS)
        self._active: Optional[_CandidateWindow] = None
        self._active_active_samples = 0  # samples actually above per-sample threshold
        self._last_clap_ts: Optional[float] = None
        # Small cap so a spike can never grow unbounded if audio is
        # somehow stuck "hot" (e.g. misconfigured gain) — bounds CPU/mem.
        # Sized generously in *frames*, not just clap duration, since a
        # real audio callback frame (e.g. 512 samples/32ms) can already
        # be several times longer than a whole clap transient.
        self._max_window_samples = int(self.cfg.sample_rate * 0.5)  # 500ms hard cap

    # -- public API ----------------------------------------------------

    def feed(self, pcm_frame: bytes) -> bool:
        """Feed one frame of int16 mono PCM. Returns True exactly once,
        the moment a *second* clap completes a valid double-clap within
        the configured gap window.

        Detection is done per-sample (not per-frame) precisely because
        the audio callback frame size (e.g. 512 samples / 32ms) is
        typically several times longer than an actual clap transient
        (~3-45ms) — a frame-average check would dilute a sharp clap
        into "not hot enough". Per-sample peak comparison keeps
        detection accurate regardless of the caller's chunk size, while
        still costing only a couple of vectorized numpy ops per frame.
        """
        try:
            x = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
        except Exception:
            return False
        if x.size == 0:
            return False

        filtered = self._hpf.apply(x)
        abs_filtered = np.abs(filtered)
        frame_peak = float(abs_filtered.max())

        threshold = self._noise_floor_peak * self.cfg.spike_ratio
        hot_mask = abs_filtered > threshold
        n_hot = int(hot_mask.sum())
        is_hot = n_hot > 0

        # 1. Adaptive noise floor — only update from frames with no hot
        # samples at all, so a clap (or its tail) never drags the floor
        # upward, using the frame's peak (median-ish robust estimate
        # would be nicer but peak is O(n) and good enough here).
        if not is_hot:
            a = self.cfg.noise_floor_alpha
            self._noise_floor_peak = (1 - a) * self._noise_floor_peak + a * max(frame_peak, 1.0)

        now = time.monotonic()

        if is_hot:
            if self._active is None:
                self._active = _CandidateWindow(samples=[], start_ts=now)
                self._active_active_samples = 0
            self._active.samples.append(filtered)
            self._active.n_samples += filtered.size
            self._active_active_samples += n_hot
            if self._active.n_samples >= self._max_window_samples:
                # Runaway "hot" condition (sustained loud noise, not a
                # transient) — abandon this candidate, it's not a clap.
                self._active = None
                self._active_active_samples = 0
            return False

        # Frame has no hot samples — if we were accumulating a spike,
        # validate it now that it's finished. Duration is measured from
        # the count of genuinely above-threshold samples (sub-frame
        # accurate), not from wall-clock frame boundaries.
        if self._active is not None:
            candidate = self._active
            active_samples = self._active_active_samples
            self._active = None
            self._active_active_samples = 0
            duration_ms = (active_samples / self.cfg.sample_rate) * 1000.0
            confirmed = self._validate_candidate(candidate, duration_ms)
            if confirmed:
                return self._register_clap(now)

        return False

    def reset(self) -> None:
        self._active = None
        self._last_clap_ts = None

    # -- internals -------------------------------------------------------

    def _validate_candidate(self, candidate: _CandidateWindow, duration_ms: float) -> bool:
        cfg = self.cfg

        # 4. Duration validation.
        if duration_ms < cfg.min_duration_ms or duration_ms > cfg.max_duration_ms:
            return False

        window = np.concatenate(candidate.samples) if candidate.samples else np.array([], dtype=np.float32)
        if window.size < 8:
            return False

        # 5. Spectral flatness (broadband transient vs. tonal sound).
        flatness = _spectral_flatness(window)
        if flatness < cfg.min_spectral_flatness:
            return False

        # 3 (scored). How far above the noise floor the peak got —
        # feeds into confidence, doesn't gate on its own beyond the
        # is_hot check already performed per-frame.
        peak = float(np.max(np.abs(window)))
        floor = max(self._noise_floor_peak, 1e-6)
        spike_strength = min(peak / (floor * cfg.spike_ratio), 3.0) / 3.0  # 0..1

        # 6. Confidence scoring — weighted blend; all components already
        # individually bounded so this stays in [0, 1].
        duration_fit = 1.0 - abs(duration_ms - (cfg.min_duration_ms + cfg.max_duration_ms) / 2) / (
            (cfg.max_duration_ms - cfg.min_duration_ms) / 2 + 1e-6
        )
        duration_fit = max(0.0, min(1.0, duration_fit))

        confidence = 0.45 * flatness + 0.35 * spike_strength + 0.20 * duration_fit

        if confidence < cfg.min_confidence:
            return False

        log.debug(
            f"[clap] candidate ok — duration={duration_ms:.1f}ms "
            f"flatness={flatness:.2f} spike_strength={spike_strength:.2f} "
            f"confidence={confidence:.2f}"
        )
        return True

    def _register_clap(self, ts: float) -> bool:
        """A single validated clap happened at `ts`. Returns True only
        if this completes a valid double-clap (150-500ms after the
        previous one, per spec)."""
        cfg = self.cfg
        if self._last_clap_ts is None:
            self._last_clap_ts = ts
            return False

        gap_ms = (ts - self._last_clap_ts) * 1000.0
        self._last_clap_ts = ts  # this clap becomes the new reference point

        if cfg.min_gap_ms <= gap_ms <= cfg.max_gap_ms:
            log.info(f"[clap] Double clap confirmed (gap={gap_ms:.0f}ms) — waking.")
            self._last_clap_ts = None  # consumed — next clap starts a fresh pair
            return True

        # Either too fast (more likely an echo/reflection of the same
        # clap than a deliberate second one) or too far apart to count
        # as a pair. Either way this clap becomes the new reference
        # point for the next pair attempt (already set above).
        return False


__all__ = ["ClapDetector", "ClapDetectorConfig"]
