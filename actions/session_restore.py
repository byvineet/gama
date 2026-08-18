"""
actions/session_restore.py — Save and restore the last GAMA session.

Saves which user-facing apps were open when GAMA went to sleep.
On the next wake, GAMA can mention what was open and offer to restore it.

Author: Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
from datetime import datetime
from pathlib import Path

import psutil

log = get_logger(__name__)
SESSION_FILE = Path.home() / ".gama_last_session.json"

# Known user-facing processes → friendly display name.
# Kept conservative: only apps the user would consciously "restore".
_TRACKED: dict[str, str] = {
    "chrome.exe":       "Google Chrome",
    "msedge.exe":       "Microsoft Edge",
    "firefox.exe":      "Firefox",
    "Code.exe":         "VS Code",
    "notepad.exe":      "Notepad",
    "notepad++.exe":    "Notepad++",
    "WINWORD.EXE":      "Word",
    "EXCEL.EXE":        "Excel",
    "POWERPNT.EXE":     "PowerPoint",
    "spotify.exe":      "Spotify",
    "Discord.exe":      "Discord",
    "steam.exe":        "Steam",
    "vlc.exe":          "VLC",
    "Slack.exe":        "Slack",
    "Teams.exe":        "Teams",
    "Postman.exe":      "Postman",
    "devenv.exe":       "Visual Studio",
    "pycharm64.exe":    "PyCharm",
    "idea64.exe":       "IntelliJ IDEA",
    "obs64.exe":        "OBS",
    "figma.exe":        "Figma",
    "notion.exe":       "Notion",
    "WhatsApp.exe":     "WhatsApp",
}


def save_session() -> dict:
    """Snapshot currently-open tracked apps to disk. Thread-safe (single write)."""
    open_apps: list[str] = []
    seen: set[str] = set()
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                pname = (proc.info.get("name") or "").strip()
                if pname in _TRACKED and pname not in seen:
                    open_apps.append(_TRACKED[pname])
                    seen.add(pname)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as exc:
        log.debug(f"[session_restore] process scan failed: {exc}")

    data = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "open_apps": open_apps,
    }
    try:
        SESSION_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        log.info(f"[session_restore] Saved session: {open_apps}")
    except Exception as exc:
        log.debug(f"[session_restore] write failed: {exc}")
    return data


def load_session() -> dict:
    """Load the last saved session. Returns {} if nothing saved yet."""
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception as exc:
        log.debug(f"[session_restore] read failed: {exc}")
    return {}


def session_restore_action(action: str = "load", apps: list | None = None) -> str:
    """
    action='save'    — snapshot open apps now (called automatically on sleep).
    action='load'    — return a human-readable summary of the last session.
    action='restore' — reopen the previously saved apps (or a given list).
    """
    if action == "save":
        data = save_session()
        names = data.get("open_apps", [])
        return f"Session saved. Open: {', '.join(names) if names else 'none'}."

    if action == "load":
        data = load_session()
        if not data:
            return "No previous session found."
        ts = data.get("saved_at", "unknown")[:16].replace("T", " at ")
        names = data.get("open_apps", [])
        if not names:
            return "Last session had no tracked apps open."
        return f"Last session ({ts}): {', '.join(names)} were open."

    if action == "restore":
        data = load_session()
        to_open = apps or data.get("open_apps", [])
        if not to_open:
            return "Nothing to restore from the last session."
        results: list[str] = []
        for app in to_open:
            try:
                from actions.app_launcher import open_app
                r = open_app(app)
                results.append(f"{app}: {r}")
            except Exception as exc:
                results.append(f"{app}: failed ({exc})")
        return "\n".join(results)

    return "Unknown action. Use: save, load, restore."


__all__ = ["session_restore_action", "save_session", "load_session"]
