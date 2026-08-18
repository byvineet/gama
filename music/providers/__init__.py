"""Music providers package."""

from music.providers.base import BaseProvider, TrackInfo
from music.providers.local import LocalMusicProvider
from music.providers.spotify_desktop import SpotifyDesktopProvider

__all__ = [
    "BaseProvider",
    "TrackInfo",
    "LocalMusicProvider",
    "SpotifyDesktopProvider",
]
