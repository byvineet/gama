"""
music/providers/spotify_desktop.py — Spotify Desktop Provider.
===============================================================
Fully automated Spotify playback: no window focus, no keyboard typing
into Spotify's UI, no manual track picking. The flow is:

    User query
        -> normalized cache lookup                (spotify_rapidapi_cache)
        -> Spotify23 RapidAPI search on cache miss (spotify_rapidapi_client)
        -> best playable match selected            (this module)
        -> os.startfile("spotify:track:<id>")      (launches Spotify Desktop)
        -> playback verified via Windows GSMTC      (music.media_session)
        -> cache updated for next time

If Spotify Desktop isn't installed, RapidAPI has no key configured, the
network is unavailable, or playback can't be verified, `play()` simply
returns False so the MusicController moves on to the next provider
(Spotify Web, YouTube Music, ...) — this provider never raises out to
its caller and never exposes a raw exception to the user.
"""

from __future__ import annotations

import difflib
import logging
import os
import random
import time
from typing import Any, Dict, Optional

from music.providers.base import BaseProvider, TrackInfo
from music.providers import spotify_rapidapi_cache as cache
from music.providers import spotify_rapidapi_client as api

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"
_VERIFY_TIMEOUT = 6.0   # seconds to wait for GSMTC to confirm playback
_VERIFY_POLL = 0.3

# Spoken the moment the URI has been launched, *while* GSMTC verification
# runs in the background — so Gama talks and confirms in parallel instead
# of going quiet during the 1-6s it takes to verify playback.
_PLAYING_LINES = [
    "Okay sir, playing {title}.",
    "Sure sir, playing {title} now.",
    "On it, sir — playing {title}.",
    "Right away, sir. Playing {title}.",
]
_PLAYING_LINES_NO_TITLE = [
    "Okay sir, playing that now.",
    "Sure sir, one moment — playing it now.",
    "On it, sir.",
]


def _speak(text: str) -> None:
    """Fire-and-forget scripted speech via Gama's SpeechManager. Never
    blocks and never raises — if voice isn't available (headless/dev
    environment), this is a silent no-op."""
    try:
        from voice import speech_manager
        from voice.speech_manager import Priority
        speech_manager.say(text, priority=Priority.ACK, kind="music")
    except Exception:
        logger.debug("[SpotifyDesktop] speech_manager unavailable — skipping ack", exc_info=True)


class SpotifyDesktopProvider(BaseProvider):
    """Spotify Desktop app integration — RapidAPI search + URI playback."""

    name = "spotify_desktop"

    def __init__(self) -> None:
        self._last_query: str = ""
        self._last_track: Optional[TrackInfo] = None
        self._media = None  # lazy: music.media_session.MediaSessionManager

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        # We can always *attempt* to launch Spotify via its URI protocol
        # on Windows; there's no reliable pre-check besides that.
        return _IS_WINDOWS

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> Optional[Dict[str, Any]]:
        """Resolve `query` to a single best playable track dict, using
        the cache first and RapidAPI on a cache miss/expiry. Returns
        None if nothing playable could be found. Never raises."""
        query = (query or "").strip()
        if not query:
            return None

        cached = cache.get(query)
        if cached:
            logger.info("[SpotifyDesktop] Cache hit for '%s'", query)
            return {
                "id": cached.track_id,
                "uri": cached.uri,
                "title": cached.title,
                "artists": cached.artists,
                "album": cached.album,
                "artwork": cached.artwork,
                "duration_ms": cached.duration_ms,
                "share_url": cached.share_url,
            }

        logger.info("[SpotifyDesktop] Cache miss for '%s' — searching RapidAPI", query)
        try:
            candidates = api.search_tracks(query)
        except Exception:
            logger.debug("[SpotifyDesktop] RapidAPI search raised", exc_info=True)
            return None

        if not candidates:
            logger.info("[SpotifyDesktop] No playable results for '%s'", query)
            return None

        best = self._select_best(query, candidates)
        if not best or not best.get("uri"):
            return None

        logger.info("[SpotifyDesktop] Best match: '%s' by %s",
                    best.get("title", ""), best.get("artists", ""))

        cache.put(query, cache.CachedTrack(
            query=query,
            track_id=best.get("id", ""),
            uri=best.get("uri", ""),
            title=best.get("title", ""),
            artists=best.get("artists", ""),
            album=best.get("album", ""),
            artwork=best.get("artwork", ""),
            duration_ms=int(best.get("duration_ms", 0) or 0),
            share_url=best.get("share_url", ""),
        ))
        return best

    @staticmethod
    def _select_best(query: str, candidates: list) -> Optional[Dict[str, Any]]:
        """Pick the best playable candidate: weighted by title similarity
        to the query and popularity. Non-playable tracks are already
        filtered out by the client's normalization step."""
        if not candidates:
            return None

        norm_query = cache.normalize(query)

        def score(item: Dict[str, Any]) -> float:
            title_sim = difflib.SequenceMatcher(
                None, norm_query, cache.normalize(item.get("title", ""))
            ).ratio()
            combined_sim = difflib.SequenceMatcher(
                None, norm_query,
                cache.normalize(f"{item.get('title', '')} {item.get('artists', '')}"),
            ).ratio()
            popularity = (item.get("popularity", 0) or 0) / 100.0
            return max(title_sim, combined_sim) * 0.8 + popularity * 0.2

        return max(candidates, key=score)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def play(self, query: str) -> bool:
        if not _IS_WINDOWS:
            logger.info("[SpotifyDesktop] Not on Windows — URI launch unsupported")
            return False

        try:
            track = self.search(query)
        except Exception:
            logger.debug("[SpotifyDesktop] search() raised", exc_info=True)
            track = None

        if not track or not track.get("uri"):
            logger.info("[SpotifyDesktop] Could not resolve '%s' to a playable track", query)
            return False

        self._last_query = query
        if not self._launch_uri(track["uri"]):
            return False

        # Speak immediately — this call is fire-and-forget, so the TTS
        # plays on its own thread while we verify playback below, giving
        # the "I'm already on it" feel instead of a silent pause.
        title = track.get("title", "")
        if title:
            _speak(random.choice(_PLAYING_LINES).format(title=title))
        else:
            _speak(random.choice(_PLAYING_LINES_NO_TITLE))

        if not self._verify_playback(track):
            logger.info("[SpotifyDesktop] Launched '%s' but playback could not be verified",
                        track.get("title", query))
            return False

        logger.info("[SpotifyDesktop] Verified playback of '%s'", track.get("title", query))
        return True

    def play_url(self, url: str) -> bool:
        if not _IS_WINDOWS or not url:
            return False
        uri = url
        if url.startswith("https://open.spotify.com/track/"):
            track_id = url.rstrip("/").split("/")[-1].split("?")[0]
            uri = f"spotify:track:{track_id}"
        elif not url.startswith("spotify:"):
            return False
        if not self._launch_uri(uri):
            return False
        return self._verify_playback(None)

    def _launch_uri(self, uri: str) -> bool:
        """Open a spotify:track:... URI directly via the OS shell. This
        launches Spotify Desktop if it isn't running and starts playback
        of that exact track — no window focus, no search box, no
        simulated clicks or keystrokes."""
        try:
            os.startfile(uri)  # noqa: this module only ever runs on Windows
            logger.info("[SpotifyDesktop] Launched URI: %s", uri)
            return True
        except FileNotFoundError:
            logger.warning("[SpotifyDesktop] Spotify Desktop doesn't appear to be installed "
                            "(URI protocol handler missing)")
            return False
        except Exception:
            logger.debug("[SpotifyDesktop] URI launch failed for %s", uri, exc_info=True)
            return False

    def _verify_playback(self, track: Optional[Dict[str, Any]]) -> bool:
        """Wait briefly, then confirm via Windows GSMTC that Spotify is
        actually playing (ideally the requested track)."""
        media = self._media_session()
        if media is None or not media.is_available():
            # No GSMTC available on this system — best effort: assume the
            # URI launch worked rather than failing the whole provider.
            time.sleep(1.5)
            return True

        # Give Spotify a moment to report into GSMTC before polling.
        time.sleep(1.0)

        deadline = time.monotonic() + _VERIFY_TIMEOUT
        expected_title = cache.normalize(track.get("title", "")) if track else ""
        while time.monotonic() < deadline:
            info = media.current_track(app_hint="spotify")
            if info and info.is_playing:
                if not expected_title or expected_title in cache.normalize(info.title):
                    self._last_track = TrackInfo(
                        title=info.title or (track or {}).get("title", ""),
                        artist=info.artist or (track or {}).get("artists", ""),
                        album=info.album or (track or {}).get("album", ""),
                        duration=info.duration or 0.0,
                        position=info.position or 0.0,
                        source=self.name,
                        url=(track or {}).get("share_url", ""),
                        artwork=(track or {}).get("artwork", ""),
                        is_playing=True,
                    )
                    return True
            time.sleep(_VERIFY_POLL)
        return False

    def _media_session(self):
        if self._media is None:
            try:
                from music.media_session import MediaSessionManager
                self._media = MediaSessionManager()
            except Exception:
                logger.debug("[SpotifyDesktop] MediaSessionManager unavailable", exc_info=True)
                return None
        return self._media

    # ------------------------------------------------------------------
    # Transport controls — delegated to Windows GSMTC, scoped to Spotify
    # so these never accidentally control a different app's session.
    # ------------------------------------------------------------------

    def pause(self) -> bool:
        media = self._media_session()
        return bool(media and media.send("pause", app_hint="spotify"))

    def resume(self) -> bool:
        media = self._media_session()
        return bool(media and media.send("play", app_hint="spotify"))

    def stop(self) -> bool:
        media = self._media_session()
        return bool(media and media.send("stop", app_hint="spotify"))

    def next(self) -> bool:
        media = self._media_session()
        return bool(media and media.send("next", app_hint="spotify"))

    def previous(self) -> bool:
        media = self._media_session()
        return bool(media and media.send("previous", app_hint="spotify"))

    def seek(self, seconds: float) -> bool:
        return False

    def set_volume(self, percent: int) -> bool:
        return False

    def current_track(self) -> Optional[TrackInfo]:
        media = self._media_session()
        if media:
            track = media.current_track(app_hint="spotify")
            if track:
                return track
        return self._last_track

    def is_playing(self) -> bool:
        track = self.current_track()
        return bool(track and track.is_playing)


__all__ = ["SpotifyDesktopProvider"]
