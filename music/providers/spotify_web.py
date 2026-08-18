"""
music/providers/spotify_web.py — Spotify Web Provider.
=======================================================
Wraps the existing Web API + URI auto-play logic. This is the preferred
Spotify path because it can start playback without touching the UI.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from music.providers.base import BaseProvider, TrackInfo

logger = logging.getLogger(__name__)


class SpotifyWebProvider(BaseProvider):
    """Spotify Web API + URI playback provider."""

    name = "spotify_web"

    def __init__(self) -> None:
        self._last_query: str = ""
        self._last_track: Optional[TrackInfo] = None

    def is_available(self) -> bool:
        try:
            from actions import spotify_auth
            return spotify_auth.is_configured()
        except Exception:
            return False

    def play(self, query: str) -> bool:
        try:
            from actions.spotify_web import play_async
            # Extract optional "by Artist" from query for better matching.
            song, artist = self._split_query(query)
            result = asyncio.run(play_async(song, artist))
            self._last_query = query
            if result and "playing" in result.lower():
                # The existing play_async returns a confirmation string like
                # "Playing 'Title' by Artist on Spotify."
                m = re.search(r"Playing '(.+?)'(?: by (.+?))? on Spotify", result)
                if m:
                    self._last_track = TrackInfo(
                        title=m.group(1),
                        artist=m.group(2) or "",
                        source=self.name,
                        is_playing=True,
                    )
                logger.info("[SpotifyWeb] %s", result)
                return True
            logger.info("[SpotifyWeb] Could not auto-play: %s", result)
            return False
        except Exception:
            logger.debug("[SpotifyWeb] play failed", exc_info=True)
            return False

    def play_url(self, url: str) -> bool:
        return False

    @staticmethod
    def _split_query(query: str) -> tuple:
        parts = query.split(" by ", 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return query.strip(), ""

    # Transport controls are delegated to Windows SMTC by the MusicController.
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
        try:
            from actions.spotify_controller import _spotify_now_playing
            info = asyncio.run(_spotify_now_playing())
            if info:
                return TrackInfo(
                    title=info.get("title", ""),
                    artist=info.get("artist", ""),
                    album=info.get("album", ""),
                    source=self.name,
                    is_playing=info.get("status") == "playing",
                )
        except Exception:
            pass
        return self._last_track

    def is_playing(self) -> bool:
        track = self.current_track()
        return track.is_playing if track else False
