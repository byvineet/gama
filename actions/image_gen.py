"""
actions/image_gen.py — AI Image Generation for Gama
=====================================================
Two-tier image generation:
  1. Primary   — Gemini's native image model (gemini-3.1-flash-lite-image),
                 called via the same google-genai client used for
                 Live/routing/TTS. Requires GEMINI_API_KEY.
  2. Fallback  — Pollinations AI's free image endpoint (no API key, no
                 quota). Used automatically if the Gemini call fails for
                 any reason (no key, quota, network, model error).

Result is saved (Desktop by default) and shown on the Gama display stage
unless open_file=True is requested.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = get_logger(__name__)
# Primary backend — Gemini native image generation model. Overridable via
# .env for future model swaps without a code change.
_GEMINI_IMAGE_MODEL = os.environ.get("IMAGE_GEN_MODEL", "gemini-3.1-flash-lite-image").strip() or "gemini-3.1-flash-lite-image"

# Maximum prompt length accepted by Pollinations (conservative)
_MAX_PROMPT_LEN = 1500

# Default image settings — only changed if the user explicitly asks
_DEFAULT_WIDTH = 1024
_DEFAULT_HEIGHT = 1024
_DEFAULT_SEED = 42


def _desktop_path() -> Path:
    """Return the user's Desktop path, cross-platform."""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            desktop, _ = winreg.QueryValueEx(key, "Desktop")
            winreg.CloseKey(key)
            p = Path(desktop)
            if p.exists():
                return p
        except Exception:
            pass
    return Path.home() / "Desktop"


def _safe_filename(prompt: str) -> str:
    """Turn a prompt into a safe filename stem."""
    stem = re.sub(r"[^\w\s-]", "", prompt.lower())
    stem = re.sub(r"[\s_-]+", "_", stem).strip("_")
    stem = stem[:60] or "image"
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"gama_{stem}_{ts}.png"


def _open_file(path: Path) -> None:
    """Open a file with the system default viewer."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        log.warning(f"[image_gen] Could not open file: {exc}")


def _pollinations_url(prompt: str, width: int, height: int, seed: int) -> str:
    """Build the Pollinations GET URL for a prompt."""
    encoded = urllib.parse.quote(prompt, safe="")
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&seed={seed}&nologo=true&negative_prompt=blurry,low+quality,watermark,text"
    )


def _get_gemini_api_key() -> str:
    try:
        from core.config_manager import config as _cfg
        return _cfg.gemini_key() or ""
    except Exception as exc:
        log.debug(f"[image_gen] Could not read Gemini API key: {exc}")
        return ""


def _generate_via_gemini(prompt: str) -> Optional[bytes]:
    """Try the primary backend: Gemini's native image model. Returns raw
    image bytes on success, or None on any failure (caller falls back to
    Pollinations)."""
    try:
        from google import genai

        api_key = _get_gemini_api_key()
        if not api_key:
            log.debug("[image_gen] No Gemini API key — skipping primary backend.")
            return None

        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        response = client.models.generate_content(
            model=_GEMINI_IMAGE_MODEL,
            contents=prompt,
        )

        for part in response.candidates[0].content.parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data

        log.warning("[image_gen] Gemini image response contained no image data.")
        return None
    except Exception as exc:
        log.warning(f"[image_gen] Gemini image generation failed, falling back: {exc}")
        return None


def _generate_via_pollinations(prompt: str, width: int, height: int) -> Optional[bytes]:
    """Fallback backend: Pollinations AI free image endpoint. Returns raw
    image bytes on success, or None on failure."""
    url = _pollinations_url(prompt, width, height, _DEFAULT_SEED)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/125.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            image_bytes = resp.read()
        if not image_bytes or len(image_bytes) < 1024:
            log.warning("[image_gen] Pollinations returned an empty/too-small image.")
            return None
        return image_bytes
    except Exception as exc:
        log.error(f"[image_gen] Pollinations request failed: {exc}")
        return None


def generate_image(
    prompt: str,
    speak_fn: Optional[Callable[[str], None]] = None,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    open_file: bool = False,
    show_on_canvas: bool = True,
) -> str:
    """Generate an image from *prompt*, save it, and show on the display stage.

    Tries the Gemini native image model first; if that fails for any
    reason (no key, quota, network, model error), falls back to
    Pollinations AI automatically — the caller never sees the difference.

    Args:
        prompt:          Text description of the image to generate.
        speak_fn:        Optional TTS callback.
        width / height:  Pixel size (Pollinations fallback; Gemini uses model defaults).
        open_file:       If True, also open in the system image viewer.
        show_on_canvas:  If True (default), push to display_stage as a movable/resizable card.

    Returns:
        A human-readable result string for the Gemini session.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "No prompt provided. Please describe the image you'd like me to generate."

    if len(prompt) > _MAX_PROMPT_LEN:
        prompt = prompt[:_MAX_PROMPT_LEN]

    # Note: the caller (main.py) already speaks an initial "Generating your
    # image now, sir." ack via _TOOL_ACK_MAP. We skip the duplicate start
    # announcement and only speak at completion.

    image_bytes = _generate_via_gemini(prompt)
    source = "gemini"
    if not image_bytes:
        image_bytes = _generate_via_pollinations(prompt, width, height)
        source = "pollinations"

    if not image_bytes:
        _say(speak_fn, "I encountered an error while generating the image, Sir.")
        return "Image generation failed on both the primary and fallback backends."

    # ── Save to Desktop ──
    desktop = _desktop_path()
    desktop.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(prompt)
    save_path = desktop / filename

    try:
        save_path.write_bytes(image_bytes)
    except Exception as exc:
        log.error(f"[image_gen] Failed to save image: {exc}")
        _say(speak_fn, "I generated the image but could not save it to the Desktop, Sir.")
        return f"Image generated but save failed: {exc}"

    log.info(f"[image_gen] Saved ({source}) → {save_path}")

    shown = False
    if show_on_canvas:
        try:
            from actions.display_stage import display_stage
            import time as _t
            sid = f"gen-image-{int(_t.time() * 1000) % 10_000_000}"
            display_stage(
                action="image",
                path=str(save_path),
                caption=(prompt[:80] + ("…" if len(prompt) > 80 else "")),
                scene_id=sid,
                title="Generated image",
            )
            shown = True
        except Exception as exc:
            log.warning(f"[image_gen] canvas display failed: {exc}")

    if open_file:
        _say(speak_fn, "Done, sir. Opening your image now.", kind="narrator")
        _open_file(save_path)
        return (
            f"Image generated and saved to Desktop as '{filename}'. "
            f"Opened in the system viewer"
            + (" and shown on the display stage." if shown else ".")
        )

    _say(speak_fn, "Done, sir. Your image is on the display.", kind="narrator")
    return (
        f"Image generated and saved to Desktop as '{filename}'. "
        + ("Shown on the display stage (drag to move, corner to resize). "
           "Say 'open the image' if you want it in the system viewer."
           if shown else
           "Could not push to the display stage — file is on the Desktop.")
    )


def _say(
    speak_fn: Optional[Callable[[str], None]],
    text: str,
    *,
    kind: str = "ack",
) -> None:
    """Fire the TTS callback if provided, silently on failure.

    Supports both simple ``speak_fn(text)`` signatures and the routed
    ``speak_fn(text, kind=...)`` signature used by the main handler.
    """
    if speak_fn is None:
        return
    try:
        try:
            speak_fn(text, kind=kind)
        except TypeError:
            speak_fn(text)
    except Exception as exc:
        log.debug(f"[image_gen] speak_fn error: {exc}")


__all__ = ["generate_image"]
