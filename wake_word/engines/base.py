"""
Gama - Wake Word Engine Base
============================
Every backend (Vosk, Porcupine, ...) implements this tiny interface so
`wake_word/listener.py` and `main.py` never need to know which one is
actually running.

Author : Vineet Machchal
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class WakeEngineBase(ABC):
    """A wake/interrupt-word spotting engine.

    Frames are raw little-endian int16 mono PCM `bytes`, matching exactly
    what Gama's existing mic InputStream already produces (16kHz,
    blocksize=512) — so no resampling or re-buffering is required to
    plug an engine in.
    """

    #: Samples per frame this engine expects. `None` = any size accepted.
    frame_length: Optional[int] = None
    #: Sample rate (Hz) this engine expects.
    sample_rate: int = 16000

    @abstractmethod
    def process(self, pcm_frame: bytes) -> Optional[str]:
        """Feed one frame of audio.

        Returns a label (e.g. "wake", "stop", "cancel", "listen") the
        instant it's confidently detected, or None otherwise. Must be
        fast (<< frame duration) since this runs on the audio thread.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release native resources (models, handles). Safe to call twice."""
        pass
