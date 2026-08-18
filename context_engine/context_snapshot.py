"""
context_engine/context_snapshot.py — Fast Context Snapshot (Part 2)
====================================================================
Provides a single, unified, cached context snapshot that any module can
query instantly without touching the LLM, filesystem, or OS APIs.

This answers common contextual questions in microseconds:
  "What app is focused?" → snapshot.active_app
  "What's playing?"      → snapshot.media_playing
  "What's on clipboard?" → snapshot.clipboard
  "What folder am I in?" → snapshot.current_folder
  "Am I online?"         → snapshot.network_online

Design:
  - Aggregates data already collected by desktop_context.py (active
    window, clipboard, battery, downloads) and state_engine.StateManager.
  - Never polls, never calls OS APIs itself — reads from the cached
    snapshots other modules maintain.
  - Thread-safe, zero-latency reads (mutex-protected dict copy).
  - Detects contextual "modes" (studying, coding, gaming, browsing)
    from the active app + recent commands.

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from utils.logger import get_logger

log = get_logger(__name__)

# Detected session modes — inferred from active app + recent context
SESSION_MODES = {"studying", "coding", "gaming", "browsing", "working", "idle"}

_CODING_APPS = {"code.exe", "code", "pycharm64.exe", "idea64.exe", "devenv.exe",
                "sublime_text.exe", "atom.exe", "vim", "nvim", "emacs"}
_GAMING_APPS = {"steam.exe", "epicgameslauncher.exe", "gog galaxy.exe", "battlenet.exe",
                "riotclientservices.exe", "leagueclient.exe"}
_BROWSER_APPS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}
_TERMINAL_APPS = {"cmd.exe", "powershell.exe", "windowsterminal.exe", "wt.exe",
                  "bash.exe", "python.exe"}


@dataclass
class ContextSnapshot:
    """Unified, cached context block. All fields are best-effort / None if unknown."""
    # Active window / app
    active_app: Optional[str] = None
    active_window_title: Optional[str] = None
    browser_tab: Optional[str] = None
    vscode_workspace: Optional[str] = None
    current_folder: Optional[str] = None

    # Media
    media_playing: Optional[str] = None  # app name if something is playing

    # System
    network_online: bool = True
    battery_percent: Optional[int] = None
    battery_plugged: Optional[bool] = None
    cpu_percent: Optional[float] = None
    ram_percent: Optional[float] = None

    # Clipboard / selection
    clipboard: Optional[str] = None

    # Inferred mode
    session_mode: str = "idle"  # one of SESSION_MODES

    # Timestamp
    updated_at: float = field(default_factory=time.time)

    def is_fresh(self, max_age_s: float = 5.0) -> bool:
        return (time.time() - self.updated_at) < max_age_s

    def as_prompt_block(self) -> str:
        """Compact text for LLM injection."""
        lines = ["[CONTEXT SNAPSHOT]"]
        if self.active_app:
            lines.append(f"  focused_app: {self.active_app}")
        if self.active_window_title:
            lines.append(f"  window_title: {self.active_window_title}")
        if self.browser_tab:
            lines.append(f"  browser_tab: {self.browser_tab}")
        if self.vscode_workspace:
            lines.append(f"  vscode_workspace: {self.vscode_workspace}")
        if self.current_folder:
            lines.append(f"  current_folder: {self.current_folder}")
        if self.media_playing:
            lines.append(f"  media_playing: {self.media_playing}")
        if self.clipboard:
            clip = (self.clipboard[:80] + "…") if len(self.clipboard) > 80 else self.clipboard
            lines.append(f"  clipboard: {clip}")
        lines.append(f"  network: {'online' if self.network_online else 'offline'}")
        if self.session_mode != "idle":
            lines.append(f"  session_mode: {self.session_mode}")
        if self.battery_percent is not None:
            lines.append(f"  battery: {self.battery_percent}% ({'charging' if self.battery_plugged else 'on battery'})")
        return "\n".join(lines) if len(lines) > 1 else ""


def _detect_mode(active_app: Optional[str], window_title: Optional[str]) -> str:
    """Fast, local heuristic — no LLM call needed."""
    if not active_app:
        return "idle"
    a = active_app.lower()
    if a in _CODING_APPS:
        return "coding"
    if a in _GAMING_APPS:
        return "gaming"
    if a in _BROWSER_APPS:
        # Sub-classify browsing by tab title
        t = (window_title or "").lower()
        if any(k in t for k in ("youtube", "netflix", "twitch", "primevideo")):
            return "browsing"  # media browsing
        if any(k in t for k in ("classroom", "coursera", "udemy", "khan", "lecture", "study")):
            return "studying"
        return "browsing"
    if a in _TERMINAL_APPS:
        return "coding"
    return "working"


class _ContextSnapshotManager:
    """Process-wide singleton that maintains a live snapshot."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot = ContextSnapshot()

    def refresh(self) -> ContextSnapshot:
        """Rebuild the snapshot from other modules' cached data (no OS calls)."""
        try:
            from actions.desktop_context import get_desktop_snapshot
            ds = get_desktop_snapshot()
        except Exception:
            ds = {}

        with self._lock:
            s = ContextSnapshot()
            s.active_app = ds.get("active_app")
            s.active_window_title = ds.get("active_window_title")
            s.browser_tab = ds.get("browser_tab")
            s.vscode_workspace = ds.get("vscode_workspace")
            s.media_playing = ds.get("music")
            s.network_online = ds.get("network", "online") == "online"
            s.cpu_percent = ds.get("cpu_percent")
            s.ram_percent = ds.get("ram_percent")
            s.clipboard = ds.get("clipboard_preview")
            bat = ds.get("battery")
            if bat:
                s.battery_percent = bat.get("percent")
                s.battery_plugged = bat.get("plugged")

            # Detect current_folder from VS Code workspace or terminal
            s.current_folder = s.vscode_workspace

            # Detect session mode
            s.session_mode = _detect_mode(s.active_app, s.active_window_title)

            s.updated_at = time.time()
            self._snapshot = s
            return s

    def get(self, force_refresh: bool = False) -> ContextSnapshot:
        """Return the cached snapshot, refreshing if stale."""
        with self._lock:
            snap = self._snapshot
        if force_refresh or not snap.is_fresh(max_age_s=4.0):
            return self.refresh()
        return snap

    def query(self, question: str) -> Optional[str]:
        """Answer a simple context question without LLM.

        Examples:
          "what app" / "focused" → active app name
          "playing" / "music"    → media app
          "clipboard"            → clipboard text
          "online" / "network"   → network status
          "mode" / "session"     → detected session mode
        """
        snap = self.get()
        q = question.lower()
        if any(k in q for k in ("app", "focused", "active", "window")):
            return snap.active_app or snap.active_window_title
        if any(k in q for k in ("play", "music", "media", "song")):
            return snap.media_playing
        if "clip" in q:
            return snap.clipboard
        if any(k in q for k in ("online", "network", "internet", "connected")):
            return "online" if snap.network_online else "offline"
        if any(k in q for k in ("mode", "session", "doing")):
            return snap.session_mode
        if any(k in q for k in ("battery", "charge")):
            if snap.battery_percent is not None:
                return f"{snap.battery_percent}% ({'charging' if snap.battery_plugged else 'on battery'})"
        if any(k in q for k in ("folder", "directory", "workspace")):
            return snap.current_folder
        if any(k in q for k in ("tab", "browser")):
            return snap.browser_tab
        return None


# Process-wide singleton
_manager = _ContextSnapshotManager()


def get_snapshot(force_refresh: bool = False) -> ContextSnapshot:
    """Return the current unified context snapshot."""
    return _manager.get(force_refresh=force_refresh)


def query_context(question: str) -> Optional[str]:
    """Answer a simple context question without LLM (fast path)."""
    return _manager.query(question)


def refresh_snapshot() -> ContextSnapshot:
    """Force a fresh snapshot rebuild."""
    return _manager.refresh()


__all__ = [
    "ContextSnapshot", "get_snapshot", "query_context", "refresh_snapshot",
    "SESSION_MODES",
]
