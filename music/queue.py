"""
music/queue.py — Session-local music queue.
============================================
Simple in-memory queue with shuffle, repeat, and basic persistence for
Gama's running session. Nothing here is written to disk unless requested.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class QueueItem:
    query: str
    title: str = ""
    artist: str = ""
    source: str = ""
    url: str = ""


class MusicQueue:
    """Thread-safe queue of upcoming tracks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: List[QueueItem] = []
        self._original: List[QueueItem] = []
        self._position: int = 0
        self._repeat_mode: str = "off"  # off | one | all
        self._shuffle: bool = False

    # --- state --------------------------------------------------------

    def is_empty(self) -> bool:
        with self._lock:
            return not self._items

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def position(self) -> int:
        with self._lock:
            return self._position

    # --- mutation -----------------------------------------------------

    def add(self, item: QueueItem) -> None:
        with self._lock:
            self._items.append(item)
            self._sync_original()

    def add_next(self, item: QueueItem) -> None:
        with self._lock:
            insert_at = min(self._position + 1, len(self._items))
            self._items.insert(insert_at, item)
            self._sync_original()

    def remove(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._items):
                self._items.pop(index)
                if self._position >= len(self._items):
                    self._position = max(0, len(self._items) - 1)
                self._sync_original()
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._original.clear()
            self._position = 0

    def set_repeat(self, mode: str) -> None:
        mode = mode.lower().strip()
        if mode in ("off", "one", "all"):
            with self._lock:
                self._repeat_mode = mode

    def set_shuffle(self, enabled: bool) -> None:
        with self._lock:
            if self._shuffle == enabled:
                return
            self._shuffle = enabled
            if enabled:
                self._original = list(self._items)
                # Keep already-played items in place up to current position,
                # shuffle the rest.
                rest = self._items[self._position + 1:]
                random.shuffle(rest)
                self._items = self._items[:self._position + 1] + rest
            else:
                # Restore original order but keep current position on the
                # currently playing item.
                if self._original:
                    current = self._items[self._position] if self._items else None
                    self._items = list(self._original)
                    if current:
                        try:
                            self._position = self._items.index(current)
                        except ValueError:
                            self._position = 0
                self._sync_original()

    # --- navigation ---------------------------------------------------

    def current(self) -> Optional[QueueItem]:
        with self._lock:
            if 0 <= self._position < len(self._items):
                return self._items[self._position]
            return None

    def next(self) -> Optional[QueueItem]:
        with self._lock:
            if self._repeat_mode == "one":
                return self._items[self._position] if self._items else None
            nxt = self._position + 1
            if nxt >= len(self._items):
                if self._repeat_mode == "all" and self._items:
                    nxt = 0
                else:
                    return None
            self._position = nxt
            return self._items[nxt]

    def previous(self) -> Optional[QueueItem]:
        with self._lock:
            prev = self._position - 1
            if prev < 0:
                if self._repeat_mode == "all" and self._items:
                    prev = len(self._items) - 1
                else:
                    return None
            self._position = prev
            return self._items[prev]

    def list_items(self) -> List[QueueItem]:
        with self._lock:
            return list(self._items)

    def _sync_original(self) -> None:
        if not self._shuffle:
            self._original = list(self._items)


__all__ = ["MusicQueue", "QueueItem"]
