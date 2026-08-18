"""
state_engine/timeline.py — rolling history of state transitions.

Keeps the last N entries (configurable) in memory only — this is a
debugging/transparency aid, not a persisted audit log.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional


@dataclass(frozen=True)
class TimelineEntry:
    timestamp: float
    kind: str          # "primary" | "activity" | "mood" | "event"
    value: str
    detail: str = ""

    @property
    def time_str(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))

    def __str__(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{self.time_str} {self.value}{suffix}"


class StateTimeline:
    def __init__(self, max_entries: int = 500) -> None:
        self._lock = threading.RLock()
        self._entries: Deque[TimelineEntry] = deque(maxlen=max_entries)

    def add(self, kind: str, value: str, detail: str = "") -> TimelineEntry:
        entry = TimelineEntry(timestamp=time.time(), kind=kind, value=value, detail=detail)
        with self._lock:
            self._entries.append(entry)
        return entry

    def recent(self, limit: Optional[int] = None, kind: Optional[str] = None) -> List[TimelineEntry]:
        with self._lock:
            items = list(self._entries)
        if kind:
            items = [e for e in items if e.kind == kind]
        if limit:
            items = items[-limit:]
        return items

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
