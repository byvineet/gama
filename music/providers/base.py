"""
music/providers/base.py — Provider interface for the Music Engine.
====================================================================
Every music source implements this unified interface so the controller
can switch providers without changing higher-level logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TrackInfo:
    """Normalized metadata for whatever is currently playing."""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    position: float = 0.0
    source: str = ""          # provider name, e.g. "spotify_web"
    url: str = ""
    artwork: str = ""
    is_playing: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Abstract base for all music providers."""

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider can be used right now."""
        ...

    @abstractmethod
    def play(self, query: str) -> bool:
        """Find and start playing the requested music. Return True on success."""
        ...

    def play_url(self, url: str) -> bool:
        """Optional: play a direct URL/URI."""
        return False

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def stop(self) -> bool:
        return False

    def next(self) -> bool:
        return False

    def previous(self) -> bool:
        return False

    def seek(self, seconds: float) -> bool:
        return False

    def set_volume(self, percent: int) -> bool:
        return False

    def current_track(self) -> Optional[TrackInfo]:
        return None

    def is_playing(self) -> bool:
        return False
