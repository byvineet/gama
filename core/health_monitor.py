"""
core/health_monitor.py — Lightweight Module Health Monitor
============================================================
Phase 1 of the JARVIS reliability architecture.

Continuously monitors the health of every critical subsystem and restarts
only the failing module — never the whole assistant.

Monitored modules:
  voice_engine, wake_word, stt, tts, ai_api, memory,
  context, scheduler, state_engine, music

Health states:
  HEALTHY   — last check passed
  DEGRADED  — check passed with warnings
  FAILED    — check failed, restart attempted
  UNKNOWN   — not yet checked / no probe registered

Design:
  - Each module registers a probe (a lightweight callable → bool/str)
  - A background daemon thread polls probes at a configurable interval
  - On failure: retry probe twice, then call the module's restart_fn
  - Preserves active tasks during restart (never clears TaskQueue)
  - Publishes health events to EventBus so the UI can show a dot indicator
  - Exposes a health_report() dict for diagnostics

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Health state enum
# ---------------------------------------------------------------------------

class HealthState(str, Enum):
    HEALTHY  = "healthy"
    DEGRADED = "degraded"
    FAILED   = "failed"
    UNKNOWN  = "unknown"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Module descriptor
# ---------------------------------------------------------------------------

@dataclass
class ModuleHealth:
    name: str
    probe: Optional[Callable[[], bool]] = None
    restart_fn: Optional[Callable[[], None]] = None
    state: HealthState = HealthState.UNKNOWN
    last_checked: float = 0.0
    last_ok: float = 0.0
    fail_streak: int = 0
    restart_count: int = 0
    last_error: str = ""
    enabled: bool = True
    poll_interval: float = 30.0   # seconds between checks
    max_restarts: int = 5         # give up after this many restarts within a window
    restart_window: float = 600.0 # restart count resets after this many seconds
    _restart_window_start: float = field(default_factory=time.time, repr=False)

    def should_check(self) -> bool:
        return self.enabled and (time.time() - self.last_checked) >= self.poll_interval

    def record_ok(self) -> None:
        self.state = HealthState.HEALTHY
        self.fail_streak = 0
        self.last_ok = time.time()
        self.last_checked = time.time()
        self.last_error = ""

    def record_fail(self, error: str) -> None:
        self.fail_streak += 1
        self.last_error = error
        self.last_checked = time.time()
        self.state = HealthState.FAILED

    def can_restart(self) -> bool:
        # Reset restart count if window has passed
        if (time.time() - self._restart_window_start) > self.restart_window:
            self.restart_count = 0
            self._restart_window_start = time.time()
        return self.restart_fn is not None and self.restart_count < self.max_restarts

    def record_restart(self) -> None:
        self.restart_count += 1
        if (time.time() - self._restart_window_start) > self.restart_window:
            self.restart_count = 1
            self._restart_window_start = time.time()


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """
    Lightweight module health checker.

    Usage::

        from core.health_monitor import health_monitor

        # Register a module
        health_monitor.register(
            name="tts",
            probe=lambda: tts_engine.is_alive(),
            restart_fn=tts_engine.restart,
            poll_interval=20.0,
        )

        # Start monitoring (call once at startup)
        health_monitor.start()

        # Query
        report = health_monitor.health_report()
        is_ok  = health_monitor.is_healthy("tts")
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modules: Dict[str, ModuleHealth] = {}
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._bus = None

    # ── Registration ─────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        probe: Optional[Callable[[], bool]] = None,
        restart_fn: Optional[Callable[[], None]] = None,
        poll_interval: float = 30.0,
        max_restarts: int = 5,
        enabled: bool = True,
    ) -> None:
        """Register a module for health monitoring."""
        with self._lock:
            self._modules[name] = ModuleHealth(
                name=name,
                probe=probe,
                restart_fn=restart_fn,
                poll_interval=poll_interval,
                max_restarts=max_restarts,
                enabled=enabled,
            )
        log.debug(f"[HealthMonitor] Registered: {name}")

    def disable(self, name: str) -> None:
        with self._lock:
            if name in self._modules:
                self._modules[name].enabled = False

    def enable(self, name: str) -> None:
        with self._lock:
            if name in self._modules:
                self._modules[name].enabled = True

    # ── Start / stop ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background monitoring thread (daemon, safe to call multiple times)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="HealthMonitor",
            daemon=True,
        )
        self._thread.start()
        log.info("[HealthMonitor] Started.")

    def stop(self) -> None:
        self._running = False

    # ── Public query ─────────────────────────────────────────────────────────

    def is_healthy(self, name: str) -> bool:
        with self._lock:
            m = self._modules.get(name)
        if m is None:
            return True  # unknown = assume ok
        return m.state in (HealthState.HEALTHY, HealthState.UNKNOWN)

    def health_report(self) -> Dict[str, dict]:
        with self._lock:
            return {
                name: {
                    "state": m.state.value,
                    "fail_streak": m.fail_streak,
                    "restart_count": m.restart_count,
                    "last_ok": m.last_ok,
                    "last_error": m.last_error,
                }
                for name, m in self._modules.items()
            }

    def get_failed_modules(self) -> List[str]:
        with self._lock:
            return [n for n, m in self._modules.items() if m.state == HealthState.FAILED]

    def mark_healthy(self, name: str) -> None:
        """External modules can manually mark themselves healthy after self-recovery."""
        with self._lock:
            m = self._modules.get(name)
            if m:
                m.record_ok()
        self._emit("ModuleHealthy", module=name)

    # ── Monitor loop ─────────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        while self._running:
            try:
                self._run_checks()
            except Exception as exc:
                log.warning(f"[HealthMonitor] Loop error: {exc}")
            time.sleep(5)  # minimum sleep between full sweeps

    def _run_checks(self) -> None:
        with self._lock:
            modules_to_check = [
                m for m in self._modules.values() if m.should_check()
            ]

        for m in modules_to_check:
            self._check_module(m)

    def _check_module(self, m: ModuleHealth) -> None:
        if m.probe is None:
            with self._lock:
                m.last_checked = time.time()
                if m.state == HealthState.UNKNOWN:
                    m.state = HealthState.HEALTHY
            return

        # Run probe with retries
        ok = False
        error = ""
        for attempt in range(2):
            try:
                result = m.probe()
                if result or result is None:  # None = probe didn't raise = assume ok
                    ok = True
                    break
                error = f"probe returned {result!r}"
            except Exception as exc:
                error = str(exc)
            if attempt == 0:
                time.sleep(1.0)  # brief pause before retry

        with self._lock:
            if ok:
                prev_state = m.state
                m.record_ok()
                if prev_state == HealthState.FAILED:
                    log.info(f"[HealthMonitor] {m.name}: recovered → HEALTHY")
                    self._emit("ModuleRecovered", module=m.name)
            else:
                m.record_fail(error)
                log.warning(f"[HealthMonitor] {m.name}: FAILED — {error}")

        if not ok and m.restart_fn is not None:
            self._try_restart(m)
        elif not ok:
            # No restart_fn was registered for this module — it's either
            # self-healing elsewhere (e.g. an external reconnect loop) or
            # intentionally monitor-only. Don't spam "giving up" on every
            # poll; the FAILED state above already reflects the problem.
            log.debug(
                f"[HealthMonitor] {m.name}: no restart_fn registered — "
                "leaving recovery to the module itself."
            )

    def _try_restart(self, m: ModuleHealth) -> None:
        with self._lock:
            can = m.can_restart()

        if not can:
            log.warning(
                f"[HealthMonitor] {m.name}: max restarts reached — giving up."
            )
            self._emit("ModuleGaveUp", module=m.name, error=m.last_error)
            return

        log.info(f"[HealthMonitor] {m.name}: attempting restart #{m.restart_count + 1}…")
        self._emit("ModuleRestarting", module=m.name)

        try:
            m.restart_fn()
            with self._lock:
                m.record_restart()
                m.fail_streak = 0
                m.state = HealthState.DEGRADED  # tentatively degraded until next probe
            log.info(f"[HealthMonitor] {m.name}: restart issued.")
            self._emit("ModuleRestarted", module=m.name)
        except Exception as exc:
            log.error(f"[HealthMonitor] {m.name}: restart raised: {exc}")
            self._emit("ModuleRestartFailed", module=m.name, error=str(exc))

    # ── EventBus ─────────────────────────────────────────────────────────────

    def _emit(self, event_name: str, **data: Any) -> None:
        try:
            if self._bus is None:
                from state_engine.event_bus import event_bus
                self._bus = event_bus
            self._bus.publish(event_name, **data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

health_monitor = HealthMonitor()

__all__ = ["HealthMonitor", "ModuleHealth", "HealthState", "health_monitor"]


