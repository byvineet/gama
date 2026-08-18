"""
music/player_state.py — Shared playback state for the Music Engine.
=====================================================================
Lightweight, thread-safe-ish state object. Providers and the controller
read/write this to keep a single source of truth.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from music.providers.base import TrackInfo


@dataclass
class PlaybackState:
    is_playing: bool = False
    current_track: Optional[TrackInfo] = None
    provider_name: str = ""
    queue_position: int = 0
    repeat_mode: str = "off"      # off | one | all
    shuffle: bool = False
    volume: int = 100
    last_query: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class StateStore:
    """Thread-safe wrapper around PlaybackState."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = PlaybackState()

    def get(self) -> PlaybackState:
        with self._lock:
            return self._state

    def update(self, **kwargs) -> PlaybackState:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._state, k, v)
            return self._state

    def reset(self) -> None:
        with self._lock:
            self._state = PlaybackState()


__all__ = ["PlaybackState", "StateStore"]
