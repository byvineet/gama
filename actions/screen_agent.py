"""
actions/screen_agent.py — Gama Visual Screen Agent
=====================================================
See the screen, find UI elements, click them, and read the result.

Pipeline (for a task like "check notifications on PW"):
  1. Optionally open a URL or app first.
  2. Wait for the UI to settle.
  3. Capture a FULL-RESOLUTION screenshot (1280×800 — NOT scaled, so the
     pixel coordinates Gemini returns map 1-to-1 to real screen pixels).
  4. Ask Gemini Vision to locate the target element → returns (x, y).
  5. Click via pyautogui.
  6. Repeat for follow-up elements (e.g. a submenu that opens after click).
  7. Final screenshot → ask Gemini to READ and summarise the content.
  8. Return the text for Gama to speak.

Author: Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import io
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

log = get_logger(__name__)
logger = log  # back-compat alias
# Screen resolution (user's machine). Used in vision prompts so Gemini
# knows the coordinate space.
def _detect_screen_size() -> tuple[int, int]:
    """Return active display size; fall back to 1280×800."""
    try:
        import pyautogui
        w, h = pyautogui.size()
        if w and h:
            return int(w), int(h)
    except Exception:
        pass
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[0]  # virtual full desktop
            w, h = int(mon.get("width") or 0), int(mon.get("height") or 0)
            if w and h:
                return w, h
    except Exception:
        pass
    return 1280, 800


SCREEN_W, SCREEN_H = _detect_screen_size()

# Max JPEG quality for the full-res "find element" shot — higher than
# screen_processor.py (which downsamples) because we need readable text
# and sharp icon edges for accurate coordinate detection.
JPEG_Q_LOCATE = 80   # locating — quality matters for small icons
JPEG_Q_READ   = 65   # reading content — can compress a bit more




def _get_api_key() -> str:
    try:
        cfg = _get_base_dir() / "config" / "api_keys.json"
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Core screenshot helper
# ---------------------------------------------------------------------------

def _capture(jpeg_quality: int = JPEG_Q_LOCATE) -> Optional[bytes]:
    """Return a JPEG-encoded screenshot at full screen resolution, or None."""
    try:
        import pyautogui
        from PIL import Image

        img: Image.Image = pyautogui.screenshot()
        # Ensure we're at the declared resolution (handles fractional DPI).
        if img.width != SCREEN_W or img.height != SCREEN_H:
            img = img.resize((SCREEN_W, SCREEN_H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        return buf.getvalue()
    except Exception as exc:
        logger.error(f"Screen capture failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Vision helpers (call Gemini's vision model synchronously)
# ---------------------------------------------------------------------------

def _gemini_vision(image_bytes: bytes, prompt: str) -> str:
    """Send a screenshot + prompt to Gemini 3.5 Flash and return the text."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_get_api_key())
        resp = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt,
            ],
        )
        return (resp.text or "").strip()
    except Exception as exc:
        logger.error(f"Gemini vision call failed: {exc}")
        return f"ERROR: {exc}"


_LOCATE_SYSTEM = (
    f"You are looking at a Windows screenshot ({SCREEN_W}×{SCREEN_H} pixels). "
    "Your ONLY job is to return a JSON object — nothing else, no markdown, no explanation. "
    "Format: "
    '{"found": true, "x": <int>, "y": <int>, "label": "<what you see>", "confidence": <0.0-1.0>} '
    "or if not found: "
    '{"found": false, "x": 0, "y": 0, "label": "not found", "confidence": 0.0}'
)


def _find_element(description: str, image_bytes: bytes) -> Tuple[int, int, bool, str]:
    """Ask Gemini where a UI element is. Returns (x, y, found, label)."""
    prompt = (
        f"{_LOCATE_SYSTEM}\n\n"
        f"Find this element: {description}\n"
        f"Return the CENTER pixel coordinates of that element within the {SCREEN_W}×{SCREEN_H} image."
    )
    raw = _gemini_vision(image_bytes, prompt)

    # Extract JSON — Gemini sometimes wraps in ```json ... ```
    json_match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
    if not json_match:
        logger.warning(f"No JSON in vision response: {raw[:200]}")
        return 0, 0, False, "parse error"
    try:
        data = json.loads(json_match.group())
        found = bool(data.get("found", False))
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        label = str(data.get("label", ""))
        # Clamp to screen bounds
        x = max(1, min(x, SCREEN_W - 1))
        y = max(1, min(y, SCREEN_H - 1))
        return x, y, found, label
    except Exception as exc:
        logger.warning(f"JSON parse error in _find_element: {exc} — raw: {raw[:200]}")
        return 0, 0, False, "parse error"


def _read_screen_content(image_bytes: bytes, prompt: str) -> str:
    """Ask Gemini to read / summarise content visible on screen."""
    full_prompt = (
        f"You are looking at a Windows screenshot ({SCREEN_W}×{SCREEN_H} pixels). "
        f"{prompt}"
    )
    return _gemini_vision(image_bytes, full_prompt)


# ---------------------------------------------------------------------------
# Click helper
# ---------------------------------------------------------------------------

def _click(x: int, y: int, double: bool = False) -> bool:
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        if double:
            pyautogui.doubleClick(x, y)
        else:
            pyautogui.click(x, y)
        logger.info(f"Clicked {'double' if double else 'single'} at ({x}, {y})")
        return True
    except Exception as exc:
        logger.error(f"Click at ({x}, {y}) failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Playwright URL opener — replaces hard sleep(3.5) with networkidle wait.
# The Playwright Chromium window stays open so pyautogui can screenshot it.
# Falls back to webbrowser + a short heuristic sleep when Playwright is not
# installed or fails (e.g. browsers not downloaded yet).
# ---------------------------------------------------------------------------
_pw_instance = None   # playwright.sync_api.Playwright  (kept alive)
_pw_browser  = None   # playwright.sync_api.Browser     (kept alive)


def _open_url_playwright(url: str, load_timeout_ms: int = 8_000,
                          idle_timeout_ms: int = 5_000) -> bool:
    """Navigate to *url* in a persistent Playwright Chromium window.

    Waits for ``domcontentloaded`` first (hard requirement), then tries
    ``networkidle`` with a softer timeout — a partial load is still useful
    because pyautogui will screenshot whatever rendered.

    Returns True on success, False on any error.
    """
    global _pw_instance, _pw_browser
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as _PWTimeout

        if _pw_browser is None or not _pw_browser.is_connected():
            # Start a new persistent instance — keep it alive for the process
            # lifetime so subsequent calls reuse the same window.
            _pw_instance = sync_playwright().start()
            _pw_browser = _pw_instance.chromium.launch(
                headless=False,
                args=["--start-maximized", "--disable-extensions"],
            )
            logger.info("Playwright Chromium launched")

        page = _pw_browser.new_page()
        page.goto(url, timeout=load_timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
        except _PWTimeout:
            logger.debug("Playwright networkidle timeout — proceeding with partial load")
        logger.info(f"Playwright opened {url}")
        return True
    except Exception as exc:
        logger.warning(f"Playwright unavailable ({exc!r}); falling back to webbrowser")
        return False


def _open_url(url: str) -> None:
    """Open a URL and wait for it to load.

    Tries Playwright first (smart networkidle wait, no fixed sleep).
    Falls back to the default browser + a short heuristic sleep when
    Playwright is not installed.
    """
    if _open_url_playwright(url):
        return
    # Fallback — webbrowser + short adaptive sleep
    try:
        import webbrowser
        webbrowser.open(url)
        logger.info(f"Opened URL via webbrowser: {url}")
        time.sleep(1.2)   # reduced from 3.5s; Playwright path needs no sleep
    except Exception as exc:
        logger.warning(f"Could not open URL {url}: {exc}")


def _open_app_name(app: str) -> None:
    try:
        from actions.open_app import open_app
        open_app(app)
    except Exception as exc:
        logger.warning(f"Could not open app '{app}': {exc}")


# ---------------------------------------------------------------------------
# High-level actions
# ---------------------------------------------------------------------------

def _action_find_and_click(element: str, double: bool = False) -> str:
    """Capture screen, find element, click it."""
    img = _capture(JPEG_Q_LOCATE)
    if img is None:
        return "Could not take screenshot."
    x, y, found, label = _find_element(element, img)
    if not found:
        return f"Could not find '{element}' on screen."
    ok = _click(x, y, double=double)
    if ok:
        return f"Found '{label}' at ({x}, {y}) and clicked it."
    return f"Found '{label}' at ({x}, {y}) but click failed."


def _action_read_screen(prompt: str) -> str:
    """Capture screen and read/summarise content."""
    img = _capture(JPEG_Q_READ)
    if img is None:
        return "Could not take screenshot."
    return _read_screen_content(img, prompt)


def _action_screenshot_and_describe(prompt: str) -> str:
    img = _capture(JPEG_Q_READ)
    if img is None:
        return "Could not take screenshot."
    return _read_screen_content(img, prompt or "Describe everything visible on the screen.")


# ---------------------------------------------------------------------------
# Visual task orchestrator
# ---------------------------------------------------------------------------

# Known site shortcuts so Gemini doesn't have to guess the URL.
_SITE_MAP = {
    "pw":           "https://www.pw.live/study-v2/study",
    "physics wallah": "https://www.pw.live/study-v2/study",
    "physicswallah": "https://www.pw.live/study-v2/study",
    "youtube":      "https://www.youtube.com",
    "gmail":        "https://mail.google.com",
    "github":       "https://github.com",
    "google":       "https://www.google.com",
    "spotify":      "https://open.spotify.com",
    "notion":       "https://www.notion.so",
}

# Step plans: map a task keyword pattern to an ordered list of
# (description_to_find, action, read_prompt_after_click) tuples.
# "description_to_find" can be None to skip the find-and-click step
# and go straight to reading.
_TASK_PLANS = [
    # notifications
    (re.compile(r"\bnotif", re.I), [
        ("notification bell icon or notification icon or alerts button", "click"),
        (None, "read", "List ALL notification text visible — titles, messages, counts. "
                       "If there are no notifications say so clearly."),
    ]),
    # messages / DMs
    (re.compile(r"\b(message|dm|chat|inbox)\b", re.I), [
        ("messages icon or chat icon or inbox icon", "click"),
        (None, "read", "List all message previews or conversations visible."),
    ]),
    # profile
    (re.compile(r"\bprofile\b", re.I), [
        ("profile picture or avatar or user icon", "click"),
        (None, "read", "Describe the profile information shown."),
    ]),
    # search
    (re.compile(r"\bsearch\b", re.I), [
        ("search bar or search icon", "click"),
    ]),
]


def _resolve_site_url(text: str) -> Optional[str]:
    t = text.lower().strip()
    for key, url in _SITE_MAP.items():
        if key in t:
            return url
    return None


def _resolve_task_plan(task: str):
    for pat, plan in _TASK_PLANS:
        if pat.search(task):
            return plan
    return None


def _action_visual_task(
    task: str,
    url: str = "",
    app: str = "",
    steps: int = 3,
    extra_element: str = "",
) -> str:
    """
    Full autonomous pipeline:
      [open URL/app] → screenshot → find element → click →
      [screenshot → find next element → click] → screenshot → read
    """
    steps = max(1, min(int(steps), 6))
    log = []

    # ── 1. Open URL or app if specified ──────────────────────────────────────
    target_url = url or _resolve_site_url(task)
    if target_url:
        _open_url(target_url)
        log.append(f"Opened {target_url}.")
        # Playwright path already waited for networkidle — no extra sleep needed.
        # Webbrowser fallback already did a 1.2s sleep inside _open_url.
    elif app:
        _open_app_name(app)
        log.append(f"Opened {app}.")
        time.sleep(0.8)  # reduced from 2.5s; apps are already running, just foregrounded

    # ── 2. Determine step plan ────────────────────────────────────────────────
    plan = _resolve_task_plan(task)
    if plan is None:
        # Generic: use Gemini to understand what to do from a single screenshot
        img = _capture(JPEG_Q_LOCATE)
        if img is None:
            return "Could not take screenshot."
        # Ask Gemini what element to interact with for this task
        guide_prompt = (
            f"I need to: {task}\n"
            f"Looking at this {SCREEN_W}×{SCREEN_H} screenshot, what is the FIRST UI element "
            "I should click to accomplish this task? Reply with ONLY a short description of "
            "that element (e.g. 'notification bell icon', 'search bar', 'settings gear'). "
            "If no click is needed and information is already visible, reply EXACTLY: READ_ONLY"
        )
        guide = _gemini_vision(img, guide_prompt).strip().strip('"\'')
        log.append(f"Vision guidance: {guide}")

        if guide.upper() == "READ_ONLY" or guide.upper().startswith("READ"):
            result = _read_screen_content(img,
                f"The user asked: '{task}'. Read and summarise the relevant information "
                "visible on the screen. Be thorough but concise."
            )
            return "\n".join(log) + "\n\n" + result

        # Build a simple one-click plan from the guidance
        plan = [
            (guide, "click"),
            (None, "read",
             f"The user asked: '{task}'. Now that I have clicked, "
             "read and summarise ALL relevant information visible. "
             "If a panel/dropdown opened, list its contents in full."),
        ]

    # ── 3. Execute the plan ───────────────────────────────────────────────────
    final_read_result = ""
    for step in plan[:steps]:
        if step[0] is not None:
            # Find-and-click step
            element_desc = step[0]
            img = _capture(JPEG_Q_LOCATE)
            if img is None:
                log.append("Screenshot failed — aborting.")
                break
            x, y, found, label = _find_element(element_desc, img)
            if not found:
                log.append(f"Could not find '{element_desc}' — skipping step.")
                continue
            log.append(f"Found '{label}' at ({x},{y}).")
            _click(x, y)
            time.sleep(0.35)  # reduced from 1.2s; UI animations typically complete in <300ms
        else:
            # Read-only step
            read_prompt = step[2] if len(step) > 2 else (
                f"User asked: '{task}'. Summarise all relevant information visible."
            )
            img = _capture(JPEG_Q_READ)
            if img is None:
                log.append("Screenshot failed on read step.")
                break
            final_read_result = _read_screen_content(img, read_prompt)

    if not final_read_result:
        # Always end with a final read if no explicit read step ran
        img = _capture(JPEG_Q_READ)
        if img:
            final_read_result = _read_screen_content(
                img,
                f"User asked: '{task}'. Summarise all relevant information now visible "
                "after the actions taken."
            )

    prefix = "  ".join(log)
    return (prefix + "\n\n" + final_read_result).strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def screen_agent(action: str = "visual_task", **kwargs) -> str:
    """
    Visual screen agent — see the screen, find UI elements, click them,
    read the result.

    Actions
    -------
    visual_task          Full autonomous pipeline: [open URL/app] → find
                         element → click → read result. Use for natural-language
                         tasks like 'check notifications on PW'.
    find_and_click       Screenshot → find element by description → click.
    read_screen          Screenshot → read/summarise specific content.
    screenshot_and_describe  Basic screen describe (like screen_process but
                         at full 1280×800 resolution).
    """
    action = (action or "visual_task").lower().strip()

    if action == "visual_task":
        return _action_visual_task(
            task=kwargs.get("task", ""),
            url=kwargs.get("url", ""),
            app=kwargs.get("app", ""),
            steps=int(kwargs.get("steps", 3)),
        )

    if action == "find_and_click":
        return _action_find_and_click(
            element=kwargs.get("element", kwargs.get("description", "")),
            double=bool(kwargs.get("double", False)),
        )

    if action == "read_screen":
        return _action_read_screen(
            prompt=kwargs.get("prompt",
                "Read and summarise all important information visible on screen.")
        )

    if action == "screenshot_and_describe":
        return _action_screenshot_and_describe(kwargs.get("prompt", ""))

    return (f"Unknown screen_agent action: '{action}'. "
            "Use: visual_task, find_and_click, read_screen, screenshot_and_describe.")


__all__ = ["screen_agent"]
