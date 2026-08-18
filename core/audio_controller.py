"""
core/audio_controller.py — Playback flush / barge-in helpers
===========================================================
Keeps audio stop/flush logic out of the main monolith so session and
speech authority can share one implementation.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger(__name__)


class AudioController:
    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant
        self.suppress_barge_in_until: float = 0.0

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    def flush_playback(self, reason: str = "") -> None:
        asst = self._asst
        if asst is None:
            return
        try:
            if hasattr(asst, "_set_speaking"):
                try:
                    asst._set_speaking(False, interrupted=True)
                except TypeError:
                    asst._set_speaking(False)
        except Exception:
            pass
        try:
            if hasattr(asst, "_hard_stop_speaker"):
                asst._hard_stop_speaker()
        except Exception:
            pass
        q = getattr(asst, "audio_in_queue", None)
        if q is not None:
            try:
                while not q.empty():
                    q.get_nowait()
            except Exception:
                pass
        self.suppress_barge_in_until = time.monotonic() + 1.2
        try:
            asst._last_barge_in_ts = time.monotonic()
            asst._barge_in_suppress_until = self.suppress_barge_in_until
        except Exception:
            pass
        if reason:
            log.debug(f"AudioController flush ({reason})")

    def barge_in_allowed(self, transcript: str = "") -> bool:
        if time.monotonic() < self.suppress_barge_in_until:
            return False
        if transcript and len(transcript.strip()) < 4:
            return False
        asst = self._asst
        if asst is None:
            return False
        if not getattr(asst, "_awake", False):
            return False
        if getattr(asst, "_announcing_while_asleep", False):
            return False
        if not getattr(asst, "_barge_in_enabled", True):
            return False
        return True


__all__ = ["AudioController"]
