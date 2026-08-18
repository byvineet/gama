"""
utils/adaptive_perf.py — Adaptive Performance Governor
==========================================================
Watches CPU load on a single low-frequency background thread (not a
busy loop — sleeps between samples) and exposes a small set of flags
the rest of the app can check cheaply. When CPU crosses the high
watermark, background/non-latency-critical work should back off;
voice responsiveness always wins.

Usage:
    from utils.adaptive_perf import governor
    governor.start()
    ...
    if governor.should_reduce_quality():
        beam_size = 1
    else:
        beam_size = 5
    if governor.should_pause_background():
        skip_this_desktop_refresh()

This does not itself throttle anything — it only reports state.
Callers (voice/stt_whisper.py's mode selection, state_engine's
background_tasks, desktop analysis) check it and decide what to skip.
That keeps this module tiny, dependency-light (psutil only), and free
of any coupling to what it's protecting.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

HIGH_WATERMARK = 75.0   # % CPU — start shedding load above this
LOW_WATERMARK = 55.0    # % CPU — safe to restore full quality below this
SAMPLE_INTERVAL_S = 2.0  # cheap enough to run forever; not a hot loop


class PerformanceGovernor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._degraded = False
        self._last_cpu = 0.0

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, name="perf-governor", daemon=True)
        self._thread.start()
        log.info("Adaptive performance governor started.")

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def _loop(self) -> None:
        try:
            import psutil
        except Exception as exc:
            log.warning(f"psutil unavailable ({exc}); adaptive performance governor disabled.")
            return

        # First call to cpu_percent() with no interval just primes the
        # internal counters and returns 0 — call it once up front so the
        # first real sample below isn't a throwaway.
        psutil.cpu_percent(interval=None)
        while True:
            with self._lock:
                if not self._running:
                    return
            time.sleep(SAMPLE_INTERVAL_S)
            try:
                cpu = psutil.cpu_percent(interval=None)
            except Exception:
                continue
            self._last_cpu = cpu

            if not self._degraded and cpu >= HIGH_WATERMARK:
                self._degraded = True
                log.info(f"CPU at {cpu:.0f}% — reducing background load, prioritizing voice.")
            elif self._degraded and cpu <= LOW_WATERMARK:
                self._degraded = False
                log.info(f"CPU back to {cpu:.0f}% — restoring full quality.")

    # ── cheap read-only checks for callers ──────────────────────
    def should_reduce_quality(self) -> bool:
        """True when Whisper should drop to greedy/small-beam decoding
        and similar quality-for-speed trades."""
        return self._degraded

    def should_pause_background(self) -> bool:
        """True when non-essential background tasks (desktop refresh,
        prefetching, etc.) should skip this cycle."""
        return self._degraded

    @property
    def current_cpu(self) -> float:
        return self._last_cpu


governor = PerformanceGovernor()

__all__ = ["PerformanceGovernor", "governor", "HIGH_WATERMARK", "LOW_WATERMARK"]
