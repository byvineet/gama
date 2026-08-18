"""
actions/spotify_controller.py — Gama Simple Spotify Search
================================================================
Deliberately minimal. Gama does not try to guess which track you
meant — it opens Spotify, searches for what you asked for, and lets
you pick from the results yourself.

Flow for "Play <song> on Spotify":
    1. Launch Spotify if it isn't already running.
    2. Bring its window to the foreground.
    3. Focus the search box (Ctrl+L), clear any previous query, type
       the new one, and press Enter.
    4. Tell the user to pick the track from the results.

That's it — no track selection, no autoplay, no playback
verification, no local cache, no Spotify Web API, no OAuth. Every
interaction is native keyboard input via SendInput (WinAPI); nothing
here ever clicks a coordinate, drives a browser, or scrapes pixels.

Author : Gama
"""

from __future__ import annotations

from utils.logger import get_logger

import asyncio
import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# SendInput — native Windows keyboard injection (WinAPI). No mouse, no
# coordinate clicks, no UI-Automation invoke/click anywhere in this file.
# ---------------------------------------------------------------------------

PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong),
                ("wParamL", ctypes.c_short),
                ("wParamH", ctypes.c_ushort)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _InputUnion)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_END = 0x23
VK_BACK = 0x08
VK_L = 0x4C


def _extra() -> PUL:
    return ctypes.pointer(ctypes.c_ulong(0))


def _vk_event(vk: int, up: bool) -> _Input:
    ii = _InputUnion()
    ii.ki = _KeyBdInput(vk, 0, (KEYEVENTF_KEYUP if up else 0), 0, _extra())
    return _Input(INPUT_KEYBOARD, ii)


def _unicode_event(ch: str, up: bool) -> _Input:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    ii = _InputUnion()
    ii.ki = _KeyBdInput(0, ord(ch), flags, 0, _extra())
    return _Input(INPUT_KEYBOARD, ii)


def _send(*events: _Input) -> None:
    n = len(events)
    arr = (_Input * n)(*events)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(_Input))


def press_key(vk: int, hold: float = 0.02) -> None:
    _send(_vk_event(vk, False))
    time.sleep(hold)
    _send(_vk_event(vk, True))


def press_combo(*vks: int, hold: float = 0.03) -> None:
    for vk in vks:
        _send(_vk_event(vk, False))
    time.sleep(hold)
    for vk in reversed(vks):
        _send(_vk_event(vk, True))


def type_unicode(text: str, delay: float = 0.01) -> None:
    for ch in text:
        _send(_unicode_event(ch, False))
        _send(_unicode_event(ch, True))
        time.sleep(delay)


def clear_field(max_len: int = 128) -> None:
    """Deterministically empty the search box: jump to end, then
    backspace a bounded number of times, rather than relying on
    Ctrl+A/Select-All semantics being consistent across builds."""
    press_key(VK_END, hold=0.01)
    for _ in range(max_len):
        press_key(VK_BACK, hold=0.008)


# ---------------------------------------------------------------------------
# Window discovery / activation — native win32 only, never a click.
# ---------------------------------------------------------------------------

def _find_spotify_hwnd() -> Optional[int]:
    if not _IS_WINDOWS:
        return None
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return None

    candidates = []

    def _cb(hwnd, _acc):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            if proc.name().lower() == "spotify.exe":
                candidates.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        logger.debug("spotify: EnumWindows failed", exc_info=True)
        return None

    if not candidates:
        return None
    try:
        def _area(h):
            rect = win32gui.GetWindowRect(h)
            return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        candidates.sort(key=_area, reverse=True)
    except Exception:
        pass
    return candidates[0]


def _process_exists() -> bool:
    from actions.reliability import is_process_running
    return is_process_running("Spotify")


def _window_is_foreground(hwnd: int) -> bool:
    try:
        import win32gui
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _force_foreground(hwnd: int) -> bool:
    try:
        import win32gui
        import win32con
        import win32process
        import win32api
    except Exception:
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        fg_hwnd = win32gui.GetForegroundWindow()
        cur_thread = win32api.GetCurrentThreadId()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]

        attached = False
        if fg_thread and fg_thread != target_thread:
            try:
                attached = bool(win32process.AttachThreadInput(target_thread, fg_thread, True))
            except Exception:
                attached = False

        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(target_thread, fg_thread, False)
                except Exception:
                    pass

        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        logger.debug("spotify: force_foreground failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Bounded wait helper — polls a condition on a short interval up to a
# timeout. No unconditional fixed sleeps standing in for a real wait.
# ---------------------------------------------------------------------------

async def _wait_until(condition, timeout: float, poll: float = 0.15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:
            pass
        await asyncio.sleep(poll)
    try:
        return bool(condition())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The flow: launch -> foreground -> search -> Enter -> hand off to user
# ---------------------------------------------------------------------------

async def spotify_play_async(song: str, artist: str = "") -> str:
    query = f"{song} {artist}".strip() or (song or "").strip()
    if not query:
        return "I need a song name to search for on Spotify."

    if not _IS_WINDOWS:
        return _fallback_web_search(query)
    try:
        import win32gui  # noqa: F401
        import psutil  # noqa: F401
    except Exception:
        logger.debug("spotify: pywin32/psutil unavailable, falling back to web search")
        return _fallback_web_search(query)

    # -- Launch if not already running ---------------------------------
    if not _process_exists():
        logger.info("[Spotify] Launching")
        launched = False
        try:
            os.startfile("spotify:")  # protocol launch — fast, no path lookup
            launched = True
        except Exception:
            logger.debug("spotify: protocol launch failed, trying alias", exc_info=True)
        if not launched:
            try:
                os.startfile("spotify")
                launched = True
            except Exception:
                pass
        if not launched:
            return "Spotify doesn't seem to be installed, or I couldn't launch it."

        ok = await _wait_until(_process_exists, timeout=15.0, poll=0.3)
        if not ok:
            return "Spotify is still launching — try again in a moment."

    # -- Find the window --------------------------------------------------
    ok = await _wait_until(lambda: _find_spotify_hwnd() is not None, timeout=10.0, poll=0.3)
    hwnd = _find_spotify_hwnd()
    if not ok or hwnd is None:
        return "Spotify's window never appeared — it may still be starting up."

    # -- Foreground ---------------------------------------------------------
    if not _window_is_foreground(hwnd):
        logger.info("[Spotify] Bringing to foreground")
        if not _force_foreground(hwnd):
            await asyncio.sleep(0.15)
            _force_foreground(hwnd)
        await _wait_until(lambda: _window_is_foreground(hwnd), timeout=2.0, poll=0.1)

    # -- Search + Enter -------------------------------------------------------
    logger.info(f"[Spotify] Searching for '{query}'")
    press_combo(VK_CONTROL, VK_L)  # focus search box
    await asyncio.sleep(0.15)
    clear_field()
    await asyncio.sleep(0.05)
    type_unicode(query)
    await asyncio.sleep(0.2)
    press_key(VK_RETURN)
    logger.info("[Spotify] Search submitted — waiting for user to pick a track")

    return f"Opened Spotify and searched for '{query}' — pick the track you want."


def spotify_play(song: str, artist: str = "") -> str:
    """Sync wrapper for callers that aren't async-aware (e.g. media_controller's
    dispatch)."""
    try:
        return asyncio.run(spotify_play_async(song, artist))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(spotify_play_async(song, artist))
        finally:
            loop.close()


def _fallback_web_search(query: str) -> str:
    import urllib.parse
    import webbrowser
    webbrowser.open(f"https://open.spotify.com/search/{urllib.parse.quote(query)}", new=2)
    return f"Searching '{query}' on Spotify Web — pick the track you want from there."


__all__ = ["spotify_play", "spotify_play_async"]
