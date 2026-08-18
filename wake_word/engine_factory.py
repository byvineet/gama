"""
Gama - Wake Word Engine Factory
================================
Builds whichever backend config/wake_word.json asks for, and never lets
a missing model/dependency take the whole assistant down — if wake word
setup fails for any reason, we log why and return None. Callers then
fall back to "always awake" (the old behaviour) instead of crashing.

Author : Vineet Machchal
"""

from __future__ import annotations

from typing import Optional

from utils.logger import get_logger
from .config import WakeWordConfig
from .engines.base import WakeEngineBase

log = get_logger(__name__)


def create_engine(cfg: WakeWordConfig) -> Optional[WakeEngineBase]:
    if not cfg.enabled:
        log.info("Wake word detection disabled in config.")
        return None

    try:
        if cfg.backend == "porcupine":
            from .engines.porcupine_engine import PorcupineWakeEngine
            return PorcupineWakeEngine(
                access_key=cfg.porcupine_access_key,
                keywords=cfg.porcupine_keywords,
                base_dir=cfg.base_dir,
            )

        if cfg.backend == "vosk":
            from .engines.vosk_engine import VoskWakeEngine
            return VoskWakeEngine(
                model_path=str(cfg.resolve(cfg.vosk_model_path)),
                wake_phrases=cfg.wake_phrases,
                interrupt_words=cfg.interrupt_words,
                confirm_silence_ms=cfg.wake_confirm_silence_ms,
                confirm_rms_threshold=cfg.wake_confirm_rms_threshold,
            )

        log.error(f"Unknown wake_word backend '{cfg.backend}' (expected 'vosk' or 'porcupine').")
        return None

    except Exception as exc:
        log.error(
            f"Wake word engine ('{cfg.backend}') failed to initialize: {exc}. "
            "GAMA will run with wake word detection OFF (always awake) until this is fixed."
        )
        return None


__all__ = ["create_engine"]
