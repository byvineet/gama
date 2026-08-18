"""
actions/media_controller.py — Gama Unified Media Controller
=============================================================
Single entry point for controlling whatever is actually playing media
right now — Spotify, VLC, Windows Media Player, browser tabs (YouTube
etc.) — instead of a hardcoded "assume Spotify" approach.

Strategy (spec section 3):
  1. Prefer the native OS API: Windows' System Media Transport Controls
     (SMTC) via `winsdk`. Almost every modern media app (Spotify, Edge/
     Chrome playing YouTube, VLC, Windows Media Player, Groove) reports
     into SMTC automatically — it's the same API that powers the
     media flyout in the Windows taskbar. This gives us real play/
     pause/next/previous/seek + "what's playing" *without* any UI
     automation, and lets us target a *specific* app among several
     active sessions.
  2. If winsdk / SMTC is unavailable or no session is found, fall back
     to global media keys (pynput) — works for whatever app currently
     owns the system media focus, no app-awareness but always works.
  3. Volume/mute uses pycaw for precise system (or per-app) volume
     control; falls back to media keys if pycaw isn't available.
  4. "Launch and play a specific song" still goes through app-specific
     handling (Spotify URI / YouTube Music search) since SMTC has no
     concept of "search and play X".

Everything here is synchronous from the caller's perspective but the
winsdk calls are async under the hood — we run them on a short-lived
event loop per call. These calls are cheap (single WinRT round trip)
and only happen on explicit user command, so this never costs any
idle CPU.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import asyncio
import logging
import os
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# SMTC (Windows System Media Transport Controls) session access
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run a coroutine to completion, tolerant of being called from an existing running loop."""
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


async def _get_manager():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
    )
    return await Manager.request_async()


async def _list_sessions_async() -> List[Any]:
    mgr = await _get_manager()
    return list(mgr.get_sessions())


async def _get_session_async(app_hint: Optional[str] = None):
    """Return the best-matching SMTC session: the one whose source app
    id contains `app_hint` if given, else the manager's current session,
    else the first active/playing session found."""
    mgr = await _get_manager()
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

    # Prefer a session that's actually playing over a paused/idle one.
    for s in sessions:
        try:
            info = s.get_playback_info()
            if info and info.playback_status == 4:  # Playing
                return s
        except Exception:
            continue
    return sessions[0]


def _friendly_app_name(app_id: str) -> str:
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
    return app_id or "the active player"


async def _now_playing_async(app_hint: Optional[str] = None) -> Dict[str, Any]:
    session = await _get_session_async(app_hint)
    if session is None:
        return {}
    props = await session.try_get_media_properties_async()
    info = session.get_playback_info()
    timeline = session.get_timeline_properties()
    status_map = {0: "closed", 1: "opened", 2: "changing", 3: "stopped",
                  4: "playing", 5: "paused"}
    return {
        "app": _friendly_app_name(session.source_app_user_model_id),
        "raw_app_id": session.source_app_user_model_id,
        "title": getattr(props, "title", "") or "",
        "artist": getattr(props, "artist", "") or "",
        "album": getattr(props, "album_title", "") or "",
        "status": status_map.get(getattr(info, "playback_status", -1), "unknown"),
        "position_sec": getattr(timeline, "position", None).total_seconds()
            if getattr(timeline, "position", None) else 0,
        "duration_sec": getattr(timeline, "end_time", None).total_seconds()
            if getattr(timeline, "end_time", None) else 0,
    }


async def _control_async(op: str, app_hint: Optional[str] = None,
                          seek_sec: Optional[float] = None) -> bool:
    session = await _get_session_async(app_hint)
    if session is None:
        return False
    try:
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
        if op == "seek" and seek_sec is not None:
            from winsdk.windows.foundation import TimeSpan
            ticks = int(seek_sec * 10_000_000)  # 100ns units
            return bool(await session.try_change_playback_position_async(ticks))
    except Exception:
        logger.debug("SMTC control op %s failed", op, exc_info=True)
    return False


def _smtc_available() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import winsdk.windows.media.control  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fallbacks: media keys + volume
# ---------------------------------------------------------------------------

def _media_key(key_name: str) -> bool:
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        key_map = {
            "play": Key.media_play_pause,
            "pause": Key.media_play_pause,
            "toggle": Key.media_play_pause,
            "stop": Key.media_stop,
            "next": Key.media_next,
            "previous": Key.media_previous,
            "volume_up": Key.media_volume_up,
            "volume_down": Key.media_volume_down,
            "mute": Key.media_volume_mute,
        }
        key = key_map.get(key_name)
        if key is None:
            return False
        kb.press(key)
        kb.release(key)
        return True
    except Exception:
        logger.debug("media key fallback failed", exc_info=True)
        return False


def _set_system_volume(level: int) -> bool:
    """Set master system volume (0-100) via pycaw."""
    if not _IS_WINDOWS:
        return False
    try:
        from utils.audio_endpoint import get_volume_endpoint
        volume = get_volume_endpoint()
        volume.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100.0, None)
        return True
    except Exception:
        logger.debug("pycaw volume set failed", exc_info=True)
        return False


def _set_app_volume(app_name: str, level: int) -> bool:
    """Set per-application volume (e.g. just Spotify) via pycaw."""
    if not _IS_WINDOWS:
        return False
    try:
        from pycaw.pycaw import AudioUtilities
        from utils.audio_endpoint import ensure_com_initialized
        ensure_com_initialized()
        sessions = AudioUtilities.GetAllSessions()
        hit = False
        for s in sessions:
            try:
                if s.Process and app_name.lower() in s.Process.name().lower():
                    volume = s.SimpleAudioVolume
                    volume.SetMasterVolume(max(0, min(100, level)) / 100.0, None)
                    hit = True
            except Exception:
                continue
        return hit
    except Exception:
        logger.debug("pycaw per-app volume failed", exc_info=True)
        return False


def _set_mute(mute: bool, app_name: Optional[str] = None) -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        from pycaw.pycaw import AudioUtilities
        from utils.audio_endpoint import ensure_com_initialized
        ensure_com_initialized()
        if app_name:
            sessions = AudioUtilities.GetAllSessions()
            hit = False
            for s in sessions:
                try:
                    if s.Process and app_name.lower() in s.Process.name().lower():
                        s.SimpleAudioVolume.SetMute(1 if mute else 0, None)
                        hit = True
                except Exception:
                    continue
            return hit
        from utils.audio_endpoint import get_volume_endpoint
        volume = get_volume_endpoint()
        volume.SetMute(1 if mute else 0, None)
        return True
    except Exception:
        logger.debug("pycaw mute failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# App launch / search (when nothing is currently playing what's asked for)
#
# Spotify's "play a specific track" flow lives in
# brings it to the foreground, searches for the requested song, and
# presses Enter, then hands off to the user to pick the actual track
# from the results. No auto-selection, no UI-Automation clicks, no
# coordinates, no browser automation. See that module for details.
# ---------------------------------------------------------------------------

def _launch_and_play(song: str, artist: str, platform: str) -> str:
    query = f"{song} {artist}".strip()
    platform = (platform or "spotify").lower().strip()

    if platform == "spotify":
        if _IS_WINDOWS:
            try:
                # Prefer the hybrid Web API + URI path: it searches Spotify's
                # catalog, picks the best match, and opens spotify:track:... so
                # playback starts automatically with no UI automation. Falls back
                # to desktop search if the Web API isn't authenticated.
                # Spotify Web/API path removed; use music engine / rapidapi instead
                return "Use the music engine for Spotify playback."
            except Exception:
                logger.debug("Spotify web/URI play failed, falling back to web search",
                             exc_info=True)
        webbrowser.open(f"https://open.spotify.com/search/{urllib.parse.quote(query)}", new=2)
        return f"Searching '{query}' on Spotify Web — couldn't auto-play a specific track."

    if platform in ("youtube", "yt", "youtube_music"):
        webbrowser.open(f"https://music.youtube.com/search?q={urllib.parse.quote(query)}", new=2)
        return f"Playing '{query}' on YouTube Music."

    if platform == "vlc":
        return "Tell me the file path and I can open it with VLC via file_controller/open_app."

    webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote(query + ' song')}", new=2)
    return f"Searching for '{query}'."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def media_controller(action: str = "play", **kwargs) -> str:
    """
    Unified media control entry point.

    action: play | pause | resume | toggle | stop | next | previous |
            seek | volume | mute | unmute | fullscreen | now_playing
    kwargs:
      song, artist, platform  -> for 'play' with a specific track
      app                     -> target a specific player when several
                                  are active (e.g. "spotify", "vlc",
                                  "edge"); otherwise the most relevant
                                  active session is used
      level                   -> 0-100 for 'volume'
      seconds                 -> absolute seek position for 'seek'
    """
    action = (action or "play").lower().strip()
    app_hint = kwargs.get("app")

    # --- Reporting -----------------------------------------------------
    if action in ("now_playing", "status", "current"):
        return _now_playing(app_hint)

    # --- Explicit track request -> launch/search the platform app -----
    if action == "play" and kwargs.get("song"):
        return _launch_and_play(kwargs.get("song", ""), kwargs.get("artist", ""),
                                 kwargs.get("platform", "spotify"))

    # --- Transport controls ---------------------------------------------
    transport_map = {
        "play": "play", "resume": "play", "pause": "pause",
        "toggle": "toggle", "stop": "stop",
        "next": "next", "skip": "next",
        "previous": "previous", "prev": "previous", "back": "previous",
    }
    if action in transport_map:
        op = transport_map[action]
        if _smtc_available():
            try:
                ok = _run_async(_control_async(op, app_hint))
                if ok:
                    return f"{op.capitalize()}."
            except Exception:
                logger.debug("SMTC transport failed, falling back to media keys", exc_info=True)
        # Fallback: generic media key
        key_name = "toggle" if op == "play" else op
        if _media_key(key_name):
            return f"{op.capitalize()} (media key)."
        return f"Couldn't {op} — no active media session found and media keys failed."

    if action == "seek":
        seconds = kwargs.get("seconds")
        if seconds is None:
            return "Give me a target position in seconds to seek to."
        if _smtc_available():
            ok = _run_async(_control_async("seek", app_hint, float(seconds)))
            if ok:
                return f"Seeked to {int(seconds)}s."
        return "Seeking isn't supported for the active player."

    # --- Volume ----------------------------------------------------------
    if action == "volume":
        level = kwargs.get("level")
        if level is None:
            return "Give me a volume level from 0-100."
        level = int(level)
        if app_hint and _set_app_volume(app_hint, level):
            return f"Set {app_hint} volume to {level}%."
        if _set_system_volume(level):
            return f"Set system volume to {level}%."
        # crude fallback: nudge with keys
        key = "volume_up" if level >= 50 else "volume_down"
        _media_key(key)
        return f"Adjusted volume toward {level}% (approximate, no direct API available)."

    if action in ("mute", "unmute"):
        mute = action == "mute"
        if _set_mute(mute, app_hint):
            return f"{'Muted' if mute else 'Unmuted'}{' ' + app_hint if app_hint else ''}."
        if _media_key("mute"):
            return f"{'Muted' if mute else 'Unmuted'} (media key)."
        return "Couldn't change mute state."

    # --- Fullscreen (only meaningful for browser video) ------------------
    if action == "fullscreen":
        try:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            kb.press(Key.f11)
            kb.release(Key.f11)
            return "Toggled fullscreen (F11) on the active window."
        except Exception:
            return "Couldn't toggle fullscreen."

    return (f"Unknown media action: {action}. Use: play, pause, resume, stop, next, "
            f"previous, seek, volume, mute, unmute, fullscreen, now_playing.")


def _now_playing(app_hint: Optional[str] = None) -> str:
    if not _smtc_available():
        return "Can't detect what's playing on this system (Windows media session API unavailable)."
    try:
        info = _run_async(_now_playing_async(app_hint))
    except Exception:
        logger.debug("now_playing failed", exc_info=True)
        info = {}
    if not info:
        return "Nothing appears to be playing right now."
    title = info.get("title") or "Unknown title"
    artist = info.get("artist")
    app = info.get("app", "the active player")
    status = info.get("status", "unknown")
    piece = f"{title}" + (f" by {artist}" if artist else "")
    return f"{piece} — {status} on {app}."


def list_media_sessions() -> List[Dict[str, str]]:
    """Return every active SMTC session's app id — used by callers that
    want to disambiguate ('which player did you mean?')."""
    if not _smtc_available():
        return []
    try:
        sessions = _run_async(_list_sessions_async())
    except Exception:
        return []
    out = []
    for s in sessions:
        try:
            out.append({"app": _friendly_app_name(s.source_app_user_model_id),
                        "raw_app_id": s.source_app_user_model_id})
        except Exception:
            continue
    return out


__all__ = ["media_controller", "list_media_sessions"]