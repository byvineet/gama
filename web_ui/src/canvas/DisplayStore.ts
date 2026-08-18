/**
 * DisplayStore — reactive scene registry for Gama Nexus.
 * SceneManager logic lives here; React subscribes via useSyncExternalStore.
 */

import type {
  DisplayCommand,
  SceneLayer,
  SceneNode,
  TransitionSpec,
} from "./DisplayProtocol";

export interface ActiveScene extends SceneNode {
  layer: SceneLayer;
  createdAt: number;
  updatedAt: number;
  /** internal: for exit animation */
  exiting?: boolean;
}

type Listener = () => void;

const DEFAULT_LAYER: SceneLayer = 1;

function layerOf(s: SceneNode): SceneLayer {
  const L = s.layer;
  if (L === 0 || L === 1 || L === 2 || L === 3 || L === 4) return L;
  return DEFAULT_LAYER;
}

class DisplayStoreImpl {
  private scenes = new Map<string, ActiveScene>();
  private stack: string[] = [];
  private listeners = new Set<Listener>();
  private revision = 0;

  subscribe = (fn: Listener) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = () => this.revision;

  getScenes(): ActiveScene[] {
    return Array.from(this.scenes.values())
      .filter((s) => !s.exiting)
      .sort((a, b) => a.layer - b.layer || a.createdAt - b.createdAt);
  }

  getScene(id: string): ActiveScene | undefined {
    return this.scenes.get(id);
  }

  private emit() {
    this.revision += 1;
    this.listeners.forEach((fn) => fn());
  }

  apply(cmd: DisplayCommand) {
    const action = cmd.action;
    switch (action) {
      case "show":
      case "replace":
        if (cmd.scene?.id) this.upsert(cmd.scene, action === "replace");
        break;
      case "update": {
        if (cmd.scene_id || cmd.scene?.id) {
          const id = (cmd.scene_id || cmd.scene!.id) as string;
          const partial: Partial<SceneNode> = { ...(cmd.scene || {}) };
          const cmdAny = cmd as DisplayCommand & { position?: SceneNode["position"]; size?: SceneNode["size"] };
          if (cmdAny.position) partial.position = cmdAny.position;
          if (cmdAny.size) partial.size = cmdAny.size;
          // data._position from voice move
          if (cmd.data && typeof cmd.data === "object" && (cmd.data as { _position?: unknown })._position) {
            partial.position = (cmd.data as { _position: SceneNode["position"] })._position;
          }
          this.patch(id, partial, cmd.data);
        }
        break;
      }
      case "remove":
        if (cmd.scene_id) this.remove(cmd.scene_id);
        else if (cmd.scene?.id) this.remove(cmd.scene.id);
        break;
      case "clear":
        this.clear(cmd.layer as SceneLayer | undefined);
        break;
      case "push":
        if (cmd.scene?.id) {
          this.stack.push(cmd.scene.id);
          this.upsert(cmd.scene, false);
        }
        break;
      case "pop": {
        const id = this.stack.pop();
        if (id) this.remove(id);
        break;
      }
      case "save_layout":
      case "load_layout":
      case "list_layouts":
        // Handled in useGamaSocket / GamaCanvas (localStorage)
        break;
      case "animate":
        if (cmd.scene_id && cmd.animation) {
          const s = this.scenes.get(cmd.scene_id);
          if (s) {
            s.animation = { ...s.animation, ...cmd.animation };
            s.updatedAt = Date.now();
            this.emit();
          }
        }
        break;
      default:
        break;
    }
  }

  private upsert(node: SceneNode, replaceSameType: boolean) {
    const now = Date.now();
    if (replaceSameType) {
      // remove other scenes of same type on same layer
      for (const [id, s] of this.scenes) {
        if (s.type === node.type && s.layer === layerOf(node) && id !== node.id) {
          this.scenes.delete(id);
        }
      }
    }
    const prev = this.scenes.get(node.id);
    // Auto-place new cards so they do not stack on top of each other
    let position = node.position ?? prev?.position;
    if (!position && node.type !== "idle") {
      const others = Array.from(this.scenes.values()).filter(
        (s) => !s.exiting && s.type !== "idle" && s.id !== node.id,
      );
      if (others.length === 0) {
        position = { x: 0.5, y: 0.42 };
      } else {
        const slots = [
          { x: 0.28, y: 0.32 },
          { x: 0.72, y: 0.32 },
          { x: 0.28, y: 0.68 },
          { x: 0.72, y: 0.68 },
          { x: 0.5, y: 0.5 },
        ];
        position = slots[others.length % slots.length];
      }
    }
    const next: ActiveScene = {
      ...prev,
      ...node,
      layer: layerOf(node),
      position,
      data: node.data ?? prev?.data ?? {},
      children: node.children ?? prev?.children,
      createdAt: prev?.createdAt ?? now,
      updatedAt: now,
      exiting: false,
    };
    this.scenes.set(node.id, next);

    if (typeof node.duration === "number" && node.duration > 0) {
      const id = node.id;
      window.setTimeout(() => this.remove(id), node.duration);
    }
    this.emit();
  }

  private patch(
    id: string,
    partial?: Partial<SceneNode>,
    data?: Record<string, unknown>,
  ) {
    const prev = this.scenes.get(id);
    if (!prev) {
      if (partial?.type) this.upsert({ id, type: partial.type, ...partial, data }, false);
      return;
    }
    const next: ActiveScene = {
      ...prev,
      ...partial,
      id,
      layer: partial?.layer != null ? layerOf(partial as SceneNode) : prev.layer,
      data: { ...(prev.data || {}), ...(partial?.data || {}), ...(data || {}) },
      updatedAt: Date.now(),
    };
    this.scenes.set(id, next);
    this.emit();
  }

  remove(id: string) {
    if (!this.scenes.has(id)) return;
    this.scenes.delete(id);
    this.stack = this.stack.filter((x) => x !== id);
    this.emit();
  }

  clear(layer?: SceneLayer) {
    if (layer == null) {
      this.scenes.clear();
      this.stack = [];
    } else {
      for (const [id, s] of this.scenes) {
        if (s.layer === layer) this.scenes.delete(id);
      }
      this.stack = this.stack.filter((id) => this.scenes.has(id));
    }
    this.emit();
  }

  /** Convenience: show idle home if canvas empty */
  ensureIdle(idleScene: SceneNode) {
    if (this.getScenes().length === 0) {
      this.upsert(idleScene, false);
    }
  }
}

export const displayStore = new DisplayStoreImpl();

/** Map legacy display envelopes from older bridge → protocol commands */
export function legacyEnvelopeToCommands(env: Record<string, unknown>): DisplayCommand[] {
  const cmds: DisplayCommand[] = [];
  if (env.close || env.mode === "orb") {
    cmds.push({ action: "clear" });
    return cmds;
  }
  const mode = String(env.mode || "");
  if (!mode) return cmds;

  const typeMap: Record<string, SceneNode["type"]> = {
    weather: "weather",
    reminders: "reminders",
    alerts: "alerts",
    goals: "goals",
    tasks: "tasks",
    timer: "timer",
    clock: "clock",
    time: "clock",
    confirm: "confirm",
    enrollment: "information",
    info: "information",
    canvas: "dsl",
  };
  const type = typeMap[mode];
  if (!type) return cmds;

  const data: Record<string, unknown> = {};
  if (mode === "weather" && env.weather) Object.assign(data, env.weather as object);
  if (mode === "timer" && env.timer) Object.assign(data, env.timer as object);
  if ((mode === "clock" || mode === "time") && (env as { clock?: object }).clock) {
    Object.assign(data, (env as { clock: object }).clock);
  }
  if (mode === "confirm" && env.confirm) Object.assign(data, env.confirm as object);
  if (mode === "info" && env.info) Object.assign(data, env.info as object);
  if (mode === "enrollment" && env.enrollment) {
    const e = env.enrollment as Record<string, unknown>;
    data.title = e.title || "Enrollment";
    data.content = e.instruction || e.status || "";
    data.metadata = [
      e.step != null ? `Step ${e.step}${e.total != null ? ` / ${e.total}` : ""}` : "",
      e.recording ? "Recording…" : "",
    ].filter(Boolean);
  }
  if (mode === "canvas" && env.canvas) Object.assign(data, env.canvas as object);

  cmds.push({
    action: "show",
    scene: {
      id: `legacy-${mode}`,
      type,
      layer: mode === "confirm" || mode === "alerts" ? 3 : 1,
      title: String(env.title || mode),
      data,
      transition: { enter: "fade", exit: "dissolve", duration: 280 },
    },
  });
  return cmds;
}
