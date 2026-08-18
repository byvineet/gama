"""
music/providers/youtube_music.py — YouTube Music Provider.
===========================================================
Opens YouTube Music search for the requested track. If Playwright is
available, it will also try to click the first result and start playback.
Otherwise falls back to opening the browser search page for the user.
"""

from __future__ import annotations

import logging
import urllib.parse
import webbrowser
from typing import Optional

from music.providers.base import BaseProvider, TrackInfo

logger = logging.getLogger(__name__)


class YouTubeMusicProvider(BaseProvider):
    """YouTube Music search and (best-effort) auto-play."""

    name = "youtube_music"

    def __init__(self) -> None:
        self._last_query: str = ""

    def is_available(self) -> bool:
        return True  # only needs a browser

    def play(self, query: str) -> bool:
        self._last_query = query
        url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"
        try:
            # Try Playwright automation if installed and browser is available.
            if self._try_playwright(url, query):
                logger.info("[YouTubeMusic] Auto-played via Playwright: %s", query)
                return True
        except Exception:
            logger.debug("[YouTubeMusic] Playwright path failed", exc_info=True)
        # Fallback: open browser search page.
        try:
            webbrowser.open(url, new=2)
            logger.info("[YouTubeMusic] Opened search page: %s", url)
            return True  # we did launch something; verification is on user
        except Exception:
            logger.debug("[YouTubeMusic] open failed", exc_info=True)
            return False

    def _try_playwright(self, url: str, query: str) -> bool:
        """Best-effort Playwright automation. Returns True only if playback
        actually started and was verified."""
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except Exception:
            return False

        with sync_playwright() as p:
            # Prefer existing Chrome/Edge channel if available.
            browser = None
            for channel in ("msedge", "chrome"):
                try:
                    browser = p.chromium.launch(channel=channel, headless=False)
                    break
                except Exception:
                    continue
            if not browser:
                browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # Accept cookie/age dialog if it appears (best-effort).
                try:
                    page.locator("button:has-text('Accept all')").click(timeout=3000)
                except PWTimeout:
                    pass
                # Try to click the first playable result. The first thumbnail
                # in a YT Music search page is usually a clickable link/image.
                page.locator("ytmusic-shelf-renderer a, ytmusic-two-row-item-renderer a").first.click(timeout=5000)
                page.wait_for_timeout(2000)
                # Check if video player is present (means a track loaded).
                player = page.locator("video, .ytp-play-button").first
                if player.is_visible(timeout=3000):
                    return True
            except Exception:
                logger.debug("[YouTubeMusic] Playwright interaction failed", exc_info=True)
            finally:
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
        return False

    def play_url(self, url: str) -> bool:
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
