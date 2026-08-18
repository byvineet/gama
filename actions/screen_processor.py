"""
actions/screen_processor.py — Gama Screen & Webcam Vision (Mark style)
Real-time screen analysis and webcam vision via Gemini.

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

log = get_logger(__name__)
logger = log  # back-compat alias
try:
    from context_engine import publish_context_event, set_activity
except Exception:  # context_engine is additive — vision must work without it
    def publish_context_event(*a, **k): pass
    def set_activity(*a, **k): pass

LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
IMG_MAX_W = 640
IMG_MAX_H = 360
JPEG_Q = 55




BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def screen_process(prompt: str = "What's on my screen?") -> str:
    """Capture screen and analyze with E.D.I.T.H. Tactical Vision Engine."""
    set_activity("ANALYZING_SCREEN")
    try:
        from actions.edith_vision import edith_analyze_screen
        result = edith_analyze_screen(prompt=prompt)
        publish_context_event("VisionCompleted", source="edith_screen", ok=True)
        return result
    except Exception as exc:
        publish_context_event("VisionCompleted", source="edith_screen", ok=False, error=str(exc))
        return f"E.D.I.T.H. Vision analysis failed: {exc}"
    finally:
        set_activity("NONE")


def webcam_process(prompt: str = "What do you see?") -> str:
    """Capture webcam frame and analyze with Gemini.

    Prefer the continuous Live vision frame when camera mode is already on
    (faster, no open/close of the device). Otherwise one-shot capture.
    For ongoing awareness, prefer the live_vision tool instead.
    """
    set_activity("LOOKING_CAMERA")
    try:
        from google import genai
        from google.genai import types

        image_bytes = None
        # Prefer live stream frame (no extra camera open)
        try:
            from vision.live_vision import get_live_vision
            eng = get_live_vision()
            st = eng.status()
            if st.camera_active:
                image_bytes = eng.snapshot_camera_jpeg()
        except Exception:
            image_bytes = None

        if not image_bytes:
            import cv2
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW if os.name == "nt" else 0)
            if not cap.isOpened():
                return "Webcam not available. Try live_vision action=enable_camera first."
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return "Webcam capture failed."
            ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
            if not ret:
                return "Webcam encode failed."
            image_bytes = buf.tobytes()

        api_key = _get_api_key()
        if not api_key:
            return "Gemini API key not configured."

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt or "What do you see?",
            ],
        )
        result = (response.text or "").strip()
        publish_context_event("VisionCompleted", source="webcam", ok=True)
        return result or "Nothing notable in the camera view."
    except Exception as exc:
        publish_context_event("VisionCompleted", source="webcam", ok=False, error=str(exc))
        return f"Webcam vision failed: {exc}"
    finally:
        set_activity("NONE")


