"""
actions/spotify_web.py — Hybrid Spotify Playback (Priorities 1-4)
=======================================================================
Replaces "search Spotify's own UI" as the default way Gama plays a
requested track. Only the last-resort path (actions/spotify_controller.py)
still touches Spotify's desktop UI at all.

Priority chain for every "Play <song> on Spotify" request:

    1. Local Track Cache   (actions/spotify_cache.py)   — O(1), no network
    2. Spotify Web API     (search + score candidates)  — only on cache miss
    3. Spotify URI Playback (os.startfile("spotify:track:..."))
    4. Desktop Automation  (actions/spotify_controller.py) — last resort only

Every stage is logged with the exact [Spotify] tags Gama's other
automation modules use, so this shows up consistently in logs/gama.log.

Nothing here ever falls into an unbounded retry loop: each stage
either produces a usable result or falls through to the next
priority exactly once. If everything fails, the very last thing this
module does is hand off to the existing keyboard-driven automation,
unchanged.

Author : Gama Spotify Hybrid Integration
"""

from __future__ import annotations

from utils.logger import get_logger

import asyncio
import difflib
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from actions import spotify_auth, spotify_cache
from utils.http_pool import get_session, HTTP_TIMEOUT

log = get_logger(__name__)
logger = log  # back-compat alias
_IS_WINDOWS = os.name == "nt"

SEARCH_URL = "https://api.spotify.com/v1/search"
_HTTP_TIMEOUT = HTTP_TIMEOUT  # 5s strict, pooled/keep-alive session
_MAX_API_ATTEMPTS = 2  # first try + one bounded retry (e.g. after a 429 or token refresh)


# ---------------------------------------------------------------------------
# Shared verification / normalization — reused from spotify_controller.py so
# "is this really the track playing" is judged identically everywhere in
# Gama, whether the track was launched via URI or via desktop automation.
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return spotify_cache.normalize(text)


async def _verify_playback(query: str, timeout: float) -> Optional[Dict[str, Any]]:
    try:
        from actions.spotify_controller import _smtc_available, _spotify_now_playing, _matches_query
    except Exception:
        logger.debug("spotify_web: could not import SMTC helpers from spotify_controller", exc_info=True)
        return None

    if not _smtc_available():
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = await _spotify_now_playing()
        if info and _matches_query(query, info):
            return info
        await asyncio.sleep(0.3)
    return None


# ---------------------------------------------------------------------------
# Priority 3 — URI playback (never UI automation)
# ---------------------------------------------------------------------------

def _launch_uri(uri: str) -> bool:
    """Open a spotify:track:... URI directly. This launches Spotify if
    needed and starts playback of that exact track — no search, no
    dropdown, no keyboard input into the app at all."""
    if not _IS_WINDOWS:
        return False
    try:
        os.startfile(uri)
        return True
    except Exception:
        logger.debug(f"spotify_web: URI launch failed for {uri}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Priority 2 — Spotify Web API search + best-match selection
# ---------------------------------------------------------------------------

def _explicit_preference() -> str:
    """'avoid' | 'prefer' | 'any' — read from config, defaults to 'any'
    (no preference) so this never silently filters results unless the
    user has actually configured a preference."""
    try:
        from utils.paths import user_data_path
        import json
        with open(user_data_path("config/api_keys.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        val = str(data.get("spotify_explicit_preference", "any")).strip().lower()
        return val if val in ("avoid", "prefer") else "any"
    except Exception:
        return "any"


def _score_candidate(song: str, artist: str, item: Dict[str, Any],
                      preferred_artists: set) -> float:
    title = item.get("name", "")
    artists = item.get("artists", []) or []
    primary_artist = artists[0]["name"] if artists else ""

    title_sim = difflib.SequenceMatcher(None, _normalize(song), _normalize(title)).ratio()
    popularity = (item.get("popularity", 0) or 0) / 100.0
    artist_bonus = 0.08 if _normalize(primary_artist) in preferred_artists else 0.0

    explicit_penalty = 0.0
    pref = _explicit_preference()
    if item.get("explicit"):
        if pref == "avoid":
            explicit_penalty = 0.20
        elif pref == "prefer":
            explicit_penalty = -0.05

    if artist:
        artist_sim = difflib.SequenceMatcher(None, _normalize(artist), _normalize(primary_artist)).ratio()
        # Also check against the full artist credit list (features etc.)
        all_artists_norm = _normalize(", ".join(a.get("name", "") for a in artists))
        if _normalize(artist) in all_artists_norm:
            artist_sim = max(artist_sim, 0.9)
        score = (title_sim * 0.50) + (artist_sim * 0.25) + (popularity * 0.15) + artist_bonus
    else:
        score = (title_sim * 0.70) + (popularity * 0.20) + artist_bonus

    return score - explicit_penalty


def _error_detail(resp: requests.Response) -> str:
    """Best-effort ' — <spotify message>' suffix for log lines. Spotify
    error bodies look like {"error": {"status": 403, "message": "..."}}
    — surfacing that beats a bare status code when diagnosing why a
    call failed (expired dev-mode token, missing allowlist entry, a
    malformed query, etc.)."""
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message")
        return f" — {msg}" if msg else ""
    except Exception:
        return ""


def _request_search(token: str, query: str) -> requests.Response:
    return get_session().get(
        SEARCH_URL,
        params={"q": query, "type": "track", "limit": 10},
        headers={"Authorization": f"Bearer {token}"},
        timeout=_HTTP_TIMEOUT,
    )


async def _search_best_match(song: str, artist: str) -> Optional[Dict[str, Any]]:
    query = f"{song} {artist}".strip() or song.strip()
    preferred = spotify_cache.preferred_artists()

    for attempt in range(1, _MAX_API_ATTEMPTS + 1):
        token = await spotify_auth.get_access_token()
        if not token:
            logger.info("[Spotify] Web API not authenticated — skipping search")
            return None

        try:
            resp = await asyncio.to_thread(_request_search, token, query)
        except requests.exceptions.RequestException:
            logger.warning("[Spotify] Web API request failed (network/timeout)")
            return None

        if resp.status_code == 401:
            # Access token stale despite our cache — force one silent
            # refresh and retry once; never prompt the user mid-request.
            spotify_auth.invalidate_access_token()
            if attempt < _MAX_API_ATTEMPTS:
                continue
            return None

        if resp.status_code == 429:
            retry_after = min(float(resp.headers.get("Retry-After", 1)), 5.0)
            logger.info(f"[Spotify] Web API rate-limited — waiting {retry_after:.1f}s")
            await asyncio.sleep(retry_after)
            if attempt < _MAX_API_ATTEMPTS:
                continue
            return None

        if resp.status_code == 403:
            # Almost never a bug in the request itself — Spotify returns
            # 403 (not 401) when the token is valid but not *authorized*
            # for this call. By far the most common cause for a personal
            # app: it's still in Development Mode and the logged-in
            # Spotify account hasn't been added under the app's
            # Dashboard -> Settings -> User Management allowlist (up to
            # 25 users). Surfacing Spotify's own message makes the real
            # cause visible in the log instead of just "HTTP 403".
            detail = _error_detail(resp)
            logger.warning(
                f"[Spotify] Web API search forbidden (HTTP 403){detail} — if this app is "
                f"still in Development Mode on the Spotify Dashboard, add this account "
                f"under Settings -> User Management, or request Extended Quota Mode."
            )
            return None

        if resp.status_code != 200:
            logger.warning(f"[Spotify] Web API search failed (HTTP {resp.status_code}){_error_detail(resp)}")
            return None

        try:
            items = (resp.json().get("tracks", {}) or {}).get("items", []) or []
        except Exception:
            return None

        if not items:
            return None

        best = max(items, key=lambda it: _score_candidate(song, artist, it, preferred))
        artists = best.get("artists", []) or []
        return {
            "uri": best.get("uri", ""),
            "title": best.get("name", song),
            "artist": artists[0]["name"] if artists else artist,
            "album": (best.get("album") or {}).get("name", ""),
        }

    return None


# ---------------------------------------------------------------------------
# Fallback — hand off to the existing keyboard-driven automation
# ---------------------------------------------------------------------------

async def _fallback_to_desktop_automation(song: str, artist: str, reason: str) -> str:
    logger.info(f"[Spotify] Falling back to desktop automation ({reason})")
    try:
        from actions.spotify_controller import spotify_play_async
        return await spotify_play_async(song, artist)
    except Exception:
        logger.error("[Spotify] Desktop automation fallback also failed", exc_info=True)
        return f"Couldn't play '{song}' on Spotify through any available method."


# ---------------------------------------------------------------------------
# Public entry point — the full hybrid flow
# ---------------------------------------------------------------------------

async def play_async(song: str, artist: str = "") -> str:
    if not song or not song.strip():
        return "I need a song name to play on Spotify."

    query = f"{song} {artist}".strip() or song.strip()

    if not _IS_WINDOWS:
        return await _fallback_to_desktop_automation(song, artist, "non-Windows platform")

    # -- Priority 1: Local Track Cache --------------------------------------
    cached = spotify_cache.get(song, artist)
    resolved: Optional[Dict[str, Any]] = None
    cache_key: Optional[str] = None

    if cached:
        logger.info("[Spotify] Cache Hit")
        resolved = cached
        cache_key = cached.get("_key") or spotify_cache.make_key(song, artist)
    else:
        logger.info("[Spotify] Cache Miss")
        # -- Priority 2: Spotify Web API ------------------------------------
        logger.info("[Spotify] Searching Spotify API")
        match = await _search_best_match(song, artist)
        if match and match.get("uri"):
            logger.info(f"[Spotify] Best Match Selected: {match['title']} — {match.get('artist', '')}")
            cache_key = spotify_cache.put(
                song, artist, match["uri"], match.get("title", ""),
                match.get("artist", ""), match.get("album", ""),
            )
            logger.info("[Spotify] URI Cached")
            resolved = match

    if not resolved or not resolved.get("uri"):
        return await _fallback_to_desktop_automation(
            song, artist, "no track resolved via cache or Web API")

    # -- Priority 3: Spotify URI Playback ------------------------------------
    uri = resolved["uri"]
    if not _launch_uri(uri):
        return await _fallback_to_desktop_automation(song, artist, "URI launch failed")
    logger.info(f"[Spotify] Launching URI: {uri}")

    # -- Playback verification (Windows Global Media Session / SMTC) --------
    info = await _verify_playback(query, timeout=6.0)
    if not info:
        return await _fallback_to_desktop_automation(
            song, artist, "playback could not be verified after URI launch")

    logger.info("[Spotify] Playback Verified")
    if cache_key:
        spotify_cache.record_play(cache_key, info.get("title", ""), info.get("artist", ""))
        logger.info("[Spotify] Cache Updated")

    title = info.get("title") or resolved.get("title") or song
    artist_out = info.get("artist") or resolved.get("artist") or artist
    return f"Playing '{title}'" + (f" by {artist_out}" if artist_out else "") + " on Spotify."


def play(song: str, artist: str = "") -> str:
    """Sync wrapper for callers (media_controller.py) that aren't
    async-aware yet — mirrors spotify_controller.spotify_play()."""
    try:
        return asyncio.run(play_async(song, artist))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(play_async(song, artist))
        finally:
            loop.close()


__all__ = ["play", "play_async"]
