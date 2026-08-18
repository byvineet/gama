"""
music/history.py — Recently played tracks.
============================================
Lightweight JSON-backed history for the Music Engine. Stores only the last
N plays so Gama can answer "what was that song?" or "play it again".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from utils.paths import user_data_path

logger = logging.getLogger(__name__)

_MAX_HISTORY = 50
_HISTORY_FILE = "music_history.json"


@dataclass
class HistoryEntry:
    query: str
    title: str = ""
    artist: str = ""
    source: str = ""
    url: str = ""
    played_at: float = field(default_factory=time.time)


class HistoryManager:
    """Thread-safe, disk-backed recent-play history."""

    def __init__(self, max_entries: int = _MAX_HISTORY,
                 file_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._max = max_entries
        self._path = file_path or user_data_path(_HISTORY_FILE)
        self._entries: List[HistoryEntry] = []
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data[-self._max:]:
                    self._entries.append(HistoryEntry(**item))
        except Exception:
            logger.debug("music history load failed", exc_info=True)
            self._entries = []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump([asdict(e) for e in self._entries[-self._max:]],
                          f, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("music history save failed", exc_info=True)

    def add(self, query: str, title: str = "", artist: str = "",
            source: str = "", url: str = "") -> None:
        entry = HistoryEntry(
            query=query, title=title, artist=artist,
            source=source, url=url, played_at=time.time(),
        )
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max:]
            self._save()

    def recent(self, n: int = 10) -> List[HistoryEntry]:
        with self._lock:
            return list(self._entries[-n:][::-1])

    def last(self) -> Optional[HistoryEntry]:
        with self._lock:
            return self._entries[-1] if self._entries else None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save()


__all__ = ["HistoryManager", "HistoryEntry"]
