"""
actions/self_repair.py — Gama Safe Code Self-Editing & On-Demand Repair
========================================================================
Enables Gama to inspect its own codebase, apply validated patches/fixes,
automatically backup modified files, verify Python syntax (AST parsing),
and revert changes if anything fails or is requested.

Safety Boundaries:
1. Strict Codebase Scope: Operations are strictly restricted to files within
   Gama's root directory (_BASE_DIR). External paths, system directories,
   and root drives are blocked.
2. Protected Targets: .git internals, credentials (config/api_keys.json),
   and virtualenv/binary folders cannot be overwritten.
3. Pre-Edit Backups: Every modified file is backed up to storage/backups/code/
   with a timestamp before changes are written.
4. AST Syntax Validation: Python (.py) edits are parsed with ast.parse() prior
   to disk write. If syntax is invalid, the edit is aborted with 0 changes.
5. Atomic Replacement: Files are written to a temporary file first and atomically
   swapped to prevent corrupt partial writes.

Author : Gama
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import get_logger

log = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_BACKUP_DIR = _BASE_DIR / "storage" / "backups" / "code"

_PROTECTED_REL_PATHS = frozenset({
    "config/api_keys.json",
    ".git",
    "venv",
    ".venv",
})


class SafetyViolationError(Exception):
    """Raised when a code edit violates project safety boundaries."""


def _validate_safe_path(raw_path: str) -> Path:
    """Ensure path is inside Gama's project root and not a protected file."""
    if not raw_path or not str(raw_path).strip():
        raise SafetyViolationError("Path cannot be empty.")

    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = (_BASE_DIR / p).resolve()
    else:
        p = p.resolve()

    # 1. Must be strictly inside _BASE_DIR
    try:
        rel = p.relative_to(_BASE_DIR)
    except ValueError:
        raise SafetyViolationError(
            f"Access denied: '{p}' is outside the Gama project root ({_BASE_DIR})."
        )

    # 2. Check protected relative paths
    rel_str = rel.as_posix()
    for prot in _PROTECTED_REL_PATHS:
        if rel_str == prot or rel_str.startswith(prot + "/"):
            raise SafetyViolationError(
                f"Access denied: modifying '{rel_str}' is prohibited by safety rules."
            )

    return p


def _create_backup(target_path: Path) -> Path:
    """Create a timestamped backup before modifying a file."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    rel_name = target_path.relative_to(_BASE_DIR).as_posix().replace("/", "__")
    stamp = int(time.time())
    backup_file = _BACKUP_DIR / f"{rel_name}_{stamp}.bak"
    shutil.copy2(target_path, backup_file)
    return backup_file


def _validate_syntax(content: str, filename: str = "<source>") -> Tuple[bool, str]:
    """Validate Python AST syntax."""
    if filename.endswith(".py") or "<" in filename:
        try:
            ast.parse(content, filename=filename)
        except SyntaxError as exc:
            return False, f"SyntaxError on line {exc.lineno}: {exc.msg}"
        except Exception as exc:
            return False, f"Validation error: {exc}"
    return True, ""


def read_code(path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
    """Read numbered lines from a file in the Gama codebase."""
    try:
        p = _validate_safe_path(path)
    except SafetyViolationError as exc:
        return f"Safety error: {exc}"

    if not p.exists() or not p.is_file():
        return f"File not found: {path}"

    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        s = max(1, start_line)
        e = min(total, end_line) if end_line else min(total, s + 150)

        numbered = [
            f"{i:4d} | {lines[i - 1]}"
            for i in range(s, e + 1)
        ]
        header = f"=== {p.relative_to(_BASE_DIR).as_posix()} (lines {s}-{e} of {total}) ==="
        return header + "\n" + "\n".join(numbered)
    except Exception as exc:
        return f"Error reading code from {path}: {exc}"


def edit_code(
    path: str,
    target_content: str,
    replacement_content: str,
    allow_multiple: bool = False,
) -> str:
    """Safely replace target text with replacement text in a codebase file.

    Includes AST validation and automated backup.
    """
    try:
        p = _validate_safe_path(path)
    except SafetyViolationError as exc:
        return f"Safety error: {exc}"

    if not p.exists() or not p.is_file():
        return f"File not found: {path}"

    if not target_content:
        return "target_content cannot be empty."

    try:
        original = p.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Could not read {path}: {exc}"

    count = original.count(target_content)
    if count == 0:
        return f"Target content not found in {p.name}. Make sure target_content matches exact lines and indentation."
    if count > 1 and not allow_multiple:
        return (
            f"Target content found {count} times in {p.name}. "
            "Specify a larger or more unique target chunk or set allow_multiple=True."
        )

    new_content = original.replace(target_content, replacement_content, 1 if not allow_multiple else -1)

    # Validate Python syntax before touching the disk
    if p.suffix.lower() == ".py":
        valid, err = _validate_syntax(new_content, filename=p.name)
        if not valid:
            return f"Code edit rejected due to syntax error:\n  {err}\nOriginal file left unchanged."

    # Backup original file
    backup_path = _create_backup(p)

    # Atomic write
    tmp_path = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp_path.write_text(new_content, encoding="utf-8")
        tmp_path.replace(p)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return f"Failed to save changes to {p.name}: {exc}"

    rel_path = p.relative_to(_BASE_DIR).as_posix()
    return f"Successfully updated '{rel_path}'. Backup saved at '{backup_path.name}'. Syntax check passed."


def revert_edit(path: str, backup_filename: Optional[str] = None) -> str:
    """Revert a codebase file to its latest (or specified) backup."""
    try:
        p = _validate_safe_path(path)
    except SafetyViolationError as exc:
        return f"Safety error: {exc}"

    rel_name = p.relative_to(_BASE_DIR).as_posix().replace("/", "__")
    if not _BACKUP_DIR.exists():
        return "No backups directory found."

    backups = sorted(
        _BACKUP_DIR.glob(f"{rel_name}_*.bak"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if not backups:
        return f"No backups found for {p.name}."

    chosen_backup = None
    if backup_filename:
        for b in backups:
            if b.name == backup_filename or b.stem == backup_filename:
                chosen_backup = b
                break
        if not chosen_backup:
            return f"Specified backup '{backup_filename}' not found for {p.name}."
    else:
        chosen_backup = backups[0]

    try:
        shutil.copy2(chosen_backup, p)
        return f"Reverted '{p.relative_to(_BASE_DIR).as_posix()}' to backup '{chosen_backup.name}'."
    except Exception as exc:
        return f"Failed to restore backup: {exc}"


def list_backups(path: Optional[str] = None) -> str:
    """List available code backups."""
    if not _BACKUP_DIR.exists():
        return "No backups currently exist."

    pattern = "*.bak"
    if path:
        try:
            p = _validate_safe_path(path)
            rel_name = p.relative_to(_BASE_DIR).as_posix().replace("/", "__")
            pattern = f"{rel_name}_*.bak"
        except Exception:
            pass

    backups = sorted(_BACKUP_DIR.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    if not backups:
        return "No backups found."

    lines = [f"Code backups ({len(backups)}):"]
    for b in backups[:15]:
        t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b.stat().st_mtime))
        lines.append(f"  - {b.name} ({t_str}, {b.stat().st_size} bytes)")
    return "\n".join(lines)


def verify_syntax(path: str) -> str:
    """Validate Python AST syntax of a specific file."""
    try:
        p = _validate_safe_path(path)
    except SafetyViolationError as exc:
        return f"Safety error: {exc}"

    if not p.exists() or not p.is_file():
        return f"File not found: {path}"

    if p.suffix.lower() != ".py":
        return f"{p.name} is not a Python file."

    try:
        content = p.read_text(encoding="utf-8")
        valid, err = _validate_syntax(content, filename=p.name)
        if valid:
            return f"Syntax check passed for {p.relative_to(_BASE_DIR).as_posix()}."
        return f"Syntax error in {p.relative_to(_BASE_DIR).as_posix()}: {err}"
    except Exception as exc:
        return f"Error reading {p.name}: {exc}"


def self_repair(action: str = "read_code", **kwargs) -> str:
    """Safe Code Inspection & On-Demand Self-Repair Tool."""
    action = (action or "read_code").lower().strip().replace("-", "_").replace(" ", "_")

    path = str(kwargs.get("path") or kwargs.get("file") or kwargs.get("file_path") or "").strip()

    if action in ("read_code", "read", "read_file", "view_file", "view"):
        if not path:
            return "Provide 'path' to read code."
        start_line = int(kwargs.get("start_line") or kwargs.get("start") or 1)
        end_line = kwargs.get("end_line") or kwargs.get("end")
        end_line = int(end_line) if end_line is not None else None
        return read_code(path, start_line=start_line, end_line=end_line)

    if action in ("edit_code", "edit", "patch", "patch_file", "fix"):
        if not path:
            return "Provide 'path' of the file to edit."
        target_content = str(kwargs.get("target_content") or kwargs.get("target") or kwargs.get("old") or "")
        replacement_content = str(kwargs.get("replacement_content") or kwargs.get("replacement") or kwargs.get("new") or "")
        allow_multiple = kwargs.get("allow_multiple") in (True, "true", "1", 1)
        return edit_code(
            path,
            target_content=target_content,
            replacement_content=replacement_content,
            allow_multiple=allow_multiple,
        )

    if action in ("revert", "rollback", "restore", "undo"):
        if not path:
            return "Provide 'path' of the file to revert."
        backup_file = kwargs.get("backup") or kwargs.get("backup_filename") or None
        return revert_edit(path, backup_filename=backup_file)

    if action in ("verify_syntax", "check_syntax", "syntax"):
        if not path:
            return "Provide 'path' of the Python file to verify."
        return verify_syntax(path)

    if action in ("list_backups", "backups"):
        return list_backups(path=path or None)

    return (
        "Unknown self_repair action. Use: read_code, edit_code, revert, "
        "verify_syntax, list_backups."
    )


__all__ = [
    "self_repair",
    "read_code",
    "edit_code",
    "revert_edit",
    "verify_syntax",
    "list_backups",
]
