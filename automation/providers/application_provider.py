"""
automation/providers/application_provider.py — Application Automation.

Reuses running instances (context-integration requirement) instead of
re-launching, keeps a small process cache, and verifies launch/exit via
psutil rather than assuming success.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

from utils.logger import get_logger
from automation.models import ActionResult, Capability, ExecutionMethod
from automation.registry import registry

log = get_logger(__name__)

try:
    import psutil  # type: ignore
    _HAVE_PSUTIL = True
except Exception:
    _HAVE_PSUTIL = False

_IS_WINDOWS = sys.platform == "win32"

# name -> pid, so "open VS Code" twice reuses the running one.
_launched_cache: Dict[str, int] = {}

# Common friendly-name -> Windows launch command aliases.
_ALIASES = {
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "chrome": "chrome", "edge": "msedge", "discord": "discord",
    "spotify": "spotify", "explorer": "explorer", "notepad": "notepad",
    "terminal": "wt", "calculator": "calc", "word": "winword",
    "excel": "excel", "powerpoint": "powerpnt",
}


def _resolve_cmd(name: str) -> str:
    return _ALIASES.get(name.lower().strip(), name)


def _find_running(name: str) -> Optional[int]:
    if not _HAVE_PSUTIL:
        return None
    cached = _launched_cache.get(name.lower())
    if cached and psutil.pid_exists(cached):
        return cached
    needle = name.lower().replace(".exe", "")
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = (proc.info["name"] or "").lower().replace(".exe", "")
            if needle in pname or pname in needle:
                _launched_cache[name.lower()] = proc.info["pid"]
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _launch(name: str, reuse: bool = True, **_) -> ActionResult:
    if reuse:
        pid = _find_running(name)
        if pid:
            return ActionResult(ok=True, message=f"'{name}' already running (pid {pid}), reused it",
                                 data={"pid": pid, "reused": True})

    cmd = _resolve_cmd(name)
    try:
        if _IS_WINDOWS:
            import os
            os.startfile(cmd)
        else:
            resolved = shutil.which(cmd) or cmd
            proc = subprocess.Popen([resolved], shell=False)
    except Exception as exc:
        return ActionResult(ok=False, message=f"Failed to launch '{name}': {exc}")

    # Give the OS a brief moment, then verify via psutil.
    time.sleep(0.3)
    pid = _find_running(name)
    if pid:
        return ActionResult(ok=True, message=f"Launched '{name}'", data={"pid": pid},
                             method=ExecutionMethod.NATIVE_API)
    return ActionResult(ok=True, message=f"Launch requested for '{name}' (verification pending)")


def _verify_launch(name: str, **_) -> Tuple[bool, str]:
    pid = _find_running(name)
    return (pid is not None), (f"pid {pid}" if pid else "process not found")


def _close(name: str, force: bool = False, **_) -> ActionResult:
    if not _HAVE_PSUTIL:
        return ActionResult(ok=False, message="psutil not available")
    pid = _find_running(name)
    if not pid:
        return ActionResult(ok=True, message=f"'{name}' is not running")
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            proc.terminate()
        proc.wait(timeout=3)
        _launched_cache.pop(name.lower(), None)
        return ActionResult(ok=True, message=f"Closed '{name}' (pid {pid})")
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            return ActionResult(ok=True, message=f"Force-killed unresponsive '{name}'")
        except Exception as exc:
            return ActionResult(ok=False, message=f"Failed to close '{name}': {exc}")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Failed to close '{name}': {exc}")


def _verify_exit(name: str, **_) -> Tuple[bool, str]:
    pid = _find_running(name)
    return (pid is None), ("still running" if pid else "confirmed exited")


def _is_running(name: str, **_) -> ActionResult:
    pid = _find_running(name)
    return ActionResult(ok=True, message=("running" if pid else "not running"),
                         data={"running": pid is not None, "pid": pid})


def _restart(name: str, **_) -> ActionResult:
    close_res = _close(name)
    time.sleep(0.5)
    return _launch(name, reuse=False)


def register() -> None:
    registry.register_many([
        Capability("app.launch", _launch, verify=_verify_launch, cost=3, speed_ms=300,
                   description="Launch (or reuse) an application",
                   keywords=("open", "launch", "start", "run")),
        Capability("app.close", _close, verify=_verify_exit, cost=1, speed_ms=100,
                   description="Close an application", keywords=("close", "quit", "exit")),
        Capability("app.kill", lambda name, **kw: _close(name, force=True), cost=1, speed_ms=50,
                   description="Force-kill an application", keywords=("kill", "force close")),
        Capability("app.restart", _restart, cost=4, speed_ms=800,
                   description="Restart an application", keywords=("restart",)),
        Capability("app.is_running", _is_running, cost=0, speed_ms=10,
                   description="Check if an app is running", keywords=("is running", "check if")),
    ])


register()
