"""
knowledge/watcher.py — Lightweight folder indexer for knowledge_action
======================================================================
Provides scan_once + schedule_background_scan used by actions/knowledge_action.py.
Uses knowledge.index upsert + documents.extract_excerpt. No long-running FS watcher
required for correctness — explicit scans cover user-driven "index now" requests.
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

log = get_logger(__name__)
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
    "AppData", "Application Data", "$Recycle.Bin", "System Volume Information",
}
_MAX_FILES_PER_SCAN = 2500


def _default_folders() -> list[Path]:
    from utils.windows_paths import get_user_folder
    out: list[Path] = []
    for name in ("Downloads", "Documents", "Desktop"):
        try:
            p = get_user_folder(name)
            if p and Path(p).exists():
                out.append(Path(p))
        except Exception:
            pass
    home = Path.home()
    for name in ("Downloads", "Documents", "Desktop"):
        p = home / name
        if p.exists() and p not in out:
            out.append(p)
    return out


def _resolve_folders(folders: Optional[Iterable[str]]) -> list[Path]:
    if not folders:
        return _default_folders()
    from utils.windows_paths import resolve_user_path
    resolved: list[Path] = []
    for f in folders:
        try:
            p = resolve_user_path(str(f))
            if p.exists() and p.is_dir():
                resolved.append(p)
        except Exception:
            continue
    return resolved or _default_folders()


def scan_once(folders: Optional[Iterable[str]] = None, force: bool = False) -> dict:
    """Synchronously index files under the given folders (or defaults)."""
    from knowledge import index as kindex
    from knowledge.documents import extract_excerpt

    roots = _resolve_folders(folders)
    scanned = 0
    upserted = 0
    for root in roots:
        try:
            for dirpath, dirnames, filenames in _os_walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if scanned >= _MAX_FILES_PER_SCAN:
                        return {"scanned": scanned, "upserted": upserted, "truncated": True}
                    path = Path(dirpath) / fn
                    scanned += 1
                    try:
                        st = path.stat()
                        ext = path.suffix.lstrip(".").lower()
                        excerpt = extract_excerpt(path, ext) or ""
                        kindex.upsert_file(
                            str(path),
                            size_bytes=int(st.st_size),
                            created_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)),
                            modified_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                            text_excerpt=excerpt,
                            embed=True,
                            force=force,
                        )
                        upserted += 1
                    except Exception as exc:
                        log.debug("index skip %s: %s", path, exc)
        except Exception as exc:
            log.debug("scan root failed %s: %s", root, exc)
    return {"scanned": scanned, "upserted": upserted, "truncated": False}


def _os_walk(root: Path):
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        yield Path(dirpath), dirnames, filenames


def schedule_background_scan(folders: Optional[Iterable[str]] = None, force: bool = False) -> str:
    """Fire-and-forget scan on a daemon thread. Returns a task id string."""
    task_id = f"knowledge-scan-{uuid.uuid4().hex[:8]}"
    folder_list = list(folders) if folders else None

    def _run():
        try:
            result = scan_once(folder_list, force=force)
            log.info(
                "[%s] knowledge scan done scanned=%s upserted=%s truncated=%s",
                task_id, result.get("scanned"), result.get("upserted"), result.get("truncated"),
            )
        except Exception as exc:
            log.warning("[%s] knowledge scan failed: %s", task_id, exc)

    threading.Thread(target=_run, name=task_id, daemon=True).start()
    return task_id


__all__ = ["scan_once", "schedule_background_scan"]
