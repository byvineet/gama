"""
music/controller.py — Music Engine Controller.
===============================================
Central entry point for all music-related requests. Coordinates providers,
verifies playback, manages queue/history, and integrates with Windows media
session.

Provider priority:
    1. Local music
    2. Spotify Desktop
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from music.history import HistoryManager
from music.intent import MusicIntent, MusicIntentParser
from music.media_session import MediaSessionManager
from music.player_state import PlaybackState, StateStore, TrackInfo
from music.providers.base import BaseProvider
from music.providers.local import LocalMusicProvider
from music.providers.spotify_desktop import SpotifyDesktopProvider
from music.queue import MusicQueue, QueueItem

logger = logging.getLogger(__name__)

_DEFAULT_VOLUME_STEP = 10


class MusicController:
    """Main music engine controller."""

    def __init__(self) -> None:
        self._state = StateStore()
        self._queue = MusicQueue()
        self._history = HistoryManager()
        self._parser = MusicIntentParser()
        self._media = MediaSessionManager()

        # Build providers lazily; only their is_available() checks run here.
        self._providers: List[BaseProvider] = [
            LocalMusicProvider(),
            SpotifyDesktopProvider(),
        ]
        self._provider_map: Dict[str, BaseProvider] = {p.name: p for p in self._providers}

        # Bounded shared pool for background playback-verification checks.
        # Previously each play command spawned a brand-new threading.Thread;
        # under rapid command bursts (e.g. skipping through a queue quickly)
        # that could exhaust OS threads. A small bounded pool caps concurrent
        # verification workers instead.
        self._verify_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="music-verify")

    # ------------------------------------------------------------------
    # Public command API
    # ------------------------------------------------------------------

    def handle(self, text: str) -> str:
        """Parse a natural-language command and execute it."""
        intent = self._parser.parse(text)
        if not intent:
            return "I didn't catch that as a music command."
        return self.execute(intent)

    def execute(self, intent: MusicIntent) -> str:
        action = intent.action
        if action == "play":
            return self.play(intent.query, platform=intent.platform or "")
        if action == "pause":
            return self.pause()
        if action == "resume":
            return self.resume()
        if action == "stop":
            return self.stop()
        if action == "next":
            return self.next()
        if action == "previous":
            return self.previous()
        if action == "restart":
            return self.seek(0)
        if action == "seek":
            return self.seek(intent.seconds)
        if action == "shuffle":
            return self.shuffle(not self._queue._shuffle)
        if action == "repeat":
            return self.repeat(intent.seconds)
        if action == "volume":
            return self.volume(intent.level)
        if action == "mute":
            return self.mute(True)
        if action == "unmute":
            return self.mute(False)
        if action == "now_playing":
            return self.now_playing()
        if action == "play_similar":
            return self.play_similar()
        return "Unknown music command."

    # ------------------------------------------------------------------
    # Playback commands
    # ------------------------------------------------------------------

    def play(self, query: str, platform: str = "") -> str:
        if not query or not query.strip():
            return "What should I play?"
        query = query.strip()

        # Context-aware "play this / that / it" — resolve the referenced song.
        if query in ("__context__", "this", "that", "it", "this song", "that song", "this track", "that track"):
            resolved, context_source = self._resolve_context()
            if not resolved:
                return "I don't know which song you're referring to."
            query = resolved
            logger.info("[MusicEngine] Resolved context '%s' from %s", query, context_source)
            if context_source == "clipboard URL":
                return self.play_url(query)

        # Platform hint overrides provider order.
        providers = self._providers[:]
        if platform:
            providers = self._reorder_by_platform(providers, platform)

        # Try each provider until one succeeds.
        #
        # NOTE on latency: this used to block here for up to
        # `_verify_playback(timeout=6.0)` — polling every 300ms — before
        # returning anything to the caller, which is what produced the
        # "SLOW STAGE: Tool took 9496 ms (budget 1500 ms)" warning (the
        # os.startfile()/app-launch time on top of a full 6s verify
        # poll). provider.play() returning True already means the launch
        # call itself succeeded (file handed to the OS / player process
        # spawned) — that's a reliable enough signal to answer the user
        # immediately ("Playing X on Local.") instead of making them wait
        # for a best-effort confirmation loop. Verification still runs,
        # just off the response path, in the background.
        for provider in providers:
            if not provider.is_available():
                continue
            logger.info("[MusicEngine] Trying %s for '%s'", provider.name, query)
            if provider.play(query):
                self._state.update(
                    is_playing=True,
                    provider_name=provider.name,
                    last_query=query,
                )
                track = provider.current_track() or self._media.current_track()
                self._record_history(query, track, provider.name)
                self._verify_playback_async(provider, query)
                return self._success_message(track, provider.name)

        return f"Couldn't find or play '{query}' from any available music source."

    def _verify_playback_async(self, provider: "BaseProvider", query: str) -> None:
        """Best-effort playback confirmation, run off the hot path.

        If it turns out nothing actually started (rare — e.g. no default
        app registered for the file type), corrects `is_playing` in the
        background state so later status queries ("what's playing?")
        stay accurate, without making the *play* command itself wait.
        """
        def _worker() -> None:
            try:
                if not self._verify_playback(provider, timeout=6.0):
                    logger.info(
                        "[MusicEngine] %s started but playback not verified for '%s'",
                        provider.name, query,
                    )
                    if self._state.get().provider_name == provider.name:
                        self._state.update(is_playing=False)
            except Exception:
                logger.exception("[MusicEngine] verification worker failed for '%s'", query)

        self._verify_pool.submit(_worker)

    def _resolve_context(self) -> tuple:
        """Resolve 'this song' / 'that' / 'it' into a real query.

        Priority: current/paused media session > last history > clipboard URL.
        Returns (query, source_description) or (None, None)."""
        # 1. Current Windows media session (playing or paused)
        try:
            track = self._media.current_track()
            if track and track.title:
                q = f"{track.title} {track.artist}".strip()
                if q:
                    return q, "current media session"
        except Exception:
            logger.debug("context resolution: media session failed", exc_info=True)

        # 2. Recently played history
        last = self._history.last()
        if last:
            q = f"{last.title} {last.artist}".strip() or last.query
            if q:
                return q, "recent history"

        # 3. Clipboard music URL
        try:
            from actions.clipboard import clipboard
            clip = clipboard("read")
            if clip and ("spotify.com" in clip or "music.youtube.com" in clip
                         or "youtube.com/watch" in clip or "youtu.be/" in clip):
                return clip, "clipboard URL"
        except Exception:
            logger.debug("context resolution: clipboard failed", exc_info=True)

        return None, None

    def play_url(self, url: str) -> str:
        for provider in self._providers:
            if provider.is_available() and provider.play_url(url):
                self._state.update(is_playing=True, provider_name=provider.name)
                self._verify_playback_async(provider, url)
                return f"Playing from {provider.name}."
        return "Couldn't play that URL."

    def _current_track_label(self) -> str:
        """Best-effort 'Title by Artist' for whatever's actually playing
        right now, so transport confirmations can say what they acted on
        instead of a bare "Paused." — same context-awareness "play this"
        already gets via _resolve_context(), just for pause/resume/stop.
        """
        track = self._current_from_active_provider() or self._media.current_track()
        if not track or not track.title:
            return ""
        label = f"'{track.title}'"
        if track.artist:
            label += f" by {track.artist}"
        return label

    def pause(self) -> str:
        label = self._current_track_label()
        ok = self._delegate("pause")
        if ok:
            self._state.update(is_playing=False)
            return f"Paused {label}." if label else "Paused."
        return "Nothing is currently playing to pause."

    def resume(self) -> str:
        ok = self._delegate("resume")
        if ok:
            self._state.update(is_playing=True)
            label = self._current_track_label()
            return f"Resumed {label}." if label else "Resumed."
        return "Nothing is paused right now to resume."

    def stop(self) -> str:
        label = self._current_track_label()
        ok = self._delegate("stop")
        if ok:
            self._state.update(is_playing=False)
            return f"Stopped {label}." if label else "Stopped."
        return "Nothing is currently playing to stop."

    def next(self) -> str:
        nxt = self._queue.next()
        if nxt:
            return self.play(nxt.query)
        ok = self._delegate("next")
        return "Next track." if ok else "Couldn't skip."

    def previous(self) -> str:
        prev = self._queue.previous()
        if prev:
            return self.play(prev.query)
        ok = self._delegate("previous")
        return "Previous track." if ok else "Couldn't go back."

    def seek(self, seconds: int) -> str:
        ok = self._delegate("seek", seconds)
        if ok:
            return f"Seeked to {seconds}s."
        return "Seeking isn't supported right now."

    def volume(self, level: int) -> str:
        if level == 0:
            return self.mute(True)
        # Relative or absolute
        if abs(level) <= 20 and level != 0:
            current = self._state.get().volume
            level = max(0, min(100, current + level))
        ok = self._delegate("set_volume", level)
        if ok:
            self._state.update(volume=level)
            return f"Volume set to {level}%."
        # Fallback: media keys
        return self._media_volume(level)

    def mute(self, enabled: bool) -> str:
        # Try to set system volume to 0 / restore
        try:
            from actions.media_controller import _set_system_volume, _set_mute
            if _set_mute(enabled):
                return "Muted." if enabled else "Unmuted."
        except Exception:
            pass
        return "Couldn't mute right now."

    def shuffle(self, enabled: bool) -> str:
        self._queue.set_shuffle(enabled)
        return f"Shuffle {'on' if enabled else 'off'}."

    def repeat(self, mode_code: int) -> str:
        mode_map = {0: "off", 1: "one", 2: "all"}
        mode = mode_map.get(mode_code, "off")
        self._queue.set_repeat(mode)
        return f"Repeat {mode}."

    def now_playing(self) -> str:
        track = self._media.current_track()
        if not track:
            track = self._current_from_active_provider()
        if not track or not track.title:
            return "Nothing is playing right now."
        extra = f" by {track.artist}" if track.artist else ""
        extra += f" on {track.source}" if track.source else ""
        return f"Playing '{track.title}'{extra}."

    def play_similar(self) -> str:
        last = self._history.last()
        if not last:
            return "I don't know what song to base similar music on."
        query = f"{last.title} {last.artist}".strip()
        return self.play(f"{query} radio")

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    def queue_add(self, query: str) -> str:
        self._queue.add(QueueItem(query=query))
        return f"Added '{query}' to the queue."

    def queue_clear(self) -> str:
        self._queue.clear()
        return "Queue cleared."

    def queue_list(self) -> str:
        items = self._queue.list_items()
        if not items:
            return "The queue is empty."
        lines = [f"{i+1}. {it.query}" for i, it in enumerate(items)]
        return "Queue:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reorder_by_platform(self, providers: List[BaseProvider], platform: str) -> List[BaseProvider]:
        platform = platform.lower().strip()
        priority = []
        rest = []
        for p in providers:
            if platform in p.name or (platform == "spotify" and "spotify" in p.name):
                priority.append(p)
            else:
                rest.append(p)
        return priority + rest

    def _verify_playback(self, provider: BaseProvider, timeout: float = 6.0) -> bool:
        """Wait briefly and verify that playback actually started."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if provider.is_playing() or self._media.is_playing():
                return True
            time.sleep(0.3)
        return False

    def _delegate(self, method: str, *args, **kwargs) -> bool:
        """Call a control method on the most recently active provider, then
        fall back through SMTC."""
        active_name = self._state.get().provider_name
        order = self._providers[:]
        if active_name:
            order = [p for p in order if p.name == active_name] + [p for p in order if p.name != active_name]
        for provider in order:
            if not provider.is_available():
                continue
            fn = getattr(provider, method, None)
            if fn and fn(*args, **kwargs):
                return True
        # Final fallback to SMTC if the method maps to a transport op.
        smtc_map = {
            "pause": "pause", "resume": "play", "stop": "stop",
            "next": "next", "previous": "previous",
        }
        if method in smtc_map:
            return self._media.send(smtc_map[method])
        return False

    def _media_volume(self, level: int) -> str:
        try:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            key = Key.media_volume_up if level > self._state.get().volume else Key.media_volume_down
            for _ in range(5):
                kb.press(key)
                kb.release(key)
            return f"Adjusted volume toward {level}%."
        except Exception:
            return "Couldn't adjust volume."

    def _record_history(self, query: str, track: Optional[TrackInfo], source: str) -> None:
        if track:
            self._history.add(query, track.title, track.artist, source, track.url)
        else:
            self._history.add(query, source=source)

    def _current_from_active_provider(self) -> Optional[TrackInfo]:
        active_name = self._state.get().provider_name
        provider = self._provider_map.get(active_name)
        if provider:
            return provider.current_track()
        return None

    def _success_message(self, track: Optional[TrackInfo], source: str) -> str:
        if not track or not track.title:
            return f"Playing on {source}."
        source_label = source.replace("_", " ").title()
        extra = f" by {track.artist}" if track.artist else ""
        return f"Playing '{track.title}'{extra} on {source_label}."


__all__ = ["MusicController"]
