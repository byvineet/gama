"""
state_engine/event_bus.py — thread-safe publish/subscribe bus.

Modules publish named events (with arbitrary payload kwargs) instead of
touching the UI or state directly. StateManager subscribes internally
to translate events into state transitions; anything else (a debug
panel, a future plugin) can subscribe independently without coupling
to StateManager's internals.

Designed to be cheap to call from a hot path (mic callback, per-frame
background tick): publish() never blocks on subscriber work — each callback
is invoked synchronously but is expected to be fast/non-blocking itself
(subscribers that need to do real work should hand off to asyncio /
a thread, same discipline as Qt signal handlers).
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

log = get_logger(__name__)
logger = log  # back-compat alias
@dataclass(frozen=True)
class Event:
    name: str
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[Event], None]


class EventBus:
    """One process-wide bus. Thread-safe: publish() and subscribe() can
    be called from any thread (mic thread, Qt thread, asyncio loop)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Subscriber]] = {}
        self._wildcard_subscribers: List[Subscriber] = []

    def subscribe(self, event_name: str, callback: Subscriber) -> None:
        """event_name == '*' subscribes to every event."""
        with self._lock:
            if event_name == "*":
                self._wildcard_subscribers.append(callback)
            else:
                self._subscribers.setdefault(event_name, []).append(callback)

    def unsubscribe(self, event_name: str, callback: Subscriber) -> None:
        with self._lock:
            try:
                if event_name == "*":
                    self._wildcard_subscribers.remove(callback)
                else:
                    self._subscribers.get(event_name, []).remove(callback)
            except ValueError:
                pass

    def publish(self, event_name: str, **data: Any) -> Event:
        evt = Event(name=event_name, data=data)
        with self._lock:
            targets = list(self._subscribers.get(event_name, ())) + list(self._wildcard_subscribers)
        for cb in targets:
            try:
                cb(evt)
            except Exception:
                # A misbehaving subscriber (e.g. a debug panel) must never
                # take down the module that published the event.
                logger.exception(f"EventBus: subscriber for '{event_name}' raised")
        return evt


# Process-wide singleton — every module imports this same instance.
event_bus = EventBus()


class QtEventBusDispatcher:
    """Helper to marshal EventBus notifications safely to the PySide6 UI thread (Phase 3.2)."""

    def __init__(self, bus: EventBus = event_bus):
        self.bus = bus
        self._signal_emitter = None
        self._init_qt()

    def _init_qt(self) -> None:
        try:
            from PySide6.QtCore import QObject, Signal
            class _Emitter(QObject):
                event_emitted = Signal(str, dict)

            self._signal_emitter = _Emitter()
        except Exception:
            self._signal_emitter = None

    def publish_ui(self, event_name: str, **data: Any) -> None:
        if self._signal_emitter:
            try:
                self._signal_emitter.event_emitted.emit(event_name, data)
                return
            except Exception:
                pass
        self.bus.publish(event_name, **data)

