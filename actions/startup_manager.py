"""
actions/startup_manager.py — Gama Startup Manager
===================================================
Manage Windows startup programs.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import winreg
from pathlib import Path
from typing import List

log = get_logger(__name__)
logger = log  # back-compat alias
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_FOLDER = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_manager(action: str = "list", **kwargs) -> str:
    """Manage Windows startup programs."""
    action = (action or "list").lower().strip()

    if action == "list":
        return _list()
    if action == "add":
        return _add(kwargs.get("name", ""), kwargs.get("path", ""))
    if action == "remove":
        return _remove(kwargs.get("name", ""))
    if action == "status":
        return _status(kwargs.get("name", ""))
    return f"Unknown startup action: {action}. Use: list, add, remove, status."


def _list() -> str:
    """List all startup programs."""
    lines = ["Startup programs:"]

    # From registry (HKCU)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                    lines.append(f"  [registry] {name}: {value}")
                    i += 1
                except OSError:
                    break
    except Exception as exc:
        lines.append(f"  [registry error: {exc}]")

    # From startup folder
    try:
        if _STARTUP_FOLDER.exists():
            for item in _STARTUP_FOLDER.iterdir():
                lines.append(f"  [folder] {item.name}")
    except Exception:
        pass

    return "\n".join(lines) if len(lines) > 1 else "No startup programs found."


def _add(name: str, path: str) -> str:
    """Add a program to startup."""
    if not name or not path:
        return "Please provide both name and path."
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, path)
        return f"Added '{name}' to startup."
    except Exception as exc:
        return f"Add failed: {exc}"


def _remove(name: str) -> str:
    """Remove a program from startup."""
    if not name:
        return "Which startup program should I remove?"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, name)
                return f"Removed '{name}' from startup."
            except FileNotFoundError:
                return f"'{name}' not found in startup registry."
    except Exception as exc:
        return f"Remove failed: {exc}"


def _status(name: str) -> str:
    """Check if a program is in startup."""
    if not name:
        return "Which program should I check?"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, name)
                return f"'{name}' is in startup → {value}"
            except FileNotFoundError:
                return f"'{name}' is NOT in startup."
    except Exception as exc:
        return f"Status check failed: {exc}"


__all__ = ["startup_manager"]
