"""
actions/self_awareness.py — Gama Self-Awareness (JARVIS style)
================================================================
Gives Gama first-person knowledge of its OWN codebase — what it is,
how it's built, what files/modules make it up, what tools it can call —
plus the ability to read and (carefully) edit its own source files on
command, the way JARVIS reconfigures his own systems for Tony.

This is scoped ENTIRELY to Gama's own install directory (_BASE_DIR).
It never touches the user's personal files — that's file_controller's
job. Think of this as "introspection + self-modification", not general
file management.

Actions
-------
  about()                       -> plain-English self-description
  architecture()                -> directory-by-directory map of how Gama is built
  capabilities()                -> live list of every tool Gama can call right now
  list_files(path="")           -> tree listing of Gama's own source (relative to root)
  read_file(path)                -> read one of Gama's own source files
  search(query)                  -> grep Gama's own codebase for a term/function/symbol
  edit_file(path, find, replace) -> find-and-replace edit inside one of Gama's own
                                     source files (creates a .bak backup, verifies
                                     the change landed, reports a diff-style summary)
  create_file(path, content)     -> create a brand-new file inside Gama's own project
                                     (e.g. a new actions/*.py module)
  revert_file(path)              -> restore the most recent .bak backup for a file

Safety
------
  - Every path is resolved and confined inside _BASE_DIR; any attempt to
    escape it (.., absolute paths outside the repo, symlink tricks) is
    refused.
  - A short list of sensitive files (.env, config/api_keys.json,
    config/wake_word.json, anything under logs/ or memory/) is read-only
    even via read_file, and always refused for edit_file/create_file —
    Gama can talk about the fact that these exist, never dump secrets or
    let itself be tricked into rewriting its own credential store.
  - edit_file/create_file are registered as MEDIUM/HIGH risk tools in
    core/tool_dispatch.py, so the normal confidence-scorer confirmation
    flow applies exactly like any other file-mutating action.
  - edit_file always takes a timestamped backup before writing, and
    refuses if `find` doesn't match exactly once (never guesses).

Author : Vineet Machchal
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from utils.logger import get_logger

log = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent

# Files Gama will never read the contents of, or edit/overwrite, even on
# request — credentials, secrets, and its own logs/memory stores.
_SENSITIVE_PATTERNS = (
    ".env",
    "config/api_keys.json",
    "config/wake_word.json",
    "logs/",
    "memory/",
    "storage/",
    ".git/",
)

# Directories worth summarizing in architecture()/list_files() — anything
# else (venvs, caches, build output) is noise for a self-description.
_ARCH_MAP: Dict[str, str] = {
    "main.py": "Entry point — session/audio orchestration, wake word loop, wires GamaAssistant together.",
    "ui.py": "Desktop overlay UI (the HUD Gama renders on screen).",
    "core/": "Brain: tool dispatch, tool declarations, confidence scoring, fast-intent shortcuts, sub-agent task force.",
    "actions/": "Every individual capability Gama has (one file per tool: files, apps, music, calendar, self-diagnostics, etc.) — this file lives here too.",
    "automation/": "Higher-level automation engine and providers (browser, UI, files) that actions/computer_agent.py orchestrates.",
    "context_engine/": "Resolves ambiguous references ('open it', 'that folder') using recent activity and desktop context.",
    "knowledge/": "Local semantic index over the user's documents/files for knowledge_action / knowledge_query.",
    "learning/": "Long-horizon learning/personalization signal storage.",
    "memory/": "Long-term + short-term memory backends (facts, preferences) behind memory/facade.py.",
    "security/": "Trust levels, credential store, security setup for destructive actions.",
    "state_engine/": "Tracks Gama's own runtime/session state.",
    "voice/": "TTS/voice pipeline and voice profile handling.",
    "wake_word/": "Wake-word detection engine.",
    "widgets/": "HUD widget components rendered by ui.py.",
    "integrations/": "Third-party integrations (Spotify, Google Calendar, etc.).",
    "utils/": "Shared low-level helpers (logging, path resolution, process helpers).",
    "config/": "Runtime configuration/JSON settings (schedules, wake word, API key slots).",
    "build.py": "Packages Gama into a standalone executable (see Gama.spec).",
}


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

class _UnsafePath(Exception):
    pass


def _safe_repo_path(raw: str) -> Path:
    """Resolve a path string to an absolute Path that is guaranteed to sit
    inside _BASE_DIR. Refuses anything that would escape the repo."""
    raw = (raw or "").strip().strip("/\\")
    candidate = (_BASE_DIR / raw).resolve() if raw else _BASE_DIR
    try:
        candidate.relative_to(_BASE_DIR)
    except ValueError:
        raise _UnsafePath(f"'{raw}' resolves outside Gama's own project — refusing.")
    return candidate


def _is_sensitive(p: Path) -> bool:
    try:
        rel = p.relative_to(_BASE_DIR).as_posix().lower()
    except ValueError:
        return True
    return any(rel == pat.lower() or rel.startswith(pat.lower()) for pat in _SENSITIVE_PATTERNS)


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def self_awareness(action: str, **kwargs) -> str:
    action = (action or "about").lower().strip()
    try:
        if action == "about":
            return _about()
        if action == "architecture":
            return _architecture()
        if action == "capabilities":
            return _capabilities()
        if action == "list_files":
            return _list_files(kwargs.get("path", ""))
        if action == "read_file":
            return _read_file(kwargs.get("path", ""))
        if action == "search":
            return _search(kwargs.get("query", ""))
        if action == "edit_file":
            return _edit_file(kwargs.get("path", ""), kwargs.get("find", ""), kwargs.get("replace", ""))
        if action == "create_file":
            return _create_file(kwargs.get("path", ""), kwargs.get("content", ""))
        if action == "revert_file":
            return _revert_file(kwargs.get("path", ""))
        return (f"Unknown self_awareness action: {action}. Use: about, architecture, "
                f"capabilities, list_files, read_file, search, edit_file, create_file, revert_file.")
    except _UnsafePath as exc:
        return str(exc)
    except Exception as exc:
        log.exception("self_awareness failed")
        return f"self_awareness failed: {exc}"


# ---------------------------------------------------------------------------
# Read-only introspection
# ---------------------------------------------------------------------------

def _about() -> str:
    py_files = [p for p in _BASE_DIR.rglob("*.py")
                if "__pycache__" not in p.parts and ".git" not in p.parts]
    return (
        "I'm Gama — a JARVIS-inspired, voice-first AI desktop assistant. I run locally "
        f"from {_BASE_DIR.name}/, built in Python, entry point main.py, with a Qt-based "
        f"HUD overlay in ui.py. My logic is split across {len(py_files)} Python files: "
        "core/ is my dispatch and reasoning layer, actions/ is every individual thing I "
        "can do (files, apps, music, calendar, code, self-diagnostics, and so on), and "
        "the rest (automation/, context_engine/, memory/, knowledge/, security/, voice/, "
        "wake_word/) are the supporting systems around that. I can now also introspect "
        "and edit my own source through this module — ask me to list my files, read one, "
        "or change something, and I will, inside my own project folder only."
    )


def _architecture() -> str:
    lines = ["Gama's architecture:\n"]
    for name, desc in _ARCH_MAP.items():
        exists = (_BASE_DIR / name.rstrip("/")).exists()
        mark = "✓" if exists else "–"
        lines.append(f"  {mark} {name:<18} {desc}")
    return "\n".join(lines)


def _capabilities() -> str:
    """Live list of every tool currently registered with the dispatcher,
    so this never drifts out of sync with what Gama can actually call."""
    try:
        from core.tool_declarations import TOOL_DECLARATIONS
        lines = [f"I currently have {len(TOOL_DECLARATIONS)} callable tools:\n"]
        for decl in TOOL_DECLARATIONS:
            name = decl.get("name", "?")
            desc = (decl.get("description", "") or "").split(". ")[0]
            lines.append(f"  • {name} — {desc}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Couldn't load live tool list ({exc}), but my tool set is declared in core/tool_declarations.py."


def _list_files(rel_path: str) -> str:
    root = _safe_repo_path(rel_path)
    if not root.exists():
        return f"No such path in my project: {rel_path or '.'}"
    if root.is_file():
        return f"{root.relative_to(_BASE_DIR)} ({root.stat().st_size} bytes)"

    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
    lines = [f"Contents of {root.relative_to(_BASE_DIR) or '.'}:\n"]
    items = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    shown = 0
    for item in items:
        if item.name in skip_dirs:
            continue
        tag = "📁" if item.is_dir() else "📄"
        sens = "  (protected)" if _is_sensitive(item) else ""
        lines.append(f"  {tag} {item.name}{sens}")
        shown += 1
        if shown >= 60:
            lines.append("  ... (truncated)")
            break
    return "\n".join(lines)


def _read_file(rel_path: str) -> str:
    if not rel_path:
        return "Tell me which file — e.g. path='actions/self_awareness.py'."
    p = _safe_repo_path(rel_path)
    if not p.exists() or not p.is_file():
        return f"No such file in my project: {rel_path}"
    if _is_sensitive(p):
        return f"'{rel_path}' holds credentials/secrets or runtime data — I won't read that out even for myself."
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Couldn't read '{rel_path}': {exc}"
    if len(text) > 12000:
        text = text[:12000] + f"\n\n... (truncated, {len(text)} bytes total)"
    return f"--- {rel_path} ---\n{text}"


def _search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "Give me something to search for — a function name, string, or symbol."
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv", "logs", "memory", "storage"}
    hits: List[str] = []
    for p in _BASE_DIR.rglob("*.py"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if query.lower() in line.lower():
                    hits.append(f"{p.relative_to(_BASE_DIR)}:{i}: {line.strip()[:140]}")
                    if len(hits) >= 40:
                        break
        except Exception:
            continue
        if len(hits) >= 40:
            break
    if not hits:
        return f"No matches for '{query}' in my own source."
    return f"Found {len(hits)} match(es) for '{query}':\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# Self-modification
# ---------------------------------------------------------------------------

def _backup(p: Path) -> Path:
    bak = p.with_name(p.name + f".{int(time.time())}.bak")
    shutil.copy2(p, bak)
    return bak


def _edit_file(rel_path: str, find: str, replace: str) -> str:
    if not rel_path or not find:
        return "I need path and the exact text to find (find=) to edit a file safely."
    p = _safe_repo_path(rel_path)
    if not p.exists() or not p.is_file():
        return f"No such file in my project: {rel_path}"
    if _is_sensitive(p):
        return f"'{rel_path}' is protected — I won't edit credentials, secrets, logs, or memory stores."

    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Couldn't read '{rel_path}': {exc}"

    count = text.count(find)
    if count == 0:
        return f"Couldn't find that exact text in '{rel_path}' — nothing changed. Try search() first to get the exact line."
    if count > 1:
        return (f"That text appears {count} times in '{rel_path}' — I only edit an exact, "
                f"unique match so I don't change the wrong spot. Give me more surrounding context.")

    bak = _backup(p)
    new_text = text.replace(find, replace, 1)
    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        return f"Edit failed, no changes written: {exc}"

    verified = p.read_text(encoding="utf-8") == new_text
    status = "verified" if verified else "written but could not verify"
    return (f"Edited {rel_path} ({status}). Backup saved as {bak.name} — "
            f"say 'revert {rel_path}' to undo. Note: I edit source on disk, not the "
            f"already-running process — restart_self to pick up the change.")


def _create_file(rel_path: str, content: str) -> str:
    if not rel_path:
        return "Give me a path for the new file, e.g. actions/my_new_tool.py."
    p = _safe_repo_path(rel_path)
    if _is_sensitive(p):
        return f"'{rel_path}' sits in a protected area — I won't create files there."
    if p.exists():
        return f"'{rel_path}' already exists — use edit_file to modify it instead, so nothing gets clobbered."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "", encoding="utf-8")
    except Exception as exc:
        return f"Couldn't create '{rel_path}': {exc}"
    return (f"Created {rel_path} ({len(content or '')} bytes). Note: new modules aren't "
            f"wired into core/tool_declarations.py or core/tool_dispatch.py automatically — "
            f"tell me to register it as a tool if you want me to call it.")


def _revert_file(rel_path: str) -> str:
    if not rel_path:
        return "Tell me which file to revert."
    p = _safe_repo_path(rel_path)
    if _is_sensitive(p):
        return f"'{rel_path}' is protected."
    backups = sorted(p.parent.glob(p.name + ".*.bak"), key=lambda b: b.stat().st_mtime, reverse=True)
    if not backups:
        return f"No backup found for '{rel_path}' — I only keep one from my last edit_file call on it."
    latest = backups[0]
    try:
        shutil.copy2(latest, p)
    except Exception as exc:
        return f"Revert failed: {exc}"
    return f"Reverted {rel_path} from {latest.name}. Restart_self to reload it into the running process."


__all__ = ["self_awareness"]
