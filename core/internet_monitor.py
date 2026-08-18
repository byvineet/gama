"""
core/internet_monitor.py — Lightweight async internet connectivity monitor
==========================================================================
Checks whether outbound internet is available by attempting a short TCP
connection to Google's public DNS (8.8.8.8:53).  Runs as a persistent
asyncio background task; fires registered callbacks whenever reachability
flips so the rest of the system can react immediately.

Usage (called from GamaAssistant.run):
    monitor = InternetMonitor(interval=15.0)
    monitor.on_change(my_callback)          # callback(available: bool)
    asyncio.ensure_future(monitor.start())  # runs forever
    if monitor.available: ...
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable, List, Optional

log = logging.getLogger("gama.internet")

_TEST_HOST = "8.8.8.8"
_TEST_PORT = 53
_CONNECT_TIMEOUT = 3.0   # seconds for the raw TCP connect
_ASYNC_TIMEOUT = 4.0     # extra buffer for the thread handoff overhead


class InternetMonitor:
    """Async background internet-availability poller."""

    def __init__(self, interval: float = 15.0) -> None:
        self._interval = interval
        self._available: bool = True  # optimistic: assume online until first check
        self._callbacks: List[Callable[[bool], None]] = []
        self._running: bool = False

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True if the last connectivity check succeeded."""
        return self._available

    def on_change(self, callback: Callable[[bool], None]) -> None:
        """Register a callback invoked on every availability flip.
        Called with ``True`` when internet returns, ``False`` when it drops.
        Safe to call before ``start()``."""
        self._callbacks.append(callback)

    async def check_once(self) -> bool:
        """Single non-blocking connectivity probe.  Returns True = online."""
        loop = asyncio.get_event_loop()
        try:
            conn = await asyncio.wait_for(
                loop.run_in_executor(None, self._tcp_connect),
                timeout=_ASYNC_TIMEOUT,
            )
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return True
        except Exception:
            return False

    async def probe(self) -> bool:
        """Single authoritative probe — updates ``available`` immediately.
        Await this *before* entering the Gemini reconnect loop so the state
        is correct on the very first iteration (``start()`` runs concurrently
        via ``ensure_future`` and would otherwise update only after the first
        ``interval`` seconds, by which point Gemini may already be connecting).
        """
        available = await self.check_once()
        self._apply(available)
        return available

    async def start(self) -> None:
        """Run the polling loop forever (until ``stop()`` is called).
        Schedule with ``asyncio.ensure_future(monitor.start())``."""
        self._running = True

        # Immediate first probe — skip if probe() was already awaited by the
        # caller (state already accurate), but harmless to re-run.
        available = await self.check_once()
        self._apply(available)

        while self._running:
            await asyncio.sleep(self._interval)
            available = await self.check_once()
            self._apply(available)

    def stop(self) -> None:
        self._running = False

    # ── Internal ─────────────────────────────────────────────────────────────

    def _tcp_connect(self) -> Optional[socket.socket]:
        """Blocking TCP connect — runs in a thread executor."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect((_TEST_HOST, _TEST_PORT))
        return sock

    def _apply(self, available: bool) -> None:
        if available == self._available:
            return  # no change — skip callbacks
        self._available = available
        state = "restored" if available else "lost"
        log.info(f"[internet] Connectivity {state}.")
        for cb in list(self._callbacks):
            try:
                cb(available)
            except Exception as exc:
                log.debug(f"[internet] Callback error (ignored): {exc}")
