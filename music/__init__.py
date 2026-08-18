"""
music — Modular Music Engine for Gama
======================================
Provider-based music playback subsystem.

The main entry point is `MusicController` from `music.controller`.
"""

from music.controller import MusicController
from music.player_state import PlaybackState, TrackInfo
from music.providers.base import BaseProvider

__all__ = ["MusicController", "PlaybackState", "TrackInfo", "BaseProvider"]
