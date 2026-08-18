"""
music/providers/youtube.py — YouTube Provider (final fallback).
=================================================================
Opens a YouTube search or direct YouTube Music video link. This is the
last-resort provider when nothing else succeeded. Avoids automation
when possible to keep CPU/RAM low.
"""

from __future__ import annotations

import logging
import urllib.parse
import webbrowser
from typing import Optional

from music.providers.base import BaseProvider, TrackInfo

logger = logging.getLogger(__name__)


class YouTubeProvider(BaseProvider):
    """YouTube search fallback."""

    name = "youtube"

    def __init__(self) -> None:
        self._last_query: str = ""

    def is_available(self) -> bool:
        return True

    def play(self, query: str) -> bool:
        self._last_query = query
        # Try to open the first likely result via a "I'm Feeling Lucky" style
        # search, excluding Shorts. A regular YouTube search URL is the safest
        # fallback that works without scraping.
        search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query + ' -shorts')}"
        try:
            webbrowser.open(search_url, new=2)
            logger.info("[YouTube] Opened search results: %s", search_url)
            return True
        except Exception:
            logger.debug("[YouTube] open failed", exc_info=True)
            return False

    def play_url(self, url: str) -> bool:
        try:
            webbrowser.open(url, new=2)
            return True
        except Exception:
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
