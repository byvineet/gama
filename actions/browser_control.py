"""
actions/browser_control.py — Gama Browser Automation
======================================================
Autonomous browser control via Playwright.
Uses the user's INSTALLED Edge or Chrome (no separate browser download).

Capabilities:
  - open_browser(visible=True)        → launch visible browser
  - navigate(url)                     → go to URL
  - click(selector_or_text)           → click an element by CSS selector or visible text
  - type_text(selector, text)         → type into an input field
  - press_key(key)                    → press a keyboard key (Enter, Tab, etc.)
  - read_page(max_chars)              → extract text content of the page
  - screenshot()                      → capture the current page
  - scroll(direction, amount)         → scroll up/down
  - go_back() / go_forward()          → browser history navigation
  - close_browser()                   → close the browser

The browser stays open between turns so Gama can do multi-step tasks
like: open Edge → ask what to search → type query → read results.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

log = get_logger(__name__)
logger = log  # back-compat alias
# Persistent browser state (kept across turns)
_browser_lock = threading.Lock()
_playwright_instance = None
_browser = None
_context = None
_page = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread = None

# ── Idle auto-close watchdog ────────────────────────────────────────────────
# Chromium (launched via Playwright) is a full browser process — easily
# 150-300MB+ RAM and a non-trivial idle CPU/power draw — and the code above
# deliberately keeps it open "between turns so Gama can do multi-step tasks".
# That's the right call while a task is active, but nothing previously ever
# closed it again: if the user's last browser action was hours (or days)
# ago, Chromium just sits there burning RAM and battery for no reason.
# This watchdog tracks last-activity time and auto-closes the browser after
# a period of inactivity, exactly as if the user had said "close browser" —
# it only ever acts on idle time, never while a task is in progress, and it
# re-arms itself on every subsequent open.
_IDLE_CLOSE_SECONDS = 10 * 60  # auto-close Chromium after 10 min of no activity
_last_activity_ts: float = 0.0
_idle_watchdog_thread = None
_idle_watchdog_stop: Optional[threading.Event] = None


def _touch_activity() -> None:
    """Record that the browser was just used — resets the idle countdown."""
    global _last_activity_ts
    import time as _time
    _last_activity_ts = _time.monotonic()


def _start_idle_watchdog() -> None:
    """Start (once) a lightweight daemon thread that closes the browser
    after _IDLE_CLOSE_SECONDS of inactivity. Cheap: wakes once every 30s,
    no polling of the browser/page itself."""
    global _idle_watchdog_thread, _idle_watchdog_stop
    if _idle_watchdog_thread is not None and _idle_watchdog_thread.is_alive():
        return
    _idle_watchdog_stop = threading.Event()
    stop_event = _idle_watchdog_stop

    def _watch() -> None:
        import time as _time
        while not stop_event.wait(30.0):
            with _browser_lock:
                if _browser is None:
                    continue
                idle_for = _time.monotonic() - _last_activity_ts
                if idle_for < _IDLE_CLOSE_SECONDS:
                    continue
            # Idle long enough — close outside the lock (close_browser
            # acquires it) so we don't deadlock.
            try:
                logger.info(
                    f"[browser_control] Idle {idle_for:.0f}s — auto-closing "
                    "Chromium to free RAM/CPU."
                )
                _run_async(_close_browser())
            except Exception as exc:
                logger.debug(f"[browser_control] Idle auto-close failed: {exc}")

    _idle_watchdog_thread = threading.Thread(
        target=_watch, name="gama-browser-idle-watchdog", daemon=True
    )
    _idle_watchdog_thread.start()


def _ensure_loop():
    """Ensure we have a running asyncio event loop in a dedicated thread.
    Playwright's async API needs a loop, and we call from sync context.
    """
    global _event_loop, _loop_thread
    if _event_loop is not None and not _event_loop.is_closed():
        return _event_loop
    _loop_thread = threading.Thread(target=_run_loop, daemon=True)
    _loop_thread.start()
    # Wait for the loop to be ready
    import time
    for _ in range(50):
        if _event_loop is not None and not _event_loop.is_closed():
            return _event_loop
        time.sleep(0.05)
    raise RuntimeError("Failed to start asyncio loop for browser.")


def _run_loop():
    global _event_loop
    _event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_event_loop)
    _event_loop.run_forever()


def _run_async(coro):
    """Run a coroutine on the dedicated browser loop, blocking until done."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=60)


# ============================================================
# Public API
# ============================================================
def browser_control(action: str = "open", **kwargs) -> str:
    """Main entry point for browser automation."""
    action = (action or "open").lower().strip()
    if action != "close_tab":  # close_tab drives the user's real Edge window, not ours
        _touch_activity()
        _start_idle_watchdog()
    try:
        if action == "open":
            return _run_async(_open_browser(
                kwargs.get("url", ""),
                kwargs.get("visible", True),
                kwargs.get("channel", "msedge"),
            ))
        if action == "navigate":
            return _run_async(_navigate(kwargs.get("url", "")))
        if action == "click":
            return _run_async(_click(
                kwargs.get("selector", ""),
                kwargs.get("text", ""),
            ))
        if action == "type":
            return _run_async(_type_text(
                kwargs.get("selector", ""),
                kwargs.get("text", ""),
                kwargs.get("press_enter", True),
            ))
        if action == "press_key":
            return _run_async(_press_key(kwargs.get("key", "Enter")))
        if action == "read":
            return _run_async(_read_page(int(kwargs.get("max_chars", 2000))))
        if action == "screenshot":
            return _run_async(_screenshot())
        if action == "scroll":
            return _run_async(_scroll(
                kwargs.get("direction", "down"),
                int(kwargs.get("amount", 500)),
            ))
        if action == "go_back":
            return _run_async(_go_back())
        if action == "go_forward":
            return _run_async(_go_forward())
        if action == "close":
            return _run_async(_close_browser())
        if action == "close_tab":
            # Closing ONE tab (e.g. "close youtube") needs to act on the
            # user's real, already-open Edge window — the same one
            # edge_search drives via UI automation — not this module's
            # own Playwright-controlled instance, which is a separate
            # browser process the user isn't looking at. Delegate there
            # so "close X" never takes down the whole window by mistake.
            from actions.edge_search import close_tab as _edge_close_tab
            return _edge_close_tab(kwargs.get("query", "") or kwargs.get("title", ""))
        return f"Unknown browser action: {action}. Use: open, navigate, click, type, press_key, read, screenshot, scroll, go_back, go_forward, close, close_tab. For web searches, use edge_search instead."
    except Exception as exc:
        logger.error(f"browser_control '{action}' failed: {exc}")
        return f"Browser action failed: {exc}"


# ============================================================
# Async implementations
# ============================================================
async def _open_browser(url: str = "", visible: bool = True,
                         channel: str = "msedge"):
    """Open a browser. Uses the user's installed Edge or Chrome."""
    global _playwright_instance, _browser, _context, _page

    with _browser_lock:
        if _browser is not None:
            # Browser already open — just navigate if URL provided
            if url:
                await _navigate(url)
                return f"Browser already open. Navigated to {url}."
            return "Browser already open."

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ("Playwright not installed. Run: pip install playwright && "
                    "playwright install msedge (or just 'playwright install' for chromium).")

        _playwright_instance = await async_playwright().start()

        # Try the requested channel (msedge, chrome), fall back to chromium
        launch_kwargs = {"headless": not visible}
        try:
            _browser = await _playwright_instance.chromium.launch(
                channel=channel, **launch_kwargs,
            )
        except Exception as exc:
            logger.warning(f"channel={channel} failed ({exc}), using bundled chromium")
            try:
                _browser = await _playwright_instance.chromium.launch(**launch_kwargs)
            except Exception as exc2:
                return (f"Could not launch browser. Install Playwright browsers: "
                        f"playwright install. Error: {exc2}")

        _context = await _browser.new_context()
        _page = await _context.new_page()

        if url:
            await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return f"Browser opened → {url}"
        return "Browser opened."


async def _navigate(url: str):
    global _page
    if _page is None:
        return _open_browser(url)
    if not url:
        return "Which URL should I navigate to?"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
    return f"Navigated to {url}"


async def _click(selector: str = "", text: str = ""):
    """Click an element by CSS selector or visible text."""
    global _page
    if _page is None:
        return "Browser not open. Open a page first."
    if not selector and not text:
        return "Provide a CSS selector or visible text to click."
    try:
        if text:
            await _page.get_by_text(text, exact=False).first.click(timeout=5000)
            return f"Clicked element with text: '{text}'"
        else:
            await _page.click(selector, timeout=5000)
            return f"Clicked: {selector}"
    except Exception as exc:
        return f"Click failed: {exc}"


async def _type_text(selector: str, text: str, press_enter: bool = True):
    """Type text into an input field."""
    global _page
    if _page is None:
        return "Browser not open."
    if not text:
        return "What text should I type?"
    try:
        if selector:
            await _page.fill(selector, text, timeout=5000)
        else:
            # Type into whatever is focused
            await _page.keyboard.type(text)
        if press_enter:
            await _page.keyboard.press("Enter")
        return f"Typed: {text}"
    except Exception as exc:
        return f"Type failed: {exc}"


async def _press_key(key: str = "Enter"):
    """Press a keyboard key."""
    global _page
    if _page is None:
        return "Browser not open."
    if not key:
        key = "Enter"
    await _page.keyboard.press(key)
    return f"Pressed: {key}"


async def _read_page(max_chars: int = 2000):
    """Read the text content of the current page."""
    global _page
    if _page is None:
        return "Browser not open."
    try:
        text = await _page.inner_text("body")
        text = text.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return text if text else "(page has no text content)"
    except Exception as exc:
        return f"Read failed: {exc}"


async def _screenshot():
    """Take a screenshot of the current page."""
    global _page
    if _page is None:
        return "Browser not open."
    try:
        from datetime import datetime
        save_dir = Path.home() / "Pictures" / "GamaScreenshots"
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"gama_browser_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        await _page.screenshot(path=str(path), full_page=False)
        return f"Screenshot saved: {path}"
    except Exception as exc:
        return f"Screenshot failed: {exc}"


async def _scroll(direction: str = "down", amount: int = 500):
    """Scroll the page up or down."""
    global _page
    if _page is None:
        return "Browser not open."
    delta = -amount if direction.lower() == "up" else amount
    await _page.mouse.wheel(0, delta)
    return f"Scrolled {direction} by {amount}px"


async def _go_back():
    global _page
    if _page is None:
        return "Browser not open."
    await _page.go_back(timeout=10000)
    return "Went back."


async def _go_forward():
    global _page
    if _page is None:
        return "Browser not open."
    await _page.go_forward(timeout=10000)
    return "Went forward."


async def _close_browser():
    """Close the browser."""
    global _playwright_instance, _browser, _context, _page
    with _browser_lock:
        try:
            if _page:
                await _page.close()
            if _context:
                await _context.close()
            if _browser:
                await _browser.close()
            if _playwright_instance:
                await _playwright_instance.stop()
        except Exception:
            pass
        _page = None
        _context = None
        _browser = None
        _playwright_instance = None
    return "Browser closed."


__all__ = ["browser_control"]
