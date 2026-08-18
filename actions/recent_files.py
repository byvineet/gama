"""
actions/recent_files.py — Recently-touched file/folder memory.

Purpose
-------
Gives commands like "delete it", "remove that folder", or "delete the
last downloaded file" something to resolve against. This is a tiny,
in-memory-only ring buffer — no disk I/O, no new services, negligible
CPU/RAM (a deque of <=25 small dataclasses).

Every mutating file operation (create_folder, move, copy, rename,
open_folder, download, ...) calls `record()` on success. Nothing here
is ever used to authorize a destructive action by itself — callers
(actions/context_resolver.py) still validate the resolved path exists
before acting on it.

Author : Gama
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

_MAX_ENTRIES = 25


@dataclass(frozen=True)
class RecentFileEntry:
    path: str
    op: str            # "created" | "moved" | "copied" | "renamed" | "opened" | "downloaded" | "listed"
    timestamp: float


class _RecentFilesTracker:
    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._entries: Deque[RecentFileEntry] = deque(maxlen=max_entries)

    def record(self, path: str, op: str) -> None:
        if not path:
            return
        with self._lock:
            # De-dupe: bump an existing entry for the same path to the front
            # instead of accumulating duplicates when a file is touched
            # repeatedly (e.g. re-opened).
            self._entries = deque(
                (e for e in self._entries if e.path != path),
                maxlen=self._entries.maxlen,
            )
            self._entries.append(RecentFileEntry(path=path, op=op, timestamp=time.time()))

    def recent(self, limit: int = 8, ops: Optional[List[str]] = None,
               max_age_seconds: Optional[float] = None) -> List[RecentFileEntry]:
        with self._lock:
            items = list(self._entries)
        items.reverse()  # most recent first
        if ops:
            items = [e for e in items if e.op in ops]
        if max_age_seconds is not None:
            cutoff = time.time() - max_age_seconds
            items = [e for e in items if e.timestamp >= cutoff]
        return items[:limit]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_tracker = _RecentFilesTracker()


def record(path: str, op: str) -> None:
    """Fire-and-forget; never raises so a tracking failure can't break
    the file operation that triggered it."""
    try:
        _tracker.record(str(path), op)
    except Exception:
        pass


def recent(limit: int = 8, ops: Optional[List[str]] = None,
           max_age_seconds: Optional[float] = None) -> List[RecentFileEntry]:
    try:
        return _tracker.recent(limit=limit, ops=ops, max_age_seconds=max_age_seconds)
    except Exception:
        return []


def clear() -> None:
    _tracker.clear()


__all__ = ["record", "recent", "clear", "RecentFileEntry"]
