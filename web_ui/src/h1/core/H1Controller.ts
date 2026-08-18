/**
 * H1Controller — public API for H1 spatial workspace.
 * Does not expose Three.js / MediaPipe internals.
 */
import {
  DEFAULT_H1_STATE,
  createObject,
  type H1Object,
  type H1ObjectType,
  type H1StateSnapshot,
  type H1InteractionState,
} from "./H1State";

type Listener = (snap: H1StateSnapshot) => void;

class H1ControllerImpl {
  private state: H1StateSnapshot = {
    ...DEFAULT_H1_STATE,
    objects: [],
  };
  private listeners = new Set<Listener>();
  private transitionTimer: number | null = null;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = (): H1StateSnapshot => this.state;

  private notify() {
    const snap = this.state;
    this.listeners.forEach((fn) => {
      try {
        fn(snap);
      } catch (e) {
        console.warn("[H1Controller]", e);
      }
    });
  }

  private set(partial: Partial<H1StateSnapshot>) {
    this.state = { ...this.state, ...partial };
    this.notify();
  }

  isActive() {
    return this.state.active && this.state.visualState !== "exiting";
  }

  enter() {
    if (this.state.active && this.state.visualState !== "exiting") return;
    if (this.transitionTimer != null) {
      window.clearTimeout(this.transitionTimer);
      this.transitionTimer = null;
    }
    this.set({
      active: true,
      visualState: "entering",
      objects: [],
      selectedId: null,
      hoverId: null,
      interactionState: "IDLE",
      sceneRotationX: 0,
      sceneRotationY: 0,
      sceneAngularVelocityX: 0,
      sceneAngularVelocityY: 0,
      gestureAvailable: false,
      gestureMessage: undefined,
    });
    this.transitionTimer = window.setTimeout(() => {
      this.transitionTimer = null;
      if (this.state.active) {
        this.set({ visualState: "idle" });
      }
    }, 450);
  }

  exit() {
    if (!this.state.active || this.state.visualState === "exiting") return;
    if (this.transitionTimer != null) {
      window.clearTimeout(this.transitionTimer);
      this.transitionTimer = null;
    }
    this.set({
      visualState: "exiting",
      selectedId: null,
      hoverId: null,
      interactionState: "IDLE",
    });
    this.transitionTimer = window.setTimeout(() => {
      this.transitionTimer = null;
      this.set({
        active: false,
        visualState: "idle",
        objects: [],
        sceneRotationX: 0,
        sceneRotationY: 0,
        sceneAngularVelocityX: 0,
        sceneAngularVelocityY: 0,
      });
    }, 400);
  }

  setGestureAvailable(ok: boolean, message?: string) {
    this.set({ gestureAvailable: ok, gestureMessage: message });
  }

  addObject(type: H1ObjectType): string {
    const obj = createObject(type);
    obj.position = [
      (Math.random() - 0.5) * 0.4,
      (Math.random() - 0.5) * 0.3,
      (Math.random() - 0.5) * 0.3,
    ];
    const objects = [...this.state.objects, obj];
    this.set({ objects });
    return obj.id;
  }

  removeObject(id: string) {
    const objects = this.state.objects.filter((o) => o.id !== id);
    const selectedId = this.state.selectedId === id ? null : this.state.selectedId;
    const hoverId = this.state.hoverId === id ? null : this.state.hoverId;
    this.set({ objects, selectedId, hoverId });
  }

  updateObject(id: string, partial: Partial<H1Object>) {
    const objects = this.state.objects.map((o) =>
      o.id === id ? { ...o, ...partial } : o,
    );
    this.set({ objects });
  }

  setSelectedId(id: string | null) {
    this.set({ selectedId: id });
  }

  setHoverId(id: string | null) {
    this.set({ hoverId: id });
  }

  setInteractionState(s: H1InteractionState) {
    this.set({ interactionState: s });
  }

  setSceneRotation(rx: number, ry: number, avx = 0, avy = 0) {
    this.set({
      sceneRotationX: rx,
      sceneRotationY: ry,
      sceneAngularVelocityX: avx,
      sceneAngularVelocityY: avy,
    });
  }

  setObjectColor(id: string, color: string) {
    this.updateObject(id, { color });
  }

  setDwellMs(ms: number) {
    this.set({ dwellMs: Math.max(150, Math.min(1200, ms)) });
  }

  /** Apply a simple calibration affine: [a,b,c,d,tx,ty] maps normalized → screen-ish */
  setCalibration(mat: number[]) {
    if (mat.length >= 6) this.set({ calibration: mat.slice(0, 6) });
  }

  /** Tick physics / inertia — called from render loop.
   *  Damping ~e^(-1.35 t) so a strong flick coasts ~2–3s like a physical spin. */
  tick(dt: number) {
    if (!this.state.active) return;
    const damp = Math.exp(-1.35 * dt);
    let changed = false;

    let srx = this.state.sceneRotationX;
    let sry = this.state.sceneRotationY;
    let savx = this.state.sceneAngularVelocityX * damp;
    let savy = this.state.sceneAngularVelocityY * damp;
    if (Math.abs(savx) > 1e-4 || Math.abs(savy) > 1e-4) {
      srx += savx * dt;
      sry += savy * dt;
      changed = true;
    } else {
      savx = 0;
      savy = 0;
    }

    const objects = this.state.objects.map((o) => {
      const { continuousRotation, continuousDirection, continuousAxis } = o;
      let av = [...o.angularVelocity] as [number, number, number];
      let rot = [...o.rotation] as [number, number, number];

      if (continuousRotation) {
        const speed = 1.8 * continuousDirection;
        if (continuousAxis === "x") av[0] = speed;
        else if (continuousAxis === "z") av[2] = speed;
        else av[1] = speed;
      } else {
        av[0] *= damp;
        av[1] *= damp;
        av[2] *= damp;
        if (Math.abs(av[0]) < 1e-4) av[0] = 0;
        if (Math.abs(av[1]) < 1e-4) av[1] = 0;
        if (Math.abs(av[2]) < 1e-4) av[2] = 0;
      }

      if (av[0] || av[1] || av[2]) {
        rot[0] += av[0] * dt;
        rot[1] += av[1] * dt;
        rot[2] += av[2] * dt;
        changed = true;
      }

      if (
        rot[0] !== o.rotation[0] ||
        rot[1] !== o.rotation[1] ||
        rot[2] !== o.rotation[2] ||
        av[0] !== o.angularVelocity[0] ||
        av[1] !== o.angularVelocity[1] ||
        av[2] !== o.angularVelocity[2]
      ) {
        return { ...o, rotation: rot, angularVelocity: av };
      }
      return o;
    });

    if (changed) {
      this.set({
        objects,
        sceneRotationX: srx,
        sceneRotationY: sry,
        sceneAngularVelocityX: savx,
        sceneAngularVelocityY: savy,
      });
    }
  }

  getObject(id: string): H1Object | undefined {
    return this.state.objects.find((o) => o.id === id);
  }
}

export const h1 = new H1ControllerImpl();

export function exitH1() {
  h1.exit();
}
