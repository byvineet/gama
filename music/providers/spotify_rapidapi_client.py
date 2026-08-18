"""
music/providers/spotify_rapidapi_client.py — Spotify23 RapidAPI search client.
=================================================================================
Thin, dependency-light wrapper around the Spotify23 RapidAPI search
endpoint (https://spotify23.p.rapidapi.com/search/). This is the *only*
module in the Spotify Desktop provider that talks to the network.

    GET https://spotify23.p.rapidapi.com/search/?q=<query>&type=tracks
    Headers:
        x-rapidapi-key:  <loaded from config, never hardcoded>
        x-rapidapi-host: spotify23.p.rapidapi.com
        Content-Type:    application/json

Nothing here ever raises out to the caller — every failure mode (missing
key, network error, bad response shape, rate limiting) is caught and
turned into `None` / an empty list, with a log line explaining why.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "https://spotify23.p.rapidapi.com/search/"
RAPIDAPI_HOST = "spotify23.p.rapidapi.com"
_HTTP_TIMEOUT = 6.0


# ---------------------------------------------------------------------------
# API key — loaded from config, never hardcoded. Checks the encrypted
# credential store first (same convention as core/config_manager.gemini_key),
# then falls back to the plaintext config/api_keys.json field.
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    try:
        from security.credential_store import get_secret
        stored = get_secret("spotify_rapidapi_key")
        if stored:
            return stored.strip()
    except Exception:
        pass
    try:
        from core.config_manager import config
        raw = str(config.get("spotify_rapidapi_key", "") or "").strip()
        if raw and raw.lower() not in ("", "your_rapidapi_key_here", "your-rapidapi-key"):
            return raw
    except Exception:
        logger.debug("spotify_rapidapi_client: config lookup failed", exc_info=True)
    return ""


def is_configured() -> bool:
    return bool(get_api_key())


# ---------------------------------------------------------------------------
# Result normalization
# ---------------------------------------------------------------------------

def _normalize_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one raw `tracks.items[]` entry into Gama's flat track shape,
    or None if it isn't playable / isn't shaped like a track at all."""
    try:
        # Spotify23 sometimes nests the actual track under "data" or
        # "track" depending on the search flavor — handle both.
        data = item.get("data") if isinstance(item.get("data"), dict) else item

        track_id = data.get("id") or ""
        name = data.get("name") or ""
        if not track_id or not name:
            return None

        playable = data.get("playability", {}).get("playable", True) \
            if isinstance(data.get("playability"), dict) else data.get("is_playable", True)
        if playable is False:
            return None

        artists_raw = data.get("artists") or {}
        if isinstance(artists_raw, dict):
            artist_items = artists_raw.get("items", []) or []
        else:
            artist_items = artists_raw or []
        artist_names = []
        for a in artist_items:
            prof = a.get("profile", {}) if isinstance(a, dict) else {}
            n = prof.get("name") or a.get("name") or ""
            if n:
                artist_names.append(n)
        artists = ", ".join(artist_names)

        album = data.get("albumOfTrack") or data.get("album") or {}
        album_name = album.get("name", "") if isinstance(album, dict) else ""

        artwork = ""
        cover = album.get("coverArt") if isinstance(album, dict) else None
        if isinstance(cover, dict):
            sources = cover.get("sources", []) or []
            if sources:
                # Largest first for quality, but any is fine as a fallback.
                sources = sorted(sources, key=lambda s: s.get("width", 0) or 0, reverse=True)
                artwork = sources[0].get("url", "")
        elif isinstance(data.get("album"), dict):
            images = data["album"].get("images", []) or []
            if images:
                artwork = images[0].get("url", "")

        duration_ms = 0
        dur = data.get("duration") or {}
        if isinstance(dur, dict):
            duration_ms = int(dur.get("totalMilliseconds", 0) or 0)
        elif isinstance(data.get("duration_ms"), (int, float)):
            duration_ms = int(data["duration_ms"])

        popularity = data.get("popularity", 0) or 0
        if isinstance(data.get("trackPopularity"), (int, float)):
            popularity = data["trackPopularity"]

        uri = data.get("uri") or f"spotify:track:{track_id}"

        return {
            "id": track_id,
            "uri": uri,
            "title": name,
            "artists": artists,
            "album": album_name,
            "artwork": artwork,
            "duration_ms": duration_ms,
            "share_url": f"https://open.spotify.com/track/{track_id}",
            "popularity": popularity,
            "explicit": bool(data.get("explicit", {}).get("isExplicit", False))
                if isinstance(data.get("explicit"), dict) else bool(data.get("explicit", False)),
        }
    except Exception:
        logger.debug("spotify_rapidapi_client: failed to normalize item", exc_info=True)
        return None


def _extract_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Spotify23's response shape has shifted between deployments; handle
    the two shapes seen in practice:
        {"tracks": {"items": [...]}}
        {"tracks": {"items": [{"data": {...}}, ...]}}
    """
    tracks = payload.get("tracks") or {}
    if isinstance(tracks, dict):
        items = tracks.get("items", []) or []
    elif isinstance(tracks, list):
        items = tracks
    else:
        items = []
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_tracks(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search Spotify23 (RapidAPI) for `query` and return a list of
    normalized, playable track candidates (best-effort, may be empty).
    Never raises."""
    query = (query or "").strip()
    if not query:
        return []

    api_key = get_api_key()
    if not api_key:
        logger.info("[SpotifyRapidAPI] No API key configured — skipping search")
        return []

    try:
        resp = requests.get(
            SEARCH_URL,
            params={"q": query, "type": "tracks"},
            headers={
                "x-rapidapi-key": api_key,
                "x-rapidapi-host": RAPIDAPI_HOST,
                "Content-Type": "application/json",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        logger.warning("[SpotifyRapidAPI] Search request failed (network/timeout)")
        return []

    if resp.status_code == 429:
        logger.warning("[SpotifyRapidAPI] Rate-limited by RapidAPI")
        return []
    if resp.status_code == 401 or resp.status_code == 403:
        logger.warning("[SpotifyRapidAPI] Auth rejected (HTTP %s) — check the "
                        "'spotify_rapidapi_key' subscription/quota", resp.status_code)
        return []
    if resp.status_code != 200:
        logger.warning("[SpotifyRapidAPI] Search failed (HTTP %s)", resp.status_code)
        return []

    try:
        payload = resp.json()
    except Exception:
        logger.warning("[SpotifyRapidAPI] Response wasn't valid JSON")
        return []

    items = _extract_items(payload)
    results: List[Dict[str, Any]] = []
    for item in items[:max(limit, 1)]:
        normalized = _normalize_item(item)
        if normalized:
            results.append(normalized)
    return results


__all__ = ["search_tracks", "is_configured", "get_api_key", "SEARCH_URL", "RAPIDAPI_HOST"]
