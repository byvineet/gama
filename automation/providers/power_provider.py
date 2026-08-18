"""
automation/providers/power_provider.py — Power Automation.

Uses native `ctypes`/`os.system("shutdown ...")` calls — no external
process needed for lock/shutdown, which keeps latency near-zero.
"""

from __future__ import annotations

import os
import sys

from utils.logger import get_logger
from automation.models import ActionResult, Capability
from automation.registry import registry

log = get_logger(__name__)
_IS_WINDOWS = sys.platform == "win32"


def _lock(**_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Lock is Windows-only")
    try:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return ActionResult(ok=True, message="Locked the workstation")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Lock failed: {exc}")


def _shutdown(delay_seconds: int = 0, **_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Shutdown is Windows-only")
    rc = os.system(f"shutdown /s /t {delay_seconds}")
    return ActionResult(ok=(rc == 0), message="Shutdown scheduled" if rc == 0 else "Shutdown command failed")


def _restart(delay_seconds: int = 0, **_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Restart is Windows-only")
    rc = os.system(f"shutdown /r /t {delay_seconds}")
    return ActionResult(ok=(rc == 0), message="Restart scheduled" if rc == 0 else "Restart command failed")


def _cancel_shutdown(**_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Windows-only")
    rc = os.system("shutdown /a")
    return ActionResult(ok=(rc == 0), message="Pending shutdown cancelled" if rc == 0 else "Nothing to cancel")


def _sleep(**_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Sleep is Windows-only")
    try:
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        return ActionResult(ok=True, message="Sleeping")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Sleep failed: {exc}")


def _hibernate(**_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Hibernate is Windows-only")
    try:
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(1, 1, 0)
        return ActionResult(ok=True, message="Hibernating")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Hibernate failed: {exc}")


def register() -> None:
    registry.register_many([
        # These five mirror actions/confirmation.py::DESTRUCTIVE_ACTIONS —
        # marked destructive=True so automation/engine.py refuses to run
        # them without a verified confirmation code, same as computer_settings.
        Capability("power.lock", _lock, cost=0, speed_ms=10, destructive=True,
                   description="Lock the workstation", keywords=("lock",)),
        Capability("power.shutdown", _shutdown, cost=1, speed_ms=10, destructive=True,
                   description="Shut down the PC", keywords=("shutdown", "shut down", "turn off")),
        Capability("power.restart", _restart, cost=1, speed_ms=10, destructive=True,
                   description="Restart the PC", keywords=("restart", "reboot")),
        Capability("power.cancel_shutdown", _cancel_shutdown, cost=0, speed_ms=10,
                   description="Cancel a pending shutdown/restart", keywords=("cancel shutdown",)),
        Capability("power.sleep", _sleep, cost=0, speed_ms=10, destructive=True,
                   description="Put the PC to sleep", keywords=("sleep",)),
        Capability("power.hibernate", _hibernate, cost=0, speed_ms=10, destructive=True,
                   description="Hibernate the PC", keywords=("hibernate",)),
    ])


register()
