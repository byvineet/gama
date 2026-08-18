"""
actions/system_info.py — Gama System Information
==================================================
 System details focused on OS, CPU, RAM, disk, battery, and network.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import platform
import socket
from functools import lru_cache

log = get_logger(__name__)
logger = log  # back-compat alias
@lru_cache(maxsize=1)
def _static_machine_facts() -> dict:
    """OS/CPU identity facts that are constant for the life of the process.

    PERF: platform.uname()/psutil.cpu_count() were previously re-queried
    from scratch on every "system_info" call (overview *and* cpu both
    called them independently). None of this changes while Gama is
    running, so we resolve it once and cache it.
    """
    import psutil
    uname = platform.uname()
    return {
        "uname": uname,
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "processor": platform.processor() or "Unknown",
        "python_version": platform.python_version(),
    }


def system_info(action: str = "overview", **kwargs) -> str:
    """Get detailed system information."""
    action = (action or "overview").lower().strip()

    if action == "overview":
        return _overview()
    if action == "cpu":
        return _cpu_info()
    if action == "memory":
        return _memory_info()
    if action == "disk":
        return _disk_info()
    if action == "battery":
        return _battery_info()
    if action == "network":
        return _network_info()
    if action == "time":
        return _current_time()
    if action in ("usage", "consumption", "status", "monitor", "stats"):
        return _usage()
    return f"Unknown system_info action: {action}. Use: overview, cpu, memory, disk, battery, network, time, usage."


def _current_time() -> str:
    """Return the current date/time, always anchored to Indian Standard Time.

    Uses zoneinfo("Asia/Kolkata") explicitly rather than the machine's local
    clock/timezone setting, so the answer is correct even if the system
    timezone is misconfigured (e.g. set to UTC or another region).
    """
    try:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            ist = ZoneInfo("Asia/Kolkata")
        except Exception:
            # Fallback if tzdata isn't available: fixed UTC+5:30 offset (IST has no DST).
            from datetime import timezone, timedelta
            ist = timezone(timedelta(hours=5, minutes=30))

        now_ist = datetime.now(ist)
        return (
            f"It's {now_ist.strftime('%I:%M %p').lstrip('0')} "
            f"({now_ist.strftime('%A, %d %B %Y')})"
        )
    except Exception as exc:
        return f"Could not determine current time: {exc}"



def _overview() -> str:
    try:
        import psutil

        facts = _static_machine_facts()
        uname = facts["uname"]
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        lines = [
            f"OS: {uname.system} {uname.release} ({uname.machine})",
            f"Hostname: {uname.node}",
            f"CPU: {facts['cpu_count_logical']} cores ({facts['processor']})",
            f"RAM: {mem.total / 1024**3:.1f} GB total, {mem.available / 1024**3:.1f} GB available",
            f"Disk: {disk.total / 1024**3:.1f} GB total, {disk.free / 1024**3:.1f} GB free",
            f"Python: {facts['python_version']}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"System info failed: {exc}"


def _cpu_info() -> str:
    try:
        import psutil
        facts = _static_machine_facts()
        cpu_percent = psutil.cpu_percent(interval=0.5)
        freq = psutil.cpu_freq()
        lines = [
            f"CPU: {facts['processor']}",
            f"Cores: {facts['cpu_count_physical']} physical, {facts['cpu_count_logical']} logical",
            f"Current usage: {cpu_percent}%",
        ]
        if freq:
            lines.append(f"Frequency: {freq.current:.0f} MHz (max: {freq.max:.0f} MHz)")
        return "\n".join(lines)
    except Exception as exc:
        return f"CPU info failed: {exc}"


def _memory_info() -> str:
    try:
        import psutil
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        lines = [
            f"RAM: {mem.total / 1024**3:.1f} GB total",
            f"  Used: {mem.used / 1024**3:.1f} GB ({mem.percent}%)",
            f"  Available: {mem.available / 1024**3:.1f} GB",
            f"Swap: {swap.total / 1024**3:.1f} GB total ({swap.percent}% used)",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Memory info failed: {exc}"


def _disk_info() -> str:
    try:
        import psutil
        disk = psutil.disk_usage("/")
        lines = [
            f"Disk: {disk.total / 1024**3:.1f} GB total",
            f"  Used: {disk.used / 1024**3:.1f} GB ({disk.percent}%)",
            f"  Free: {disk.free / 1024**3:.1f} GB",
        ]
        # List partitions
        try:
            partitions = psutil.disk_partitions()
            if partitions:
                lines.append("\nPartitions:")
                for p in partitions[:5]:
                    try:
                        u = psutil.disk_usage(p.mountpoint)
                        lines.append(f"  {p.device} -> {p.mountpoint} ({u.total/1024**3:.0f}GB)")
                    except Exception:
                        lines.append(f"  {p.device} -> {p.mountpoint}")
        except Exception:
            pass
        return "\n".join(lines)
    except Exception as exc:
        return f"Disk info failed: {exc}"


def _battery_info() -> str:
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat is None:
            return "No battery detected (desktop PC or not available)."
        plugged = "plugged in" if bat.power_plugged else "on battery"
        return f"Battery: {bat.percent}% ({plugged})"
    except Exception as exc:
        return f"Battery info failed: {exc}"


def _network_info() -> str:
    try:
        import psutil
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "Unknown"
        net = psutil.net_io_counters()
        lines = [
            f"Hostname: {hostname}",
            f"Local IP: {local_ip}",
            f"Data sent: {net.bytes_sent / 1024**2:.1f} MB",
            f"Data received: {net.bytes_recv / 1024**2:.1f} MB",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Network info failed: {exc}"


__all__ = ["system_info"]


def get_system_status() -> dict:
    """Return current CPU/RAM consumption snapshot (CPU RAM snapshot)."""
    try:
        import psutil
    except ImportError:
        return {}
    # interval=None: non-blocking — returns usage since the previous call
    # instead of sleeping 200ms to sample (the sleep stalled tool handlers).
    # web_bridge's 2s sysstats loop keeps the sample fresh between calls.
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    return {
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(ram.percent, 1),
        "ram_used_gb": round(ram.used / 1024 ** 3, 1),
        "ram_total_gb": round(ram.total / 1024 ** 3, 1),
    }

def _usage() -> str:
    st = get_system_status()
    if not st:
        return "System usage unavailable (psutil missing)."
    return (
        f"CPU: {st.get('cpu_percent', '?')}%. "
        f"RAM: {st.get('ram_percent', '?')}% "
        f"({st.get('ram_used_gb', '?')} / {st.get('ram_total_gb', '?')} GB)."
    )
