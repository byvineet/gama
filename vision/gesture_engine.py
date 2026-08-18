"""
vision/gesture_engine.py — Hand gesture recognition for Gama
=============================================================
Uses MediaPipe Hands when available for accurate 21-point landmarks,
with an OpenCV contour fallback. Draws skeleton (lines + dots) on the
camera frame for the UI overlay.

Supported gestures (music-oriented):
  OPEN_PALM   → pause
  FIST        → play / resume
  THUMBS_UP   → volume up
  THUMBS_DOWN → volume down
  POINT_RIGHT → next track
  POINT_LEFT  → previous track
  VICTORY     → next track (alt)
  OK_SIGN     → mute toggle

Author : Vineet Machchal
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Tuple

import numpy as np

log = logging.getLogger("gama.vision.gesture")

# MediaPipe hand landmark indices
_WRIST = 0
_THUMB_CMC, _THUMB_MCP, _THUMB_IP, _THUMB_TIP = 1, 2, 3, 4
_INDEX_MCP, _INDEX_PIP, _INDEX_DIP, _INDEX_TIP = 5, 6, 7, 8
_MIDDLE_MCP, _MIDDLE_PIP, _MIDDLE_DIP, _MIDDLE_TIP = 9, 10, 11, 12
_RING_MCP, _RING_PIP, _RING_DIP, _RING_TIP = 13, 14, 15, 16
_PINKY_MCP, _PINKY_PIP, _PINKY_DIP, _PINKY_TIP = 17, 18, 19, 20

# Connections for skeleton drawing (pairs of landmark indices)
HAND_CONNECTIONS: List[Tuple[int, int]] = [
    (0, 1), (1, 2), (2, 3), (3, 4),           # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),           # index
    (0, 9), (9, 10), (10, 11), (11, 12),      # middle
    (0, 13), (13, 14), (14, 15), (15, 16),    # ring
    (0, 17), (17, 18), (18, 19), (19, 20),    # pinky
    (5, 9), (9, 13), (13, 17),                # palm
]


class GestureType(str, Enum):
    NONE = "none"
    OPEN_PALM = "open_palm"
    FIST = "fist"
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    POINT_RIGHT = "point_right"
    POINT_LEFT = "point_left"
    VICTORY = "victory"
    OK_SIGN = "ok_sign"
    # Continuous pointer-control gestures (system-wide mouse)
    INDEX_POINT = "index_point"   # only index extended → move cursor
    PINCH = "pinch"               # thumb+index close → grab / select


@dataclass
class GestureEvent:
    gesture: GestureType
    confidence: float
    handedness: str = "Right"  # "Left" | "Right"
    timestamp: float = field(default_factory=time.time)


@dataclass
class LandmarkFrame:
    """Continuous hand state for pointer / drag control (every detect frame)."""
    index_tip: Tuple[float, float]   # normalized 0..1 (x, y) — mirrored camera
    thumb_tip: Tuple[float, float]
    wrist: Tuple[float, float]
    pinch_dist: float                # thumb–index Euclidean distance (normalized)
    is_pinching: bool
    is_index_point: bool             # only index extended
    is_open_palm: bool
    gesture: GestureType
    confidence: float
    handedness: str = "Right"
    timestamp: float = field(default_factory=time.time)


FrameCallback = Callable[[np.ndarray, Optional[GestureType]], None]
GestureCallback = Callable[[GestureEvent], None]
LandmarkCallback = Callable[[LandmarkFrame], None]

# Pinch distance threshold (normalized image space)
PINCH_THRESHOLD = 0.055
PINCH_RELEASE_THRESHOLD = 0.085  # hysteresis so drag doesn't flicker


def _finger_extended(lm, tip: int, pip: int, mcp: int, handedness: str = "Right") -> bool:
    """True when fingertip is clearly beyond the PIP joint (extended)."""
    # For non-thumb fingers: tip.y < pip.y (image coords: y grows downward)
    return lm[tip].y < lm[pip].y - 0.02


def _thumb_extended(lm, handedness: str) -> bool:
    """Thumb extended sideways relative to the palm."""
    # Compare tip x to IP x; direction depends on hand
    if handedness == "Right":
        return lm[_THUMB_TIP].x < lm[_THUMB_IP].x - 0.03
    return lm[_THUMB_TIP].x > lm[_THUMB_IP].x + 0.03


def _pinch_distance(lm) -> float:
    return (
        (lm[_THUMB_TIP].x - lm[_INDEX_TIP].x) ** 2
        + (lm[_THUMB_TIP].y - lm[_INDEX_TIP].y) ** 2
    ) ** 0.5


def _classify_hand(lm, handedness: str) -> Tuple[GestureType, float]:
    """Classify a single hand from 21 normalized landmarks."""
    index_up = _finger_extended(lm, _INDEX_TIP, _INDEX_PIP, _INDEX_MCP)
    middle_up = _finger_extended(lm, _MIDDLE_TIP, _MIDDLE_PIP, _MIDDLE_MCP)
    ring_up = _finger_extended(lm, _RING_TIP, _RING_PIP, _RING_MCP)
    pinky_up = _finger_extended(lm, _PINKY_TIP, _PINKY_PIP, _PINKY_MCP)
    thumb_up = _thumb_extended(lm, handedness)

    # Thumb pointing up/down (vertical) — tip well above/below wrist
    thumb_vertical_up = lm[_THUMB_TIP].y < lm[_WRIST].y - 0.12 and abs(
        lm[_THUMB_TIP].x - lm[_WRIST].x
    ) < 0.15
    thumb_vertical_down = lm[_THUMB_TIP].y > lm[_WRIST].y + 0.12 and abs(
        lm[_THUMB_TIP].x - lm[_WRIST].x
    ) < 0.15

    fingers = sum([index_up, middle_up, ring_up, pinky_up])
    pinch_dist = _pinch_distance(lm)

    # PINCH — thumb tip near index tip (priority for grab/select)
    if pinch_dist < PINCH_THRESHOLD:
        return GestureType.PINCH, 0.92

    # OPEN PALM — all four fingers + optional thumb
    if fingers >= 4:
        return GestureType.OPEN_PALM, 0.9

    # FIST — no fingers extended
    if fingers == 0 and not thumb_up and not thumb_vertical_up:
        return GestureType.FIST, 0.88

    # THUMBS UP — thumb vertical up, other fingers folded
    if thumb_vertical_up and fingers <= 1:
        return GestureType.THUMBS_UP, 0.9

    # THUMBS DOWN
    if thumb_vertical_down and fingers <= 1:
        return GestureType.THUMBS_DOWN, 0.9

    # VICTORY — index + middle up, ring + pinky down
    if index_up and middle_up and not ring_up and not pinky_up:
        return GestureType.VICTORY, 0.9

    # INDEX POINT — only index extended (cursor move). Prefer this over
    # directional POINT_LEFT/RIGHT so continuous pointer mode is stable.
    if index_up and not middle_up and not ring_up and not pinky_up:
        return GestureType.INDEX_POINT, 0.88

    # OK sign — thumb tip near index tip, other fingers up-ish
    if pinch_dist < 0.08 and (middle_up or ring_up):
        return GestureType.OK_SIGN, 0.85

    return GestureType.NONE, 0.0


def _build_landmark_frame(lm, gesture: GestureType, conf: float, handedness: str) -> LandmarkFrame:
    pinch_dist = _pinch_distance(lm)
    index_up = _finger_extended(lm, _INDEX_TIP, _INDEX_PIP, _INDEX_MCP)
    middle_up = _finger_extended(lm, _MIDDLE_TIP, _MIDDLE_PIP, _MIDDLE_MCP)
    ring_up = _finger_extended(lm, _RING_TIP, _RING_PIP, _RING_MCP)
    pinky_up = _finger_extended(lm, _PINKY_TIP, _PINKY_PIP, _PINKY_MCP)
    fingers = sum([index_up, middle_up, ring_up, pinky_up])
    return LandmarkFrame(
        index_tip=(float(lm[_INDEX_TIP].x), float(lm[_INDEX_TIP].y)),
        thumb_tip=(float(lm[_THUMB_TIP].x), float(lm[_THUMB_TIP].y)),
        wrist=(float(lm[_WRIST].x), float(lm[_WRIST].y)),
        pinch_dist=pinch_dist,
        is_pinching=pinch_dist < PINCH_THRESHOLD,
        is_index_point=index_up and not middle_up and not ring_up and not pinky_up,
        is_open_palm=fingers >= 4,
        gesture=gesture,
        confidence=conf,
        handedness=handedness,
    )



def _create_hands_solution():
    """Create a hand tracker.

    MediaPipe 0.10.30+ / 1.x removed `mp.solutions`. Use the Tasks
    HandLandmarker API instead. Falls back to OpenCV contours if the
    model cannot be loaded.
    """
    try:
        from mediapipe.tasks.python.core import base_options as base_options_module
        from mediapipe.tasks.python import vision as mp_vision
        from mediapipe.tasks.python.vision.core import vision_task_running_mode

        model_path = _ensure_hand_landmarker_model()
        if model_path is None:
            return None, False

        BaseOptions = base_options_module.BaseOptions
        HandLandmarker = mp_vision.HandLandmarker
        HandLandmarkerOptions = mp_vision.HandLandmarkerOptions
        RunningMode = vision_task_running_mode.VisionTaskRunningMode

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        landmarker = HandLandmarker.create_from_options(options)
        log.info("MediaPipe HandLandmarker (Tasks API) ready — model=%s", model_path.name)
        return landmarker, True
    except Exception as exc:
        log.warning("MediaPipe Tasks HandLandmarker unavailable (%s) — contour fallback", exc)
        return None, False


def _ensure_hand_landmarker_model():
    """Download the official hand_landmarker.task once into ~/.gama/models/."""
    from pathlib import Path as _Path
    import urllib.request

    dest_dir = _Path.home() / ".gama" / "models"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "hand_landmarker.task"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest

    url = (
        "https://storage.googleapis.com/mediapipe-models/"
        "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
    )
    log.info("Downloading HandLandmarker model (~8MB) to %s ...", dest)
    try:
        tmp = dest.with_suffix(".tmp")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
        log.info("HandLandmarker model saved (%s bytes)", dest.stat().st_size)
        return dest
    except Exception as exc:
        log.error("Could not download hand_landmarker.task: %s", exc)
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        return None



class GestureEngine:
    """
    Background camera + hand-tracking loop.

    Thread-safe. Call start() / stop(). Subscribe via on_frame / on_gesture.
    """

    def __init__(
        self,
        camera_index: int = 0,
        width: int = 640,
        height: int = 360,
        target_fps: float = 30.0,
        gesture_cooldown_sec: float = 0.8,
        stable_frames: int = 2,
    ) -> None:
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._frame_interval = 1.0 / max(1.0, target_fps)
        self._cooldown = gesture_cooldown_sec
        self._stable_frames = stable_frames

        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cap = None
        self._hands = None
        self._use_mediapipe = False

        self._frame_callbacks: List[FrameCallback] = []
        self._gesture_callbacks: List[GestureCallback] = []
        self._landmark_callbacks: List[LandmarkCallback] = []

        self._last_gesture: GestureType = GestureType.NONE
        self._last_gesture_time: float = 0.0
        self._candidate: GestureType = GestureType.NONE
        self._candidate_count: int = 0
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_gesture: Optional[GestureType] = None
        self._latest_landmarks: Optional[LandmarkFrame] = None
        self._ready: bool = False
        self._startup_error: Optional[str] = None
        self._frame_ts_ms: int = 0
        self._latest_jpeg: Optional[bytes] = None
        self._frame_idx: int = 0
        self._pinch_active: bool = False
        self._detect_every: int = 2

    # ── Public API ────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Non-blocking start. Camera + MediaPipe init run on the worker thread
        so tool handlers are never stalled by model load or camera open."""
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._startup_error = None
            self._ready = False
            self._thread = threading.Thread(target=self._loop, name="GestureEngine", daemon=True)
            self._thread.start()
            log.info("GestureEngine start requested (async)")
            return True

    def wait_until_ready(self, timeout: float = 3.0) -> bool:
        """Optional short wait used by callers that can afford a few seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._ready:
                    return True
                if self._startup_error:
                    return False
                if not self._running:
                    return False
            time.sleep(0.05)
        with self._lock:
            return bool(self._ready)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._ready = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)
        self._thread = None
        self._release_camera()
        self._release_tracker()
        with self._lock:
            self._startup_error = None
        log.info("GestureEngine stopped")

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def using_mediapipe(self) -> bool:
        with self._lock:
            return self._use_mediapipe


    def on_frame(self, cb: FrameCallback) -> None:
        with self._lock:
            if cb not in self._frame_callbacks:
                self._frame_callbacks.append(cb)

    def off_frame(self, cb: FrameCallback) -> None:
        with self._lock:
            try:
                self._frame_callbacks.remove(cb)
            except ValueError:
                pass

    def on_gesture(self, cb: GestureCallback) -> None:
        with self._lock:
            if cb not in self._gesture_callbacks:
                self._gesture_callbacks.append(cb)

    def off_gesture(self, cb: GestureCallback) -> None:
        with self._lock:
            try:
                self._gesture_callbacks.remove(cb)
            except ValueError:
                pass

    def on_landmark(self, cb: LandmarkCallback) -> None:
        """Subscribe to continuous landmark frames (for pointer / drag control)."""
        with self._lock:
            if cb not in self._landmark_callbacks:
                self._landmark_callbacks.append(cb)

    def off_landmark(self, cb: LandmarkCallback) -> None:
        with self._lock:
            try:
                self._landmark_callbacks.remove(cb)
            except ValueError:
                pass

    def get_latest_landmarks(self) -> Optional[LandmarkFrame]:
        with self._lock:
            return self._latest_landmarks

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def get_latest_jpeg(self) -> Optional[bytes]:
        """Shared JPEG for MJPEG streaming — no base64, no copy of ndarray."""
        with self._lock:
            return self._latest_jpeg

    # ── Internals ─────────────────────────────────────────────────────────

    def _open_camera(self) -> bool:
        try:
            import cv2
            import sys
            # CAP_DSHOW avoids multi-second hangs on many Windows webcams.
            backends = []
            if sys.platform == "win32":
                backends = [getattr(cv2, "CAP_DSHOW", 700), getattr(cv2, "CAP_MSMF", 1400), 0]
            else:
                backends = [0]
            cap = None
            for be in backends:
                try:
                    c = cv2.VideoCapture(self._camera_index, be) if be else cv2.VideoCapture(self._camera_index)
                    if c is not None and c.isOpened():
                        cap = c
                        break
                    if c is not None:
                        c.release()
                except Exception:
                    continue
            if cap is None or not cap.isOpened():
                log.error("Could not open camera index %s", self._camera_index)
                return False
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
                cap.set(cv2.CAP_PROP_FPS, 30)
                # Low buffer to reduce lag
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            self._cap = cap
            return True
        except Exception as exc:
            log.error("Camera open failed: %s", exc)
            return False

    def _release_camera(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _init_tracker(self) -> None:
        hands, ok = _create_hands_solution()
        self._hands = hands
        self._use_mediapipe = bool(ok)
        if ok:
            log.info("MediaPipe Hands initialized")
        else:
            log.warning("MediaPipe unavailable — contour fallback")

    def _release_tracker(self) -> None:
        if self._hands is not None:
            try:
                self._hands.close()
            except Exception:
                pass
            self._hands = None

    def _loop(self) -> None:
        import cv2

        # Heavy init happens HERE — never on the tool-call thread.
        if not self._open_camera():
            with self._lock:
                self._startup_error = "camera_open_failed"
                self._running = False
            log.error("GestureEngine: camera open failed on worker thread")
            return

        self._init_tracker()  # MediaPipe can take several seconds first time
        with self._lock:
            self._ready = True
        log.info(
            "GestureEngine ready (mediapipe=%s)", self._use_mediapipe
        )

        while True:
            with self._lock:
                if not self._running:
                    break
            t0 = time.time()
            try:
                ok, frame = self._cap.read() if self._cap else (False, None)
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue

                frame = cv2.flip(frame, 1)  # mirror for natural UX
                gesture = GestureType.NONE
                conf = 0.0
                handedness = "Right"
                self._frame_idx += 1

                # MediaPipe every 2nd frame — big CPU win; redraw last landmarks on skip
                do_detect = (self._frame_idx % max(1, getattr(self, "_detect_every", 1)) == 0)
                if self._use_mediapipe and self._hands is not None:
                    if do_detect:
                        gesture, conf, handedness, frame = self._process_mediapipe(frame)
                        self._last_lms = getattr(self, "_cached_lms", None)
                    else:
                        # cheap redraw of previous landmarks for smooth video
                        lms = getattr(self, "_cached_lms", None)
                        g_prev = self._latest_gesture or GestureType.NONE
                        if lms is not None:
                            frame = self._draw_landmarks(frame, lms, g_prev)
                        gesture = g_prev if g_prev != GestureType.NONE else GestureType.NONE
                        conf = 0.5 if gesture != GestureType.NONE else 0.0
                else:
                    if do_detect:
                        gesture, conf, frame = self._process_contour_fallback(frame)

                if do_detect:
                    self._stabilize_and_maybe_fire(gesture, conf, handedness)

                # Encode once for MJPEG clients (cheap quality)
                jpeg = None
                try:
                    ok, buf = cv2.imencode(
                        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50]
                    )
                    if ok:
                        jpeg = buf.tobytes()
                except Exception:
                    pass

                with self._lock:
                    self._latest_frame = frame
                    if jpeg is not None:
                        self._latest_jpeg = jpeg
                    if do_detect:
                        self._latest_gesture = (
                            gesture if gesture != GestureType.NONE else None
                        )
                    frame_cbs = list(self._frame_callbacks)
                    latest_g = self._latest_gesture

                for cb in frame_cbs:
                    try:
                        cb(frame, latest_g)
                    except Exception:
                        log.exception("frame callback error")

            except Exception:
                log.exception("GestureEngine loop error")
                time.sleep(0.1)

            elapsed = time.time() - t0
            sleep_for = self._frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _process_mediapipe(self, frame: np.ndarray):
        """Run MediaPipe Tasks HandLandmarker on a BGR frame."""
        import cv2

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # mediapipe.Image is the stable entry point across 0.10.30+ / 1.x
        try:
            import mediapipe as mp
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        except Exception:
            from mediapipe.tasks.python.vision.core import image as mp_image_module
            mp_image = mp_image_module.Image(
                image_format=mp_image_module.ImageFormat.SRGB,
                data=rgb,
            )
        self._frame_ts_ms += 33  # ~30fps monotonic timestamps required by VIDEO mode
        result = self._hands.detect_for_video(mp_image, self._frame_ts_ms)

        gesture = GestureType.NONE
        conf = 0.0
        handedness = "Right"

        landmark_frame: Optional[LandmarkFrame] = None
        if result.hand_landmarks:
            # Tasks API: list of NormalizedLandmark (x,y,z) — same layout as solutions
            lms = result.hand_landmarks[0]
            if result.handedness and result.handedness[0]:
                # Category.name is "Left" / "Right"
                cat = result.handedness[0][0]
                handedness = getattr(cat, "category_name", None) or getattr(cat, "display_name", "Right")
            gesture, conf = _classify_hand(lms, handedness)
            self._cached_lms = lms
            # Continuous pinch hysteresis (prevents drag flicker)
            pinch_dist = _pinch_distance(lms)
            if self._pinch_active:
                is_pinching = pinch_dist < PINCH_RELEASE_THRESHOLD
            else:
                is_pinching = pinch_dist < PINCH_THRESHOLD
            self._pinch_active = is_pinching
            if is_pinching and gesture != GestureType.OPEN_PALM:
                gesture = GestureType.PINCH
            landmark_frame = _build_landmark_frame(lms, gesture, conf, handedness)
            landmark_frame.is_pinching = is_pinching
            frame = self._draw_landmarks(frame, lms, gesture)
            with self._lock:
                self._latest_landmarks = landmark_frame
            # Fire continuous landmark callbacks every detect frame (pointer mode)
            with self._lock:
                lm_cbs = list(self._landmark_callbacks)
            for cb in lm_cbs:
                try:
                    cb(landmark_frame)
                except Exception:
                    log.exception("landmark callback error")

        return gesture, conf, handedness, frame

    def _draw_landmarks(self, frame: np.ndarray, landmarks, gesture: GestureType) -> np.ndarray:
        """Draw cyan skeleton lines + bright dots on joints."""
        import cv2

        h, w = frame.shape[:2]
        pts = []
        for lm in landmarks:
            x, y = int(lm.x * w), int(lm.y * h)
            pts.append((x, y))

        # Soft glow lines
        line_color = (255, 200, 0) if gesture != GestureType.NONE else (255, 220, 80)  # BGR cyan-ish
        for a, b in HAND_CONNECTIONS:
            cv2.line(frame, pts[a], pts[b], line_color, 1, cv2.LINE_AA)

        # Dots
        for i, (x, y) in enumerate(pts):
            radius = 5 if i in (_INDEX_TIP, _MIDDLE_TIP, _THUMB_TIP, _WRIST) else 3
            cv2.circle(frame, (x, y), radius + 2, (20, 20, 20), -1, cv2.LINE_AA)
            cv2.circle(frame, (x, y), radius, (255, 255, 255), -1, cv2.LINE_AA)
            if i in (_INDEX_TIP, _MIDDLE_TIP, _THUMB_TIP):
                cv2.circle(frame, (x, y), radius, (0, 255, 200), 1, cv2.LINE_AA)

        # Gesture label badge
        if gesture != GestureType.NONE:
            label = gesture.value.replace("_", " ").upper()
            cv2.rectangle(frame, (8, 8), (8 + 12 * len(label) + 16, 36), (15, 20, 30), -1)
            cv2.putText(
                frame, label, (16, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 180), 2, cv2.LINE_AA,
            )

        return frame

    def _process_contour_fallback(self, frame: np.ndarray):
        """Very light OpenCV skin-contour fallback when MediaPipe is missing."""
        import cv2

        gesture = GestureType.NONE
        conf = 0.0
        try:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            # Rough skin range
            lower = np.array([0, 30, 60], dtype=np.uint8)
            upper = np.array([25, 180, 255], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower, upper)
            mask = cv2.GaussianBlur(mask, (7, 7), 0)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                c = max(contours, key=cv2.contourArea)
                area = cv2.contourArea(c)
                if area > 3000:
                    hull = cv2.convexHull(c, returnPoints=False)
                    if hull is not None and len(hull) > 3:
                        defects = cv2.convexityDefects(c, hull)
                        finger_approx = 0
                        if defects is not None:
                            for i in range(defects.shape[0]):
                                s, e, f, d = defects[i, 0]
                                if d > 10000:
                                    finger_approx += 1
                                    far = tuple(c[f][0])
                                    cv2.circle(frame, far, 5, (0, 255, 200), -1)
                        cv2.drawContours(frame, [c], -1, (255, 200, 0), 2)
                        if finger_approx >= 4:
                            gesture, conf = GestureType.OPEN_PALM, 0.55
                        elif finger_approx == 0:
                            gesture, conf = GestureType.FIST, 0.5
                        elif finger_approx == 1:
                            gesture, conf = GestureType.POINT_RIGHT, 0.45
                        elif finger_approx == 2:
                            gesture, conf = GestureType.VICTORY, 0.5
        except Exception:
            pass
        return gesture, conf, frame

    def _stabilize_and_maybe_fire(
        self, gesture: GestureType, conf: float, handedness: str
    ) -> bool:
        """Require N consecutive same-gesture frames + cooldown before firing."""
        if gesture == GestureType.NONE or conf < 0.45:
            self._candidate = GestureType.NONE
            self._candidate_count = 0
            return False

        if gesture == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = gesture
            self._candidate_count = 1

        if self._candidate_count < self._stable_frames:
            return False

        now = time.time()
        if now - self._last_gesture_time < self._cooldown:
            return False
        if gesture == self._last_gesture and (now - self._last_gesture_time) < self._cooldown * 2:
            return False

        self._last_gesture = gesture
        self._last_gesture_time = now
        self._candidate_count = 0

        event = GestureEvent(gesture=gesture, confidence=conf, handedness=handedness)
        with self._lock:
            cbs = list(self._gesture_callbacks)
        for cb in cbs:
            try:
                cb(event)
            except Exception:
                log.exception("gesture callback error")
        log.info("Gesture fired: %s (%.2f, %s)", gesture.value, conf, handedness)
        return True


# Process-wide singleton
_engine: Optional[GestureEngine] = None
_engine_lock = threading.Lock()


def get_gesture_engine() -> GestureEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = GestureEngine()
        return _engine


__all__ = [
    "GestureEngine",
    "GestureEvent",
    "GestureType",
    "LandmarkFrame",
    "get_gesture_engine",
    "HAND_CONNECTIONS",
    "PINCH_THRESHOLD",
    "PINCH_RELEASE_THRESHOLD",
]
