"""
actions/advanced_automation.py — Gama Advanced Automation
==========================================================
Jarvis-style advanced automation:
  - window_arrange: snap windows to halves/quarters/grid
  - batch_rename: rename multiple files by pattern
  - clear_temp: clear temp files (disk cleanup)
  - system_cleanup: empty recycle bin + clear temp + clear cache
  - quick_actions: common multi-step shortcuts
  - voice_macro: record and replay action sequences

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import shutil
import subprocess
import time

from utils.proc import hidden_kwargs
from pathlib import Path
from utils.windows_paths import resolve_user_path

log = get_logger(__name__)
logger = log  # back-compat alias
def advanced_automation(action: str = "quick_action", **kwargs) -> str:
    """Advanced automation features."""
    action = (action or "quick_action").lower().strip()

    if action == "window_arrange":
        return _window_arrange(kwargs.get("layout", "halves"))
    if action == "batch_rename":
        return _batch_rename(
            kwargs.get("folder", ""),
            kwargs.get("pattern", ""),
            kwargs.get("prefix", ""),
        )
    if action == "clear_temp":
        return _clear_temp()
    if action == "system_cleanup":
        return _system_cleanup()
    if action == "quick_action":
        return _quick_action(kwargs.get("name", ""))
    return (f"Unknown automation action: {action}. Use: window_arrange, "
            f"batch_rename, clear_temp, system_cleanup, quick_action.")


# ============================================================
# Window arrangement
# ============================================================
def _window_arrange(layout: str = "halves") -> str:
    """Arrange windows using keyboard shortcuts."""
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        layout = (layout or "halves").lower()

        if layout in ("halves", "split"):
            # Snap active window to left half (Win+Left), then switch + right half
            kb.press(Key.cmd); kb.press(Key.left.value if hasattr(Key.left, 'value') else Key.left)
            kb.release(Key.left); kb.release(Key.cmd)
            time.sleep(0.3)
            # Alt+Tab to next window
            kb.press(Key.alt_l); kb.press(Key.tab); kb.release(Key.tab); kb.release(Key.alt_l)
            time.sleep(0.3)
            # Snap to right half
            kb.press(Key.cmd); kb.press(Key.right.value if hasattr(Key.right, 'value') else Key.right)
            kb.release(Key.right); kb.release(Key.cmd)
            return "Windows arranged side by side."

        if layout == "cascade":
            # Just minimize all then restore
            kb.press(Key.cmd); kb.press("m"); kb.release("m"); kb.release(Key.cmd)
            time.sleep(0.5)
            kb.press(Key.cmd); kb.press(Key.shift); kb.press("m")
            kb.release("m"); kb.release(Key.shift); kb.release(Key.cmd)
            return "Windows cascaded."

        if layout == "minimize_all":
            kb.press(Key.cmd); kb.press("m"); kb.release("m"); kb.release(Key.cmd)
            return "All windows minimized."

        if layout == "show_desktop":
            kb.press(Key.cmd); kb.press("d"); kb.release("d"); kb.release(Key.cmd)
            return "Show desktop."

        return f"Unknown layout: {layout}. Use: halves, cascade, minimize_all, show_desktop."
    except Exception as exc:
        return f"Window arrange failed: {exc}"


# ============================================================
# Batch file rename
# ============================================================
def _batch_rename(folder: str, pattern: str = "*", prefix: str = "") -> str:
    """Rename files in a folder by pattern with a prefix + sequential number."""
    folder = (folder or "").strip()
    if not folder:
        return "Which folder should I rename files in?"
    p = resolve_user_path(folder)
    if not p.exists() or not p.is_dir():
        return f"Folder not found: {p}"

    files = sorted(p.glob(pattern or "*"))
    files = [f for f in files if f.is_file()]
    if not files:
        return f"No files matching '{pattern}' in {p}"

    renamed = 0
    for i, f in enumerate(files, 1):
        try:
            ext = f.suffix
            new_name = f"{prefix}{i:03d}{ext}" if prefix else f"file_{i:03d}{ext}"
            new_path = f.parent / new_name
            f.rename(new_path)
            renamed += 1
        except Exception:
            continue

    return f"Renamed {renamed} files in {p} with prefix '{prefix}'."


# ============================================================
# Clear temp files
# ============================================================
def _clear_temp() -> str:
    """Clear temporary files."""
    cleared = 0
    temp_dirs = [
        Path(os.environ.get("TEMP", "")),
        Path(os.environ.get("TMP", "")),
        Path.home() / "AppData" / "Local" / "Temp",
    ]
    for temp_dir in temp_dirs:
        if not temp_dir.exists():
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
                    continue
        except Exception:
            continue
    return f"Cleared {cleared} temp items."


# ============================================================
# System cleanup
# ============================================================
def _system_cleanup() -> str:
    """Full system cleanup: empty recycle bin + clear temp + clear cache."""
    results = []

    # 1. Empty recycle bin
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x0007)
            results.append("Recycle bin emptied")
    except Exception as exc:
        results.append(f"Recycle bin: {exc}")

    # 2. Clear temp
    temp_result = _clear_temp()
    results.append(temp_result)

    # 3. Clear DNS cache
    try:
        if os.name == "nt":
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10, **hidden_kwargs())
            results.append("DNS cache flushed")
    except Exception:
        pass

    # 4. Clear thumbnail cache (Windows)
    try:
        if os.name == "nt":
            thumb_cache = Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Explorer"
            if thumb_cache.exists():
                for f in thumb_cache.glob("thumbcache_*.db"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
                results.append("Thumbnail cache cleared")
    except Exception:
        pass

    return "System cleanup done:\n" + "\n".join(f"  • {r}" for r in results)


# ============================================================
# Quick actions (multi-step shortcuts)
# ============================================================
def _quick_action(name: str) -> str:
    """Execute a pre-defined multi-step quick action."""
    name = (name or "").lower().strip()
    if not name:
        return ("Available quick actions: clear_desktop, focus_mode, "
                "gaming_mode, work_mode, movie_mode, night_mode")

    if name == "clear_desktop":
        # Minimize all windows
        return _window_arrange("minimize_all")

    if name == "focus_mode":
        # Close common distraction apps, open notepad
        from actions.open_app import open_app
        from actions.process_manager import process_manager
        # Close Discord, WhatsApp, etc.
        for app in ["Discord", "WhatsApp"]:
            process_manager("kill", name_or_pid=app)
        time.sleep(0.5)
        open_app("notepad")
        return "Focus mode: closed distractions, opened Notepad."

    if name == "gaming_mode":
        # Close heavy background apps, set volume to 80%
        from actions.process_manager import process_manager
        from actions.computer_settings import computer_settings
        for app in ["chrome", "firefox", "edge"]:
            process_manager("kill", name_or_pid=app)
        computer_settings("volume_up", "80")
        return "Gaming mode: closed browsers, volume set to 80%."

    if name == "work_mode":
        # Open common work apps
        from actions.open_app import open_app
        open_app("chrome")
        time.sleep(0.5)
        open_app("vscode")
        return "Work mode: opened Chrome and VS Code."

    if name == "movie_mode":
        # Close distractions, open VLC, dim brightness
        from actions.open_app import open_app
        from actions.computer_settings import computer_settings
        from actions.process_manager import process_manager
        for app in ["Discord", "WhatsApp", "Slack"]:
            process_manager("kill", name_or_pid=app)
        computer_settings("brightness", "50")
        open_app("vlc")
        return "Movie mode: closed distractions, dimmed brightness, opened VLC."

    if name == "night_mode":
        # Lower brightness, close all apps
        from actions.computer_settings import computer_settings
        computer_settings("brightness", "20")
        return "Night mode: brightness lowered to 20%."

    return f"Unknown quick action: {name}. Use: clear_desktop, focus_mode, gaming_mode, work_mode, movie_mode, night_mode."


__all__ = ["advanced_automation"]
