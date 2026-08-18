/**
 * H1GestureController
 * --------------------
 * Browser MediaPipe Hand Landmarker + spatial object targeting.
 * Runs only while H1 is active.
 *
 * Gestures (mirrored for selfie camera — hand left → content left):
 *   • Dwell hover + pinch          → select / lock object
 *   • Pinch + drag                 → move object (follows hand)
 *   • Pinch + fast swipe (release) → spin object with inertia
 *   • Open-hand swipe (selected)   → spin object with inertia
 *   • Open-hand / empty swipe      → rotate whole scene + inertia (~2–3s coast)
 *   • Both hands pinch on object   → resize (scale by inter-hand distance)
 *   • Pinch + fling downward       → delete selected object
 *   • Finger circle (selected)     → continuous rotation (kept)
 *
 * No cursor. Feedback via object highlight / glow only.
 */
import * as THREE from "three";
import { h1 } from "../core/H1Controller";
import { OneEuroFilter } from "../../d2/gestures/GestureSmoother";

interface Landmark {
  x: number;
  y: number;
  z: number;
}

type PinchState = "OPEN" | "PINCH_CANDIDATE" | "PINCHED" | "RELEASE_CANDIDATE";

const PINCH_ENTER = 0.065;
const PINCH_EXIT = 0.11;
const CANDIDATE_MS = 45;
const RELEASE_MS = 70;
const DETECT_INTERVAL_MS = 55;

const CIRCLE_MIN_SAMPLES = 10;
const CIRCLE_WINDOW_MS = 900;
const CIRCLE_MIN_ARC = Math.PI * 1.4;
const CIRCLE_COOLDOWN_MS = 700;

/** Hand speed above this while pinched → treat release as spin, not just place */
const SPIN_RELEASE_SPEED = 1.35;
/** Open-hand swipe speed to spin selected / scene */
const OPEN_SWIPE_SPEED = 1.1;
/** Downward fling while pinched → delete */
const DELETE_VY = 1.8;
const DELETE_MIN_DY = 0.18;

const MOVE_SENS = 2.4;
const ROT_SENS = 5.2;
const SCENE_ROT_SENS = 3.2;
const SCENE_IMPULSE = 2.4;
const OBJ_IMPULSE = 4.0;

function dist(a: Landmark, b: Landmark) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

/** Mirrored index tip (selfie cam). Blend with MCP for stability. */
function tipXY(lm: Landmark[]): { x: number; y: number } {
  const tip = lm[8];
  const mcp = lm[5];
  return {
    x: 1 - (tip.x * 0.78 + mcp.x * 0.22),
    y: tip.y * 0.78 + mcp.y * 0.22,
  };
}

function thumbIndexDist(lm: Landmark[]): number {
  return dist(
    { x: 1 - lm[4].x, y: lm[4].y, z: 0 },
    { x: 1 - lm[8].x, y: lm[8].y, z: 0 },
  );
}

interface HandSample {
  tipX: number;
  tipY: number;
  pinchD: number;
  pinched: boolean;
}

export class H1GestureController {
  private running = false;
  private video: HTMLVideoElement | null = null;
  private stream: MediaStream | null = null;
  private raf = 0;
  private handLandmarker: any = null;
  private lastDetectTs = 0;

  private fx = new OneEuroFilter(1.8, 0.012);
  private fy = new OneEuroFilter(1.8, 0.012);
  private fPinch = new OneEuroFilter(2.2, 0.02);
  private fx2 = new OneEuroFilter(1.8, 0.012);
  private fy2 = new OneEuroFilter(1.8, 0.012);
  private fPinch2 = new OneEuroFilter(2.2, 0.02);

  private pinchState: PinchState = "OPEN";
  private pinchCandidateTs = 0;
  private releaseCandidateTs = 0;

  private hoverCandidateId: string | null = null;
  private hoverEnterTs = 0;
  private stableHoverId: string | null = null;

  private lockedId: string | null = null;
  private lastTipX = 0.5;
  private lastTipY = 0.5;
  private lastTipTs = 0;
  private grabStartX = 0.5;
  private grabStartY = 0.5;
  private grabPos: [number, number, number] = [0, 0, 0];

  private recentVel: { vx: number; vy: number; t: number }[] = [];

  private isResizeMode = false;
  private baseScale: [number, number, number] = [1, 1, 1];
  private resizeStartDist = 0.2;

  private trail: { x: number; y: number; t: number }[] = [];
  private lastCircleTs = 0;

  private sceneGrab = false;
  private sceneBaseX = 0;
  private sceneBaseY = 0;
  private sceneStartTipX = 0.5;
  private sceneStartTipY = 0.5;

  private openSwipeActive = false;
  private openSwipeStartX = 0.5;
  private openSwipeStartY = 0.5;
  private openSwipeLastX = 0.5;
  private openSwipeLastY = 0.5;
  private openSwipeLastTs = 0;
  private openSwipeTarget: "object" | "scene" | null = null;

  private raycaster = new THREE.Raycaster();
  private camera: THREE.Camera | null = null;
  private objectMeshes = new Map<string, THREE.Object3D>();
  private viewportW = 1280;
  private viewportH = 800;

  setSceneRefs(
    camera: THREE.Camera,
    meshes: Map<string, THREE.Object3D>,
    w: number,
    h: number,
  ) {
    this.camera = camera;
    this.objectMeshes = meshes;
    this.viewportW = w;
    this.viewportH = h;
  }

  async start() {
    if (this.running) return;
    this.running = true;

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: "user",
          width: { ideal: 480, max: 640 },
          height: { ideal: 360, max: 480 },
          frameRate: { ideal: 20, max: 24 },
        },
        audio: false,
      });
    } catch (e) {
      console.warn("[H1] Camera denied", e);
      h1.setGestureAvailable(false, "Camera access required for H1");
      this.running = false;
      return;
    }

    this.video = document.createElement("video");
    this.video.srcObject = this.stream;
    this.video.playsInline = true;
    this.video.muted = true;
    this.video.style.display = "none";
    document.body.appendChild(this.video);
    await this.video.play();

    try {
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
        minHandDetectionConfidence: 0.55,
        minHandPresenceConfidence: 0.5,
        minTrackingConfidence: 0.5,
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
      h1.setGestureAvailable(true);
    } catch (e) {
      console.warn("[H1] MediaPipe load failed", e);
      h1.setGestureAvailable(false, "Hand tracking unavailable");
    }

    this.lastTipTs = performance.now();
    const loop = (now: number) => {
      if (!this.running) return;
      this.raf = requestAnimationFrame(loop);
      this.detect(now);
    };
    this.raf = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    if (this.video) {
      this.video.remove();
      this.video = null;
    }
    this.handLandmarker = null;
    this.lockedId = null;
    this.stableHoverId = null;
    this.hoverCandidateId = null;
    this.pinchState = "OPEN";
    this.trail = [];
    this.isResizeMode = false;
    this.sceneGrab = false;
    this.openSwipeActive = false;
    this.recentVel = [];
    this.fx.reset();
    this.fy.reset();
    this.fPinch.reset();
    this.fx2.reset();
    this.fy2.reset();
    this.fPinch2.reset();
  }

  getVideo(): HTMLVideoElement | null {
    return this.video;
  }

  private detect(now: number) {
    if (!this.video || this.video.readyState < 2) return;
    if (now - this.lastDetectTs < DETECT_INTERVAL_MS) return;
    this.lastDetectTs = now;

    if (!this.handLandmarker) return;

    let results: any;
    try {
      results = this.handLandmarker.detectForVideo(this.video, now);
    } catch {
      return;
    }

    const hands: Landmark[][] = results?.landmarks || [];
    if (!hands.length) {
      this.onNoHand(now);
      return;
    }

    const samples: HandSample[] = [];
    for (let i = 0; i < Math.min(2, hands.length); i++) {
      const lm = hands[i];
      const tip = tipXY(lm);
      const rawPinch = thumbIndexDist(lm);
      const sx = i === 0 ? this.fx.filter(tip.x, now) : this.fx2.filter(tip.x, now);
      const sy = i === 0 ? this.fy.filter(tip.y, now) : this.fy2.filter(tip.y, now);
      const pinchD =
        i === 0 ? this.fPinch.filter(rawPinch, now) : this.fPinch2.filter(rawPinch, now);
      samples.push({
        tipX: sx,
        tipY: sy,
        pinchD,
        pinched: pinchD < PINCH_ENTER,
      });
    }

    samples.sort((a, b) => Number(b.pinched) - Number(a.pinched));

    const primary = samples[0];
    const secondary = samples.length > 1 ? samples[1] : null;

    if (
      primary.pinched &&
      secondary?.pinched &&
      (this.lockedId || this.stableHoverId || h1.getSnapshot().selectedId)
    ) {
      this.handleBimanualResize(primary, secondary, now);
      return;
    }

    if (this.isResizeMode && !(primary.pinched && secondary?.pinched)) {
      this.isResizeMode = false;
      if (this.lockedId) h1.setInteractionState("OBJECT_LOCK");
    }

    this.processPrimary(primary.tipX, primary.tipY, primary.pinchD, primary.pinched, now);
  }

  private handleBimanualResize(a: HandSample, b: HandSample, now: number) {
    const targetId =
      this.lockedId || this.stableHoverId || h1.getSnapshot().selectedId;
    if (!targetId) return;

    const d = Math.hypot(a.tipX - b.tipX, a.tipY - b.tipY);

    if (!this.isResizeMode) {
      this.isResizeMode = true;
      this.lockedId = targetId;
      h1.setSelectedId(targetId);
      h1.setInteractionState("RESIZE");
      const o = h1.getObject(targetId);
      if (o) this.baseScale = [...o.scale] as [number, number, number];
      this.resizeStartDist = Math.max(0.06, d);
      h1.updateObject(targetId, {
        continuousRotation: false,
        angularVelocity: [0, 0, 0],
      });
      return;
    }

    const ratio = d / this.resizeStartDist;
    const minS = 0.15;
    const maxS = 2.8;
    const o = h1.getObject(targetId);
    if (!o) return;

    let sx = this.baseScale[0] * ratio;
    let sy = this.baseScale[1] * ratio;
    let sz = this.baseScale[2] * ratio;

    if (o.type === "cube" || o.type === "sphere" || o.type === "pyramid") {
      const s = Math.max(minS, Math.min(maxS, (sx + sy + sz) / 3));
      sx = sy = sz = s;
    } else {
      sx = Math.max(minS, Math.min(maxS, sx));
      sy = Math.max(minS, Math.min(maxS, sy));
      sz = Math.max(minS, Math.min(maxS, sz));
    }
    h1.updateObject(targetId, { scale: [sx, sy, sz] });
    this.lastTipTs = now;
  }

  private onNoHand(now: number) {
    if (this.pinchState === "PINCHED" || this.pinchState === "RELEASE_CANDIDATE") {
      this.releaseCandidateTs = this.releaseCandidateTs || now;
      if (now - this.releaseCandidateTs > RELEASE_MS * 1.5) {
        this.finishPinchRelease(this.lastTipX, this.lastTipY, 0, 0);
        this.pinchState = "OPEN";
      }
    } else {
      this.hoverCandidateId = null;
      this.stableHoverId = null;
      h1.setHoverId(null);
      if (
        h1.getSnapshot().interactionState === "HOVER" ||
        h1.getSnapshot().interactionState === "READY_TO_SELECT"
      ) {
        h1.setInteractionState("IDLE");
      }
    }
    this.openSwipeActive = false;
    this.isResizeMode = false;
  }

  private processPrimary(
    tipX: number,
    tipY: number,
    pinchD: number,
    _isPinched: boolean,
    now: number,
  ) {
    const dt = Math.max(0.008, (now - this.lastTipTs) / 1000);
    const vx = (tipX - this.lastTipX) / dt;
    const vy = (tipY - this.lastTipY) / dt;
    this.lastTipX = tipX;
    this.lastTipY = tipY;
    this.lastTipTs = now;

    this.recentVel.push({ vx, vy, t: now });
    this.recentVel = this.recentVel.filter((p) => now - p.t < 120);

    if (this.pinchState === "OPEN") {
      if (pinchD < PINCH_ENTER) {
        this.pinchState = "PINCH_CANDIDATE";
        this.pinchCandidateTs = now;
      } else {
        this.processOpenHand(tipX, tipY, vx, vy, now);
      }
    } else if (this.pinchState === "PINCH_CANDIDATE") {
      if (pinchD >= PINCH_ENTER) {
        this.pinchState = "OPEN";
      } else if (now - this.pinchCandidateTs >= CANDIDATE_MS) {
        this.pinchState = "PINCHED";
        this.openSwipeActive = false;
        this.onPinchStart(tipX, tipY);
      }
    } else if (this.pinchState === "PINCHED") {
      if (pinchD > PINCH_EXIT) {
        this.pinchState = "RELEASE_CANDIDATE";
        this.releaseCandidateTs = now;
      } else {
        this.onPinchMove(tipX, tipY, vx, vy, now);
      }
    } else if (this.pinchState === "RELEASE_CANDIDATE") {
      if (pinchD <= PINCH_EXIT) {
        this.pinchState = "PINCHED";
      } else if (now - this.releaseCandidateTs >= RELEASE_MS) {
        this.pinchState = "OPEN";
        this.finishPinchRelease(tipX, tipY, vx, vy);
      }
    }

    if (!this.lockedId && this.pinchState === "OPEN") {
      this.updateTargeting(tipX, tipY, now);
    }

    if (this.lockedId && this.pinchState === "OPEN" && !this.openSwipeActive) {
      this.trail.push({ x: tipX, y: tipY, t: now });
      this.trail = this.trail.filter((p) => now - p.t < CIRCLE_WINDOW_MS);
      if (
        this.trail.length >= CIRCLE_MIN_SAMPLES &&
        now - this.lastCircleTs > CIRCLE_COOLDOWN_MS
      ) {
        const circ = this.detectCircle();
        if (circ) {
          this.lastCircleTs = now;
          this.startContinuousRotation(circ);
        }
      }
    } else if (!this.lockedId) {
      this.trail = [];
    }
  }

  private avgRecentVel(): { vx: number; vy: number; speed: number } {
    if (!this.recentVel.length) return { vx: 0, vy: 0, speed: 0 };
    let sx = 0,
      sy = 0;
    for (const p of this.recentVel) {
      sx += p.vx;
      sy += p.vy;
    }
    const n = this.recentVel.length;
    const vx = sx / n;
    const vy = sy / n;
    return { vx, vy, speed: Math.hypot(vx, vy) };
  }

  private onPinchStart(tipX: number, tipY: number) {
    const targetId = this.lockedId || this.stableHoverId;
    if (targetId) {
      this.lockedId = targetId;
      this.isResizeMode = false;
      this.sceneGrab = false;
      h1.setSelectedId(targetId);
      h1.setInteractionState("OBJECT_LOCK");
      const o = h1.getObject(targetId);
      if (o) {
        this.grabPos = [...o.position] as [number, number, number];
        if (o.continuousRotation) {
          h1.updateObject(targetId, {
            continuousRotation: false,
            angularVelocity: [0, 0, 0],
          });
        }
      }
      this.grabStartX = tipX;
      this.grabStartY = tipY;
      return;
    }

    this.sceneGrab = true;
    this.sceneStartTipX = tipX;
    this.sceneStartTipY = tipY;
    const snap = h1.getSnapshot();
    this.sceneBaseX = snap.sceneRotationX;
    this.sceneBaseY = snap.sceneRotationY;
    h1.setSceneRotation(this.sceneBaseX, this.sceneBaseY, 0, 0);
    h1.setInteractionState("ROTATE_SCENE");
  }

  private onPinchMove(
    tipX: number,
    tipY: number,
    _vx: number,
    _vy: number,
    _now: number,
  ) {
    if (this.lockedId && !this.isResizeMode) {
      const dx = tipX - this.grabStartX;
      const dy = tipY - this.grabStartY;
      const nx = this.grabPos[0] + dx * MOVE_SENS;
      const ny = this.grabPos[1] - dy * MOVE_SENS;
      const nz = this.grabPos[2];
      const clamp = (v: number, m: number) => Math.max(-m, Math.min(m, v));
      h1.updateObject(this.lockedId, {
        position: [clamp(nx, 1.6), clamp(ny, 1.2), clamp(nz, 1.2)],
        continuousRotation: false,
      });
      h1.setInteractionState("MOVE_OBJECT");
      return;
    }

    if (this.sceneGrab) {
      const dx = tipX - this.sceneStartTipX;
      const dy = tipY - this.sceneStartTipY;
      h1.setSceneRotation(
        this.sceneBaseX - dy * SCENE_ROT_SENS,
        this.sceneBaseY + dx * SCENE_ROT_SENS,
        0,
        0,
      );
      h1.setInteractionState("ROTATE_SCENE");
    }
  }

  private finishPinchRelease(tipX: number, tipY: number, vx: number, vy: number) {
    const vel = this.avgRecentVel();
    const useVx = Math.abs(vel.vx) > Math.abs(vx) ? vel.vx : vx;
    const useVy = Math.abs(vel.vy) > Math.abs(vy) ? vel.vy : vy;
    const speed = Math.hypot(useVx, useVy);

    if (this.lockedId && useVy > DELETE_VY && tipY - this.grabStartY > DELETE_MIN_DY) {
      const id = this.lockedId;
      h1.setInteractionState("DELETE");
      h1.removeObject(id);
      this.lockedId = null;
      this.sceneGrab = false;
      this.isResizeMode = false;
      h1.setSelectedId(null);
      h1.setInteractionState("IDLE");
      return;
    }

    if (this.lockedId && speed >= SPIN_RELEASE_SPEED) {
      const avx = useVy * OBJ_IMPULSE;
      const avy = useVx * OBJ_IMPULSE;
      h1.updateObject(this.lockedId, {
        angularVelocity: [avx, avy, 0],
        continuousRotation: false,
      });
      h1.setInteractionState("ROTATE_OBJECT");
      this.sceneGrab = false;
      this.isResizeMode = false;
      return;
    }

    if (this.sceneGrab && speed >= OPEN_SWIPE_SPEED * 0.7) {
      h1.setSceneRotation(
        h1.getSnapshot().sceneRotationX,
        h1.getSnapshot().sceneRotationY,
        -useVy * SCENE_IMPULSE,
        useVx * SCENE_IMPULSE,
      );
    }

    this.sceneGrab = false;
    this.isResizeMode = false;
    if (!this.lockedId) {
      h1.setInteractionState("IDLE");
    } else {
      h1.setInteractionState("OBJECT_LOCK");
    }
  }

  private processOpenHand(
    tipX: number,
    tipY: number,
    vx: number,
    vy: number,
    now: number,
  ) {
    const speed = Math.hypot(vx, vy);
    const selected = this.lockedId || h1.getSnapshot().selectedId;

    if (!this.openSwipeActive) {
      if (speed < OPEN_SWIPE_SPEED * 0.55) return;
      this.openSwipeActive = true;
      this.openSwipeStartX = tipX;
      this.openSwipeStartY = tipY;
      this.openSwipeLastX = tipX;
      this.openSwipeLastY = tipY;
      this.openSwipeLastTs = now;
      this.openSwipeTarget = selected ? "object" : "scene";
      if (this.openSwipeTarget === "object" && selected) {
        this.lockedId = selected;
        h1.setSelectedId(selected);
        h1.updateObject(selected, { continuousRotation: false });
        h1.setInteractionState("ROTATE_OBJECT");
      } else {
        h1.setInteractionState("ROTATE_SCENE");
      }
      return;
    }

    if (speed < 0.35) {
      this.openSwipeActive = false;
      if (this.openSwipeTarget === "object" && this.lockedId) {
        h1.setInteractionState("OBJECT_LOCK");
      } else {
        h1.setInteractionState("IDLE");
      }
      this.openSwipeTarget = null;
      return;
    }

    const dt = Math.max(0.008, (now - this.openSwipeLastTs) / 1000);
    this.openSwipeLastX = tipX;
    this.openSwipeLastY = tipY;
    this.openSwipeLastTs = now;

    if (this.openSwipeTarget === "object" && this.lockedId) {
      const avx = vy * ROT_SENS * 0.35;
      const avy = vx * ROT_SENS * 0.35;
      const o = h1.getObject(this.lockedId);
      if (o) {
        h1.updateObject(this.lockedId, {
          angularVelocity: [
            o.angularVelocity[0] * 0.55 + avx * 0.45,
            o.angularVelocity[1] * 0.55 + avy * 0.45,
            o.angularVelocity[2] * 0.7,
          ],
          continuousRotation: false,
        });
      }
    } else {
      const snap = h1.getSnapshot();
      h1.setSceneRotation(
        snap.sceneRotationX - vy * dt * SCENE_ROT_SENS * 0.9,
        snap.sceneRotationY + vx * dt * SCENE_ROT_SENS * 0.9,
        -vy * SCENE_IMPULSE * 0.45,
        vx * SCENE_IMPULSE * 0.45,
      );
    }
  }

  private updateTargeting(tipX: number, tipY: number, now: number) {
    const hitId = this.raycast(tipX, tipY);
    if (hitId) {
      if (this.hoverCandidateId !== hitId) {
        this.hoverCandidateId = hitId;
        this.hoverEnterTs = now;
        this.stableHoverId = null;
        h1.setHoverId(null);
        h1.setInteractionState("HOVER");
      } else if (!this.stableHoverId) {
        const dwell = h1.getSnapshot().dwellMs;
        if (now - this.hoverEnterTs >= dwell) {
          this.stableHoverId = hitId;
          h1.setHoverId(hitId);
          h1.setInteractionState("READY_TO_SELECT");
        }
      }
    } else {
      this.hoverCandidateId = null;
      this.stableHoverId = null;
      h1.setHoverId(null);
      if (
        h1.getSnapshot().interactionState === "HOVER" ||
        h1.getSnapshot().interactionState === "READY_TO_SELECT"
      ) {
        h1.setInteractionState("IDLE");
      }
    }
  }

  private raycast(tipX: number, tipY: number): string | null {
    if (!this.camera || this.objectMeshes.size === 0) return null;

    const cal = h1.getSnapshot().calibration;
    let nx = cal[0] * tipX + cal[1] * tipY + cal[4];
    let ny = cal[2] * tipX + cal[3] * tipY + cal[5];
    nx = Math.max(0, Math.min(1, nx));
    ny = Math.max(0, Math.min(1, ny));

    const ndcX = nx * 2 - 1;
    const ndcY = -(ny * 2 - 1);

    this.raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const meshes = Array.from(this.objectMeshes.values());
    const hits = this.raycaster.intersectObjects(meshes, true);
    if (hits.length === 0) return null;

    let obj: THREE.Object3D | null = hits[0].object;
    while (obj) {
      const id = (obj as any).userData?.h1Id as string | undefined;
      if (id) return id;
      obj = obj.parent;
    }
    return null;
  }

  private detectCircle(): 1 | -1 | null {
    if (this.trail.length < CIRCLE_MIN_SAMPLES) return null;
    let cx = 0,
      cy = 0;
    for (const p of this.trail) {
      cx += p.x;
      cy += p.y;
    }
    cx /= this.trail.length;
    cy /= this.trail.length;

    let angleSum = 0;
    for (let i = 1; i < this.trail.length; i++) {
      const a0 = Math.atan2(this.trail[i - 1].y - cy, this.trail[i - 1].x - cx);
      const a1 = Math.atan2(this.trail[i].y - cy, this.trail[i].x - cx);
      let da = a1 - a0;
      if (da > Math.PI) da -= Math.PI * 2;
      if (da < -Math.PI) da += Math.PI * 2;
      angleSum += da;
    }

    if (Math.abs(angleSum) < CIRCLE_MIN_ARC) return null;
    const radii = this.trail.map((p) => Math.hypot(p.x - cx, p.y - cy));
    const meanR = radii.reduce((a, b) => a + b, 0) / radii.length;
    if (meanR < 0.04) return null;
    const variance =
      radii.reduce((s, r) => s + (r - meanR) ** 2, 0) / radii.length;
    if (Math.sqrt(variance) / meanR > 0.55) return null;

    return angleSum > 0 ? 1 : -1;
  }

  private startContinuousRotation(dir: 1 | -1) {
    if (!this.lockedId) return;
    h1.updateObject(this.lockedId, {
      continuousRotation: true,
      continuousDirection: dir,
      continuousAxis: "y",
      angularVelocity: [0, 1.8 * dir, 0],
    });
    h1.setInteractionState("CONTINUOUS_ROTATION");
  }
}

export const h1GestureController = new H1GestureController();
