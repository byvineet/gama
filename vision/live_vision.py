"""
vision/live_vision.py — Gemini Live API continuous vision
==========================================================
Streams desktop screenshots and/or webcam frames into the active
Gemini Live session via send_realtime_input(video=JPEG).

Live API limit: max ~1 frame/second as image/jpeg (or png).
Camera UI preview runs at higher FPS (MJPEG + web_bridge frames)
so the HUD feels smooth while Gemini still gets 1 FPS for understanding.

Modes:
  desktop  — continuous screen capture → Live
  camera   — continuous webcam capture → Live + smooth HUD preview
  both     — camera preferred for Live frames; desktop on demand tool still works

Author : Vineet Machchal / Gama 2.0
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("gama.vision.live")

# Live API video guidance: JPEG frames, ~1 FPS max.
LIVE_FPS = 1.0
LIVE_INTERVAL = 1.0 / LIVE_FPS
# Smooth HUD preview (not sent to Live at this rate)
PREVIEW_FPS = 24.0
PREVIEW_INTERVAL = 1.0 / PREVIEW_FPS

# Reasonable size for Live (tokens + latency)
LIVE_MAX_W = 768
LIVE_MAX_H = 432
LIVE_JPEG_Q = 60

PREVIEW_MAX_W = 480
PREVIEW_JPEG_Q = 55

# Token / cost control: auto-disable continuous Live vision after this many
# seconds with no explicit re-enable or snapshot request.
VISION_IDLE_TIMEOUT_S = 90.0


class VisionMode(str, Enum):
    OFF = "off"
    DESKTOP = "desktop"
    CAMERA = "camera"
    BOTH = "both"


@dataclass
class VisionStatus:
    mode: str
    desktop_active: bool
    camera_active: bool
    frames_sent: int
    last_error: str
    camera_index: int


# Optional callback: async-safe; main registers session sender
FrameSender = Callable[[bytes, str], None]  # (jpeg_bytes, mime)


class LiveVisionEngine:
    """Background capture + Live frame injection + smooth camera preview."""

    def __init__(self) -> None:
        self._mode = VisionMode.OFF
        self._camera_index = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cap = None  # cv2.VideoCapture
        self._sender: Optional[FrameSender] = None
        self._frames_sent = 0
        self._last_error = ""
        self._last_live_ts = 0.0
        self._last_preview_ts = 0.0
        self._latest_live_jpeg: Optional[bytes] = None
        self._last_activity_ts = 0.0  # enable / snapshot / mode change
        self._frames_since_enable = 0

    # ── public API ─────────────────────────────────────────────────────

    def set_sender(self, sender: Optional[FrameSender]) -> None:
        """Register callable(jpeg_bytes, mime) invoked from the capture thread.
        The callable must be thread-safe / schedule onto the asyncio loop."""
        self._sender = sender

    @property
    def mode(self) -> VisionMode:
        return self._mode

    def status(self) -> VisionStatus:
        with self._lock:
            return VisionStatus(
                mode=self._mode.value,
                desktop_active=self._mode in (VisionMode.DESKTOP, VisionMode.BOTH),
                camera_active=self._mode in (VisionMode.CAMERA, VisionMode.BOTH),
                frames_sent=self._frames_sent,
                last_error=self._last_error,
                camera_index=self._camera_index,
            )

    def enable(self, mode: str = "camera", camera_index: int = 0) -> str:
        """Start continuous vision. mode: desktop | camera | both."""
        mode = (mode or "camera").strip().lower()
        if mode not in ("desktop", "camera", "both"):
            return "Unknown mode. Use desktop, camera, or both."
        with self._lock:
            self._camera_index = int(camera_index)
            self._mode = VisionMode(mode)
            self._last_error = ""
            self._last_activity_ts = time.monotonic()
            self._frames_since_enable = 0
        self._ensure_running()
        # Do NOT open the camera on this thread — that blocked the tool for
        # 80s+ on some Windows DSHOW/MSMF setups. The capture loop opens it.
        # Do NOT put OpenCV frames on the display canvas — the React HUD uses
        # the browser webcam (getUserMedia) for an instant, smooth preview.
        if mode in ("camera", "both"):
            self._notify_browser_camera(True)
            # Background OpenCV open is kicked by the capture loop
        else:
            self._close_camera()
            self._notify_browser_camera(False)
        log.info("[LiveVision] enabled mode=%s (browser HUD + background Live stream)", mode)
        if mode in ("camera", "both"):
            return (
                "Camera vision enabled. The HUD will use the browser webcam "
                "(permission prompt). Gemini receives vision frames in the background."
            )
        return (
            f"Live vision ON ({mode}). Gemini can see continuous desktop frames."
        )

    def disable(self) -> str:
        with self._lock:
            self._mode = VisionMode.OFF
        self._close_camera()
        self._notify_browser_camera(False)
        log.info("[LiveVision] disabled")
        return "Live vision OFF. Browser camera and background streaming stopped."

    def snapshot_desktop_jpeg(self) -> Optional[bytes]:
        """One-shot desktop JPEG for tools / fallback."""
        return self._capture_desktop_jpeg()

    def snapshot_camera_jpeg(self) -> Optional[bytes]:
        """One-shot camera JPEG."""
        self._last_activity_ts = time.monotonic()
        return self._capture_camera_jpeg(for_live=True)

    def snapshot_and_emit(self, source: str = "camera") -> Optional[bytes]:
        """Capture exact-moment JPEG and push to Live session (vision questions).

        Use for 'what am I holding?', 'what's on my screen?' so the model
        sees the precise frame, not just the next 1 FPS tick.
        """
        self._last_activity_ts = time.monotonic()
        src = (source or "camera").strip().lower()
        jpeg = None
        if src in ("desktop", "screen"):
            jpeg = self._capture_desktop_jpeg()
            label = "desktop"
        else:
            jpeg = self._capture_camera_jpeg(for_live=True)
            label = "camera"
            if jpeg is None:
                jpeg = self._capture_desktop_jpeg()
                label = "desktop"
        if jpeg:
            self._latest_live_jpeg = jpeg
            self._emit_live(jpeg, label)
        return jpeg

    # ── internals ──────────────────────────────────────────────────────

    def _ensure_running(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="live-vision", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        log.info("[LiveVision] capture loop started")
        while not self._stop.is_set():
            try:
                mode = self._mode
                if mode == VisionMode.OFF:
                    time.sleep(0.2)
                    continue

                now = time.monotonic()
                # Token/cost control: auto-disable after idle timeout
                last_act = float(getattr(self, "_last_activity_ts", 0) or 0)
                if last_act > 0 and (now - last_act) >= VISION_IDLE_TIMEOUT_S:
                    log.info(
                        "[LiveVision] idle timeout (%.0fs) — auto-disabling continuous vision",
                        VISION_IDLE_TIMEOUT_S,
                    )
                    try:
                        self.disable()
                    except Exception as _dis_exc:
                        log.debug("[LiveVision] idle disable failed: %s", _dis_exc)
                    continue
                # HUD preview is browser-side (getUserMedia). No OpenCV preview path.

                # Live path — max 1 FPS
                if now - self._last_live_ts >= LIVE_INTERVAL:
                    self._last_live_ts = now
                    jpeg = None
                    source = ""
                    if mode == VisionMode.CAMERA:
                        jpeg = self._capture_camera_jpeg(for_live=True)
                        source = "camera"
                    elif mode == VisionMode.DESKTOP:
                        jpeg = self._capture_desktop_jpeg()
                        source = "desktop"
                    else:  # BOTH — prefer camera for Live, desktop is still
                        # available via one-shot tools
                        jpeg = self._capture_camera_jpeg(for_live=True)
                        source = "camera"
                        if jpeg is None:
                            jpeg = self._capture_desktop_jpeg()
                            source = "desktop"

                    if jpeg:
                        self._latest_live_jpeg = jpeg
                        self._emit_live(jpeg, source)
                else:
                    time.sleep(0.02)
            except Exception as exc:
                self._last_error = str(exc)
                log.warning("[LiveVision] loop error: %s", exc)
                time.sleep(0.5)
        log.info("[LiveVision] capture loop stopped")

    def _emit_live(self, jpeg: bytes, source: str) -> None:
        sender = self._sender
        if not sender:
            return
        try:
            sender(jpeg, "image/jpeg")
            self._frames_sent += 1
            if self._frames_sent % 10 == 1:
                log.debug(
                    "[LiveVision] sent frame #%s (%s, %d bytes)",
                    self._frames_sent, source, len(jpeg),
                )
        except Exception as exc:
            self._last_error = str(exc)
            log.debug("[LiveVision] sender failed: %s", exc)

    def _open_camera(self) -> bool:
        """Open webcam with robust Windows backend/index fallback.

        DirectShow (CAP_DSHOW) often fails with "can't be used to capture by
        index" on some Realtek / laptop setups. We try MSMF + default API,
        then probe indices 0..3, and only accept a handle that can actually
        read a frame.
        """
        try:
            import cv2
            import sys
            self._close_camera()

            # Preferred backend order: DSHOW first on Windows (fastest to open and read without MSMF enumeration hang),
            # then ANY, then MSMF.
            if sys.platform == "win32":
                backends = [
                    ("DSHOW", getattr(cv2, "CAP_DSHOW", 700)),
                    ("ANY",  getattr(cv2, "CAP_ANY", 0)),
                    ("MSMF", getattr(cv2, "CAP_MSMF", 1400)),
                ]
            else:
                backends = [("ANY", 0)]

            # Prefer the requested index, then 0, 1
            idx_req = int(self._camera_index)
            indices = [idx_req]
            if 0 not in indices:
                indices.append(0)
            if 1 not in indices:
                indices.append(1)

            last_err = ""
            for idx in indices:
                for be_name, be in backends:
                    cap = None
                    try:
                        # Always pass backend id explicitly (including 0)
                        cap = cv2.VideoCapture(idx, be)
                    except Exception as exc:
                        last_err = f"{be_name}/{idx}: {exc}"
                        log.debug("[LiveVision] open try %s", last_err)
                        continue
                    if cap is None or not cap.isOpened():
                        if cap is not None:
                            try:
                                cap.release()
                            except Exception:
                                pass
                        last_err = f"{be_name}/{idx}: isOpened=False"
                        continue

                    # Configure for low latency (best-effort)
                    try:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                        cap.set(cv2.CAP_PROP_FPS, 30)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass

                    # Fast warm-up + verify a real frame (some backends open but never deliver)
                    ok_frame = False
                    frame = None
                    for _ in range(3):
                        try:
                            ret, frame = cap.read()
                        except Exception as exc:
                            last_err = f"{be_name}/{idx} read: {exc}"
                            ret, frame = False, None
                        if ret and frame is not None and getattr(frame, "size", 0) > 0:
                            ok_frame = True
                            break
                        time.sleep(0.01)

                    if not ok_frame:
                        try:
                            cap.release()
                        except Exception:
                            pass
                        last_err = f"{be_name}/{idx}: opened but no frames"
                        log.debug("[LiveVision] %s", last_err)
                        continue

                    self._cap = cap
                    self._camera_index = idx
                    self._last_error = ""
                    log.info(
                        "[LiveVision] camera opened index=%s backend=%s shape=%s",
                        idx, be_name, getattr(frame, "shape", None),
                    )
                    return True

            self._last_error = last_err or "Webcam not available"
            log.warning(
                "[LiveVision] could not open any camera (tried indices %s). Last: %s",
                indices, self._last_error,
            )
            return False
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("[LiveVision] camera open failed: %s", exc)
            return False

    def _close_camera(self) -> None:
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _capture_camera_jpeg(self, for_live: bool = False) -> Optional[bytes]:
        try:
            import cv2
            if self._cap is None or not self._cap.isOpened():
                if not self._open_camera():
                    return None
            ret, frame = self._cap.read()
            if not ret or frame is None:
                # one retry after reopen
                self._open_camera()
                if self._cap is None:
                    return None
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    return None
            # Mirror (selfie) view — horizontal flip
            frame = cv2.flip(frame, 1)
            max_w = LIVE_MAX_W if for_live else PREVIEW_MAX_W
            h, w = frame.shape[:2]
            if w > max_w:
                scale = max_w / float(w)
                frame = cv2.resize(
                    frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
                )
            q = LIVE_JPEG_Q if for_live else PREVIEW_JPEG_Q
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
            if not ok:
                return None
            return buf.tobytes()
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def _capture_camera_bgr(self):
        """Return BGR ndarray for HUD preview, or None."""
        try:
            import cv2
            if self._cap is None or not self._cap.isOpened():
                if not self._open_camera():
                    return None
            ret, frame = self._cap.read()
            if not ret or frame is None:
                return None
            # Mirror (selfie) view — horizontal flip
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            if w > PREVIEW_MAX_W:
                scale = PREVIEW_MAX_W / float(w)
                frame = cv2.resize(
                    frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
                )
            return frame
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def _capture_desktop_jpeg(self) -> Optional[bytes]:
        try:
            from PIL import Image
            import pyautogui

            img = pyautogui.screenshot()
            if img.width > LIVE_MAX_W:
                ratio = LIVE_MAX_W / float(img.width)
                img = img.resize(
                    (LIVE_MAX_W, max(1, int(img.height * ratio))), Image.LANCZOS
                )
            # Cap height too
            if img.height > LIVE_MAX_H:
                ratio = LIVE_MAX_H / float(img.height)
                img = img.resize(
                    (max(1, int(img.width * ratio)), LIVE_MAX_H), Image.LANCZOS
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=LIVE_JPEG_Q, optimize=True)
            return buf.getvalue()
        except Exception as exc:
            self._last_error = str(exc)
            log.debug("[LiveVision] desktop capture failed: %s", exc)
            return None

    def _push_preview_frame(self) -> None:
        # Intentionally empty: HUD uses browser getUserMedia, not OpenCV frames.
        # Background Live path uses _capture_camera_jpeg → _emit_live only.
        return

    def _notify_browser_camera(self, enabled: bool) -> None:
        """Tell the React HUD to open/close the *browser* webcam (getUserMedia).

        Backend OpenCV frames are NOT shown on the display — that path was laggy.
        Gemini still receives background frames from the capture loop.
        """
        try:
            from core.web_bridge import push_camera_vision
            push_camera_vision(bool(enabled))
        except Exception as exc:
            log.debug("[LiveVision] push_camera_vision: %s", exc)
        # Keep gesture flag in sync for any legacy UI that still reads it
        try:
            from core.web_bridge import push_gesture_state
            push_gesture_state(False, "")  # never use gesture MJPEG panel for Live vision
        except Exception:
            pass

    def shutdown(self) -> None:
        self._stop.set()
        self.disable()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)


# Module singleton
_engine: Optional[LiveVisionEngine] = None


def get_live_vision() -> LiveVisionEngine:
    global _engine
    if _engine is None:
        _engine = LiveVisionEngine()
    return _engine


def live_vision_action(
    action: str = "status",
    mode: str = "camera",
    camera_index: int = 0,
) -> str:
    """Tool entrypoint."""
    eng = get_live_vision()
    action = (action or "status").strip().lower()
    if action in ("enable", "start", "on"):
        return eng.enable(mode=mode or "camera", camera_index=int(camera_index or 0))
    if action in ("enable_camera",):
        return eng.enable(mode="camera", camera_index=int(camera_index or 0))
    if action in ("enable_desktop",):
        return eng.enable(mode="desktop", camera_index=int(camera_index or 0))
    if action in ("enable_both",):
        return eng.enable(mode="both", camera_index=int(camera_index or 0))
    if action in ("disable", "stop", "off"):
        return eng.disable()
    if action in ("snapshot", "look", "capture", "see_now"):
        # Exact-moment frame for vision questions ("what am I holding?", "what do you see?")
        src = mode if mode in ("camera", "desktop", "both", "screen") else "camera"
        if src == "both":
            src = "camera"
        jpeg = eng.snapshot_and_emit(source=src)
        if jpeg:
            return (
                f"Exact-moment {src} frame ({len(jpeg)} bytes) captured and injected into the Live session. "
                "Look at the camera feed and answer Sir's question truthfully based ONLY on what is clearly visible in the frame. "
                "If Sir is not holding anything or the hands/view are empty, state clearly that you do not see anything being held. "
                "Never invent, assume, or guess objects."
            )
        return (
            "Could not capture a frame from the camera. The camera may be disconnected, disabled, or in use by another app. "
            "Inform Sir that the camera feed is unavailable rather than guessing."
        )
    if action in ("status", "state"):
        s = eng.status()
        return (
            f"Live vision mode={s.mode}, desktop={s.desktop_active}, "
            f"camera={s.camera_active}, frames_sent={s.frames_sent}"
            + (f", last_error={s.last_error}" if s.last_error else "")
        )
    if action in ("enable_camera", "camera"):
        return eng.enable(mode="camera", camera_index=int(camera_index or 0))
    if action in ("enable_desktop", "desktop", "screen"):
        return eng.enable(mode="desktop")
    if action in ("enable_both", "both"):
        return eng.enable(mode="both", camera_index=int(camera_index or 0))
    return (
        "Unknown live_vision action. Use: enable, disable, status, "
        "enable_camera, enable_desktop, enable_both."
    )


__all__ = [
    "LiveVisionEngine",
    "VisionMode",
    "get_live_vision",
    "live_vision_action",
]
