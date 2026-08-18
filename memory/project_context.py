"""
memory/project_context.py — Active project / work context
=========================================================
User says: "I'm working on Project Alpha"
→ stored locally; prompt injection + occasional gentle check-ins.
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

log = get_logger(__name__)
_STATE_NAME = "project_context.json"


def _path() -> Path:
    try:
        from utils.paths import get_base_dir
        DATA_DIR = get_base_dir()
        base = Path(DATA_DIR)
    except Exception:
        base = Path(__file__).resolve().parents[1] / "storage"
    base.mkdir(parents=True, exist_ok=True)
    return base / _STATE_NAME


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {"active": None, "projects": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"active": None, "projects": {}}


def _save(data: dict) -> None:
    try:
        _path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.debug("project_context save failed: %s", exc)


def set_active_project(
    name: str,
    *,
    path: str | None = None,
    notes: str | None = None,
) -> str:
    name = (name or "").strip()
    if not name:
        return "Which project should I set as active?"
    data = _load()
    key = name.lower()
    proj = data.get("projects", {}).get(key) or {
        "name": name,
        "created_ts": time.time(),
        "checkins": 0,
    }
    proj["name"] = name
    now = time.time()
    proj["last_active_ts"] = now
    proj["last_update_ts"] = now
    if path:
        proj["path"] = path
    if notes:
        proj["notes"] = notes
    data.setdefault("projects", {})[key] = proj
    data["active"] = key
    data["dnd_until"] = data.get("dnd_until")  # preserve
    _save(data)
    return f"Active project set to '{name}'. I'll keep that in mind, Sir."


def clear_active_project() -> str:
    data = _load()
    prev = data.get("active")
    data["active"] = None
    _save(data)
    if prev:
        return f"Cleared active project ('{prev}')."
    return "No active project was set."


def get_active_project() -> Optional[dict]:
    data = _load()
    key = data.get("active")
    if not key:
        return None
    return data.get("projects", {}).get(key)


def list_projects() -> list[dict]:
    data = _load()
    return list(data.get("projects", {}).values())


def touch_checkin() -> None:
    data = _load()
    key = data.get("active")
    if not key:
        return
    proj = data.get("projects", {}).get(key)
    if not proj:
        return
    proj["checkins"] = int(proj.get("checkins") or 0) + 1
    proj["last_checkin_ts"] = time.time()
    data["projects"][key] = proj
    _save(data)


def note_project_update(note: str | None = None) -> None:
    """Call when user/model records progress on the active project."""
    data = _load()
    key = data.get("active")
    if not key:
        return
    proj = data.get("projects", {}).get(key)
    if not proj:
        return
    now = time.time()
    proj["last_update_ts"] = now
    proj["last_active_ts"] = now
    if note:
        proj["last_note"] = str(note)[:240]
    data["projects"][key] = proj
    _save(data)


def set_dnd(minutes: int = 60, reason: str = "") -> str:
    data = _load()
    data["dnd_until"] = time.time() + max(1, int(minutes)) * 60
    data["dnd_reason"] = reason or "do not disturb"
    _save(data)
    return f"Do-not-disturb on for {minutes} minutes."


def clear_dnd() -> str:
    data = _load()
    data["dnd_until"] = 0
    data["dnd_reason"] = ""
    _save(data)
    return "Do-not-disturb cleared."


def is_dnd() -> bool:
    data = _load()
    until = float(data.get("dnd_until") or 0)
    return time.time() < until


def prompt_fragment() -> str:
    """Inject into system context — cheap string only."""
    parts = []
    if is_dnd():
        data = _load()
        reason = data.get("dnd_reason") or "do not disturb"
        parts.append(
            f"DND ACTIVE ({reason}): do not initiate check-ins or non-essential suggestions."
        )
    proj = get_active_project()
    if proj:
        name = proj.get("name", "active project")
        path = proj.get("path")
        notes = proj.get("notes")
        line = f"Active project: {name}."
        if path:
            line += f" Path: {path}."
        if notes:
            line += f" Notes: {notes}."
        line += " If relevant, you may briefly ask how it is going — at most occasionally, never spam."
        parts.append(line)
    return " ".join(parts)


_SET_PATTERNS = [
    re.compile(r"\bi(?:'m| am)?\s+working on (?:project\s+)?(.+)$", re.I),
    re.compile(r"\bset (?:active )?project(?: to)?\s+(.+)$", re.I),
    re.compile(r"\bswitch(?:ing)? to project\s+(.+)$", re.I),
    re.compile(r"\bmy (?:current )?project is\s+(.+)$", re.I),
]


def try_parse_set_project(text: str) -> Optional[str]:
    t = (text or "").strip()
    for pat in _SET_PATTERNS:
        m = pat.search(t)
        if m:
            name = m.group(1).strip().strip(" .!,'\"")
            name = re.sub(r"\s+(now|today|please)$", "", name, flags=re.I).strip()
            if 1 < len(name) < 80:
                return name
    return None


def project_context_action(action: str = "status", **kwargs) -> str:
    action = (action or "status").lower().strip()
    if action in ("set", "activate", "start"):
        return set_active_project(
            kwargs.get("name") or kwargs.get("project") or "",
            path=kwargs.get("path"),
            notes=kwargs.get("notes"),
        )
    if action in ("clear", "end", "stop"):
        return clear_active_project()
    if action in ("list",):
        items = list_projects()
        if not items:
            return "No projects stored yet."
        lines = [f"- {p.get('name')}" + (f" ({p.get('path')})" if p.get("path") else "") for p in items]
        active = get_active_project()
        head = f"Active: {active['name']}\n" if active else ""
        return head + "Projects:\n" + "\n".join(lines)
    if action in ("dnd", "do_not_disturb"):
        mins = int(kwargs.get("minutes") or kwargs.get("mins") or 60)
        return set_dnd(mins, reason=kwargs.get("reason") or "")
    if action in ("clear_dnd", "dnd_off"):
        return clear_dnd()
    # status
    proj = get_active_project()
    dnd = is_dnd()
    if not proj and not dnd:
        return "No active project. Say 'I'm working on Project X' to set one."
    parts = []
    if proj:
        parts.append(f"Active project: {proj.get('name')}.")
        if proj.get("path"):
            parts.append(f"Path: {proj['path']}.")
    if dnd:
        parts.append("Do-not-disturb is active.")
    return " ".join(parts)


__all__ = [
    "set_active_project",
    "clear_active_project",
    "get_active_project",
    "list_projects",
    "prompt_fragment",
    "try_parse_set_project",
    "project_context_action",
    "set_dnd",
    "clear_dnd",
    "is_dnd",
    "touch_checkin",
]
