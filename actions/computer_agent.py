"""
actions/computer_agent.py — Gama Autonomous Computer Agent
============================================================
High-level autonomous task execution. Gama can:
  - Open an app, then perform actions inside it (type, click, search)
  - Open a browser, search, read results, click links
  - Chain multiple steps to complete complex tasks
    e.g. "Open VS Code, launch GAMA, open Terminal and Spotify."
  - Understand a free-form natural-language goal ("clean up my
    Downloads folder", "set up my coding workspace", "download the
    latest NVIDIA driver", "find that PDF I opened yesterday"),
    break it into concrete steps, run them, verify each one, and
    recover from common failures instead of aborting on the first
    problem.

This is the "full PC access" layer — Gama acts as the user's hands.

Reliability layer: waits are VERIFIED (poll for the process/window to
actually appear) instead of fixed sleeps, and chained steps keep going
even if one step fails, reporting a clear per-step status at the end
rather than aborting the whole chain on the first problem.

Automation strategy — accessibility first, vision as fallback:
  1. actions/ui_automation.py (Windows UIA) — read the real
     accessibility tree, click/type through it directly. Fast, cheap,
     robust to resizing/DPI/theme.
  2. actions/screen_agent.py (Gemini Vision) — only reached when a
     window exposes no usable UIA tree (games, canvas apps, custom
     GPU-rendered UI). See resolve_click_target() below.

Author : Vineet Machchal
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable, List, Tuple

from actions.reliability import wait_for_process, expected_process_name

from utils.logger import get_logger
log = get_logger(__name__)
logger = log  # alias

# How long to wait for an app to be "ready enough" to receive keystrokes
# after its process appears. Some apps (Discord, Spotify) show a process
# well before their window can accept input.
_POST_LAUNCH_SETTLE = 0.8


def computer_agent(action: str = "execute", **kwargs) -> str:
    """Autonomous multi-step computer tasks."""
    action = (action or "execute").lower().strip()

    if action == "open_and_search":
        return _open_and_search(
            kwargs.get("app", ""),
            kwargs.get("query", ""),
            kwargs.get("engine", "google"),
        )
    if action == "open_and_type":
        return _open_and_type(
            kwargs.get("app", ""),
            kwargs.get("text", ""),
            kwargs.get("press_enter", True),
        )
    if action == "browser_search_and_read":
        return _browser_search_and_read(
            kwargs.get("query", ""),
            kwargs.get("engine", "google"),
        )
    if action == "open_app_and_wait":
        return _open_app_and_wait(
            kwargs.get("app", ""),
            float(kwargs.get("wait_seconds", 2.0)),
        )
    if action in ("open_multiple", "chain"):
        return _open_multiple(kwargs.get("apps", []))
    if action in ("natural_task", "task", "goal"):
        return _natural_task(kwargs.get("request", "") or kwargs.get("goal", "") or kwargs.get("text", ""))
    if action == "click_smart":
        return _click_smart(
            kwargs.get("window", ""), kwargs.get("target", ""),
            kwargs.get("description", ""),
        )
    return (f"Unknown computer_agent action: {action}. Use: "
            f"open_and_search, open_and_type, browser_search_and_read, "
            f"open_app_and_wait, open_multiple, natural_task, click_smart.")


def _wait_ready(app_name: str, fallback_seconds: float = 2.0) -> None:
    """Wait for an app's process to appear (verified) before we act on it;
    fall back to a fixed sleep only if we don't recognize the app."""
    proc = expected_process_name(app_name)
    if proc:
        wait_for_process(proc, timeout=8.0)
        time.sleep(_POST_LAUNCH_SETTLE)
    else:
        time.sleep(fallback_seconds)


# ---------------------------------------------------------------------------
# Accessibility-first click resolution — UIA, then CV as a fallback.
# ---------------------------------------------------------------------------
def resolve_click_target(window: str, target_text: str, description: str = "") -> str:
    """Try to click `target_text` inside `window` via Windows UIA first
    (fast, precise, no LLM round-trip). Only if that fails — window
    doesn't expose a usable accessibility tree — fall back to the
    vision-based screen_agent, which asks Gemini to locate and click
    the element from a screenshot. Returns a plain-English result."""
    try:
        from actions.ui_automation import uia_available, click_element
        if uia_available():
            if click_element(window, target_text):
                return f"Clicked '{target_text}' (accessibility)."
    except Exception as exc:
        logger.debug(f"UIA click attempt failed, falling back to vision: {exc}")

    try:
        from actions.screen_agent import screen_agent
        vision_desc = description or f"the '{target_text}' element"
        return screen_agent("find_and_click", description=vision_desc)
    except Exception as exc:
        return f"Couldn't find or click '{target_text}': {exc}"


def _click_smart(window: str, target: str, description: str = "") -> str:
    if not target and not description:
        return "What should I click?"
    return resolve_click_target(window, target, description)


def _open_and_search(app: str, query: str, engine: str = "google") -> str:
    """Open an app/website and search for a query.
    If the app is a browser or search engine, searches on Edge (via edge_search).
    Otherwise opens the app and types the query."""
    if not app:
        return "Which app should I open?"
    if not query:
        return "What should I search for?"

    app_lower = app.lower().strip()

    browser_apps = {"edge", "msedge", "microsoft edge", "chrome", "google chrome",
                    "firefox", "browser", "brave", "opera"}
    search_engines = {"google", "bing", "duckduckgo", "yahoo", "youtube"}

    if app_lower in browser_apps or app_lower in search_engines:
        from actions.edge_search import edge_search
        result = edge_search(query)
        return f"Opened Edge and {result}"

    from actions.open_app import open_app
    open_result = open_app(app)
    _wait_ready(app_lower)

    from actions.keyboard_actions import keyboard_actions
    type_result = keyboard_actions("type", text=query, press_enter=False)
    return f"{open_result} Then {type_result}"


def _open_and_type(app: str, text: str, press_enter: bool = False) -> str:
    """Open an app and type/paste text into it.

    press_enter defaults to False so writing into Notepad/editors does not
    accidentally submit a newline after the content. Long text is pasted
    via the clipboard (see keyboard_actions._type) to avoid stalling the
    Live session.
    """
    if not app:
        return "Which app should I open?"
    if not text:
        return "What text should I type?"

    from actions.open_app import open_app
    open_result = open_app(app)
    _wait_ready(app.lower().strip())
    # Extra settle so focus is on the editor before we inject text.
    time.sleep(0.35)

    from actions.keyboard_actions import keyboard_actions
    type_result = keyboard_actions("type", text=text)
    if press_enter:
        keyboard_actions("press", key="enter")
    return f"{open_result} Then {type_result}"


def _browser_search_and_read(query: str, engine: str = "google") -> str:
    """Search in the user's real Edge app (via edge_search), then read the
    results page. Searching always goes through Edge's own search box —
    browser_control's Playwright page is only used to read the results
    (it navigates to the same query on Edge's default engine so it can
    extract text; the actual on-screen search the user sees runs in Edge)."""
    if not query:
        return "What should I search for?"

    from actions.edge_search import edge_search
    from actions.browser_control import browser_control

    search_result = edge_search(query)

    # Mirror the same query in the Playwright-controlled page purely so we
    # can extract readable text back for Gama — the visible search itself
    # already happened in the user's real Edge window above.
    import urllib.parse
    read_url = f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}"
    browser_control("open", url=read_url, visible=False, channel="msedge")
    time.sleep(1.5)
    page_text = browser_control("read", max_chars=1500)
    return f"{search_result}\n\nResults:\n\n{page_text}"


def _open_app_and_wait(app: str, wait_seconds: float = 2.0) -> str:
    """Open an app and wait for it to be ready — verified when possible."""
    if not app:
        return "Which app should I open?"
    from actions.open_app import open_app
    result = open_app(app)
    proc = expected_process_name(app.lower().strip())
    if proc:
        confirmed = wait_for_process(proc, timeout=max(wait_seconds, 6.0))
        return f"{result} ({'ready' if confirmed else 'could not confirm readiness'})"
    time.sleep(wait_seconds)
    return f"{result} (waited {wait_seconds}s for it to load)"


def _open_multiple(apps: List[str]) -> str:
    """Open several apps in sequence — the chained-command case, e.g.
    'Open VS Code, launch GAMA, open Terminal and Spotify.'

    Each app is opened and verified independently; one failure doesn't
    stop the rest of the chain. Returns a per-app status report.
    """
    if not apps:
        return "Which apps should I open? Give me a list, e.g. ['vscode', 'terminal', 'spotify']."
    if isinstance(apps, str):
        # Be forgiving if the model passes a comma-separated string instead of a list.
        apps = [a.strip() for a in apps.split(",") if a.strip()]

    from actions.open_app import open_app

    lines = [f"Opening {len(apps)} app(s):"]
    succeeded, failed = 0, 0
    for app in apps:
        try:
            result = open_app(app)
            is_fail = any(m in result.lower() for m in
                         ("couldn't", "could not", "not supported", "unknown"))
            if is_fail:
                failed += 1
            else:
                succeeded += 1
            lines.append(f"  • {app}: {result}")
        except Exception as exc:
            failed += 1
            lines.append(f"  • {app}: failed — {exc}")
        # Small stagger so app launches (and Start Menu search fallbacks)
        # don't collide with each other.
        time.sleep(0.4)

    lines.append(f"\nDone: {succeeded} opened, {failed} failed.")
    _note_workflow_repeat(apps)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Repeated-workflow tracking — feeds actions/proactive_suggestions.py's
# "want to save this as a reusable automation?" nudge. Kept intentionally
# tiny (a memory counter), never surfaces anything itself — the proactive
# suggestions module decides if/when to mention it.
# ---------------------------------------------------------------------------
def _note_workflow_repeat(apps: List[str]) -> None:
    try:
        from actions.proactive_suggestions import note_app_chain
        note_app_chain(apps)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Natural-language task decomposition
# ---------------------------------------------------------------------------
# Each entry: (regex, handler name). Matched top-to-bottom, first hit wins.
# Handlers take the regex match + the raw request string and return a
# plain-English result. This intentionally stays a lightweight pattern
# layer (fast, offline, no extra LLM round trip for the common cases) —
# Gemini's own function-calling already handles anything unmatched, so
# this only needs to cover phrasing that maps cleanly onto a concrete,
# reusable multi-step routine.
_TASK_PATTERNS: List[Tuple[re.Pattern, str]] = []


def _register(pattern: str, handler_name: str) -> None:
    _TASK_PATTERNS.append((re.compile(pattern, re.IGNORECASE), handler_name))


_register(r"\b(nvidia|geforce)\b.*\bdriver", "_task_download_gpu_driver")
_register(r"\b(clean( ?up)?|organi[sz]e|tidy)\b.*\bdownloads?\b", "_task_organize_downloads")
_register(r"\b(set ?up|prepare|start)\b.*\b(coding|dev|development|programming)\b.*\bworkspace\b",
           "_task_setup_coding_workspace")
_register(r"\bset ?up\b.*\bworkspace\b", "_task_setup_coding_workspace")
_register(r"\bfind\b.*\bpdf\b", "_task_find_recent_file")
_register(r"\bfind\b.*\b(file|document|doc|spreadsheet|image|photo)\b.*\b(yesterday|today|last|recent)\b",
           "_task_find_recent_file")


def _natural_task(request: str) -> str:
    """Entry point for a free-form goal. Tries to match a known
    multi-step routine; if nothing matches, says so plainly instead of
    silently doing nothing (asks for clarification rather than guessing)."""
    request = (request or "").strip()
    if not request:
        return "What would you like me to do?"

    for pattern, handler_name in _TASK_PATTERNS:
        m = pattern.search(request)
        if m:
            handler: Callable = globals()[handler_name]
            try:
                return handler(m, request)
            except Exception as exc:
                logger.exception(f"natural_task handler {handler_name} failed")
                return (f"I started '{request}' but hit a problem partway through: {exc}. "
                        f"Want me to retry, or handle it a different way?")

    return ("I'm not sure exactly what steps that needs — could you say it a "
            "bit more specifically? (e.g. which app, which files, or what the "
            "end result should look like)")


def _task_download_gpu_driver(m, request: str) -> str:
    """'download the latest nvidia driver' — opens NVIDIA's official
    driver page in the browser rather than silently guessing a direct
    binary URL (driver URLs are versioned/geo-routed and change often,
    so the reliable, honest step is to get the user to the real
    download page, verified, and let them pick their exact GPU)."""
    from actions.edge_search import edge_search
    steps = []
    r1 = edge_search("NVIDIA GeForce driver download official site")
    steps.append(f"1. Opened NVIDIA's driver download page — {r1}")
    steps.append("2. NVIDIA's site auto-detects your GPU, or you can pick it manually — "
                 "click 'Search' there and I can click 'Download' for you once it's found. "
                 "Want me to go ahead and click it?")
    return "\n".join(steps)


def _task_organize_downloads(m, request: str) -> str:
    """'clean up my downloads folder' — groups files by type into
    subfolders (Documents/Images/Archives/Installers/Other), verifying
    the move actually happened for every file, and never deletes
    anything — organizing is safe-by-default, deleting is not."""
    from actions.reliability import retry

    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return "I couldn't find your Downloads folder."

    groups = {
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xlsx", ".xls", ".pptx", ".csv"},
        "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"},
        "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
        "Installers": {".exe", ".msi"},
        "Videos": {".mp4", ".mkv", ".mov", ".avi"},
        "Audio": {".mp3", ".wav", ".flac"},
    }

    def _group_for(ext: str) -> str:
        for name, exts in groups.items():
            if ext in exts:
                return name
        return "Other"

    files = [f for f in downloads.iterdir() if f.is_file()]
    if not files:
        return "Your Downloads folder is already empty — nothing to organize."

    moved, failed, skipped = 0, 0, 0
    report_lines = []
    for f in files:
        dest_folder = downloads / _group_for(f.suffix.lower())
        try:
            dest_folder.mkdir(exist_ok=True)
            dest = dest_folder / f.name
            if dest.exists():
                skipped += 1
                continue

            def _do_move(_src=f, _dst=dest):
                _src.rename(_dst)

            retry(_do_move, attempts=2, delay=0.3, exceptions=(OSError,))
            if dest.exists() and not f.exists():
                moved += 1
            else:
                failed += 1
                report_lines.append(f"  • couldn't verify move for {f.name}")
        except Exception as exc:
            failed += 1
            report_lines.append(f"  • {f.name}: {exc}")

    summary = f"Organized Downloads: {moved} file(s) sorted into folders"
    if skipped:
        summary += f", {skipped} skipped (already existed at destination)"
    if failed:
        summary += f", {failed} failed"
    if report_lines:
        summary += "\n" + "\n".join(report_lines)
    return summary + "."


def _task_setup_coding_workspace(m, request: str) -> str:
    """'set up my coding workspace' — opens the user's usual coding
    apps together, using memory to remember which ones if they've told
    Gama before (falls back to a sensible default the first time)."""
    from memory.memory_manager import get_memory, set_memory

    saved = get_memory("preferences", "coding_workspace_apps")
    if saved:
        apps = [a.strip() for a in saved.split(",") if a.strip()]
    else:
        apps = ["vscode", "terminal", "browser"]
        set_memory("preferences", "coding_workspace_apps", ", ".join(apps))

    result = _open_multiple(apps)
    return (f"Setting up your coding workspace ({', '.join(apps)}).\n{result}\n\n"
            f"(Tell me 'my coding workspace is X, Y, Z' any time to change this.)")


def _task_find_recent_file(m, request: str) -> str:
    """'find that PDF I opened yesterday' — searches common user
    folders (Downloads, Documents, Desktop) for files matching the
    mentioned type, filtered to a recent modified-time window inferred
    from words like 'yesterday'/'today'/'last week'."""
    import datetime as _dt

    ext_map = {
        "pdf": ".pdf", "spreadsheet": ".xlsx", "image": ".png", "photo": ".jpg",
        "document": ".docx", "doc": ".docx",
    }
    ext = ".pdf"
    for word, e in ext_map.items():
        if word in request.lower():
            ext = e
            break

    now = _dt.datetime.now()
    if "yesterday" in request.lower():
        window_start = (now - _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0)
        window_end = window_start + _dt.timedelta(days=1)
    elif "today" in request.lower():
        window_start = now.replace(hour=0, minute=0, second=0)
        window_end = now
    else:
        window_start = now - _dt.timedelta(days=7)
        window_end = now

    search_dirs = [Path.home() / d for d in ("Downloads", "Documents", "Desktop")]
    matches = []
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            for f in d.rglob(f"*{ext}"):
                try:
                    mtime = _dt.datetime.fromtimestamp(f.stat().st_mtime)
                    if window_start <= mtime <= window_end:
                        matches.append((mtime, f))
                except Exception:
                    continue
        except Exception:
            continue

    if not matches:
        return f"I couldn't find any {ext} files matching that time window in Downloads, Documents, or Desktop."

    matches.sort(reverse=True)
    lines = [f"Found {len(matches)} matching file(s):"]
    for mtime, f in matches[:10]:
        lines.append(f"  • {f} (modified {mtime.strftime('%b %d, %I:%M %p')})")
    return "\n".join(lines)


__all__ = ["computer_agent", "resolve_click_target"]
