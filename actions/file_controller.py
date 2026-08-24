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
_PROTECTED_ROOTS = {
    "c:\\", "c:/", "/", "d:\\", "d:/", "e:\\", "e:/",
    "c:\\windows", "c:/windows", "c:\\windows\\system32", "c:/windows/system32",
    "c:\\program files", "c:/program files", "c:\\program files (x86)", "c:/program files (x86)",
    str(Path.home()).lower()
}


def _is_protected_root(p: Path) -> bool:
    try:
        norm = str(p).rstrip("\\/").lower()
        if norm in _PROTECTED_ROOTS or (norm + "\\") in _PROTECTED_ROOTS or (norm + "/") in _PROTECTED_ROOTS:
            return True
        # Check if parent is Windows / System32
        win_dir = os.environ.get("SystemRoot", "C:\\Windows").lower()
        if str(p).lower().startswith(win_dir):
            return True
        return False
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
    if action in ("create_folder", "create_dir", "mkdir"):
        return _create_folder(kwargs.get("path", "") or kwargs.get("folder", ""))
    if action in ("delete", "remove", "trash", "recycle"):
        if not kwargs.get("verbal_confirmed") and not kwargs.get("confirmed"):
            resolved = resolve_file_reference(kwargs.get("path", ""))
            target = resolved.path.name if (resolved and resolved.path) else (kwargs.get("path") or "this file")
            return f"Confirmation required: Are you sure you want to delete '{target}'? Please say yes to confirm."
        return _delete(kwargs.get("path", ""))
    if action in ("move", "relocate"):
        return _move(kwargs.get("src", "") or kwargs.get("source", ""), kwargs.get("dest", "") or kwargs.get("destination", "") or kwargs.get("target", ""))
    if action == "copy":
        return _copy(kwargs.get("src", "") or kwargs.get("source", ""), kwargs.get("dest", "") or kwargs.get("destination", "") or kwargs.get("target", ""))
    if action == "rename":
        return _rename(kwargs.get("src", "") or kwargs.get("source", ""), kwargs.get("dest", "") or kwargs.get("destination", "") or kwargs.get("new_name", ""))
    if action in ("batch_rename", "rename_batch", "rename_files"):
        return _batch_rename(
            kwargs.get("path", "") or kwargs.get("folder", ""),
            pattern=kwargs.get("pattern", "*"),
            prefix=kwargs.get("prefix", ""),
            find=kwargs.get("find", ""),
            replace=kwargs.get("replace", ""),
        )
    if action in ("organize", "organize_folder", "auto_organize", "sort_folder"):
        return _organize_folder(
            kwargs.get("path", "") or kwargs.get("folder", "") or kwargs.get("target", "downloads"),
            by=kwargs.get("by", "category"),
        )
    if action in ("compress", "zip", "archive"):
        return _compress(
            kwargs.get("src", "") or kwargs.get("source", "") or kwargs.get("path", ""),
            dest=kwargs.get("dest", "") or kwargs.get("destination", "") or kwargs.get("output", ""),
        )
    if action in ("extract", "unzip", "decompress"):
        return _extract(
            kwargs.get("src", "") or kwargs.get("source", "") or kwargs.get("path", ""),
            dest=kwargs.get("dest", "") or kwargs.get("destination", "") or kwargs.get("output", ""),
        )
    if action in ("clean_temp", "clear_temp", "cleanup_temp"):
        return _clean_temp()
    if action in ("clean_empty", "clean_empty_folders"):
        return _clean_empty(kwargs.get("path", "") or kwargs.get("folder", ""))
    if action in ("list", "ls", "dir"):
        return _list(kwargs.get("path", "") or kwargs.get("folder", ""))
    if action in ("find", "search"):
        return _find(kwargs.get("root", "") or kwargs.get("path", "") or kwargs.get("folder", ""), kwargs.get("pattern", "*"))
    if action in ("open_folder", "open_dir", "open"):
        return _open_folder(kwargs.get("path", "") or kwargs.get("folder", "") or "C:/Users/notde")
    return (
        f"Unknown file action: '{action}'. Available actions: organize, create_folder, open_folder, "
        "delete, move, copy, rename, batch_rename, compress, extract, clean_temp, clean_empty, list, find."
    )


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


# ============================================================
# Autonomous File Organization & Workflows
# ============================================================

_CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".epub", ".md", ".tex"},
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff", ".raw", ".heic"},
    "Data_and_Sheets": {".csv", ".xlsx", ".xls", ".json", ".tsv", ".parquet", ".xml", ".yaml", ".yml", ".sql", ".db", ".sqlite"},
    "Code_and_Scripts": {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".cpp", ".c", ".h", ".java", ".rs", ".go", ".sh", ".bat", ".ps1", ".ipynb"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v"},
    "Audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk"},
}


def _get_unique_dest_path(dest_folder: Path, filename: str) -> Path:
    """Generate a collision-free path in dest_folder by adding (1), (2), etc."""
    target = dest_folder / filename
    if not target.exists():
        return target
    stem = target.stem
    ext = target.suffix
    counter = 1
    while True:
        candidate = dest_folder / f"{stem} ({counter}){ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def _organize_folder(path: str = "downloads", by: str = "category") -> str:
    """Organize files in a folder into subdirectories by category or file type.
    Skips subfolders, hidden files, and protected files.
    """
    import shutil
    try:
        p = _resolve_path(path or "downloads")
        if not p.exists() or not p.is_dir():
            return f"Folder not found: {p}"
        if _is_protected_root(p):
            return f"Refusing to auto-organize protected top-level system path: {p}"

        # Collect top-level files only (do not recurse into already organized subfolders)
        files = [f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")]
        if not files:
            return f"No loose files to organize in {p}."

        categorized_counts: dict[str, int] = {}
        moved_details: list[dict] = []
        errors = 0

        # Build reverse map for fast lookup
        ext_to_category: dict[str, str] = {}
        for cat, exts in _CATEGORY_EXTENSIONS.items():
            for ext in exts:
                ext_to_category[ext] = cat

        for f in files:
            ext = f.suffix.lower()
            category = ext_to_category.get(ext, "Other")
            dest_dir = p / category
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_file = _get_unique_dest_path(dest_dir, f.name)
                shutil.move(str(f), str(dest_file))
                if dest_file.exists():
                    recent_files.record(str(dest_file), "organized")
                    categorized_counts[category] = categorized_counts.get(category, 0) + 1
                    moved_details.append({
                        "file": f.name,
                        "category": category,
                        "destination": str(dest_file),
                    })
            except Exception as exc:
                log.warning(f"Failed moving {f.name}: {exc}")
                errors += 1

        total_moved = sum(categorized_counts.values())
        summary_lines = [f"Organized {total_moved} files in {p}:"]
        for cat, cnt in sorted(categorized_counts.items(), key=lambda x: -x[1]):
            summary_lines.append(f"  • {cat}: {cnt} file{'s' if cnt != 1 else ''}")
        if errors > 0:
            summary_lines.append(f"  (Skipped/failed: {errors})")

        result_msg = "\n".join(summary_lines)

        # Push visual summary card to Canvas HUD if available
        try:
            from actions.display_stage import show_workflow_on_display
            show_workflow_on_display(
                title=f"Organized {p.name or str(p)}",
                steps=[f"{cat}: {cnt} files" for cat, cnt in categorized_counts.items()],
                summary=f"Relocated {total_moved} files safely into categorized subfolders.",
                stats={"total_files": total_moved, "categories": len(categorized_counts), "errors": errors},
            )
        except Exception:
            pass

        return result_msg
    except Exception as exc:
        return f"Organize folder failed: {exc}"


def _batch_rename(path: str, pattern: str = "*", prefix: str = "",
                  find: str = "", replace: str = "") -> str:
    """Rename multiple files in a folder with sequential numbering or text replacement."""
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_dir():
            return f"Folder not found: {p}"

        files = sorted([f for f in p.glob(pattern or "*") if f.is_file() and not f.name.startswith(".")])
        if not files:
            return f"No files matching '{pattern}' in {p}."

        renamed = 0
        for idx, f in enumerate(files, 1):
            try:
                ext = f.suffix
                if find:
                    # Find-and-replace mode
                    new_stem = f.stem.replace(find, replace)
                    new_name = f"{new_stem}{ext}"
                elif prefix:
                    # Prefix + sequence mode
                    new_name = f"{prefix}_{idx:03d}{ext}" if not prefix.endswith(("_", "-")) else f"{prefix}{idx:03d}{ext}"
                else:
                    new_name = f"file_{idx:03d}{ext}"

                dest = f.parent / new_name
                if dest != f:
                    dest = _get_unique_dest_path(f.parent, new_name)
                    f.rename(dest)
                    recent_files.record(str(dest), "renamed")
                    renamed += 1
            except Exception as exc:
                log.debug(f"Batch rename error on {f}: {exc}")
                continue

        return f"Renamed {renamed} file{'s' if renamed != 1 else ''} in {p} (verified)."
    except Exception as exc:
        return f"Batch rename failed: {exc}"


def _compress(src: str, dest: str = "") -> str:
    """Compress a file or directory into a ZIP archive."""
    import zipfile
    try:
        s = _resolve_path(src)
        if not s.exists():
            return f"Source not found: {s}"

        if not dest:
            d = s.parent / f"{s.stem}.zip" if s.is_file() else s.parent / f"{s.name}.zip"
        else:
            d = _resolve_path(dest)
            if not str(d).lower().endswith(".zip"):
                d = d.parent / f"{d.name}.zip" if not d.is_dir() else d / f"{s.name}.zip"

        d.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(d, "w", zipfile.ZIP_DEFLATED) as zf:
            if s.is_file():
                zf.write(s, arcname=s.name)
                count = 1
            else:
                for root_dir, _, files in os.walk(s):
                    for file in files:
                        full_p = Path(root_dir) / file
                        arc_name = full_p.relative_to(s)
                        zf.write(full_p, arcname=str(arc_name))
                        count += 1

        if d.exists() and d.stat().st_size > 0:
            recent_files.record(str(d), "compressed")
            size_mb = d.stat().st_size / (1024 * 1024)
            return f"Compressed {count} item{'s' if count != 1 else ''} into '{d.name}' ({size_mb:.2f} MB, verified)."
        return f"Compression completed but archive '{d}' is missing or empty."
    except Exception as exc:
        return f"Compress failed: {exc}"


def _extract(src: str, dest: str = "") -> str:
    """Extract a ZIP archive into a destination folder."""
    import zipfile
    try:
        s = _resolve_path(src)
        if not s.exists() or not s.is_file():
            return f"Archive not found: {s}"

        d = _resolve_path(dest) if dest else s.parent / s.stem
        d.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(s, "r") as zf:
            zf.extractall(d)
            count = len(zf.namelist())

        recent_files.record(str(d), "extracted")
        return f"Extracted {count} item{'s' if count != 1 else ''} to '{d}' (verified)."
    except Exception as exc:
        return f"Extract failed: {exc}"


def _clean_temp() -> str:
    """Safely clear user temporary files to free disk space."""
    import shutil
    cleared = 0
    errors = 0
    temp_dirs = [
        Path(os.environ.get("TEMP", "")),
        Path(os.environ.get("TMP", "")),
        Path.home() / "AppData" / "Local" / "Temp",
    ]
    for temp_dir in temp_dirs:
        if not temp_dir.exists() or not temp_dir.is_dir():
            continue
        try:
            for item in temp_dir.iterdir():
                try:
                    if item.is_file():
                        item.unlink()
                        cleared += 1
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                        cleared += 1
                except Exception:
                    errors += 1
        except Exception:
            pass
    return f"Cleaned {cleared} temporary items from system temp directories."


def _clean_empty(path: str) -> str:
    """Clean empty subdirectories in a folder."""
    try:
        p = _resolve_path(path)
        if not p.exists() or not p.is_dir():
            return f"Folder not found: {p}"
        if _is_protected_root(p):
            return f"Refusing to clean protected root: {p}"

        removed = 0
        for root_dir, dirs, _ in os.walk(p, topdown=False):
            for d in dirs:
                full_d = Path(root_dir) / d
                try:
                    if not any(full_d.iterdir()):
                        full_d.rmdir()
                        removed += 1
                except Exception:
                    pass
        return f"Removed {removed} empty subfolder{'s' if removed != 1 else ''} in {p}."
    except Exception as exc:
        return f"Clean empty folders failed: {exc}"


__all__ = [
    "file_controller",
    "_organize_folder",
    "_batch_rename",
    "_compress",
    "_extract",
    "_clean_temp",
    "_clean_empty",
]

