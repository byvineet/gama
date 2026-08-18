"""
automation/providers/file_provider.py — File & Folder Automation.

Dedup note: move / copy / delete / rename / create_folder are NOT
reimplemented here. actions/file_controller.py already owns that logic
(Windows Known Folder resolution, transient-error retry, post-op
verification) and is exposed as its own `file_controller` tool, so this
provider delegates straight into its private helpers instead of forking
a second implementation that could drift out of sync. Only genuinely
new operations live here: archive compress/extract, folder auto-sort,
and bulk image compression.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import List, Tuple

from utils.logger import get_logger
from utils.windows_paths import resolve_user_path
from automation.models import ActionResult, Capability
from automation.registry import registry

log = get_logger(__name__)

# actions/file_controller.py's helpers are already retry+verify wrapped and
# return descriptive strings ("... (verified)." on success). We reuse them
# directly rather than duplicating move/copy/delete/rename/create_folder.
# Import is guarded: file_controller pulls in send2trash, and one missing
# optional dependency there must never take down every other provider
# (window/app/power/clipboard/media) that this package's __init__ imports
# in the same breath.
try:
    from actions.file_controller import (
        _move as _fc_move,
        _copy as _fc_copy,
        _delete as _fc_delete,
        _rename as _fc_rename,
        _create_folder as _fc_create_folder,
    )
    _HAVE_FILE_CONTROLLER = True
except Exception as exc:
    log.warning(f"file_provider: actions.file_controller unavailable ({exc}); "
                f"file.move/copy/delete/rename/create_folder will report unavailable")
    _HAVE_FILE_CONTROLLER = False

    def _unavailable(*_a, **_kw) -> str:
        return "file_controller dependency unavailable (check send2trash is installed)"

    _fc_move = _fc_copy = _fc_delete = _fc_rename = _fc_create_folder = _unavailable

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def _expand(path: str) -> Path:
    """Same Known-Folder-aware resolution file_controller uses, so
    'desktop', 'downloads', etc. work identically across both modules."""
    return resolve_user_path(path)


def _ok_from_fc_result(message: str) -> bool:
    """file_controller's helpers signal success via a '(verified)' suffix
    and failure via a leading '<Verb> failed:' / 'Source not found:' /
    'Not found:' message. Reuse that convention instead of re-deriving it."""
    low = message.lower()
    if "(verified)" in low:
        return True
    if any(low.startswith(p) for p in ("source not found", "not found", "folder not found")):
        return False
    if "failed" in low:
        return False
    # Ran but couldn't verify — treat as a soft failure so the executor's
    # verify/retry path gets a chance instead of silently trusting it.
    return "couldn't verify" not in low


# ── delegated capabilities (thin ActionResult wrappers) ─────────────────────

def _move(src: str = "", dst: str = "", **_) -> ActionResult:
    msg = _fc_move(src, dst)
    return ActionResult(ok=_ok_from_fc_result(msg), message=msg)


def _copy(src: str = "", dst: str = "", **_) -> ActionResult:
    msg = _fc_copy(src, dst)
    return ActionResult(ok=_ok_from_fc_result(msg), message=msg)


def _delete(path: str = "", **_) -> ActionResult:
    # Defaulted to "" (not required) so a plan step built without a
    # resolved path — e.g. the planner's naive keyword fallback — can
    # never raise a bare TypeError here. actions/file_controller.py's
    # _delete() resolves "" / referential text via context_resolver and
    # returns a friendly clarification/not-found message instead.
    msg = _fc_delete(path)
    low = msg.lower()
    if any(p in low for p in ("which one did you mean", "i couldn't find", "i'm not sure which",
                               "i don't have a valid", "refusing to delete")):
        ok = False
    else:
        ok = _ok_from_fc_result(msg)
    # Ambiguous/needs-clarification and not-found responses aren't really
    # "failures" to retry — but they are not successes either, so `ok`
    # staying False (from _ok_from_fc_result) is correct; the message
    # carries the clarification/reason back to the user.
    return ActionResult(ok=ok, message=msg)


def _rename(path: str = "", new_name: str = "", **_) -> ActionResult:
    dest = str(Path(path).parent / new_name) if ("/" in path or "\\" in path) else new_name
    msg = _fc_rename(path, dest)
    return ActionResult(ok=_ok_from_fc_result(msg), message=msg)


def _create_folder(path: str = "", **_) -> ActionResult:
    msg = _fc_create_folder(path)
    return ActionResult(ok=_ok_from_fc_result(msg), message=msg)


def _verify_exists(path: str, **_) -> Tuple[bool, str]:
    p = _expand(path)
    return p.exists(), str(p)


# ── capabilities genuinely new to this package ──────────────────────────────

def _compress(paths: List[str], dst: str, **_) -> ActionResult:
    d = _expand(dst)
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(d, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in paths:
                p = _expand(item)
                if p.is_dir():
                    for f in p.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(p.parent))
                elif p.exists():
                    zf.write(p, p.name)
        return ActionResult(ok=True, message=f"Compressed {len(paths)} item(s) -> {d.name}")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Compress failed: {exc}")


def _extract(src: str, dst: str = "", **_) -> ActionResult:
    s = _expand(src)
    if not s.exists():
        return ActionResult(ok=False, message=f"Archive not found: {s}")
    d = _expand(dst) if dst else s.parent / s.stem
    try:
        d.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(s, "r") as zf:
            zf.extractall(d)
        return ActionResult(ok=True, message=f"Extracted {s.name} -> {d}")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Extract failed: {exc}")


def _organize_folder(path: str, **_) -> ActionResult:
    """Sort loose files in a folder into subfolders by extension category.
    Uses file_controller's own _move under the hood so every individual
    move gets the same retry/verify treatment as a manual move would."""
    p = _expand(path)
    if not p.is_dir():
        return ActionResult(ok=False, message=f"Not a folder: {p}")

    categories = {
        "Images": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".xlsx", ".pptx"},
        "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
        "Videos": {".mp4", ".mkv", ".mov", ".avi"},
        "Audio": {".mp3", ".wav", ".flac", ".m4a"},
        "Installers": {".exe", ".msi"},
    }
    moved, errors = 0, []
    try:
        for item in list(p.iterdir()):
            if item.is_dir():
                continue
            ext = item.suffix.lower()
            category = next((c for c, exts in categories.items() if ext in exts), "Other")
            dest_dir = p / category
            target = dest_dir / item.name
            if target.exists():
                continue
            dest_dir.mkdir(exist_ok=True)
            msg = _fc_move(str(item), str(target))
            if _ok_from_fc_result(msg):
                moved += 1
            else:
                errors.append(msg)
        summary = f"Organized {moved} file(s) in {p.name}"
        if errors:
            summary += f" ({len(errors)} failed)"
        return ActionResult(ok=(not errors or moved > 0), message=summary,
                             data={"moved": moved, "errors": errors})
    except Exception as exc:
        return ActionResult(ok=False, message=f"Organize failed: {exc}")


def _compress_all_images(path: str, **_) -> ActionResult:
    """Re-encode every image in a folder to reduce size (Pillow, quality 80)."""
    p = _expand(path)
    if not p.is_dir():
        return ActionResult(ok=False, message=f"Not a folder: {p}")
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return ActionResult(ok=False, message="Pillow not installed")

    count, saved_bytes = 0, 0
    for item in p.rglob("*"):
        if item.suffix.lower() in _IMAGE_EXT and item.is_file():
            try:
                before = item.stat().st_size
                img = Image.open(item)
                img.save(item, optimize=True, quality=80)
                after = item.stat().st_size
                saved_bytes += max(0, before - after)
                count += 1
            except Exception:
                continue
    return ActionResult(ok=True, message=f"Compressed {count} image(s), saved {saved_bytes // 1024} KB",
                         data={"count": count, "saved_bytes": saved_bytes})


def register() -> None:
    registry.register_many([
        # Delegated to actions/file_controller.py (see module docstring).
        Capability("file.move", _move, cost=1, speed_ms=30,
                   description="Move a file or folder", keywords=("move",)),
        Capability("file.copy", _copy, cost=2, speed_ms=50,
                   description="Copy a file or folder", keywords=("copy", "duplicate")),
        Capability("file.delete", _delete, cost=1, speed_ms=30,
                   description="Delete a file or folder (recycle bin)", keywords=("delete", "remove", "trash")),
        Capability("file.rename", _rename, cost=1, speed_ms=20,
                   description="Rename a file or folder", keywords=("rename",)),
        Capability("file.create_folder", _create_folder, verify=_verify_exists, cost=1, speed_ms=15,
                   description="Create a folder", keywords=("create folder", "new folder")),
        # New to this package.
        Capability("file.compress", _compress, cost=3, speed_ms=200,
                   description="Compress files into a zip archive",
                   keywords=("compress", "zip")),
        Capability("file.extract", _extract, cost=3, speed_ms=200,
                   description="Extract a zip archive", keywords=("extract", "unzip")),
        Capability("file.organize_folder", _organize_folder, cost=4, speed_ms=300,
                   description="Sort loose files into category subfolders",
                   keywords=("organize", "clean up", "tidy")),
        Capability("file.compress_images", _compress_all_images, cost=5, speed_ms=500,
                   description="Compress every image in a folder",
                   keywords=("compress every image", "compress images")),
    ])


register()
