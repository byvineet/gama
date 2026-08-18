"""
voice/device_monitor.py — Audio Device Hot-Swap Monitor
=========================================================
Detects when the Windows default input (microphone) or output
(speakers / headphones) changes and notifies registered callbacks so
streams can be recreated on the new device without any restart.

Design
------
- Polls ``sounddevice.query_devices()`` every POLL_INTERVAL_S seconds.
  This is cheap (a single OS device-list query — no audio data moved).
- Callbacks are dispatched on a dedicated daemon thread so they never
  block the monitor loop or an audio callback.
- Handles devices disappearing gracefully: if the query fails, the
  previous device name is kept and no spurious change is fired.

Callbacks
---------
Each callback receives a ``DeviceChangeEvent``:
    event.kind      — "input"  or "output"
    event.old_name  — previous default device name (or "" on first call)
    event.new_name  — new default device name

Usage
-----
    mon = get_monitor()
    mon.on_output_change(lambda evt: log.info(f"Output → {evt.new_name}"))
    mon.on_input_change(lambda evt: signal_mic_restart())
    mon.start()
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

POLL_INTERVAL_S: float = 1.0   # 1 s — fast enough for Bluetooth hot-swap


@dataclass
class DeviceChangeEvent:
    kind: str       # "input" or "output"
    old_name: str
    new_name: str


OutputCallback = Callable[[DeviceChangeEvent], None]
InputCallback  = Callable[[DeviceChangeEvent], None]


def _query_default(kind: str) -> str:
    """Return the current default input or output device name.
    Returns '' on any error so callers treat it as 'unchanged'."""
    try:
        import sounddevice as sd
        idx = sd.default.device[0 if kind == "input" else 1]
        if idx is None or idx < 0:
            return ""
        devices = sd.query_devices()
        if idx < len(devices):
            return str(devices[idx].get("name", ""))
        return ""
    except Exception:
        return ""


class AudioDeviceMonitor:
    """Polls default audio I/O devices and fires callbacks on change.

    Thread-safe. One process-wide instance — use ``get_monitor()``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._out_cbs: List[OutputCallback] = []
        self._in_cbs:  List[InputCallback]  = []
        self._last_out: str = ""
        self._last_in:  str = ""
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── callback registration ─────────────────────────────────────────────────

    def on_output_change(self, cb: OutputCallback) -> None:
        """Register a callback for output-device changes."""
        with self._lock:
            self._out_cbs.append(cb)

    def on_input_change(self, cb: InputCallback) -> None:
        """Register a callback for input-device (microphone) changes."""
        with self._lock:
            self._in_cbs.append(cb)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start monitoring. Idempotent — safe to call multiple times."""
        with self._lock:
            if self._running:
                return
            self._running = True
            # Snapshot current devices BEFORE starting the thread so the
            # first poll iteration never fires a spurious "change" event.
            self._last_out = _query_default("output")
            self._last_in  = _query_default("input")

        self._thread = threading.Thread(
            target=self._loop,
            name="gama-device-monitor",
            daemon=True,
        )
        self._thread.start()
        log.info(
            f"[device_monitor] Started — "
            f"output={self._last_out!r}  input={self._last_in!r}  "
            f"poll={POLL_INTERVAL_S}s"
        )

    def stop(self) -> None:
        """Stop the polling loop."""
        with self._lock:
            self._running = False

    # ── poll loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
            time.sleep(POLL_INTERVAL_S)
            try:
                self._check()
            except Exception:
                log.exception("[device_monitor] poll error")

    def _check(self) -> None:
        new_out = _query_default("output")
        new_in  = _query_default("input")

        with self._lock:
            old_out = self._last_out
            old_in  = self._last_in
            out_changed = bool(new_out) and new_out != old_out
            in_changed  = bool(new_in)  and new_in  != old_in
            if out_changed:
                self._last_out = new_out
            if in_changed:
                self._last_in = new_in
            out_cbs = list(self._out_cbs) if out_changed else []
            in_cbs  = list(self._in_cbs)  if in_changed  else []

        if out_changed:
            evt = DeviceChangeEvent(kind="output", old_name=old_out, new_name=new_out)
            log.info(f"[device_monitor] Output: {old_out!r} → {new_out!r}")
            for cb in out_cbs:
                threading.Thread(
                    target=self._safe_call, args=(cb, evt),
                    name="gama-dev-cb", daemon=True,
                ).start()

        if in_changed:
            evt = DeviceChangeEvent(kind="input", old_name=old_in, new_name=new_in)
            log.info(f"[device_monitor] Input: {old_in!r} → {new_in!r}")
            for cb in in_cbs:
                threading.Thread(
                    target=self._safe_call, args=(cb, evt),
                    name="gama-dev-cb", daemon=True,
                ).start()

    @staticmethod
    def _safe_call(cb: Callable, evt: DeviceChangeEvent) -> None:
        try:
            cb(evt)
        except Exception:
            log.exception("[device_monitor] device-change callback raised")


# ── Process-wide singleton ────────────────────────────────────────────────────

_monitor: Optional[AudioDeviceMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> AudioDeviceMonitor:
    """Return (creating if needed) the process-wide AudioDeviceMonitor."""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = AudioDeviceMonitor()
    return _monitor


__all__ = ["AudioDeviceMonitor", "DeviceChangeEvent", "get_monitor"]
