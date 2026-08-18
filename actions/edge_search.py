from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import psutil
from pywinauto.keyboard import send_keys

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()


def _find_edge_executable() -> Optional[str]:
    if _OS != "Windows":
        return None

    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        return found

    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",

        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",

        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
    ]

    for p in candidates:
        if p.exists():
            return str(p)

    return None


def _is_edge_running():
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == "msedge.exe":
                return True
        except Exception:
            pass
    return False


def _launch_edge(edge_path: str):
    subprocess.Popen(
        [edge_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _connect_to_edge(timeout_s: float = 12.0, poll_interval_s: float = 0.5):
    """Finds Edge's actual top-level browser window and focuses it.

    Two things were wrong with the previous implementation:

    1. `Application(backend="uia").connect(path="msedge.exe")` connects
       to *a* process matching that name, but Edge (like all Chromium
       browsers) runs many `msedge.exe` processes — GPU, renderer,
       utility, network — most of which never own a top-level window.
       If `connect()` happened to latch onto one of those, `top_window()`
       would correctly report "No windows for that process could be
       found" even though Edge's actual browser window was open and
       visible the whole time.
    2. A single fixed 2s sleep after launch isn't enough for a cold
       Edge start (which routinely takes several seconds before its
       main window exists), so the very first connect attempt on a
       fresh launch was often racing the browser's own startup.

    Fix: search the desktop directly for a window with Edge's window
    class ("Chrome_WidgetWin_1") instead of trying to resolve a window
    from a specific (possibly wrong) process, and retry with backoff up
    to `timeout_s` instead of failing (or hanging) on the first miss.
    """
    from pywinauto import Desktop

    deadline = time.monotonic() + timeout_s
    last_error: Optional[Exception] = None

    while time.monotonic() < deadline:
        try:
            candidates = Desktop(backend="uia").windows(
                class_name="Chrome_WidgetWin_1", visible_only=True,
            )
            # Prefer a window that actually belongs to an msedge.exe PID
            # (Chromium reuses this class name across Edge/Chrome/etc.)
            for win in candidates:
                try:
                    proc = psutil.Process(win.process_id())
                    if proc.name().lower() == "msedge.exe":
                        if win.is_minimized():
                            win.restore()
                        win.set_focus()
                        return win
                except Exception:
                    continue
        except Exception as exc:
            last_error = exc

        time.sleep(poll_interval_s)

    raise RuntimeError(
        "Could not find Edge's browser window"
        + (f" ({last_error})" if last_error else "")
    )


def _list_tab_items(window):
    """Return Edge's tab strip items (UIA TabItem controls) for the
    given top-level Edge window. Each tab's .window_text() is its page
    title (what you'd see printed on the tab), which is what we match
    'close youtube' against — no need to know the URL."""
    try:
        return window.descendants(control_type="TabItem")
    except Exception:
        return []


def _find_tab(window, query: str):
    """Best-effort match of `query` against open tabs' titles. Prefers
    a substring hit; case-insensitive. Returns the tab control or None."""
    query_l = query.strip().lower()
    if not query_l:
        return None
    tabs = _list_tab_items(window)
    for tab in tabs:
        try:
            title = (tab.window_text() or "").lower()
        except Exception:
            continue
        if query_l in title:
            return tab
    return None


def close_tab(query: str = "") -> str:
    """Close ONE specific tab by matching its title (e.g. 'youtube'),
    instead of closing the whole Edge window like process_manager's
    close_window would (WM_CLOSE goes to the top-level window, which
    owns every tab). This clicks the matching tab to bring it to the
    front, then sends Ctrl+W — which closes only the active tab (it
    only takes the window down too if that tab happened to be the
    last one left, same as a user doing it by hand).
    """
    query = (query or "").strip()
    if not query:
        return "Which tab should I close? (e.g. 'YouTube')"

    if not _is_edge_running():
        return "Edge isn't open — there's no tab to close."

    try:
        window = _connect_to_edge(timeout_s=5.0)
        window.set_focus()
        time.sleep(0.15)

        tab = _find_tab(window, query)
        if tab is None:
            open_titles = [t.window_text() for t in _list_tab_items(window) if t.window_text()]
            hint = f" Open tabs: {', '.join(open_titles[:8])}" if open_titles else ""
            return f"No open tab matching '{query}'.{hint}"

        matched_title = tab.window_text()
        tab.click_input()
        time.sleep(0.15)
        send_keys("^w")  # Ctrl+W — closes only the now-active tab

        return f"Closed tab: '{matched_title}'."
    except Exception as e:
        logger.exception("close_tab failed")
        return f"Failed to close tab '{query}': {e}"


def edge_search(query: str = "", new_tab: bool = True) -> str:
    """Search `query` in the user's real Edge window.

    new_tab=True (default): opens a fresh tab (Ctrl+T) in the SAME
    window and searches there, leaving whatever the user already had
    open untouched — this is what a bare "search this" / "search for
    X" should do.
    new_tab=False: reuses the currently active tab (Ctrl+L to focus the
    address bar, select-all, replace) — only when the user explicitly
    says "in this tab" / "in the current tab" / "same tab".
    """

    query = query.strip()

    if not query:
        return "What should I search for?"

    edge_path = _find_edge_executable()

    if edge_path is None:
        return "Microsoft Edge is not installed."

    try:

        was_running = _is_edge_running()
        if not was_running:
            _launch_edge(edge_path)
            # A fresh launch needs more than a token sleep before its
            # window exists — _connect_to_edge below already polls with
            # its own timeout, so this just gives the process a moment
            # to get off the ground before the first poll attempt.
            time.sleep(1.0)

        # Cold-start Edge can legitimately take several seconds to
        # produce its main window; a warm/already-running Edge should
        # resolve almost immediately. Give the cold-start case more
        # room, but always bounded so a real failure surfaces in
        # seconds, not the ~45s timeout this used to silently eat.
        window = _connect_to_edge(timeout_s=15.0 if not was_running else 5.0)

        window.set_focus()

        time.sleep(0.2)

        # A brand-new Edge launch already opens on a single empty tab
        # with its address bar focused — nothing to add there. Only
        # send Ctrl+T when Edge was already running and the caller
        # actually wants a fresh tab (default behaviour).
        if new_tab and was_running:
            send_keys("^t")   # Ctrl+T — new tab, same window
            time.sleep(0.2)
        else:
            # Reuse whatever tab is currently focused.
            send_keys("^l")   # Ctrl+L — focus address bar
            time.sleep(0.1)
            send_keys("^a")   # Ctrl+A — select existing contents
            time.sleep(0.05)

        # Type query
        send_keys(query, with_spaces=True)

        time.sleep(0.05)

        send_keys("{ENTER}")

        where = "a new tab" if (new_tab and was_running) else "the current tab"
        return f"Searching for '{query}' in {where}."

    except Exception as e:
        logger.exception("Edge search failed")
        return f"Failed to search: {e}"


__all__ = ["edge_search", "close_tab"]