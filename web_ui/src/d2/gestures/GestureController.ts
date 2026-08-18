/**
 * D2 GestureController
 * --------------------
 * Browser MediaPipe Hand Landmarker (CDN) + pointer fallback.
 * Runs only while D2 is active.
 *
 * Gestures:
 *  1. Pinch / pinch+move — rotate
 *  2. Two-hand pinch — zoom
 *  3. Swipe L/R — theme cycle
 *
 * Open-palm and clap gestures removed.
 *
 * Tuned for responsive tracking: ~12 FPS detect, stronger swipe path checks.
 */
import { d2 } from "../core/D2Controller";
import { d2Events } from "../core/D2Events";
import { OneEuroFilter } from "./GestureSmoother";

export type GestureName =
  | "none"
  | "pinch"
  | "pinch_move"
  | "two_hand_pinch"
  | "swipe_left"
  | "swipe_right";

interface Landmark {
  x: number;
  y: number;
  z: number;
}

type PinchState = "OPEN" | "PINCH_CANDIDATE" | "PINCHED" | "RELEASE_CANDIDATE";

// Pinch: slightly roomier enter + clear hysteresis so grab feels solid
const PINCH_ENTER = 0.07;
const PINCH_EXIT = 0.11;

// Swipe: tuned for ~12 FPS sampling and mirrored selfie camera
const SWIPE_MIN_DX = 0.10; // normalized image width
const SWIPE_MIN_VEL = 0.22; // units/sec — was too strict at low FPS
const SWIPE_MAX_DY_RATIO = 0.65; // reject mostly-vertical motions
const SWIPE_TRAIL_MS = 520;
const SWIPE_COOLDOWN_MS = 480;
const SWIPE_MIN_SAMPLES = 4;
const SWIPE_DIR_AGREE = 0.7; // ≥70% of steps must share swipe direction

/** ~12 detections/sec — better swipe trails without starving the render thread. */
const DETECT_INTERVAL_MS = 80;
const CANDIDATE_MS = 45;
const RELEASE_MS = 70;

const ROT_SENS_X = 2.6;
const ROT_SENS_Y = 3.2;
const PTR_ROT_SENS = 3.4;

function dist(a: Landmark, b: Landmark) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

/** Mirrored index-tip position (selfie cam → natural left/right). */
function tipXY(lm: Landmark[]): { x: number; y: number } {
  // Blend index tip with MCP for stability (tip alone jitters)
  const tip = lm[8];
  const mcp = lm[5];
  return {
    x: 1 - (tip.x * 0.75 + mcp.x * 0.25),
    y: tip.y * 0.75 + mcp.y * 0.25,
  };
}

export class GestureController {
  private running = false;
  private video: HTMLVideoElement | null = null;
  private stream: MediaStream | null = null;
  private raf = 0;
  private handLandmarker: any = null;
  private lastSwipeTs = 0;
  private lastGesture: GestureName = "none";

  private pinchState: PinchState = "OPEN";
  private pinchCandidateTs = 0;
  private releaseCandidateTs = 0;

  private fx = new OneEuroFilter(1.5, 0.01);
  private fy = new OneEuroFilter(1.5, 0.01);
  private grabStartX = 0.5;
  private grabStartY = 0.5;
  private baseRotX = 0;
  private baseRotY = 0;

  private twoHandBaseDist = 0;
  private twoHandBaseZoom = 1;
  private wasTwoHand = false;
  private swipeTrail: { x: number; y: number; t: number }[] = [];
  private lastDetectTs = 0;

  async start() {
    if (this.running) return;
    this.running = true;
    this.attachPointerFallback();

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: "user",
          width: { ideal: 320, max: 480 },
          height: { ideal: 240, max: 360 },
          frameRate: { ideal: 15, max: 20 },
        },
        audio: false,
      });
    } catch (e) {
      console.warn("[D2 Gestures] camera denied", e);
      d2.setGestureAvailable(false, "Gesture control unavailable — use mouse");
      return;
    }

    this.video = document.createElement("video");
    this.video.playsInline = true;
    this.video.muted = true;
    this.video.width = 320;
    this.video.height = 240;
    this.video.srcObject = this.stream;
    await this.video.play();

    try {
      // Dynamic CDN import — no bundle weight
      const vision: any = await (Function(
        'return import("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/+esm")',
      )() as Promise<any>);
      const fileset = await vision.FilesetResolver.forVisionTasks(
        "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm",
      );
      const modelPath =
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";
      const common = {
        runningMode: "VIDEO" as const,
        numHands: 2,
        minHandDetectionConfidence: 0.5,
        minHandPresenceConfidence: 0.5,
        minTrackingConfidence: 0.45,
      };
      try {
        this.handLandmarker = await vision.HandLandmarker.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: modelPath, delegate: "GPU" },
          ...common,
        });
      } catch {
        this.handLandmarker = await vision.HandLandmarker.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: modelPath, delegate: "CPU" },
          ...common,
        });
      }
      d2.setGestureAvailable(true);
    } catch (e) {
      console.warn("[D2 Gestures] MediaPipe load failed — pointer fallback", e);
      d2.setGestureAvailable(false, "Gesture control unavailable — use mouse");
    }

    const loop = (now: number) => {
      if (!this.running) return;
      this.raf = requestAnimationFrame(loop);
      if (typeof document !== "undefined" && document.hidden) return;
      if (!this.handLandmarker || !this.video) return;
      if (now - this.lastDetectTs < DETECT_INTERVAL_MS) return;
      this.lastDetectTs = now;
      try {
        const result = this.handLandmarker.detectForVideo(this.video, now);
        const hands: Landmark[][] = (result?.landmarks || []) as Landmark[][];
        this.processHands(hands);
      } catch {
        /* frame skip */
      }
    };
    this.raf = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.stream) {
      for (const t of this.stream.getTracks()) t.stop();
      this.stream = null;
    }
    if (this.video) {
      this.video.srcObject = null;
      this.video = null;
    }
    if (this.handLandmarker?.close) {
      try {
        this.handLandmarker.close();
      } catch {
        /* */
      }
    }
    this.handLandmarker = null;
    this.detachPointerFallback();
    this.pinchState = "OPEN";
    this.wasTwoHand = false;
    this.swipeTrail = [];
    this.fx.reset();
    this.fy.reset();
  }

  private processHands(hands: Landmark[][]) {
    const now = performance.now();
    if (!hands.length) {
      this.pinchState = "OPEN";
      this.swipeTrail = [];
      this.wasTwoHand = false;
      return;
    }

    // ——— Two hands: pinch zoom only ———
    if (hands.length >= 2) {
      this.swipeTrail = [];
      const h0 = hands[0];
      const h1 = hands[1];
      const p0 = this.pinchPoint(h0);
      const p1 = this.pinchPoint(h1);
      if (p0 && p1) {
        const d = dist(p0, p1);
        if (!this.wasTwoHand) {
          this.twoHandBaseDist = d;
          this.twoHandBaseZoom = d2.getSnapshot().zoom;
          this.wasTwoHand = true;
        } else if (this.twoHandBaseDist > 0.015) {
          const ratio = d / this.twoHandBaseDist;
          // Soft clamp so one bad frame doesn't zoom to infinity
          const safe = Math.max(0.35, Math.min(2.8, this.twoHandBaseZoom * ratio));
          d2.setZoom(safe);
          this.emitGesture("two_hand_pinch");
        }
        return;
      }
      // Not both pinching — fall through to single-hand on primary hand
    }

    this.wasTwoHand = false;

    const landmarks = hands[0];
    const pinchDist = dist(landmarks[4], landmarks[8]);
    const isPinching = pinchDist < PINCH_ENTER;
    const isReleased = pinchDist > PINCH_EXIT;

    switch (this.pinchState) {
      case "OPEN":
        if (isPinching) {
          this.pinchState = "PINCH_CANDIDATE";
          this.pinchCandidateTs = now;
          this.swipeTrail = [];
        } else {
          this.trackSwipe(landmarks, now);
        }
        break;
      case "PINCH_CANDIDATE":
        if (!isPinching) {
          this.pinchState = "OPEN";
        } else if (now - this.pinchCandidateTs >= CANDIDATE_MS) {
          this.pinchState = "PINCHED";
          this.swipeTrail = [];
          const tip = landmarks[8];
          this.fx.reset();
          this.fy.reset();
          this.grabStartX = this.fx.filter(1 - tip.x, now);
          this.grabStartY = this.fy.filter(tip.y, now);
          const snap = d2.getSnapshot();
          this.baseRotX = snap.rotationX;
          this.baseRotY = snap.rotationY;
          this.emitGesture("pinch");
        }
        break;
      case "PINCHED":
        if (isReleased) {
          this.pinchState = "RELEASE_CANDIDATE";
          this.releaseCandidateTs = now;
        } else {
          const tip = landmarks[8];
          const x = this.fx.filter(1 - tip.x, now);
          const y = this.fy.filter(tip.y, now);
          const dx = x - this.grabStartX;
          const dy = y - this.grabStartY;
          d2.setRotation(
            this.baseRotX + dy * ROT_SENS_Y,
            this.baseRotY + dx * ROT_SENS_X,
          );
          this.emitGesture("pinch_move");
        }
        break;
      case "RELEASE_CANDIDATE":
        if (isPinching) this.pinchState = "PINCHED";
        else if (now - this.releaseCandidateTs >= RELEASE_MS) {
          this.pinchState = "OPEN";
          this.swipeTrail = [];
        }
        break;
    }
  }

  private pinchPoint(lm: Landmark[]): Landmark | null {
    const d = dist(lm[4], lm[8]);
    if (d > PINCH_EXIT) return null;
    return {
      x: (lm[4].x + lm[8].x) / 2,
      y: (lm[4].y + lm[8].y) / 2,
      z: (lm[4].z + lm[8].z) / 2,
    };
  }

  /**
   * Swipe detection from a short trail of index-tip positions.
   * Requires:
   *  - enough samples in the time window
   *  - net horizontal travel above threshold
   *  - velocity above threshold
   *  - mostly horizontal (not a vertical flail)
   *  - consistent direction across steps (rejects jitter that zigzags)
   */
  private trackSwipe(lm: Landmark[], now: number) {
    const { x, y } = tipXY(lm);
    this.swipeTrail.push({ x, y, t: now });
    while (this.swipeTrail.length && now - this.swipeTrail[0].t > SWIPE_TRAIL_MS) {
      this.swipeTrail.shift();
    }

    if (now - this.lastSwipeTs < SWIPE_COOLDOWN_MS) return;
    if (this.swipeTrail.length < SWIPE_MIN_SAMPLES) return;

    const trail = this.swipeTrail;
    const a = trail[0];
    const b = trail[trail.length - 1];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dt = (b.t - a.t) / 1000;
    if (dt < 0.08) return;

    const absDx = Math.abs(dx);
    const absDy = Math.abs(dy);
    if (absDx < SWIPE_MIN_DX) return;
    // Must be predominantly horizontal
    if (absDy > absDx * SWIPE_MAX_DY_RATIO) return;

    const vel = absDx / dt;
    if (vel < SWIPE_MIN_VEL) return;

    // Direction consistency: count steps that agree with net dx sign
    let agree = 0;
    let steps = 0;
    const sign = dx > 0 ? 1 : -1;
    for (let i = 1; i < trail.length; i++) {
      const sdx = trail[i].x - trail[i - 1].x;
      if (Math.abs(sdx) < 0.004) continue; // ignore micro-jitter
      steps++;
      if (sdx * sign > 0) agree++;
    }
    if (steps >= 2 && agree / steps < SWIPE_DIR_AGREE) return;

    // Peak mid-trail velocity (catches fast flicks that start/end slower)
    let peakVel = vel;
    for (let i = 1; i < trail.length; i++) {
      const sdx = Math.abs(trail[i].x - trail[i - 1].x);
      const sdt = (trail[i].t - trail[i - 1].t) / 1000;
      if (sdt > 0.001) peakVel = Math.max(peakVel, sdx / sdt);
    }
    if (peakVel < SWIPE_MIN_VEL * 0.9) return;

    this.lastSwipeTs = now;
    this.swipeTrail = [];
    if (dx > 0) {
      d2.cycleTheme(1);
      this.emitGesture("swipe_right");
    } else {
      d2.cycleTheme(-1);
      this.emitGesture("swipe_left");
    }
  }

  private emitGesture(name: GestureName) {
    if (name === this.lastGesture && (name === "pinch_move" || name === "two_hand_pinch"))
      return;
    this.lastGesture = name;
    d2Events.emit("D2_GESTURE", { name });
  }

  // Pointer fallback
  private pointerDown = false;
  private ptrStartX = 0;
  private ptrStartY = 0;

  private onPointerDown = (e: PointerEvent) => {
    if (!d2.isActive()) return;
    const t = e.target as HTMLElement | null;
    if (t?.closest?.("button, a, input, [role=button]")) return;
    this.pointerDown = true;
    this.ptrStartX = e.clientX;
    this.ptrStartY = e.clientY;
    const snap = d2.getSnapshot();
    this.baseRotX = snap.rotationX;
    this.baseRotY = snap.rotationY;
    this.emitGesture("pinch");
  };

  private onPointerMove = (e: PointerEvent) => {
    if (!this.pointerDown || !d2.isActive()) return;
    const dx = (e.clientX - this.ptrStartX) / window.innerWidth;
    const dy = (e.clientY - this.ptrStartY) / window.innerHeight;
    d2.setRotation(
      this.baseRotX + dy * PTR_ROT_SENS,
      this.baseRotY + dx * PTR_ROT_SENS,
    );
    this.emitGesture("pinch_move");
  };

  private onPointerUp = () => {
    if (!this.pointerDown) return;
    this.pointerDown = false;
  };

  private onWheel = (e: WheelEvent) => {
    if (!d2.isActive()) return;
    e.preventDefault();
    const snap = d2.getSnapshot();
    d2.setZoom(snap.zoom * (e.deltaY > 0 ? 0.94 : 1.06));
    this.emitGesture("two_hand_pinch");
  };

  private onKey = (e: KeyboardEvent) => {
    if (!d2.isActive()) return;
    if (e.key === "ArrowLeft") {
      d2.cycleTheme(-1);
      this.emitGesture("swipe_left");
    } else if (e.key === "ArrowRight") {
      d2.cycleTheme(1);
      this.emitGesture("swipe_right");
    } else if (e.key === "[") {
      d2.setDispersion(Math.max(0, d2.getSnapshot().dispersion - 0.08));
    } else if (e.key === "]") {
      d2.setDispersion(Math.min(1, d2.getSnapshot().dispersion + 0.08));
    }
  };

  private attachPointerFallback() {
    window.addEventListener("pointerdown", this.onPointerDown);
    window.addEventListener("pointermove", this.onPointerMove);
    window.addEventListener("pointerup", this.onPointerUp);
    window.addEventListener("wheel", this.onWheel, { passive: false });
    window.addEventListener("keydown", this.onKey);
  }

  private detachPointerFallback() {
    window.removeEventListener("pointerdown", this.onPointerDown);
    window.removeEventListener("pointermove", this.onPointerMove);
    window.removeEventListener("pointerup", this.onPointerUp);
    window.removeEventListener("wheel", this.onWheel);
    window.removeEventListener("keydown", this.onKey);
  }
}

export const gestureController = new GestureController();
