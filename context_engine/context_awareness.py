"""
context_engine/context_awareness.py — Context Awareness Engine
===============================================================
Phase 3 of the JARVIS intelligence architecture.

Sits between the raw desktop sensors and the World Model.
Responsibility: sense the environment and keep the World Model current.
It never duplicates state — it reads from cached OS snapshots and writes
to the World Model as the single source of truth.

Continuously observes:
  • Active window + focused app
  • Clipboard contents
  • Selected files
  • Current working folder
  • Browser URL / tab title
  • Playing media
  • Audio devices
  • CPU / RAM / Battery
  • Internet connectivity
  • Current active task
  • Conversation state

Context resolution — vague command handling:
  When the user says "send this file", the engine automatically
  resolves "this file" from selected_files or clipboard before
  the planner ever sees the command.

Design:
  - Wraps existing context_engine/context_snapshot.py (no replacement)
  - Subscribes to EventBus for reactive updates
  - Runs a low-frequency background refresh (every 4s by default)
  - Pushes all changes to core.world_model.world
  - Exposes resolve_vague(ref) for instant pronoun resolution

Author : Vineet Machchal
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Vague reference patterns — what "it", "that file", etc. map to
# ---------------------------------------------------------------------------

_PRONOUN_CANDIDATES: Dict[str, List[str]] = {
    "it":        ["file", "song", "url", "app", "folder", "message", "last_result"],
    "that":      ["file", "url", "song", "last_result"],
    "this":      ["file", "clipboard", "selection"],
    "this file": ["file", "selected_file", "clipboard_file"],
    "that file": ["file", "selected_file"],
    "there":     ["folder", "directory", "current_folder"],
    "the file":  ["file", "selected_file"],
    "it open":   ["active_window", "app"],
    "them":      ["files", "selected_files"],
}


class ContextAwarenessEngine:
    """
    Keeps the World Model current by continuously reading from:
      1. context_engine.context_snapshot  (existing module — wraps OS APIs)
      2. actions.desktop_context          (existing module — desktop tracker)
      3. state_engine.manager             (existing module — user state)

    Pushes changes to core.world_model.world on every refresh cycle.

    Usage::

        from context_engine.context_awareness import context_awareness

        context_awareness.start()

        # Instantly resolve "that file" to an actual path
        path = context_awareness.resolve_vague("that file")
    """

    def __init__(self, refresh_interval: float = 4.0) -> None:
        self._interval = refresh_interval
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.RLock()
        self._last_refresh = 0.0

        # Cache of the last known slot values for change detection
        self._last_snapshot: Dict[str, Any] = {}

    # ── Start / stop ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background context refresh thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="ContextAwareness",
            daemon=True,
        )
        self._thread.start()

        # Subscribe to EventBus for reactive updates (faster than polling)
        try:
            from state_engine.event_bus import event_bus
            event_bus.subscribe("ApplicationFocused", self._on_app_focused)
            event_bus.subscribe("ClipboardChanged", self._on_clipboard_changed)
            event_bus.subscribe("FileSelected", self._on_file_selected)
            event_bus.subscribe("MediaChanged", self._on_media_changed)
        except Exception:
            pass

        log.info("[ContextAwareness] Started.")

    def stop(self) -> None:
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh_now(self) -> Dict[str, Any]:
        """Force an immediate context refresh and return the new snapshot dict."""
        return self._refresh()

    def resolve_vague(self, ref: str) -> Optional[str]:
        """
        Resolve a vague reference ("it", "that file", "there") to a concrete value
        using the current World Model state.

        Returns the resolved value as a string, or None if unresolvable.
        """
        ref_lower = ref.lower().strip()

        # 1. Check conversation references in World Model
        try:
            from core.world_model import world
            resolved = world.resolve_reference(ref_lower)
            if resolved:
                return str(resolved)
        except Exception:
            pass

        # 2. Check pronoun candidates against World Model fields
        candidates = _PRONOUN_CANDIDATES.get(ref_lower, [ref_lower])
        try:
            from core.world_model import world
            snap = world.snapshot()
            c = snap.computer

            for candidate in candidates:
                if candidate in ("file", "selected_file") and c.selected_files:
                    return c.selected_files[0]
                if candidate in ("clipboard", "clipboard_file") and c.clipboard:
                    # Clipboard might be a file path
                    if any(c.clipboard.endswith(ext) for ext in
                           (".py", ".txt", ".md", ".pdf", ".zip", ".json", ".csv")):
                        return c.clipboard
                if candidate in ("url", "browser_url") and c.browser_url:
                    return c.browser_url
                if candidate in ("song",) and c.media_playing:
                    return c.media_playing
                if candidate in ("app", "active_window") and c.active_app:
                    return c.active_app
                if candidate in ("folder", "directory", "current_folder") and c.current_folder:
                    return c.current_folder
        except Exception:
            pass

        return None

    def get_context_for_command(self, command: str) -> Dict[str, Any]:
        """
        Return the most relevant context fields for resolving a command.
        Automatically detects what context elements the command is likely to need.
        """
        ctx: Dict[str, Any] = {}
        cmd_lower = command.lower()

        try:
            from core.world_model import world
            snap = world.snapshot()
            c = snap.computer

            # File-related commands
            if any(k in cmd_lower for k in ("file", "send", "open", "upload", "attach", "zip")):
                if c.selected_files:
                    ctx["file"] = c.selected_files[0]
                if c.current_folder:
                    ctx["folder"] = c.current_folder
                if c.clipboard:
                    ctx["clipboard"] = c.clipboard

            # App-related commands
            if any(k in cmd_lower for k in ("app", "window", "close", "minimize", "focus")):
                ctx["active_app"] = c.active_app
                ctx["active_window"] = c.active_window_title

            # Browser-related commands
            if any(k in cmd_lower for k in ("tab", "browser", "url", "site", "page")):
                ctx["browser_tab"] = c.browser_tab
                ctx["browser_url"] = c.browser_url

            # Music-related commands
            if any(k in cmd_lower for k in ("play", "pause", "song", "music", "skip")):
                ctx["media_playing"] = c.media_playing

            # System commands
            if any(k in cmd_lower for k in ("battery", "cpu", "ram", "memory", "volume")):
                ctx["battery_percent"] = c.battery_percent
                ctx["cpu_percent"] = c.cpu_percent
                ctx["ram_percent"] = c.ram_percent

            # Pronoun resolution
            for pronoun in ("it", "that", "this", "there", "them", "this file", "that file"):
                if pronoun in cmd_lower:
                    resolved = self.resolve_vague(pronoun)
                    if resolved:
                        ctx[f"resolved_{pronoun.replace(' ', '_')}"] = resolved
                        log.debug(f"[ContextAwareness] Resolved '{pronoun}' → {resolved!r}")

        except Exception:
            pass

        return ctx

    # ── Internal refresh ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._refresh()
            except Exception as exc:
                log.debug(f"[ContextAwareness] Refresh error: {exc}")
            time.sleep(self._interval)

    def _refresh(self) -> Dict[str, Any]:
        """Pull from all existing context sources and push to World Model."""
        snap = {}

        # Pull from existing context_snapshot module
        try:
            from context_engine.context_snapshot import refresh_snapshot
            cs = refresh_snapshot()
            snap.update({
                "active_app": cs.active_app,
                "active_window_title": cs.active_window_title,
                "browser_tab": cs.browser_tab,
                "current_folder": cs.current_folder,
                "media_playing": cs.media_playing,
                "network_online": cs.network_online,
                "cpu_percent": cs.cpu_percent,
                "ram_percent": cs.ram_percent,
                "battery_percent": cs.battery_percent,
                "battery_plugged": cs.battery_plugged,
                "clipboard": cs.clipboard,
                "session_mode": cs.session_mode,
            })
        except Exception:
            pass

        # Pull selected files from desktop context if available
        try:
            from actions.desktop_context import get_desktop_snapshot  # type: ignore
            ds = get_desktop_snapshot()
            if ds.get("selected_files"):
                snap["selected_files"] = ds["selected_files"]
            if ds.get("browser_url"):
                snap["browser_url"] = ds["browser_url"]
        except Exception:
            pass

        # Push to World Model (only changed fields)
        changed = {k: v for k, v in snap.items() if v != self._last_snapshot.get(k)}
        if changed:
            try:
                from core.world_model import world
                world.update_computer(**{k: v for k, v in changed.items() if v is not None})
            except Exception:
                pass
            self._last_snapshot.update(snap)

        self._last_refresh = time.time()
        return snap

    # ── EventBus handlers ─────────────────────────────────────────────────────

    def _on_app_focused(self, event: Any) -> None:
        app = event.data.get("app") or event.data.get("name")
        if app:
            try:
                from core.world_model import world
                world.update_computer(active_app=app)
                world.set_reference("app", app, ttl=300.0)
            except Exception:
                pass

    def _on_clipboard_changed(self, event: Any) -> None:
        text = event.data.get("text", "")
        if text:
            try:
                from core.world_model import world
                world.update_computer(clipboard=text)
                world.set_reference("clipboard", text, ttl=120.0)
                # If it looks like a file path, also store as "file"
                if any(text.endswith(ext) for ext in (".py", ".txt", ".md", ".pdf", ".zip")):
                    world.set_reference("file", text, ttl=300.0)
            except Exception:
                pass

    def _on_file_selected(self, event: Any) -> None:
        path = event.data.get("path") or event.data.get("file")
        if path:
            try:
                from core.world_model import world
                snap = world.snapshot()
                files = list(snap.computer.selected_files or [])
                if path not in files:
                    files.insert(0, path)
                world.update_computer(selected_files=files[:10])
                world.set_reference("file", path, ttl=600.0)
            except Exception:
                pass

    def _on_media_changed(self, event: Any) -> None:
        song = event.data.get("title") or event.data.get("song")
        if song:
            try:
                from core.world_model import world
                world.update_computer(media_playing=song)
                world.set_reference("song", song, ttl=300.0)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

context_awareness = ContextAwarenessEngine()

__all__ = [
    "ContextAwarenessEngine", "context_awareness",
]
