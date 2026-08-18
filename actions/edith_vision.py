"""
actions/edith_vision.py — E.D.I.T.H. Tactical Vision & Screen OCR Engine
========================================================================
Next-generation tactical vision system for Gama 2.0 (JARVIS & E.D.I.T.H. tier).

Capabilities:
  1. Active Window & Screen Capture (MSS / PyAutoGUI fallback)
  2. Local Fast OCR (Extracts raw screen text instantly on-device)
  3. Deep Multimodal Visual Analysis via Gemini 3.5 Flash
  4. Tactical HUD Target Overlay Signaling (draws E.D.I.T.H. corner brackets)

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

log = get_logger(__name__)
logger = log  # back-compat alias
IMG_MAX_W = 1280
IMG_MAX_H = 720
JPEG_Q = 65


BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

def _get_api_key() -> str:
    try:
        if API_CONFIG_PATH.exists():
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("gemini_api_key", "")
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "")


def get_active_window_bounds() -> Optional[Tuple[int, int, int, int]]:
    """Return (left, top, width, height) of active foreground window on Windows."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            rect = win32gui.GetWindowRect(hwnd)
            x, y, r, b = rect
            w = max(100, r - x)
            h = max(100, b - y)
            return (x, y, w, h)
    except Exception:
        pass
    return None


def extract_local_ocr_text(image) -> str:
    """Best-effort fast local OCR on-device."""
    try:
        import pytesseract
        text = pytesseract.image_to_string(image)
        if text and len(text.strip()) > 5:
            return text.strip()
    except Exception:
        pass
    return ""


def edith_analyze_screen(prompt: str = "What am I looking at right now?", target_window_only: bool = False) -> str:
    """E.D.I.T.H. Tactical Vision entrypoint.
    
    Captures screen frame, triggers target HUD overlay on active window,
    performs local text extraction, and uses Gemini 3.5 Flash for visual analysis.
    """
    logger.info(f"[EDITH-Vision] Processing query: '{prompt}'")
    try:
        from PIL import Image
        import pyautogui

        # 1. Capture screen (prefer live-vision desktop frame when streaming)
        bounds = get_active_window_bounds()
        full_img = None
        try:
            from vision.live_vision import get_live_vision
            st = get_live_vision().status()
            if st.desktop_active or st.mode in ("desktop", "both"):
                jpeg = get_live_vision().snapshot_desktop_jpeg()
                if jpeg:
                    full_img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception:
            full_img = None
        if full_img is None:
            full_img = pyautogui.screenshot()

        # 2. Crop to active window if requested and valid
        if target_window_only and bounds:
            x, y, w, h = bounds
            crop_box = (max(0, x), max(0, y), min(full_img.width, x + w), min(full_img.height, y + h))
            img = full_img.crop(crop_box)
        else:
            img = full_img

        # 3. Resize for optimal VLM token efficiency
        if img.width > IMG_MAX_W:
            ratio = IMG_MAX_W / img.width
            img = img.resize((IMG_MAX_W, int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q)
        image_bytes = buf.getvalue()

        # 4. Trigger E.D.I.T.H. UI Target Brackets if active UI exists
        try:
            from ui import GamaUI
            if bounds:
                GamaUI.show_edith_target(*bounds, label="E.D.I.T.H. TARGET")
        except Exception:
            pass

        # 5. Local OCR check (for instant text matching if prompt asks for text/code)
        ocr_snippet = extract_local_ocr_text(img)

        # 6. Deep Multimodal Analysis via Gemini Flash
        api_key = _get_api_key()
        if not api_key:
            if ocr_snippet:
                return f"[E.D.I.T.H. OCR Text Detected]:\n{ocr_snippet}"
            return "Gemini API key not configured for visual analysis, Sir."

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        
        enhanced_prompt = (
            f"[TACTICAL E.D.I.T.H. VISION]\n"
            f"Analyze the attached screen capture for the user request: '{prompt}'.\n"
            f"Be precise, concise, and smart (JARVIS/E.D.I.T.H. tone).\n"
        )
        if ocr_snippet:
            enhanced_prompt += f"\nLocal OCR Text Hint:\n{ocr_snippet[:1000]}\n"

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                enhanced_prompt,
            ],
        )

        result = (response.text or "").strip()
        if not result and ocr_snippet:
            return f"[E.D.I.T.H. OCR]: {ocr_snippet}"
        return result or "No visual features identified."

    except Exception as exc:
        logger.error(f"[EDITH-Vision] Vision processing failed: {exc}", exc_info=True)
        return f"E.D.I.T.H. Vision encountered an error: {exc}"


__all__ = ["edith_analyze_screen", "get_active_window_bounds", "extract_local_ocr_text"]
