"""
actions/notes.py — Gama Notes System
=====================================
Create, read, list, delete notes saved to Documents/GamaNotes/.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
from datetime import datetime
from pathlib import Path

log = get_logger(__name__)
logger = log  # back-compat alias
_NOTES_DIR = Path.home() / "Documents" / "GamaNotes"


def _ensure_dir() -> Path:
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return _NOTES_DIR


def notes(action: str = "create", **kwargs) -> str:
    """Notes management."""
    action = (action or "create").lower().strip()

    if action == "create":
        return _create(kwargs.get("name", ""), kwargs.get("content", ""))
    if action == "read":
        return _read(kwargs.get("name", ""))
    if action == "list":
        return _list()
    if action == "delete":
        return _delete(kwargs.get("name", ""))
    if action == "append":
        return _append(kwargs.get("name", ""), kwargs.get("content", ""))
    return f"Unknown notes action: {action}. Use: create, read, list, delete, append."


def _create(name: str, content: str) -> str:
    name = (name or "").strip()
    if not name:
        return "What should I name the note?"
    if not content:
        return "What content should I write?"
    _ensure_dir()
    if not name.endswith(".txt"):
        name += ".txt"
    path = _NOTES_DIR / name
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    full_content = f"Created: {timestamp}\n\n{content}\n"
    path.write_text(full_content, encoding="utf-8")
    return f"Note created: {path}\n\n{content}"


def _read(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "Which note should I read?"
    if not name.endswith(".txt"):
        name += ".txt"
    path = _NOTES_DIR / name
    if not path.exists():
        return f"Note '{name}' not found."
    return path.read_text(encoding="utf-8", errors="ignore")


def _list() -> str:
    _ensure_dir()
    files = sorted(_NOTES_DIR.glob("*.txt"))
    if not files:
        return "No notes yet."
    lines = [f"Notes ({len(files)}):"]
    for f in files:
        size = f.stat().st_size
        size_str = f"{size} B" if size < 1024 else f"{size/1024:.1f} KB"
        lines.append(f"  📄 {f.stem} ({size_str})")
    return "\n".join(lines)


def _delete(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "Which note should I delete?"
    if not name.endswith(".txt"):
        name += ".txt"
    path = _NOTES_DIR / name
    if not path.exists():
        return f"Note '{name}' not found."
    try:
        path.unlink()
        return f"Note '{name}' deleted."
    except Exception as exc:
        return f"Delete failed: {exc}"


def _append(name: str, content: str) -> str:
    name = (name or "").strip()
    if not name or not content:
        return "Note name and content required."
    if not name.endswith(".txt"):
        name += ".txt"
    path = _NOTES_DIR / name
    if not path.exists():
        return _create(name, content)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n[{timestamp}]\n{content}\n")
    return f"Appended to note '{name}'."


__all__ = ["notes"]
