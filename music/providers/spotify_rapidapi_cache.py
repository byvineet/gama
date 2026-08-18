"""
music/providers/spotify_rapidapi_cache.py — Smart local cache for the
Spotify Desktop (RapidAPI) provider.
=============================================================================
A dependency-free JSON cache that maps a normalized search query straight
to a previously-resolved track (URI + metadata). This is what lets Gama
skip the RapidAPI network round-trip entirely for anything it has already
played before.

Design
------
* Keyed by a normalized query string (lowercase, punctuation stripped,
  whitespace collapsed) — a cache hit is a single dict lookup.
* Every entry carries a ``last_updated`` timestamp; entries older than
  ``STALE_SECONDS`` (default 30 days) are treated as expired and are
  swept out automatically, so a query is transparently re-resolved
  against the API once its cached result goes stale.
* Persisted to ``memory/spotify_rapidapi_cache.json`` (same folder Gama
  already uses for its other local caches) and loaded once per process,
  then kept in memory for fast repeated access.
* Pure local storage — this module never talks to Spotify, RapidAPI, or
  Windows, so it's always safe to import.

This is intentionally separate from ``actions/spotify_cache.py`` (used by
the OAuth-based Spotify Web provider) so the two Spotify code paths never
share state or accidentally cross-pollinate results from two different
search backends.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from utils.paths import user_data_path

logger = logging.getLogger(__name__)

_CACHE_PATH = user_data_path("memory/spotify_rapidapi_cache.json")
_MAX_ENTRIES = 500
_STALE_SECONDS = 30 * 24 * 3600  # 30 days

_lock = threading.RLock()
_mem_cache: Optional[Dict[str, Any]] = None


@dataclass
class CachedTrack:
    """Everything the provider needs to launch playback without ever
    calling RapidAPI again for this query."""
    query: str = ""
    track_id: str = ""
    uri: str = ""
    title: str = ""
    artists: str = ""
    album: str = ""
    artwork: str = ""
    duration_ms: int = 0
    share_url: str = ""
    last_updated: float = field(default_factory=time.time)

    def is_stale(self, ttl_seconds: int = _STALE_SECONDS) -> bool:
        return (time.time() - float(self.last_updated or 0)) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CachedTrack":
        return cls(
            query=d.get("query", ""),
            track_id=d.get("track_id", ""),
            uri=d.get("uri", ""),
            title=d.get("title", ""),
            artists=d.get("artists", ""),
            album=d.get("album", ""),
            artwork=d.get("artwork", ""),
            duration_ms=int(d.get("duration_ms", 0) or 0),
            share_url=d.get("share_url", ""),
            last_updated=float(d.get("last_updated", 0) or 0),
        )


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------

def _load() -> Dict[str, Any]:
    global _mem_cache
    with _lock:
        if _mem_cache is not None:
            return _mem_cache
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _mem_cache = data
        return _mem_cache


def _save() -> None:
    with _lock:
        data = _mem_cache or {}
        _sweep_stale(data)
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(_CACHE_PATH)
        except Exception:
            logger.debug("spotify_rapidapi_cache: save failed", exc_info=True)


def _sweep_stale(data: Dict[str, Any]) -> None:
    """Drop expired entries first, then trim to _MAX_ENTRIES by
    least-recently-updated. Mutates `data` in place."""
    now = time.time()
    stale = [k for k, v in data.items()
             if now - float(v.get("last_updated", 0) or 0) > _STALE_SECONDS]
    for k in stale:
        data.pop(k, None)

    if len(data) > _MAX_ENTRIES:
        ordered = sorted(data.items(), key=lambda kv: kv[1].get("last_updated", 0))
        overflow = len(data) - _MAX_ENTRIES
        for k, _ in ordered[:overflow]:
            data.pop(k, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(query: str) -> Optional[CachedTrack]:
    """Return a fresh cached entry for `query`, or None on a miss or an
    expired entry (expired entries are dropped as a side effect)."""
    key = normalize(query)
    if not key:
        return None
    with _lock:
        data = _load()
        raw = data.get(key)
        if not raw:
            return None
        entry = CachedTrack.from_dict(raw)
        if entry.is_stale():
            logger.debug("spotify_rapidapi_cache: '%s' expired — evicting", key)
            data.pop(key, None)
            _save()
            return None
        return entry


def put(query: str, track: CachedTrack) -> None:
    """Insert/overwrite a resolved track for `query`."""
    key = normalize(query)
    if not key:
        return
    track.query = query
    track.last_updated = time.time()
    with _lock:
        data = _load()
        data[key] = track.to_dict()
        _save()


def purge_stale() -> int:
    """Remove every expired entry now. Returns the number removed."""
    with _lock:
        data = _load()
        before = len(data)
        _sweep_stale(data)
        removed = before - len(data)
        if removed:
            _save()
        return removed


def size() -> int:
    with _lock:
        return len(_load())


__all__ = ["CachedTrack", "normalize", "get", "put", "purge_stale", "size"]
