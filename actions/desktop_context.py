"""
actions/desktop_context.py — Gama Live Desktop Awareness
=========================================================
Keeps a cheap, continuously-updated snapshot of "what the user is doing
right now" so commands like "continue working" or "save this" become
contextual — WITHOUT ever sending screenshots or screen content to an
LLM. Everything here uses local OS APIs only (psutil / pygetwindow /
pywin32 / pyperclip) and is refreshed by a lightweight background
thread on a slow poll interval, so it costs almost no CPU at idle.

Public surface
--------------
DesktopContextTracker  — background poller, start()/stop()
get_desktop_snapshot()  — instantaneous dict snapshot (module-level
                          singleton, safe to call from any thread)
desktop_context(action) — tool entrypoint (matches other actions/*.py
                          modules' `def action_name(action, **kwargs)`
                          convention so main.py's dispatcher pattern is
                          unchanged)
summarize_for_prompt()  — short natural-language blurb injected into
                          the Gemini system prompt each session so GAMA
                          "just knows" the current context passively.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import platform
import threading
import time
from typing import Dict, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()

# Consecutive failed _network_status() polls before we actually report
# "offline" to the rest of the system (see _network_status below).
_consecutive_offline_polls = 0
_OFFLINE_DEBOUNCE_POLLS = 2

try:
    from context_engine import publish_context_event, register_background_task, update_background_task
except Exception:  # context_engine is additive — desktop awareness must work without it
    def publish_context_event(*a, **k): pass
    def register_background_task(*a, **k): pass
    def update_background_task(*a, **k): pass

_POLL_INTERVAL = 2.0          # seconds — active window / clipboard / net
_SLOW_POLL_EVERY = 5           # every Nth tick, refresh battery/downloads (~10s)


# ---------------------------------------------------------------------------
# Low-level, best-effort local probes. Every probe is wrapped so a single
# missing dependency (e.g. no pygetwindow on Linux) never breaks the tracker
# — it just omits that field.
# ---------------------------------------------------------------------------
def _active_window() -> Dict[str, Optional[str]]:
    """Active app name + window title. Windows-first, graceful elsewhere."""
    title, app = None, None
    try:
        if _OS == "Windows":
            import win32gui
            import win32process
            import psutil

            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                title = win32gui.GetWindowText(hwnd) or None
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    app = psutil.Process(pid).name()
                except Exception:
                    pass
        else:
            import pygetwindow as gw
            win = gw.getActiveWindow()
            if win:
                title = win.title or None
    except Exception:
        pass
    return {"active_app": app, "active_window_title": title}


def _browser_tab_hint(title: str | None) -> Optional[str]:
    """Best-effort 'current tab' — browsers put the page title in the
    window title (e.g. 'GitHub - Google Chrome'), so we strip the
    trailing ' - <Browser>' suffix rather than automating the DOM."""
    if not title:
        return None
    for suffix in (" - Google Chrome", " - Microsoft​ Edge", " - Microsoft Edge",
                   " - Mozilla Firefox", " - Brave"):
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return None


def _vscode_workspace(title: str | None) -> Optional[str]:
    """VS Code puts the open folder/workspace in its title bar, e.g.
    'main.py - Gama 2.0 - Visual Studio Code'."""
    if not title or "Visual Studio Code" not in title:
        return None
    parts = [p.strip() for p in title.split(" - ")]
    # Title shapes: "file - workspace - Visual Studio Code" or
    # "workspace - Visual Studio Code"
    parts = [p for p in parts if p != "Visual Studio Code"]
    return parts[-1] if parts else None


def _clipboard_preview() -> Optional[str]:
    try:
        import pyperclip
        text = pyperclip.paste()
        if not text:
            return None
        text = text.strip().replace("\n", " ")
        return (text[:120] + "…") if len(text) > 120 else text
    except Exception:
        return None


def _network_status() -> str:
    # A single TCP attempt to one host with a 1s timeout was firing
    # "offline" on any transient blip (one dropped packet, a momentary
    # Wi-Fi hiccup, a slow DNS/router reply) — that single false sample
    # then propagated straight through context_snapshot -> world_model
    # -> proactive_engine's HIGH-priority NetworkOfflineRule, waking GAMA
    # to announce "internet lost" while it was in fact still connected.
    # Try a couple of well-known hosts, and only report offline if none
    # of them answer — a real outage fails every one of these; a blip
    # doesn't.
    #
    # Port 443 (HTTPS) instead of 53 (DNS): plenty of home routers,
    # corporate networks, and some ISPs block or intercept outbound TCP
    # port 53 (forcing clients through their own resolver) while general
    # internet access over HTTPS works completely fine. Probing 53
    # produced "offline" readings on those networks even though the user
    # was demonstrably online. 443 is virtually never blocked, since
    # blocking it would break the web entirely.
    #
    # Debounce: also require the *previous* poll to have failed too
    # before reporting "offline". _poll() already calls this every
    # ~2s, so back-to-back-cycle debouncing costs only one extra poll
    # (~2s) of latency on a genuine outage while filtering out
    # single-cycle blips that survive even the multi-host check above
    # (e.g. the whole machine briefly losing Wi-Fi during a suspend/
    # resume or an AP roam).
    import socket
    global _consecutive_offline_polls
    targets = [("1.1.1.1", 443), ("8.8.8.8", 443), ("1.0.0.1", 443)]
    for host, port in targets:
        try:
            sock = socket.create_connection((host, port), timeout=1.5)
            sock.close()
            _consecutive_offline_polls = 0
            return "online"
        except Exception:
            continue

    _consecutive_offline_polls += 1
    if _consecutive_offline_polls < _OFFLINE_DEBOUNCE_POLLS:
        return "online"
    return "offline"


def _battery() -> Optional[Dict]:
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat is None:
            return None
        return {"percent": round(bat.percent), "plugged": bool(bat.power_plugged)}
    except Exception:
        return None


def _cpu_ram() -> Dict:
    try:
        import psutil
        return {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "ram_percent": round(psutil.virtual_memory().percent, 1),
        }
    except Exception:
        return {"cpu_percent": None, "ram_percent": None}


def _disk_usage() -> Optional[Dict]:
    """Disk usage for the system drive (cached — only polled slowly)."""
    try:
        import psutil
        d = psutil.disk_usage("/")
        return {
            "total_gb": round(d.total / 1e9, 1),
            "used_gb": round(d.used / 1e9, 1),
            "free_gb": round(d.free / 1e9, 1),
            "percent": d.percent,
        }
    except Exception:
        return None


def _selected_text() -> Optional[str]:
    """Best-effort: read the current clipboard selection.
    On Windows, Ctrl+C copy then read; on others just read clipboard as-is.
    This is a lightweight heuristic, not a hook into OS selection APIs."""
    try:
        import pyperclip
        text = pyperclip.paste()
        if not text:
            return None
        text = text.strip()
        # Only report if it looks like a text selection (not a file path or URL)
        if len(text) > 5 and len(text) < 2000:
            return (text[:200] + "…") if len(text) > 200 else text
        return None
    except Exception:
        return None


def _music_playing() -> Optional[str]:
    """Cheap heuristic: is a known media process running + window title
    hints at playback (avoids talking to Spotify/YT APIs)."""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if name in ("spotify.exe", "spotify"):
                return "Spotify"
    except Exception:
        pass
    return None


def _terminal_open(app: Optional[str]) -> bool:
    if not app:
        return False
    app_l = app.lower()
    return any(k in app_l for k in ("cmd.exe", "powershell", "windowsterminal", "wt.exe", "bash"))


def _downloads_recent() -> Optional[str]:
    """Name of the most recently modified file in ~/Downloads, if any
    changed in the last poll window — used for 'download completed'
    proactive suggestions (system_info hooks into this)."""
    try:
        from pathlib import Path
        d = Path.home() / "Downloads"
        if not d.is_dir():
            return None
        files = [f for f in d.iterdir() if f.is_file()]
        if not files:
            return None
        latest = max(files, key=lambda f: f.stat().st_mtime)
        return latest.name
    except Exception:
        return None


def _recent_notifications() -> list[str]:
    """Retrieve the 5 most recent Windows notification center messages."""
    if _OS != "Windows":
        return []
    import os
    import shutil
    import sqlite3
    import tempfile
    import re
    from datetime import datetime

    db_path = os.path.expandvars(r'%LocalAppData%\Microsoft\Windows\Notifications\wpndatabase.db')
    if not os.path.exists(db_path):
        return []

    import uuid
    temp_db = os.path.join(tempfile.gettempdir(), f'gama_wpn_temp_{uuid.uuid4().hex}.db')
    try:
        shutil.copy2(db_path, temp_db)
        conn = sqlite3.connect(temp_db)
        cur = conn.cursor()
        cur.execute("""
            SELECT n.ArrivalTime, h.PrimaryId, n.Payload 
            FROM Notification n 
            JOIN NotificationHandler h ON n.HandlerId = h.RecordId 
            WHERE n.Payload IS NOT NULL
            ORDER BY n.ArrivalTime DESC 
            LIMIT 5
        """)
        rows = cur.fetchall()
        conn.close()
        try:
            os.remove(temp_db)
        except Exception:
            pass

        results = []
        for filetime, app_id, payload_bytes in rows:
            if not payload_bytes:
                continue
            try:
                unix = (filetime - 116444736000000000) / 10000000.0
                dt = datetime.fromtimestamp(unix)
                time_str = dt.strftime("%H:%M")
                
                payload_str = payload_bytes.decode('utf-8', errors='ignore')
                texts = re.findall(r'<text[^>]*>(.*?)</text>', payload_str)
                texts = [t.strip() for t in texts if t.strip()]
                if texts:
                    msg = " | ".join(texts)
                    app_name = app_id.split('!')[-1].split('_')[0]
                    results.append(f"[{time_str}] {app_name}: {msg}")
            except Exception:
                pass
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------
class DesktopContextTracker:
    """Background poller maintaining a live desktop-context snapshot.

    Deliberately cheap: active window + clipboard + network every
    ~2s (all near-instant local calls), heavier probes (battery,
    downloads folder scan) only every ~10s. No screenshots, no OCR,
    no LLM calls — this never leaves the machine.
    """

    def __init__(self, on_download: Optional[callable] = None) -> None:
        self._lock = threading.Lock()
        self._snapshot: Dict = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_count = 0
        self._on_download = on_download
        self._last_seen_download: Optional[str] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gama-desktop-context"
        )
        self._thread.start()
        register_background_task("desktop_monitor", "Desktop Monitor", "Watching active app/window/clipboard")
        logger.info("Desktop context tracker started.")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            from context_engine import complete_background_task
            complete_background_task("desktop_monitor", ok=True, detail="stopped")
        except Exception:
            pass

    def snapshot(self) -> Dict:
        with self._lock:
            return dict(self._snapshot)

    def _loop(self) -> None:
        while self._running:
            try:
                self._poll()
            except Exception as exc:
                logger.debug(f"desktop_context poll failed (non-fatal): {exc}")
            time.sleep(_POLL_INTERVAL)

    def _poll(self) -> None:
        self._tick_count += 1
        win = _active_window()
        data: Dict = {
            **win,
            "browser_tab": _browser_tab_hint(win.get("active_window_title")),
            "vscode_workspace": _vscode_workspace(win.get("active_window_title")),
            "clipboard_preview": _clipboard_preview(),
            "network": _network_status(),
            "terminal_open": _terminal_open(win.get("active_app")),
            "music": _music_playing(),
            "updated_at": time.time(),
        }
        data.update(_cpu_ram())

        # Slower probes — battery + downloads folder + notifications + disk
        if self._tick_count % _SLOW_POLL_EVERY == 0:
            data["battery"] = _battery()
            latest_dl = _downloads_recent()
            data["latest_download"] = latest_dl
            data["notifications"] = _recent_notifications()
            # Disk usage — checked slowly (every ~10s) since it rarely changes
            data["disk"] = _disk_usage()
            if (
                latest_dl
                and self._last_seen_download is not None
                and latest_dl != self._last_seen_download
                and self._on_download
            ):
                try:
                    self._on_download(latest_dl)
                except Exception:
                    pass
            if latest_dl:
                self._last_seen_download = latest_dl
        else:
            with self._lock:
                data["battery"] = self._snapshot.get("battery")
                data["latest_download"] = self._snapshot.get("latest_download")
                data["notifications"] = self._snapshot.get("notifications", [])
                data["disk"] = self._snapshot.get("disk")

        with self._lock:
            prev = self._snapshot
            self._snapshot = data

        # Unified memory integration: record window / workspace context changes
        if prev.get("active_window_title") != data.get("active_window_title"):
            title = data.get("active_window_title")
            app = data.get("active_app")
            workspace = data.get("vscode_workspace")
        self._publish_diff_events(prev, data)

        # M3 (GAMA_ARCHITECTURE_AUDIT.md): World Model `sync_from_context()`
        # existed but had no caller anywhere in the codebase, so `world.computer.*`
        # could go stale indefinitely. This tracker's own poll loop is the
        # natural place to drive it — it already runs on a steady interval
        # and is the ultimate source (via context_engine's snapshot cache)
        # of everything sync_from_context() reads. Kept fire-and-forget /
        # best-effort, matching every other cross-cutting call in this loop.
        try:
            from core.world_model import world as _world
            _world.sync_from_context()
        except Exception:
            pass

    def _publish_diff_events(self, prev: Dict, new: Dict) -> None:
        """Event-driven per the spec: only fires when a cached value
        actually changes, never on every poll tick."""
        try:
            if new.get("active_app") and new.get("active_app") != prev.get("active_app"):
                publish_context_event("ApplicationFocused", app=new["active_app"],
                                       window_title=new.get("active_window_title"))
            if new.get("clipboard_preview") and new.get("clipboard_preview") != prev.get("clipboard_preview"):
                publish_context_event("ClipboardUpdated")
            if new.get("music") and not prev.get("music"):
                publish_context_event("MusicStarted", app=new["music"])
            elif prev.get("music") and not new.get("music"):
                publish_context_event("MusicStopped", app=prev["music"])
            bat = new.get("battery")
            prev_bat = prev.get("battery")
            if bat and bat.get("percent") is not None and bat["percent"] <= 20 and not bat.get("plugged") \
                    and not (prev_bat and prev_bat.get("percent", 100) <= 20):
                publish_context_event("BatteryLow", percent=bat["percent"])
            latest_dl = new.get("latest_download")
            if latest_dl and latest_dl != prev.get("latest_download"):
                publish_context_event("DownloadCompleted", filename=latest_dl)
        except Exception:
            logger.debug("desktop_context: diff-event publish failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton — main.py owns lifecycle (start/stop), but any
# action module can import get_desktop_snapshot() without needing a
# reference to the instance.
# ---------------------------------------------------------------------------
_tracker: Optional[DesktopContextTracker] = None


def init_tracker(on_download: Optional[callable] = None) -> DesktopContextTracker:
    global _tracker
    if _tracker is None:
        _tracker = DesktopContextTracker(on_download=on_download)
    return _tracker


def get_tracker() -> Optional[DesktopContextTracker]:
    return _tracker


def get_desktop_snapshot() -> Dict:
    if _tracker is None:
        return {}
    return _tracker.snapshot()


def summarize_for_prompt() -> str:
    """Short natural-language blurb for injection into the system
    prompt so GAMA passively knows the current desktop context without
    a tool round-trip. Kept intentionally terse to save prompt tokens."""
    s = get_desktop_snapshot()
    if not s:
        return ""
    bits = []
    if s.get("active_app") or s.get("active_window_title"):
        bits.append(
            f"Active app: {s.get('active_app') or 'unknown'} "
            f"(\"{s.get('active_window_title') or ''}\")"
        )
    if s.get("vscode_workspace"):
        bits.append(f"VS Code workspace: {s['vscode_workspace']}")
    if s.get("browser_tab"):
        bits.append(f"Browser tab: {s['browser_tab']}")
    if s.get("terminal_open"):
        bits.append("A terminal window is open.")
    if s.get("music"):
        bits.append(f"{s['music']} appears to be running.")
    if s.get("network"):
        bits.append(f"Network: {s['network']}.")
    bat = s.get("battery")
    if bat:
        bits.append(f"Battery: {bat['percent']}% ({'plugged in' if bat['plugged'] else 'on battery'}).")
    notifs = s.get("notifications")
    if notifs:
        bits.append(f"Recent Notifications: {'; '.join(notifs)}.")

    # Time-of-day contextual awareness
    from datetime import datetime
    now_hour = datetime.now().hour
    if 5 <= now_hour < 12:
        tod = "Morning"
    elif 12 <= now_hour < 17:
        tod = "Afternoon"
    elif 17 <= now_hour < 22:
        tod = "Evening"
    else:
        tod = "Late Night"
    bits.append(f"Time Context: {tod} ({datetime.now().strftime('%I:%M %p')}).")

    if not bits:
        return ""
    return (
        "[LIVE DESKTOP CONTEXT — Situational Awareness & Persona Guidance]\n"
        "Use this context to be naturally attentive, human-like, and context-aware. "
        "Do not recite context fields verbatim unless relevant. Speak concisely, naturally, and warmly.\n"
        + " ".join(bits)
    )


def desktop_context(action: str = "status", **kwargs) -> str:
    """Tool entrypoint — mirrors the actions/*.py `(action, **kwargs)`
    convention used across the codebase (see main.py's dispatcher)."""
    action = (action or "status").lower().strip()
    s = get_desktop_snapshot()
    if not s:
        return "Desktop awareness is still starting up — try again in a moment."

    if action in ("status", "overview"):
        blurb = summarize_for_prompt()
        return blurb or "No desktop context available yet."
    if action == "active_window":
        return f"Active: {s.get('active_app') or 'unknown'} — \"{s.get('active_window_title') or ''}\""
    if action == "clipboard":
        return s.get("clipboard_preview") or "Clipboard is empty or unreadable."
    if action == "network":
        return f"Network is {s.get('network', 'unknown')}."
    if action == "battery":
        bat = s.get("battery")
        return (f"Battery at {bat['percent']}% "
                f"({'plugged in' if bat['plugged'] else 'on battery'})." if bat
                else "No battery detected.")
    if action == "downloads":
        return f"Most recent download: {s.get('latest_download') or 'none found'}."
    if action == "disk":
        disk = s.get("disk")
        if not disk:
            return "Disk usage not yet available."
        return f"Disk: {disk['used_gb']} GB used / {disk['total_gb']} GB total ({disk['percent']}% used, {disk['free_gb']} GB free)."
    if action == "selected_text":
        sel = _selected_text()
        return sel or "No selected text detected on clipboard."
    return f"Unknown desktop_context action: {action}. Use: status, active_window, clipboard, network, battery, downloads, disk, selected_text."


__all__ = [
    "DesktopContextTracker", "init_tracker", "get_tracker",
    "get_desktop_snapshot", "summarize_for_prompt", "desktop_context",
]