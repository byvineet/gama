"""
utils/proc.py — Helpers for launching subprocesses without flashing a console window.

When a PyInstaller app is built with console=False (windowed mode), any subprocess
it spawns (powershell, wmic, cmd, etc.) can still briefly create its own visible
console window on Windows. Passing the kwargs from `hidden_kwargs()` into
subprocess.run/Popen prevents that flash.
"""

from __future__ import annotations

import platform
import subprocess

_IS_WINDOWS = platform.system() == "Windows"

# CREATE_NO_WINDOW only exists on Windows builds of subprocess.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def hidden_kwargs() -> dict:
    """Return kwargs to merge into subprocess.run/Popen calls to suppress
    the console window that would otherwise flash on screen.

    Usage:
        subprocess.run(["powershell", "-Command", "..."], **hidden_kwargs(), ...)
    """
    if not _IS_WINDOWS:
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE

    return {
        "startupinfo": startupinfo,
        "creationflags": CREATE_NO_WINDOW,
    }
