"""
actions/open_app.py — Gama App Launcher (2.0)
==============================================
Context-aware application launching:
  - Already running?  → Bring to foreground (focus/restore), not a duplicate.
  - Minimized?        → Restore + focus.
  - Not running?      → Launch, then verify via psutil.
  - Unknown name?     → Fuzzy-match against a pre-built Start Menu index.
  - URL / website?    → Open in default browser.

Fuzzy matching covers partial names ("VS" → "VS Code", "Explorer" → File
Explorer) so natural speech works without needing an exact app name.

The app index is built once in the background at startup (via
core/startup_preloader) and cached; Start Menu is never rescanned during
a command unless explicitly requested.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from difflib import get_close_matches
from pathlib import Path
from typing import Dict, List, Optional

from actions.reliability import (
    expected_process_name,
    is_process_running,
    wait_for_process,
)

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()

# ---------------------------------------------------------------------------
# Known app alias table  (display-name key → OS-specific launch target)
# ---------------------------------------------------------------------------
_APP_ALIASES: Dict[str, Dict[str, str]] = {
    "whatsapp":           {"Windows": "WhatsApp",           "Darwin": "WhatsApp",            "Linux": "whatsapp"},
    "chrome":             {"Windows": "chrome",             "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",             "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",            "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",             "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "microsoft edge":     {"Windows": "msedge",             "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "spotify":            {"Windows": "Spotify",            "Darwin": "Spotify",              "Linux": "spotify"},
    "vscode":             {"Windows": "code",               "Darwin": "Visual Studio Code",   "Linux": "code"},
    "vs code":            {"Windows": "code",               "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",               "Darwin": "Visual Studio Code",   "Linux": "code"},
    "vs":                 {"Windows": "code",               "Darwin": "Visual Studio Code",   "Linux": "code"},
    "discord":            {"Windows": "Discord",            "Darwin": "Discord",              "Linux": "discord"},
    "telegram":           {"Windows": "Telegram",           "Darwin": "Telegram",             "Linux": "telegram"},
    "notepad":            {"Windows": "notepad.exe",        "Darwin": "TextEdit",             "Linux": "gedit"},
    "notepad++":          {"Windows": "notepad++.exe",      "Darwin": "",                     "Linux": ""},
    "calculator":         {"Windows": "calc.exe",           "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "terminal":           {"Windows": "wt.exe",             "Darwin": "Terminal",             "Linux": "gnome-terminal"},
    "windows terminal":   {"Windows": "wt.exe",             "Darwin": "Terminal",             "Linux": "gnome-terminal"},
    "cmd":                {"Windows": "cmd.exe",            "Darwin": "Terminal",             "Linux": "bash"},
    "command prompt":     {"Windows": "cmd.exe",            "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",     "Darwin": "Terminal",             "Linux": "bash"},
    "explorer":           {"Windows": "explorer.exe",       "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",       "Darwin": "Finder",               "Linux": "nautilus"},
    "paint":              {"Windows": "mspaint.exe",        "Darwin": "Preview",              "Linux": "gimp"},
    "word":               {"Windows": "winword",            "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",              "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",           "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "vlc":                {"Windows": "vlc",                "Darwin": "VLC",                  "Linux": "vlc"},
    "zoom":               {"Windows": "Zoom",               "Darwin": "zoom.us",              "Linux": "zoom"},
    "slack":              {"Windows": "Slack",              "Darwin": "Slack",                "Linux": "slack"},
    "steam":              {"Windows": "steam",              "Darwin": "Steam",                "Linux": "steam"},
    "task manager":       {"Windows": "taskmgr.exe",        "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",       "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "control panel":      {"Windows": "control.exe",        "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "snipping tool":      {"Windows": "snippingtool.exe",   "Darwin": "",                     "Linux": "gnome-screenshot"},
    "snip":               {"Windows": "snippingtool.exe",   "Darwin": "",                     "Linux": "gnome-screenshot"},
    "registry editor":    {"Windows": "regedit.exe",        "Darwin": "",                     "Linux": ""},
    "regedit":            {"Windows": "regedit.exe",        "Darwin": "",                     "Linux": ""},
    "camera":             {"Windows": "microsoft.windows.camera:", "Darwin": "",                "Linux": "cheese"},
    "windows camera":     {"Windows": "microsoft.windows.camera:", "Darwin": "",                "Linux": "cheese"},
    "webcam":             {"Windows": "microsoft.windows.camera:", "Darwin": "",                "Linux": "cheese"},
    "obs":                {"Windows": "obs64.exe",          "Darwin": "OBS",                  "Linux": "obs"},
    "obs studio":         {"Windows": "obs64.exe",          "Darwin": "OBS",                  "Linux": "obs"},
    "notion":             {"Windows": "Notion",             "Darwin": "Notion",               "Linux": "notion"},
    "figma":              {"Windows": "Figma",              "Darwin": "Figma",                "Linux": ""},
    "postman":            {"Windows": "Postman",            "Darwin": "Postman",              "Linux": "postman"},
    "cursor":             {"Windows": "cursor",             "Darwin": "Cursor",               "Linux": "cursor"},
    "github desktop":     {"Windows": "GitHubDesktop",      "Darwin": "GitHub Desktop",       "Linux": ""},
    "gitkraken":          {"Windows": "gitkraken",          "Darwin": "GitKraken",            "Linux": "gitkraken"},
    "7zip":               {"Windows": "7zFM.exe",           "Darwin": "",                     "Linux": ""},
    "winrar":             {"Windows": "WinRAR.exe",         "Darwin": "",                     "Linux": ""},
}

# Known websites — open in browser instead of as an app.
_WEBSITES: Dict[str, str] = {
    "youtube":      "https://www.youtube.com",
    "google":       "https://www.google.com",
    "gmail":        "https://mail.google.com",
    "github":       "https://github.com",
    "chatgpt":      "https://chat.openai.com",
    "netflix":      "https://www.netflix.com",
    "spotify web":  "https://open.spotify.com",
    "whatsapp web": "https://web.whatsapp.com",
    # "instagram" removed — handled by the instagram tool (instagrapi, not browser)
    "facebook":     "https://www.facebook.com",
    "twitter":      "https://twitter.com",
    "reddit":       "https://www.reddit.com",
    "linkedin":     "https://www.linkedin.com",
    "wikipedia":    "https://www.wikipedia.org",
    "amazon":       "https://www.amazon.com",
    "stackoverflow": "https://stackoverflow.com",
}

_VERIFY_TIMEOUT = float(os.environ.get("GAMA_APP_VERIFY_TIMEOUT", "7.0"))

# ---------------------------------------------------------------------------
# App index — Start Menu scanner with an in-memory cache
# ---------------------------------------------------------------------------
_app_index_lock = threading.Lock()
_app_index: Optional[Dict[str, str]] = None   # name_lower -> .lnk path
_app_index_building = False
# Signaled when a build finishes, so waiting threads wake up instead of
# each kicking off their own redundant scan.
_app_index_built_event = threading.Event()


def get_app_index(force_rebuild: bool = False) -> Dict[str, str]:
    """Return the cached app index, building it if needed.

    The index maps lower-case shortcut stem names to their .lnk paths so
    fuzzy matching can find apps by partial name without scanning disk on
    every request. Building is done once per process; the result is cached
    indefinitely (apps don't change mid-session).

    Concurrency note: open_app() calls for multiple apps are frequently
    dispatched together (e.g. a routine step or a Gemini multi-tool batch
    opening Notepad + Spotify at once). Without coordination, every one of
    those threads would see an empty cache at the same moment and each
    launch its own full recursive Start Menu scan simultaneously — a
    thundering herd that spikes CPU/disk I/O and can stall unrelated tool
    calls elsewhere in the process for many seconds. `_app_index_building`
    plus `_app_index_built_event` make only the first caller do the scan;
    everyone else just waits for it to finish and reuses the result.
    """
    global _app_index, _app_index_building
    with _app_index_lock:
        if _app_index is not None and not force_rebuild:
            return _app_index
        if _app_index_building:
            # Someone else is already scanning — wait for them instead of
            # starting a second concurrent scan.
            is_builder = False
        else:
            _app_index_building = True
            _app_index_built_event.clear()
            is_builder = True

    if not is_builder:
        _app_index_built_event.wait(timeout=15.0)
        with _app_index_lock:
            if _app_index is not None:
                return _app_index
        # Builder timed out/failed — fall through and build it ourselves
        # rather than returning an empty index forever.

    index: Dict[str, str] = {}
    search_dirs = [
        Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]

    t0 = time.monotonic()
    try:
        for d in search_dirs:
            if not d.exists():
                continue
            try:
                for lnk in d.rglob("*.lnk"):
                    index[lnk.stem.lower()] = str(lnk)
            except Exception:
                pass

        elapsed = (time.monotonic() - t0) * 1000
        logger.debug(f"App index built: {len(index)} shortcuts in {elapsed:.0f}ms.")

        with _app_index_lock:
            _app_index = index
        return index
    finally:
        with _app_index_lock:
            _app_index_building = False
        _app_index_built_event.set()


def _fuzzy_lookup_index(query: str) -> Optional[str]:
    """Fuzzy-match a query against the Start Menu index.

    Returns the .lnk path of the best match, or None.
    Uses difflib.get_close_matches for fast in-memory fuzzy search.
    """
    index = get_app_index()
    if not index:
        return None

    candidates = list(index.keys())

    # 1. Exact prefix match first (faster + more precise).
    for name in candidates:
        if name.startswith(query):
            return index[name]

    # 2. difflib fuzzy match — high cutoff prevents weak matches (e.g.
    #    "replit" → "regedit").  Only very similar names pass through.
    matches = get_close_matches(query, candidates, n=1, cutoff=0.82)
    if matches:
        return index[matches[0]]

    # 3. Substring match as last resort.
    for name in candidates:
        if query in name:
            return index[name]

    return None


# ---------------------------------------------------------------------------
# Foreground / restore helpers (Windows-native)
# ---------------------------------------------------------------------------

def _get_app_pids(app_name: str, expected_proc: Optional[str] = None) -> set[int]:
    """Find process PIDs matching `app_name` or `expected_proc` using psutil."""
    target_pids: set[int] = set()
    proc_needle = (expected_proc or app_name).lower().replace(".exe", "")
    app_needle = app_name.lower().replace(".exe", "")
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pname = (proc.info['name'] or '').lower()
                if proc_needle in pname or pname in proc_needle or app_needle in pname:
                    target_pids.add(proc.info['pid'])
            except Exception:
                continue
    except Exception:
        pass
    return target_pids


def _bring_to_foreground(app_name: str, expected_proc: Optional[str] = None) -> bool:
    """Try to focus + restore the running window for `app_name`.

    Returns True if a window was found and focused.
    Uses PID matching + Win32 ALT-key focus stealing bypass.
    """
    needle = app_name.lower().replace(".exe", "")

    if _OS == "Windows":
        try:
            import win32gui
            import win32con
            import win32api
            import win32process

            target_pids = _get_app_pids(app_name, expected_proc)
            found: list = []

            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if target_pids and pid in target_pids:
                        found.append(hwnd)
                    else:
                        title = (win32gui.GetWindowText(hwnd) or "").lower()
                        if title and (needle in title or title in needle):
                            found.append(hwnd)
                return True

            win32gui.EnumWindows(_cb, None)

            if found:
                hwnd = found[0]
                # Send ALT key pulse to allow SetForegroundWindow from background process
                try:
                    win32api.keybd_event(0x12, 0, 0, 0)  # ALT down
                except Exception:
                    pass

                placement = win32gui.GetWindowPlacement(hwnd)
                if placement[1] in (win32con.SW_SHOWMINIMIZED, win32con.SW_MINIMIZE):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.1)
                else:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)

                try:
                    win32api.keybd_event(0x12, 0, 0x0002, 0)  # ALT up
                except Exception:
                    pass

                return True
        except Exception as exc:
            logger.debug(f"win32gui foreground failed for '{app_name}': {exc}")

    # pygetwindow fallback.
    try:
        import pygetwindow as gw
        windows = [w for w in gw.getAllWindows()
                   if needle in (w.title or "").lower()]
        if windows:
            w = windows[0]
            if w.isMinimized:
                w.restore()
                time.sleep(0.1)
            w.activate()
            return True
    except Exception as exc:
        logger.debug(f"pygetwindow foreground failed for '{app_name}': {exc}")

    return False


def _is_app_window_visible(app_name: str) -> bool:
    """True if there's a visible window whose title or PID matches app_name."""
    needle = app_name.lower().replace(".exe", "")
    if _OS == "Windows":
        try:
            import win32gui
            import win32process
            target_pids = _get_app_pids(app_name)
            found = []

            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if target_pids and pid in target_pids:
                        found.append(hwnd)
                    else:
                        t = (win32gui.GetWindowText(hwnd) or "").lower()
                        if needle in t:
                            found.append(hwnd)
                return True

            win32gui.EnumWindows(_cb, None)
            return bool(found)
        except Exception:
            pass
    try:
        import pygetwindow as gw
        return any(needle in (w.title or "").lower() for w in gw.getAllWindows())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def open_app(app_name: str, new_window: bool = False) -> str:
    """Open an application by name, or a website if the name looks like a URL.

    If new_window is False and the app is already running:
      - brings its window to foreground (focus/restore)
      - does NOT spawn a duplicate process

    If new_window is True:
      - opens a new instance / new window of the application
    """
    name = (app_name or "").strip()
    if not name:
        return "Which app should I open, Sir?"

    lower = name.lower()

    # ── Claude / Claude Code special-case ──────────────────────────────
    # "claude" is NOT in _APP_ALIASES and has no reliable Start-Menu
    # shortcut naming convention, which used to let it fall all the way
    # through to autonomous_recover_app()'s Tier 11/12 fuzzy matching,
    # where difflib would occasionally match it to Gama's own shortcut
    # ("gama.exe.lnk") since both are short, similarly-shaped names.
    # Handle it explicitly, before any fuzzy matching ever runs, so that
    # can't happen again.
    if lower in ("claude", "claude.exe", "claude desktop", "anthropic claude"):
        return _open_claude(new_window)

    if lower in ("claude code", "claude-code", "claudecode"):
        return _open_claude_code()

    # ── Instagram — always handled by the instagram tool, never here ───
    # Guard against Gemini or fast-intent accidentally routing instagram
    # commands to open_app (which would open the website in a browser).
    # The instagram tool uses instagrapi (API-based, no browser) and is the
    # canonical handler for all Instagram commands.
    # if "instagram" in lower or lower in ("insta",):
    #     return (
    #         "Instagram is handled by the instagram tool, not the browser. "
    #         "Say 'connect Instagram' or 'check Instagram notifications' to use it."
    #     )

    # ── URL / website ──────────────────────────────────────────────────
    if (lower.startswith(("http://", "https://", "www.")) or
            (any(tld in lower for tld in (".com", ".org", ".net", ".io"))
             and " " not in lower)):
        return _open_url(name)

    if lower in _WEBSITES:
        return _open_url(_WEBSITES[lower])

    for site_key, site_url in _WEBSITES.items():
        if lower == site_key:
            return _open_url(site_url)

    # ── Fuzzy alias resolution ─────────────────────────────────────────
    alias_keys = list(_APP_ALIASES.keys())
    resolved_alias = _APP_ALIASES.get(lower)
    if resolved_alias is None:
        # High cutoff — only accept very close alias matches.
        # Prevents weak fuzzy hits (e.g. "replit" → "regedit").
        matches = get_close_matches(lower, alias_keys, n=1, cutoff=0.78)
        if matches:
            resolved_alias = _APP_ALIASES.get(matches[0])
            if resolved_alias:
                logger.debug(f"Fuzzy alias: '{lower}' → '{matches[0]}'")

    expected_proc = expected_process_name(lower)

    # ── Already running? Focus/restore unless a new window was requested ──
    if not new_window and (expected_proc and is_process_running(expected_proc)):
        focused = _bring_to_foreground(lower, expected_proc)
        if focused:
            return f"{name} is already running — brought to the front, Sir."
        return f"{name} is already open, Sir."

    # ── Handle new window specific flags if requested ────────────────
    if new_window and resolved_alias:
        target = resolved_alias.get(_OS) or resolved_alias.get("Windows", "")
        t_lower = target.lower()
        if "chrome" in t_lower or "msedge" in t_lower or "firefox" in t_lower or "code" in t_lower:
            target = f"{target} --new-window"
        elif "explorer" in t_lower:
            target = "explorer.exe /separate"
        result = _launch_and_verify(target, name, expected_proc=None)
        return result or f"Opened a new window of {name}, Sir."

    # ── Try alias-based launch ─────────────────────────────────────────
    if resolved_alias:
        target = resolved_alias.get(_OS) or resolved_alias.get("Windows", "")
        if not target:
            return f"{name} is not supported on {_OS}."
        result = _launch_and_verify(target, name, expected_proc)
        if result is not None:
            return result

    # ── Check if this query is actually a file/document request ────────
    _FILE_EXTS = (".pdf", ".docx", ".doc", ".txt", ".csv", ".xlsx", ".xls", ".pptx", ".ppt",
                  ".png", ".jpg", ".jpeg", ".py", ".json", ".zip", ".mp3", ".mp4")
    _FILE_KEYWORDS = ("pdf", "docx", "doc", "spreadsheet", "document", "file", "notes")
    if any(lower.endswith(ext) for ext in _FILE_EXTS) or any(f" {kw}" in lower or lower.startswith(f"{kw} ") for kw in _FILE_KEYWORDS):
        file_res = _try_open_as_file(name)
        if file_res:
            return file_res

    # ── Try PATH executable ────────────────────────────────────────────
    exe = lower if lower.endswith(".exe") else f"{lower}.exe"
    if shutil.which(exe):
        target = f"{exe} --new-window" if new_window else exe
        result = _launch_and_verify(target, name, expected_proc if not new_window else None)
        if result is not None:
            return result

    # ── Autonomous AI Error Recovery (12-Tier Cascade) ──────────────────
    recovered_path = autonomous_recover_app(name)
    if recovered_path:
        logger.info(f"[open_app] Autonomous recovery resolved '{name}' -> '{recovered_path}'")
        return _launch_shortcut(recovered_path, name, expected_proc) if recovered_path.endswith(".lnk") else (_launch_and_verify(recovered_path, name, expected_proc) or f"Done! Opened {name} via auto-recovery.")

    # ── Fallback: Try resolving as a file/document before declaring failure ──
    file_res = _try_open_as_file(name)
    if file_res:
        return file_res

    # ── Failure fallback with intelligent suggestions ─────────────────
    suggestions = _get_app_suggestions(lower)
    if suggestions:
        sug_str = ", ".join([f"'{s}'" for s in suggestions[:3]])
        return f"Couldn't find or launch '{name}'. Did you mean one of these: {sug_str}?"
    return f"Couldn't find app or file '{name}'. Please verify the name or index its folder first."


def _try_open_as_file(name: str) -> Optional[str]:
    """Fallback handler: if name looks like a document/file or no matching app exists,
    try to resolve and open it as a file/document via knowledge_action.
    """
    try:
        from actions.knowledge_action import knowledge_action
        res = knowledge_action("open", path=name)
        if res and not res.startswith("Could not find any file"):
            return res
    except Exception as exc:
        logger.debug(f"_try_open_as_file knowledge_action failed: {exc}")
    return None


def autonomous_recover_app(name: str) -> Optional[str]:
    """12-tier autonomous self-healing app launcher.

    Order:
      1. User aliases
      2. Saved aliases
      3. Installed application database (cached Start Menu index)
      4. Start Menu shortcut search
      5. Desktop shortcuts (.lnk files)
      6. Program Files (C:\\Program Files)
      7. Program Files (x86) (C:\\Program Files (x86))
      8. AppData (%LOCALAPPDATA% / %APPDATA%)
      9. Windows PATH (shutil.which)
     10. Executable name variations
     11. Typo correction (difflib close matches)
     12. Semantic similarity search / closest match
    """
    clean_name = name.strip().lower()
    if not clean_name:
        return None

    # Tier 1 & 2: User and Saved Aliases
    if clean_name in _APP_ALIASES:
        target = _APP_ALIASES[clean_name].get(_OS) or _APP_ALIASES[clean_name].get("Windows", "")
        if target and (shutil.which(target) or os.path.exists(target) or target.endswith(":")):
            return target

    # Tier 3: Installed application database
    index = get_app_index()
    if clean_name in index:
        return index[clean_name]

    # Tier 4: Start Menu direct scan
    start_menu_dirs = [
        Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    # Guard: substring containment against arbitrary shortcut names is only
    # safe once the query itself carries enough signal. A 1-2 character
    # fragment (e.g. noisy STT junk like "we") is a substring of countless
    # unrelated shortcuts ("Event Viewer", "Windows Update", "Software
    # Center"...), so below this length we skip straight to later tiers
    # that have proper confidence checks instead of guessing.
    _min_substr_len = 4
    if len(clean_name) >= _min_substr_len:
        for smd in start_menu_dirs:
            if smd.exists():
                for lnk in smd.rglob("*.lnk"):
                    if clean_name in lnk.stem.lower():
                        return str(lnk)

    # Tier 5: Desktop shortcuts
    desktop_dirs = [
        Path.home() / "Desktop",
        Path(os.environ.get("PUBLIC", "C:\\Users\\Public")) / "Desktop",
    ]
    if len(clean_name) >= _min_substr_len:
        for dtd in desktop_dirs:
            if dtd.exists():
                for lnk in dtd.glob("*.lnk"):
                    if clean_name in lnk.stem.lower():
                        return str(lnk)

    # Tier 6 & 7: Program Files / Program Files (x86)
    pf_dirs = [
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")),
    ]
    for pf in pf_dirs:
        if pf.exists():
            try:
                for exe in pf.glob(f"**/{clean_name}*.exe"):
                    return str(exe)
            except Exception:
                pass

    # Tier 8: AppData
    appdata_dirs = [
        Path(os.environ.get("LOCALAPPDATA", "")),
        Path(os.environ.get("APPDATA", "")),
    ]
    for ad in appdata_dirs:
        if ad and ad.exists():
            try:
                for exe in ad.glob(f"Programs/**/{clean_name}*.exe"):
                    return str(exe)
            except Exception:
                pass

    # Tier 9: Windows PATH
    exe_name = clean_name if clean_name.endswith(".exe") else f"{clean_name}.exe"
    path_match = shutil.which(exe_name)
    if path_match:
        return path_match

    # Tier 10: Executable name variations
    variations = [f"{clean_name}64.exe", f"win{clean_name}.exe", f"ms{clean_name}.exe", f"{clean_name}_x64.exe"]
    for var in variations:
        var_match = shutil.which(var)
        if var_match:
            return var_match

    # Tier 11: Typo correction (difflib) — high-confidence only.
    # Exclude Gama's own shortcut(s) from fuzzy candidates — Gama should
    # never be launched as a "best guess" for some other app's name; if
    # the user wants Gama they'll say so directly.
    all_keys = [
        k for k in set(list(_APP_ALIASES.keys()) + list(index.keys()))
        if "gama" not in k
    ]
    # High cutoffs prevent dangerous weak matches (e.g. "replit" → "regedit"):
    #   ≤4-char queries: 0.82 — very short words collide easily, need near-exact match
    #   >4-char queries: 0.76 — longer words tolerate one or two character differences
    # If neither cutoff is met we return None so open_app() can tell the user
    # the app was not found instead of silently launching something unrelated.
    _cutoff = 0.82 if len(clean_name) <= 4 else 0.76
    close_matches = get_close_matches(clean_name, all_keys, n=1, cutoff=_cutoff)
    if close_matches:
        match_key = close_matches[0]
        if match_key in _APP_ALIASES:
            return _APP_ALIASES[match_key].get(_OS) or _APP_ALIASES[match_key].get("Windows", "")
        if match_key in index:
            return index[match_key]

    # Tier 12: Exact substring containment only — no semantic guessing.
    # A key must fully contain the query OR the query must fully contain the
    # key (min 4 chars) — this prevents single-letter fragments causing hits.
    min_len = max(4, len(clean_name) - 1)
    for key in all_keys:
        if len(key) >= min_len and (
            (len(clean_name) >= 4 and clean_name in key) or
            (len(key) >= 4 and key in clean_name)
        ):
            if key in index:
                return index[key]
            if key in _APP_ALIASES:
                return _APP_ALIASES[key].get(_OS) or _APP_ALIASES[key].get("Windows", "")

    return None


def _find_claude_desktop_exe() -> Optional[str]:
    """Look for the Claude Desktop app in its known install locations.
    Claude Desktop is a per-user Electron install, so it never lives in
    Program Files and never shows up reliably in the Start Menu index —
    it has to be found explicitly rather than fuzzy-matched."""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude" / "Claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Claude" / "Claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Claude" / "Claude.exe",
        Path.home() / "AppData" / "Local" / "AnthropicClaude" / "Claude.exe",
    ]
    for c in candidates:
        if c and c.exists():
            return str(c)

    # Start Menu — only an exact/prefix match on "claude", never a fuzzy
    # one, so it can't collide with unrelated shortcuts (e.g. Gama's own).
    index = get_app_index()
    for stem, path in index.items():
        if stem == "claude" or stem.startswith("claude "):
            return path

    which = shutil.which("Claude.exe") or shutil.which("claude.exe")
    return which


def _open_claude(new_window: bool = False) -> str:
    """Open Claude Desktop; if it isn't installed, look for Claude Code
    (the CLI) instead of silently launching an unrelated app."""
    exe = _find_claude_desktop_exe()
    if exe:
        result = (
            _launch_shortcut(exe, "Claude", "Claude")
            if exe.endswith(".lnk")
            else _launch_and_verify(exe, "Claude", "Claude")
        )
        if result:
            return result
        return "Found Claude Desktop but couldn't confirm it launched, Sir."

    logger.info("open_app: Claude Desktop not found — falling back to Claude Code.")
    return _open_claude_code(desktop_not_found=True)


def _open_claude_code(desktop_not_found: bool = False) -> str:
    """Locate the Claude Code CLI (`claude`) on PATH and launch it inside
    a terminal, since it's a command-line tool, not a windowed app."""
    claude_cli = shutil.which("claude") or shutil.which("claude.cmd")
    prefix = "Claude Desktop isn't installed, so I looked for Claude Code instead. " \
        if desktop_not_found else ""

    if not claude_cli:
        return (
            f"{prefix}Couldn't find Claude Code on PATH either. "
            f"Install it with 'npm install -g @anthropic-ai/claude-code' "
            f"or grab Claude Desktop from claude.ai/download, Sir."
        )

    terminal_target = _APP_ALIASES.get("terminal", {}).get(_OS, "wt.exe")
    try:
        if _OS == "Windows":
            subprocess.Popen(
                [terminal_target, "cmd", "/k", claude_cli],
                shell=False,
            )
        else:
            subprocess.Popen([terminal_target, "-e", claude_cli])
        return f"{prefix}Opening Claude Code in a terminal, Sir."
    except Exception as exc:
        logger.warning(f"Failed to launch Claude Code CLI: {exc}")
        return f"{prefix}Found Claude Code at '{claude_cli}' but couldn't launch the terminal: {exc}"



def _get_app_suggestions(query: str) -> List[str]:
    """Get up to 3 closest app name suggestions for a failed query."""
    index = get_app_index()
    all_keys = list(set(list(_APP_ALIASES.keys()) + list(index.keys())))
    return get_close_matches(query.lower(), all_keys, n=3, cutoff=0.4)



# ---------------------------------------------------------------------------
# Launch helpers
# ---------------------------------------------------------------------------

def _open_url(url: str) -> str:
    try:
        import webbrowser
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        webbrowser.open(url, new=2)
        return f"Opening {url} in browser."
    except Exception as exc:
        return f"Could not open browser: {exc}"


def _launch_and_verify(
    target: str,
    display_name: str,
    expected_proc: Optional[str],
) -> Optional[str]:
    """Launch `target`, verify the process appeared. Returns message or None."""
    try:
        if target.endswith(":"):
            # URL scheme (ms-settings:, spotify:, etc.)
            if _OS == "Windows":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", target])
        elif _OS == "Windows":
            # target is a resolved app path/executable, not raw shell text.
            # Avoid shell=True (Windows shell=True + list only runs the
            # first element; with a string it also risks metacharacter
            # interpretation if the path/name ever contains shell syntax).
            if os.path.exists(target):
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                import shlex
                try:
                    parts = shlex.split(target, posix=False)
                except ValueError:
                    parts = [target]
                subprocess.Popen(parts, shell=False)
        else:
            subprocess.Popen([target])
    except Exception as exc:
        logger.warning(f"Launch of {target!r} raised: {exc}")
        return None

    if expected_proc:
        if wait_for_process(expected_proc, timeout=_VERIFY_TIMEOUT):
            return f"Done! {display_name} is open."
        return None  # caller should try another strategy
    time.sleep(0.3)
    return f"Done! {display_name} is open."


def _launch_shortcut(lnk: str, display_name: str, expected_proc: Optional[str]) -> str:
    """Launch a .lnk shortcut via os.startfile and verify."""
    try:
        os.startfile(lnk)  # type: ignore[attr-defined]
    except Exception as exc:
        return f"Found a shortcut for {display_name} but couldn't launch it: {exc}"

    if expected_proc:
        if wait_for_process(expected_proc, timeout=_VERIFY_TIMEOUT):
            return f"Done! {display_name} is open."
        time.sleep(0.4)
        return f"Launched {display_name} (couldn't confirm the process — it may still be starting)."
    time.sleep(0.4)
    return f"Done! {display_name} is open."


__all__ = ["open_app", "get_app_index"]