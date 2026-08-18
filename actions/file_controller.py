"""
actions/file_controller.py — Gama File Management (Mark style)
Create, delete, move, rename, list, find, organize files.

Reliability layer: every mutating operation checks the filesystem
afterwards to confirm it actually happened, and transient failures
(file briefly locked by antivirus/indexer, etc.) get one automatic
retry with a short backoff before we report an error.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import send2trash
import time
from pathlib import Path
from datetime import datetime

from actions.reliability import retry, is_transient_error
from utils.windows_paths import resolve_user_path
from actions import recent_files
from actions.context_resolver import resolve_file_reference

log = get_logger(__name__)
logger = log  # back-compat alias
# Paths we refuse to hand to a destructive operation even if something
# upstream resolved to them — belt-and-suspenders against a bad match
# (e.g. a badly-scoped semantic search hit) nuking something huge.
_PROTECTED_ROOTS = {"c:\\", "c:/", "/", str(Path.home()).lower()}


def _is_protected_root(p: Path) -> bool:
    try:
        norm = str(p).rstrip("\\/").lower()
        return norm in _PROTECTED_ROOTS or (norm + "\\") in _PROTECTED_ROOTS or (norm + "/") in _PROTECTED_ROOTS
    except Exception:
        return False


def _resolve_path(raw: str) -> Path:
    """Resolve a raw path string to an absolute Path.

    Uses the Windows Known Folder API (via utils.windows_paths) so
    shortcuts like "desktop", "downloads", "~/foo" all resolve to the
    user's REAL Windows folders — never to the Gama install directory.
    """
    return resolve_user_path(raw)


def file_controller(action: str, **kwargs) -> str:
    action = (action or "").lower().strip()
    if action == "create_folder":
        return _create_folder(kwargs.get("path", ""))
    if action == "delete":
        return _delete(kwargs.get("path", ""))
    if action == "move":
        return _move(kwargs.get("src", ""), kwargs.get("dest", ""))
    if action == "copy":
        return _copy(kwargs.get("src", ""), kwargs.get("dest", ""))
    if action == "rename":
        return _rename(kwargs.get("src", ""), kwargs.get("dest", ""))
    if action == "list":
        return _list(kwargs.get("path", ""))
    if action == "find":
        return _find(kwargs.get("root", ""), kwargs.get("pattern", "*"))
    if action == "open_folder":
        return _open_folder(kwargs.get("path", "") or "C:/Users/notde")
    return f"Unknown file action: {action}. Use: create_folder, open_folder, delete, move, copy, rename, list, find."


class _PermanentFsError(Exception):
    """Wraps a filesystem error we've decided is NOT worth retrying."""


def _retry_fs_op(fn, attempts: int = 3, delay: float = 0.4):
    """Retry a filesystem mutation on transient OS errors (locked file,
    AV scan in progress, etc.) — not on permanent ones like a bad path."""
    def _wrapped():
        try:
            return fn()
        except FileNotFoundError:
            raise  # never worth retrying — the source just isn't there
        except (PermissionError, OSError) as exc:
            if is_transient_error(exc):
                raise
            raise _PermanentFsError(str(exc)) from exc

    try:
        return retry(_wrapped, attempts=attempts, delay=delay,
                     exceptions=(PermissionError, OSError))
    except _PermanentFsError as perm:
        raise (perm.__cause__ or perm)


def _create_folder(path: str) -> str:
    try:
        p = _resolve_path(path)
        _retry_fs_op(lambda: p.mkdir(parents=True, exist_ok=True))
        if p.exists() and p.is_dir():
            recent_files.record(str(p), "created")
            return f"Folder created: {p} (verified)."
        return f"Create folder command ran but '{p}' doesn't exist — check permissions."
    except Exception as exc:
        return f"Create folder failed: {exc}"


def _delete(path: str = "") -> str:
    """Delete (recycle) a file or folder.

    `path` may be empty or a conversational reference ("it", "that
    folder", "the last downloaded file") — resolve_file_reference()
    figures out the concrete target from recent activity / desktop
    context / semantic search. This function never runs the actual
    delete without a validated, existing, non-root path.
    """
    try:
        resolved = resolve_file_reference(path)
        if resolved.status == "ambiguous":
            options = "\n".join(f"  - {c}" for c in resolved.candidates)
            return f"{resolved.message}\n{options}"
        if resolved.status == "not_found":
            return resolved.message or f"I couldn't find '{path}' to delete."

        p = resolved.path
        if p is None or str(p).strip() == "":
            # Should be unreachable given the checks above, but a
            # destructive action must never proceed on an empty path.
            return "I don't have a valid file or folder to delete — please tell me which one."
        if not p.exists():
            return f"Not found: {p}"
        if _is_protected_root(p):
            return f"Refusing to delete '{p}' — that's a protected top-level folder."

        _retry_fs_op(lambda: send2trash.send2trash(str(p)))
        if not p.exists():
            recent_files.record(str(p), "deleted")
            return f"Moved to Recycle Bin: {p} (verified)."
        return f"Sent '{p}' to Recycle Bin, but it's still showing at the original path."
    except Exception as exc:
        logger.exception("Delete failed")
        return f"Delete failed: {exc}"


def _move(src: str, dest: str) -> str:
    try:
        import shutil
        s = _resolve_path(src)
        d = _resolve_path(dest)
        if not s.exists():
            return f"Source not found: {s}"
        _retry_fs_op(lambda: shutil.move(str(s), str(d)))
        final = d if d.exists() else (d / s.name)
        if final.exists() and not s.exists():
            recent_files.record(str(final), "moved")
            return f"Moved: {s} -> {final} (verified)."
        return f"Move command ran but couldn't verify the result at '{d}'."
    except Exception as exc:
        return f"Move failed: {exc}"


def _copy(src: str, dest: str) -> str:
    try:
        import shutil
        s = _resolve_path(src)
        d = _resolve_path(dest)
        if not s.exists():
            return f"Source not found: {s}"

        def _do_copy():
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)

        _retry_fs_op(_do_copy)
        final = d if d.exists() else (d / s.name)
        if final.exists():
            recent_files.record(str(final), "copied")
            return f"Copied: {s} -> {final} (verified)."
        return f"Copy command ran but '{d}' doesn't exist — check the destination."
    except Exception as exc:
        return f"Copy failed: {exc}"


def _rename(src: str, dest: str) -> str:
    try:
        s = _resolve_path(src)
        d = _resolve_path(dest)
        if not s.exists():
            return f"Source not found: {s}"
        _retry_fs_op(lambda: s.rename(d))
        if d.exists() and not s.exists():
            recent_files.record(str(d), "renamed")
            return f"Renamed: {s.name} -> {d.name} (verified)."
        return f"Rename command ran but couldn't verify — check '{d}'."
    except Exception as exc:
        return f"Rename failed: {exc}"


def _list(path: str) -> str:
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_dir():
            return f"Not a directory: {p}"
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        if not items:
            return f"Empty folder: {p}"
        lines = [f"Contents of {p}:\n"]
        for item in items[:50]:
            tag = "📁" if item.is_dir() else "📄"
            size = ""
            if item.is_file():
                try:
                    s = item.stat().st_size
                    if s < 1024: size = f" ({s} B)"
                    elif s < 1024**2: size = f" ({s/1024:.1f} KB)"
                    else: size = f" ({s/1024**2:.1f} MB)"
                except Exception:
                    pass
            lines.append(f"  {tag} {item.name}{size}")
        if len(items) > 50:
            lines.append(f"\n... and {len(items) - 50} more")
        return "\n".join(lines)
    except Exception as exc:
        return f"List failed: {exc}"


def _find(root: str, pattern: str) -> str:
    try:
        p = _resolve_path(root)
        if not p.exists():
            return f"Folder not found: {p}"
        matches = list(p.rglob(pattern))[:30]
        if not matches:
            return f"No files matching '{pattern}' in {p}"
        return "\n".join(str(m) for m in matches)
    except Exception as exc:
        return f"Find failed: {exc}"


def _open_folder(path: str) -> str:
    try:
        p = _resolve_path(path)
        if not p.exists():
            return f"Folder not found: {p}"
        if not p.is_dir():
            return f"Not a folder: {p}"
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(p)])
        recent_files.record(str(p), "opened")
        return f"Opened folder: {p}"
    except Exception as exc:
        return f"Open folder failed: {exc}"


__all__ = ["file_controller"]
