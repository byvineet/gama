"""
actions/reliability.py — Gama Automation Reliability Layer
=============================================================
Shared helpers used by every automation module (open_app, computer_settings,
process_manager, file_controller, clipboard, computer_agent, terminal, ...)
so that PC control actions are consistently:

  1. VERIFIED   — we check the OS actually did the thing, not just that a
                   command was *sent*.
  2. RETRIED    — safe / idempotent operations get a couple of automatic
                   retries with backoff before we give up.
  3. CLEAR      — failures come back as a plain-English reason, not a raw
                   traceback, so Gama can say something useful out loud.

Nothing in here talks to the Gemini API — it's pure OS glue.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()


# ---------------------------------------------------------------------------
# Result object — lightweight, but richer than a bare string when a caller
# (e.g. computer_agent's chain runner) needs to know pass/fail programmatically.
# Action modules can keep returning plain strings; use str(result) for that.
# ---------------------------------------------------------------------------
@dataclass
class ActionResult:
    ok: bool
    message: str
    verified: bool = False
    attempts: int = 1
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def ok(message: str, verified: bool = False, attempts: int = 1, **details) -> ActionResult:
    return ActionResult(True, message, verified=verified, attempts=attempts, details=details)


def fail(message: str, attempts: int = 1, **details) -> ActionResult:
    return ActionResult(False, message, verified=False, attempts=attempts, details=details)


# ---------------------------------------------------------------------------
# Generic retry helper
# ---------------------------------------------------------------------------
def retry(
    func: Callable[[], Any],
    attempts: int = 3,
    delay: float = 0.6,
    backoff: float = 1.6,
    exceptions: tuple = (Exception,),
    should_retry: Optional[Callable[[Any], bool]] = None,
) -> Any:
    """Run func() up to `attempts` times with exponential backoff.

    - Retries on raised exceptions in `exceptions`.
    - Optionally also retries when `should_retry(result)` returns True even
      if no exception was raised (e.g. a subprocess returned nonzero but the
      failure looks transient).
    - Re-raises / returns the last outcome once attempts are exhausted.
    """
    last_exc = None
    wait = delay
    for attempt in range(1, attempts + 1):
        try:
            result = func()
            if should_retry is not None and should_retry(result) and attempt < attempts:
                logger.debug(f"retry: attempt {attempt} looked transient, retrying...")
                time.sleep(wait)
                wait *= backoff
                continue
            return result
        except exceptions as exc:  # noqa: PERF203 - clarity over micro-perf here
            last_exc = exc
            if attempt >= attempts:
                break
            logger.debug(f"retry: attempt {attempt} raised {exc!r}, retrying in {wait:.1f}s")
            time.sleep(wait)
            wait *= backoff
    if last_exc is not None:
        raise last_exc
    return None  # pragma: no cover


def is_transient_error(exc: BaseException) -> bool:
    """Heuristic: is this the kind of error that's worth retrying?
    (file locked, resource busy, timeouts) vs. a permanent one
    (file not found, permission denied at the OS-policy level, bad args).
    """
    msg = str(exc).lower()
    transient_markers = (
        "being used by another process", "resource busy", "timed out",
        "timeout", "temporarily unavailable", "try again", "winerror 32",
        "winerror 5",  # access denied can be transient (AV scan, indexer)
        "connection reset", "broken pipe",
    )
    return any(m in msg for m in transient_markers)


# ---------------------------------------------------------------------------
# Process verification (cross-platform via psutil)
# ---------------------------------------------------------------------------
def _iter_process_names() -> Iterable[str]:
    import psutil
    for p in psutil.process_iter(["name"]):
        try:
            name = p.info.get("name") or ""
            if name:
                yield name.lower()
        except Exception:
            continue


def is_process_running(name_substr: str) -> bool:
    """True if any running process name contains name_substr (case-insensitive)."""
    if not name_substr:
        return False
    needle = name_substr.lower().lstrip(".").replace(".exe", "")
    for name in _iter_process_names():
        if needle in name:
            return True
    return False


def wait_for_process(name_substr: str, timeout: float = 8.0, poll: float = 0.15) -> bool:
    """Poll until a process matching name_substr appears, or timeout expires."""
    if not name_substr:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_process_running(name_substr):
            return True
        time.sleep(poll)
    return is_process_running(name_substr)


def wait_for_process_gone(name_substr: str, timeout: float = 6.0, poll: float = 0.15) -> bool:
    """Poll until no process matching name_substr remains, or timeout expires."""
    if not name_substr:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_running(name_substr):
            return True
        time.sleep(poll)
    return not is_process_running(name_substr)


# ---------------------------------------------------------------------------
# Window verification (Windows-native via pywin32, with pygetwindow fallback)
# ---------------------------------------------------------------------------
def list_window_titles() -> List[str]:
    """Return visible window titles. Best-effort, empty list if unsupported."""
    titles: List[str] = []
    if _OS == "Windows":
        try:
            import win32gui  # type: ignore

            def _cb(hwnd, acc):
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t:
                        acc.append(t)
                return True

            win32gui.EnumWindows(_cb, titles)
            return titles
        except Exception:
            pass
    try:
        import pygetwindow as gw  # type: ignore
        return [w.title for w in gw.getAllWindows() if w.title]
    except Exception:
        return titles


def find_window(title_substr: str):
    """Return a native window handle/object whose title contains title_substr, or None."""
    if not title_substr:
        return None
    needle = title_substr.lower()
    if _OS == "Windows":
        try:
            import win32gui  # type: ignore
            match = {"hwnd": None}

            def _cb(hwnd, acc):
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                    if needle in win32gui.GetWindowText(hwnd).lower():
                        acc["hwnd"] = hwnd
                        return False
                return True

            win32gui.EnumWindows(lambda h, a: _cb(h, a) if a["hwnd"] is None else False, match)
            return match["hwnd"]
        except Exception:
            pass
    try:
        import pygetwindow as gw  # type: ignore
        matches = [w for w in gw.getAllWindows() if needle in (w.title or "").lower()]
        return matches[0] if matches else None
    except Exception:
        return None


def wait_for_window(title_substr: str, timeout: float = 8.0, poll: float = 0.3) -> bool:
    """Poll until a window with a matching title appears."""
    if not title_substr:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_window(title_substr) is not None:
            return True
        time.sleep(poll)
    return find_window(title_substr) is not None


def get_foreground_window_title() -> str:
    if _OS == "Windows":
        try:
            import win32gui  # type: ignore
            return win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
        except Exception:
            pass
    try:
        import pygetwindow as gw  # type: ignore
        w = gw.getActiveWindow()
        return w.title if w else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Elevation / admin check (Windows) — several settings actions silently no-op
# without admin rights; surfacing this clearly beats a vague failure.
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    if _OS != "Windows":
        return True  # not meaningful on other OSes for our purposes
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# Common display-name -> actual process-name mapping used to verify that
# open_app() / computer_agent() really launched the thing they claim to.
KNOWN_PROCESS_NAMES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "firefox": "firefox.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "spotify": "spotify.exe",
    "vscode": "code.exe", "visual studio code": "code.exe", "code": "code.exe",
    "discord": "discord.exe",
    "telegram": "telegram.exe",
    "notepad": "notepad.exe",
    "calculator": "calculatorapp.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "terminal": "windowsterminal.exe",
    "powershell": "powershell.exe",
    "windows terminal": "windowsterminal.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "vlc": "vlc.exe",
    "zoom": "zoom.exe",
    "slack": "slack.exe",
    "steam": "steam.exe",
    "task manager": "taskmgr.exe",
}


def expected_process_name(app_name: str) -> Optional[str]:
    return KNOWN_PROCESS_NAMES.get((app_name or "").strip().lower())


__all__ = [
    "ActionResult", "ok", "fail",
    "retry", "is_transient_error",
    "is_process_running", "wait_for_process", "wait_for_process_gone",
    "list_window_titles", "find_window", "wait_for_window", "get_foreground_window_title",
    "is_admin", "expected_process_name", "KNOWN_PROCESS_NAMES",
]
