"""
Gama - Wake Word Listener
=========================
Thin wrapper around a WakeEngineBase that adds:

  * per-label cooldown (so one utterance doesn't fire 10 times while
    the recognizer catches up across several audio frames)
  * a frame-size sanity check against whatever engine is configured
  * a standalone CLI mode for testing the wake word in isolation,
    without booting the rest of GAMA:

      python -m wake_word.listener

This module deliberately does NOT open its own audio stream when used
from main.py — GamaAssistant feeds it frames from the mic InputStream
it already has open for Gemini, so there is exactly one audio device
in use at all times (lower CPU, no device-contention risk).

Author : Vineet Machchal
"""

from __future__ import annotations

import time
from typing import Optional

from utils.logger import get_logger
from .config import WakeWordConfig, load_wake_word_config
from .engine_factory import create_engine
from .engines.base import WakeEngineBase
from .clap_detector import ClapDetector, ClapDetectorConfig

log = get_logger(__name__)


class WakeWordListener:
    def __init__(self, cfg: Optional[WakeWordConfig] = None, engine: Optional[WakeEngineBase] = None):
        self.cfg = cfg or load_wake_word_config()
        self.engine: Optional[WakeEngineBase] = engine if engine is not None else create_engine(self.cfg)
        self._last_fire: dict[str, float] = {}
        self._warned_frame_size = False

        # DSP double-clap wake path — independent of, and never
        # interferes with, the wake-word engine or speaker verification.
        # It shares the exact same PCM frames fed below (no extra mic
        # stream), so it costs a handful of float ops per frame.
        self._clap_detector: Optional[ClapDetector] = None
        # Clap only fires while asleep/observing — ignored when already ACTIVE.
        self._clap_armed: bool = True
        if getattr(self.cfg, "clap_wake_enabled", True):
            try:
                self._clap_detector = ClapDetector(ClapDetectorConfig(
                    sample_rate=16000,
                    sensitivity=getattr(self.cfg, "clap_sensitivity", 0.5),
                    min_gap_ms=getattr(self.cfg, "clap_min_gap_ms", 150.0),
                    max_gap_ms=getattr(self.cfg, "clap_max_gap_ms", 500.0),
                ))
            except Exception as exc:
                log.warning(f"Clap detector disabled (init failed): {exc}")
                self._clap_detector = None

    def set_clap_armed(self, armed: bool) -> None:
        """Enable clap-wake only when Gama is asleep / observing."""
        self._clap_armed = bool(armed)

    @property
    def available(self) -> bool:
        return self.engine is not None or self._clap_detector is not None

    def feed(self, pcm_frame: bytes) -> Optional[str]:
        """Feed one frame of int16 mono PCM audio. Returns a debounced
        label ("wake", "stop", "cancel", "listen", ...) or None.

        The DSP clap detector runs first and independently of the
        wake-word engine's frame-size requirements — it doesn't need
        frame_length-matched chunks, so a mismatch that makes the wake
        engine skip a frame never disables clap-wake too.
        """
        label: Optional[str] = None

        # ── DSP double-clap path (only when armed / not already awake) ──
        if self._clap_detector is not None and getattr(self, "_clap_armed", True):
            try:
                if self._clap_detector.feed(pcm_frame):
                    label = "wake"
            except Exception as exc:
                log.debug(f"Clap detector error (ignored, fail-open): {exc}")

        # ── Wake-word engine path ─────────────────────────────────
        if label is None and self.engine is not None:
            if self.engine.frame_length is not None:
                expected = self.engine.frame_length * 2
                if len(pcm_frame) != expected:
                    if not self._warned_frame_size:
                        log.warning(
                            f"Wake engine expects {expected}-byte frames but got "
                            f"{len(pcm_frame)} — check CHUNK_SIZE vs engine.frame_length. "
                            "Wake word detection will be silently skipped for mismatched frames."
                        )
                        self._warned_frame_size = True
                    return None
            label = self.engine.process(pcm_frame)

        if label is None:
            return None

        now = time.monotonic()
        last = self._last_fire.get(label, 0.0)
        if now - last < self.cfg.cooldown_seconds:
            return None
        self._last_fire[label] = now
        return label

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()
            self.engine = None


def _run_standalone() -> None:
    """`python -m wake_word.listener` — mic-in-terminal smoke test.

    Prints every detection as it happens. Use this to tune sensitivity
    and confirm your model/keyword files are wired up correctly before
    relying on it inside the full assistant.
    """
    import sounddevice as sd

    cfg = load_wake_word_config()
    listener = WakeWordListener(cfg)
    if not listener.available:
        print("Wake word engine failed to load — check the log output above.")
        return

    blocksize = listener.engine.frame_length or 512
    samplerate = listener.engine.sample_rate

    print(f"Listening for wake phrase '{cfg.wake_phrase}' and interrupt words "
          f"{cfg.interrupt_words} (backend={cfg.backend}). Ctrl+C to stop.\n")

    def callback(indata, frames, time_info, status):
        label = listener.feed(indata.tobytes())
        if label:
            print(f"  >> detected: {label}")

    try:
        with sd.InputStream(samplerate=samplerate, channels=1, dtype="int16",
                             blocksize=blocksize, callback=callback):
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    _run_standalone()
