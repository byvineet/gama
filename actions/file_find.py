"""
actions/file_find.py — Intent-based find & open
================================================
Find files by name / partial name / type / recency and open them.
Uses local filesystem only (Downloads, Documents, Desktop, project roots).
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

log = get_logger(__name__)
_MAX_RESULTS = 12
_MAX_SCAN_FILES = 4000
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
    "AppData", "Application Data", "$Recycle.Bin", "System Volume Information",
}


def _user_home() -> Path:
    return Path.home()


def _default_roots() -> list[Path]:
    home = _user_home()
    roots = [
        home / "Downloads",
        home / "Documents",
        home / "Desktop",
        home / "OneDrive",
    ]
    # Active project root if set
    try:
        from memory.project_context import get_active_project
        proj = get_active_project()
        if proj and proj.get("path"):
            roots.insert(0, Path(proj["path"]))
    except Exception:
        pass
    return [p for p in roots if p.exists() and p.is_dir()]


def _score(path: Path, query: str, now: float) -> float:
    name = path.name.lower()
    q = query.lower().strip()
    score = 0.0
    if name == q:
        score += 100
    elif name.startswith(q):
        score += 70
    elif q in name:
        score += 50
    else:
        # token overlap
        tokens = [t for t in re.split(r"[\s_\-\.]+", q) if t]
        hit = sum(1 for t in tokens if t in name)
        score += hit * 12
    try:
        age_h = max(0.0, (now - path.stat().st_mtime) / 3600.0)
        # fresher files rank higher
        score += max(0.0, 20.0 - min(age_h, 20.0))
    except Exception:
        pass
    return score


def _iter_files(roots: Iterable[Path], exts: Optional[set[str]] = None):
    count = 0
    for root in roots:
        if count >= _MAX_SCAN_FILES:
            break
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if count >= _MAX_SCAN_FILES:
                        return
                    p = Path(dirpath) / fn
                    if exts and p.suffix.lower().lstrip(".") not in exts and p.suffix.lower() not in exts:
                        continue
                    count += 1
                    yield p
        except Exception:
            continue


def find_files(
    query: str,
    *,
    limit: int = 8,
    file_type: str | None = None,
    roots: list[Path] | None = None,
) -> list[Path]:
    q = (query or "").strip()
    if not q:
        return []
    exts = None
    if file_type:
        ft = file_type.lower().strip().lstrip(".")
        alias = {
            "pdf": {"pdf"},
            "doc": {"doc", "docx"},
            "excel": {"xls", "xlsx", "csv"},
            "sheet": {"xls", "xlsx", "csv"},
            "image": {"png", "jpg", "jpeg", "webp", "gif"},
            "code": {"py", "js", "ts", "tsx", "jsx", "java", "cpp", "c", "h", "rs", "go"},
            "text": {"txt", "md", "log"},
        }
        exts = alias.get(ft, {ft})
    # extension in query: "fee sheet pdf"
    m = re.search(r"\b(pdf|docx?|xlsx?|csv|png|jpe?g|py|txt|md)\b", q, re.I)
    if m and not exts:
        exts = {m.group(1).lower()}
        q = re.sub(r"\b(pdf|docx?|xlsx?|csv|png|jpe?g|py|txt|md)\b", " ", q, flags=re.I).strip()

    now = time.time()
    scored: list[tuple[float, Path]] = []
    for p in _iter_files(roots or _default_roots(), exts):
        s = _score(p, q, now)
        if s >= 12:
            scored.append((s, p))
    scored.sort(key=lambda x: (-x[0], -x[1].stat().st_mtime if x[1].exists() else 0))
    # unique by resolved path
    seen = set()
    out: list[Path] = []
    for _, p in scored:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def open_path(path: Path | str) -> str:
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return "No path provided."
    p = Path(raw)
    # Bare filename or relative → resolve via search
    if not p.exists():
        hits = find_files(p.name if p.name else raw, limit=1)
        if hits:
            p = hits[0]
        else:
            # Try Downloads / Documents / Desktop join
            for folder in ("Downloads", "Documents", "Desktop"):
                cand = _user_home() / folder / raw
                if cand.exists():
                    p = cand
                    break
    if not p.exists():
        return f"File not found: {raw}"
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return f"Opened: {p}"
    except Exception as exc:
        return f"Failed to open {p}: {exc}"


def file_find(action: str = "find", **kwargs) -> str:
    """
    Actions:
      find   — search by query / type
      open   — find best match and open (or open path=)
      recent — list recent downloads-ish hits for query
    """
    action = (action or "find").lower().strip()
    query = (kwargs.get("query") or kwargs.get("name") or kwargs.get("q") or "").strip()
    path = kwargs.get("path") or kwargs.get("file")
    file_type = kwargs.get("type") or kwargs.get("file_type")
    limit = int(kwargs.get("limit") or 8)

    if action in ("open", "open_file") and path:
        return open_path(path)

    if action in ("open", "open_file", "find_and_open"):
        # path may be a bare filename — open_path resolves it
        if path:
            return open_path(path)
        if not query:
            return "What file should I find and open?"
        hits = find_files(query, limit=1, file_type=file_type)
        if not hits:
            return f"No file matched '{query}'."
        return open_path(hits[0])

    if action in ("find", "search", "list", "recent"):
        if action == "recent" and not query:
            dl = _user_home() / "Downloads"
            if not dl.exists():
                return "Downloads folder not found."
            files = sorted(
                [p for p in dl.iterdir() if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
            if not files:
                return "No recent files found in Downloads."
            lines = [f"{i}. {p.name}  ({p})" for i, p in enumerate(files, 1)]
            return "Recent files in Downloads:\n" + "\n".join(lines)
        if not query:
            return "What should I search for?"
        hits = find_files(query, limit=limit, file_type=file_type)
        if not hits:
            return f"No files matched '{query}'."
        # Emphasize full path so the model can open with path= exactly once
        lines = [f"{i}. {p.name}\n   path={p}" for i, p in enumerate(hits, 1)]
        best = hits[0]
        return (
            f"Found {len(hits)} file(s) for '{query}'. "
            f"Best match: {best.name} at path={best}. "
            f"To open it call file_find action=open path=\"{best}\" (do not search again).\n"
            + "\n".join(lines)
        )

    return "Unknown file_find action. Use: find, open, recent."


__all__ = ["file_find", "find_files", "open_path"]
