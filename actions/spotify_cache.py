"""
actions/spotify_cache.py — Local Track Cache (Priority 1)
=============================================================
A lightweight, dependency-free JSON cache that maps a normalized
"<song> <artist>" query straight to a previously-resolved Spotify
Track URI. This is the fast path of the hybrid Spotify integration:
a cache hit skips both the Spotify Web API and any desktop
automation entirely — the URI is already known, so Gama can launch
it immediately.

Design
------
* Keyed by the same normalization spotify_controller.py already uses
  (lowercase, punctuation stripped, whitespace collapsed) so a cache
  hit is a single dict lookup — O(1).
* Fuzzy matching only kicks in on an *exact-key miss*, using
  difflib against the existing key set — bounded by cache size, and
  only pays that cost when the fast path already failed.
* Every hit updates last_played / play_count / updated so the cache
  doubles as Gama's "learned preference" for that query — repeated
  requests for the same phrasing become instant and never re-hit the
  Spotify Web API.
* Bounded size (default 500 entries) with LRU-by-last-played
  eviction, plus a stale-entry sweep (default 180 days unused) so the
  file never grows unbounded on a long-running install.

This module never talks to Spotify or Windows — it is pure local
storage, safe to import from anywhere without side effects beyond
disk I/O under memory/.

Author : Gama Spotify Hybrid Integration
"""

from __future__ import annotations

from utils.logger import get_logger

import difflib
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from utils.paths import user_data_path

log = get_logger(__name__)
logger = log  # back-compat alias
_CACHE_PATH = user_data_path("memory/spotify_track_cache.json")
_MAX_ENTRIES = 500
_STALE_SECONDS = 180 * 24 * 3600  # 180 days unused -> eligible for eviction
_FUZZY_THRESHOLD = 0.82  # high bar — a fuzzy cache hit skips the API entirely

_lock = threading.RLock()
_mem_cache: Optional[Dict[str, Any]] = None  # process-lifetime read cache


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_key(song: str, artist: str = "") -> str:
    return normalize(f"{song} {artist}".strip() or song)


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
        if len(data) > _MAX_ENTRIES:
            _evict(data)
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(_CACHE_PATH)
        except Exception:
            logger.debug("spotify_cache: save failed", exc_info=True)


def _evict(data: Dict[str, Any]) -> None:
    """Drop stale entries first, then trim to _MAX_ENTRIES by
    least-recently-played. Mutates `data` in place."""
    now = time.time()
    stale = [k for k, v in data.items()
             if now - float(v.get("last_played", v.get("updated", 0)) or 0) > _STALE_SECONDS]
    for k in stale:
        data.pop(k, None)

    if len(data) > _MAX_ENTRIES:
        ordered = sorted(data.items(), key=lambda kv: kv[1].get("last_played", 0))
        overflow = len(data) - _MAX_ENTRIES
        for k, _ in ordered[:overflow]:
            data.pop(k, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(song: str, artist: str = "") -> Optional[Dict[str, Any]]:
    """O(1) exact-key lookup; falls back to a bounded fuzzy match
    against existing keys only if the exact key isn't present."""
    key = make_key(song, artist)
    with _lock:
        data = _load()
        hit = data.get(key)
        if hit:
            return dict(hit, _key=key)

        if not data:
            return None
        close = difflib.get_close_matches(key, data.keys(), n=1, cutoff=_FUZZY_THRESHOLD)
        if close:
            fuzzy_key = close[0]
            logger.debug(f"spotify_cache: fuzzy hit '{key}' -> '{fuzzy_key}'")
            return dict(data[fuzzy_key], _key=fuzzy_key)
        return None


def put(song: str, artist: str, uri: str, title: str = "", track_artist: str = "",
        album: str = "") -> str:
    """Insert/overwrite a resolved track. Returns the cache key used."""
    key = make_key(song, artist)
    now = time.time()
    with _lock:
        data = _load()
        existing = data.get(key, {})
        data[key] = {
            "uri": uri,
            "title": title or existing.get("title", song),
            "artist": track_artist or existing.get("artist", artist),
            "album": album or existing.get("album", ""),
            "play_count": int(existing.get("play_count", 0)),
            "last_played": existing.get("last_played", now),
            "updated": now,
        }
        _save()
    return key


def record_play(key: str, confirmed_title: str = "", confirmed_artist: str = "") -> None:
    """Bump play_count / last_played on a cache entry — the signal
    that lets Gama learn which version of a song the user actually
    wants for a given query, and which artists they favor overall."""
    with _lock:
        data = _load()
        entry = data.get(key)
        if entry is None:
            return
        entry["play_count"] = int(entry.get("play_count", 0)) + 1
        entry["last_played"] = time.time()
        if confirmed_title:
            entry["title"] = confirmed_title
        if confirmed_artist:
            entry["artist"] = confirmed_artist
        _save()


def preferred_artists(min_play_count: int = 2) -> set:
    """Normalized set of artists the user has repeatedly played —
    used as a small scoring bonus when the Web API returns multiple
    plausible matches (Web API selection step, priority 2)."""
    with _lock:
        data = _load()
        out = set()
        for v in data.values():
            if int(v.get("play_count", 0)) >= min_play_count and v.get("artist"):
                out.add(normalize(v["artist"]))
        return out


def size() -> int:
    with _lock:
        return len(_load())


__all__ = ["normalize", "make_key", "get", "put", "record_play", "preferred_artists", "size"]
