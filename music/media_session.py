"""
music/media_session.py — Windows Global Media Transport Controls.
==================================================================
Wraps Windows' SMTC so the Music Engine can observe and control whatever
is actually playing without touching each app's private UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from music.providers.base import TrackInfo

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"


class MediaSessionManager:
    """Read current playback and send transport controls via SMTC."""

    def __init__(self) -> None:
        self._available = _IS_WINDOWS

    def is_available(self) -> bool:
        if not _IS_WINDOWS:
            return False
        try:
            import winsdk.windows.media.control  # noqa: F401
            return True
        except Exception:
            return False

    async def _get_best_session(self, app_hint: Optional[str] = None):
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        mgr = await Manager.request_async()
        sessions = list(mgr.get_sessions())
        if not sessions:
            return None
        if app_hint:
            hint = app_hint.lower()
            for s in sessions:
                try:
                    if hint in (s.source_app_user_model_id or "").lower():
                        return s
                except Exception:
                    continue
        cur = mgr.get_current_session()
        if cur is not None:
            return cur
        for s in sessions:
            try:
                info = s.get_playback_info()
                if info and info.playback_status == 4:  # Playing
                    return s
            except Exception:
                continue
        return sessions[0]

    def _run_async(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(coro)).result()
        else:
            return asyncio.run(coro)

    def current_track(self, app_hint: Optional[str] = None) -> Optional[TrackInfo]:
        if not self.is_available():
            return None
        try:
            session = self._run_async(self._get_best_session(app_hint))
            if not session:
                return None
            props = self._run_async(session.try_get_media_properties_async())
            info = session.get_playback_info()
            timeline = session.get_timeline_properties()
            status_map = {0: "closed", 1: "opened", 2: "changing", 3: "stopped",
                          4: "playing", 5: "paused"}
            return TrackInfo(
                title=getattr(props, "title", "") or "",
                artist=getattr(props, "artist", "") or "",
                album=getattr(props, "album_title", "") or "",
                duration=getattr(timeline, "end_time", None).total_seconds()
                    if getattr(timeline, "end_time", None) else 0.0,
                position=getattr(timeline, "position", None).total_seconds()
                    if getattr(timeline, "position", None) else 0.0,
                source=self._friendly_app(session.source_app_user_model_id),
                is_playing=getattr(info, "playback_status", -1) == 4,
            )
        except Exception:
            logger.debug("media_session current_track failed", exc_info=True)
            return None

    def _friendly_app(self, app_id: str) -> str:
        app_id = (app_id or "").lower()
        mapping = {
            "spotify": "Spotify",
            "vlc": "VLC",
            "wmplayer": "Windows Media Player",
            "msedge": "Edge",
            "chrome": "Chrome",
            "firefox": "Firefox",
        }
        for key, label in mapping.items():
            if key in app_id:
                return label
        return app_id or "unknown"

    def send(self, op: str, app_hint: Optional[str] = None) -> bool:
        if not self.is_available():
            return False
        try:
            session = self._run_async(self._get_best_session(app_hint))
            if not session:
                return False
            async def _do():
                if op == "play":
                    return bool(await session.try_play_async())
                if op == "pause":
                    return bool(await session.try_pause_async())
                if op == "toggle":
                    return bool(await session.try_toggle_play_pause_async())
                if op == "stop":
                    return bool(await session.try_stop_async())
                if op == "next":
                    return bool(await session.try_skip_next_async())
                if op == "previous":
                    return bool(await session.try_skip_previous_async())
                return False
            return bool(self._run_async(_do()))
        except Exception:
            logger.debug("media_session send %s failed", op, exc_info=True)
            return False

    def is_playing(self, app_hint: Optional[str] = None) -> bool:
        track = self.current_track(app_hint)
        return track.is_playing if track else False


__all__ = ["MediaSessionManager"]
