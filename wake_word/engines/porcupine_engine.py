"""
Gama - Wake Word Engine: Porcupine (optional, lowest-CPU backend)
==================================================================
Picovoice Porcupine is a dedicated neural wake-word spotter — it only
ever answers "was one of my trained keywords just said?", never "what
did they say?". That narrow job is what lets it idle at a fraction of
a percent of one CPU core, far below a general speech recognizer.

Trade-off: each keyword needs a small `.ppn` model file, generated
once (free, no coding) at https://console.picovoice.ai for your exact
phrase — "wake up gama", "stop", etc. — plus a free AccessKey. Both are
one-time setup; recognition itself is fully offline afterwards.

Use this backend once you've done that setup and want lower idle CPU
than the default Vosk backend. See wake_word/README.md.

Author : Vineet Machchal
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger
from .base import WakeEngineBase
from ..config import PorcupineKeyword

log = get_logger(__name__)


class PorcupineWakeEngine(WakeEngineBase):
    def __init__(self, access_key: str, keywords: List[PorcupineKeyword], base_dir: Path):
        try:
            import pvporcupine  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "The 'pvporcupine' package isn't installed. Run: pip install pvporcupine"
            ) from exc

        if not access_key:
            raise RuntimeError(
                "porcupine.access_key is empty in config/wake_word.json. "
                "Get a free key at https://console.picovoice.ai"
            )
        if not keywords:
            raise RuntimeError(
                "No porcupine.keywords configured. Add at least a 'wake' "
                "keyword with a .ppn file path — see wake_word/README.md."
            )

        paths, sensitivities, self._labels = [], [], []
        for kw in keywords:
            p = Path(kw.path)
            if not p.is_absolute():
                p = base_dir / p
            if not p.exists():
                raise RuntimeError(f"Porcupine keyword file not found: {p}")
            paths.append(str(p))
            sensitivities.append(max(0.0, min(1.0, kw.sensitivity)))
            self._labels.append(kw.label)

        self._porcupine = pvporcupine.create(
            access_key=access_key,
            keyword_paths=paths,
            sensitivities=sensitivities,
        )
        self.frame_length = self._porcupine.frame_length
        self.sample_rate = self._porcupine.sample_rate
        log.info(
            f"PorcupineWakeEngine ready (labels={self._labels}, "
            f"frame_length={self.frame_length}, sample_rate={self.sample_rate})"
        )

    def process(self, pcm_frame: bytes) -> Optional[str]:
        try:
            expected_bytes = self.frame_length * 2  # int16 = 2 bytes/sample
            if len(pcm_frame) != expected_bytes:
                return None  # partial/mismatched frame — skip rather than crash
            pcm = struct.unpack_from(f"{self.frame_length}h", pcm_frame)
            idx = self._porcupine.process(pcm)
            if idx is not None and idx >= 0:
                return self._labels[idx]
            return None
        except Exception as exc:
            log.debug(f"Porcupine frame error (ignored): {exc}")
            return None

    def close(self) -> None:
        try:
            if self._porcupine is not None:
                self._porcupine.delete()
                self._porcupine = None
        except Exception:
            pass
