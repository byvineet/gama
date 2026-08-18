"""
actions/process_manager.py — Gama Process Manager
===================================================
List and kill running processes.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import subprocess
import time
from typing import List

from actions.reliability import wait_for_process_gone
from utils.proc import hidden_kwargs

log = get_logger(__name__)
logger = log  # back-compat alias
def process_manager(action: str = "list", **kwargs) -> str:
    """Manage running processes."""
    action = (action or "list").lower().strip()

    if action == "list":
        return _list(kwargs.get("filter", ""))
    if action == "kill":
        return _kill(kwargs.get("name_or_pid", ""))
    if action == "kill_pid":
        return _kill(kwargs.get("pid", ""), by_pid=True)
    if action == "top":
        return _top()
    if action == "close_window":
        return _close_window(kwargs.get("name_or_pid", "") or kwargs.get("name", ""))
    return f"Unknown process action: {action}. Use: list, kill, top, close_window."


def _list(filter_str: str = "") -> str:
    """List running processes."""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                name = p.info["name"] or ""
                if filter_str and filter_str.lower() not in name.lower():
                    continue
                procs.append({
                    "pid": p.info["pid"],
                    "name": name,
                    "cpu": p.info["cpu_percent"] or 0,
                    "mem": p.info["memory_percent"] or 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not procs:
            return f"No processes found matching '{filter_str}'." if filter_str else "No processes."

        procs.sort(key=lambda x: x["mem"], reverse=True)
        lines = [f"Processes ({len(procs)}):"]
        for p in procs[:20]:
            lines.append(f"  PID {p['pid']:>6}  {p['cpu']:>5.1f}% CPU  "
                         f"{p['mem']:>5.1f}% MEM  {p['name']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"List failed: {exc}"


def _top() -> str:
    """Show top processes by CPU and memory."""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append({
                    "pid": p.info["pid"],
                    "name": p.info["name"] or "",
                    "cpu": p.info["cpu_percent"] or 0,
                    "mem": p.info["memory_percent"] or 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda x: x["cpu"], reverse=True)
        lines = ["Top processes by CPU:"]
        for p in procs[:10]:
            lines.append(f"  {p['cpu']:>5.1f}% CPU  {p['name']} (PID {p['pid']})")
        return "\n".join(lines)
    except Exception as exc:
        return f"Top failed: {exc}"


def _kill(name_or_pid: str, by_pid: bool = False) -> str:
    """Kill a process by name or PID."""
    if not name_or_pid:
        return "Which process should I kill? (name or PID)"

    try:
        if by_pid or name_or_pid.isdigit():
            pid = int(name_or_pid)
            try:
                import psutil
                p = psutil.Process(pid)
                name = p.name()
                p.terminate()
                p.wait(timeout=3)
                return f"Killed process {name} (PID {pid})."
            except psutil.NoSuchProcess:
                return f"No process with PID {pid}."
            except psutil.AccessDenied:
                return f"Access denied killing PID {pid}. Try running as admin."
        else:
            # Kill by name
            import os
            name = name_or_pid
            if not name.endswith(".exe"):
                name = f"{name}.exe"
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/IM", name, "/F"],
                    capture_output=True, text=True, shell=False, **hidden_kwargs(),
                )
            else:
                result = subprocess.run(
                    ["pkill", "-f", name],
                    capture_output=True, text=True,
                )
            if result.returncode != 0:
                return f"Could not kill {name}. {result.stderr.strip() or result.stdout.strip()}"

            # Verify: the process should actually disappear. Some apps
            # relaunch a watchdog/helper immediately — one retry covers that.
            if wait_for_process_gone(name, timeout=4.0):
                return f"Killed process(es): {name} (verified)."
            time.sleep(0.5)
            if os.name == "nt":
                subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True,
                               text=True, shell=False, **hidden_kwargs())
            if wait_for_process_gone(name, timeout=3.0):
                return f"Killed process(es): {name} (verified after retry)."
            return f"Sent kill to {name}, but it (or a relaunched copy) is still running."
    except Exception as exc:
        return f"Kill failed: {exc}"


def _close_window(name_or_title: str) -> str:
    """Politely close an app's window (WM_CLOSE) instead of force-killing it —
    lets the app prompt to save changes. Falls back to a hard kill if no
    matching window is found or the close doesn't take effect."""
    if not name_or_title:
        return "Which app's window should I close?"
    from actions.reliability import find_window
    import platform
    if platform.system() != "Windows":
        return _kill(name_or_title)
    try:
        import win32gui  # type: ignore
        import win32con  # type: ignore
        hwnd = find_window(name_or_title)
        if hwnd is None:
            return f"No open window found matching '{name_or_title}'. Try `kill` by process name instead."
        title = win32gui.GetWindowText(hwnd)
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.8)
        if find_window(title) is None:
            return f"Closed '{title}' (verified)."
        return f"Sent close to '{title}' — it may be waiting on a save/confirm dialog."
    except Exception as exc:
        return f"Close window failed: {exc}"


__all__ = ["process_manager"]
