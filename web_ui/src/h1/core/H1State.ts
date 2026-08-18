/**
 * H1State — central state for the H1 spatial gesture workspace.
 * Isolated from Nexus and D2.
 */

export type H1VisualState = "idle" | "entering" | "exiting";

export type H1ObjectType = "cube" | "cuboid" | "sphere" | "pyramid";

export type H1InteractionState =
  | "IDLE"
  | "HOVER"
  | "READY_TO_SELECT"
  | "OBJECT_LOCK"
  | "RESIZE"
  | "ROTATE_OBJECT"
  | "MOVE_OBJECT"
  | "CONTINUOUS_ROTATION"
  | "ROTATE_SCENE"
  | "DELETE";

export interface H1Object {
  id: string;
  type: H1ObjectType;
  position: [number, number, number];
  rotation: [number, number, number]; // euler radians
  scale: [number, number, number];
  color: string;
  /** Angular velocity for inertia (rad/s) */
  angularVelocity: [number, number, number];
  /** Continuous rotation flag + direction */
  continuousRotation: boolean;
  continuousDirection: 1 | -1;
  continuousAxis: "x" | "y" | "z";
}

export interface H1StateSnapshot {
  active: boolean;
  visualState: H1VisualState;
  objects: H1Object[];
  selectedId: string | null;
  hoverId: string | null;
  interactionState: H1InteractionState;
  /** Scene rotation (when no object selected) */
  sceneRotationX: number;
  sceneRotationY: number;
  sceneAngularVelocityX: number;
  sceneAngularVelocityY: number;
  gestureAvailable: boolean;
  gestureMessage?: string;
  /** Calibration: simple affine [a,b,c,d,tx,ty] for fingertip → screen */
  calibration: number[];
  dwellMs: number;
}

export const H1_COLORS = [
  "#00b4ff",
  "#22d3ee",
  "#34d399",
  "#fbbf24",
  "#a78bfa",
  "#f472b6",
  "#e2e8f0",
  "#fb7185",
];

export const DEFAULT_H1_STATE: H1StateSnapshot = {
  active: false,
  visualState: "idle",
  objects: [],
  selectedId: null,
  hoverId: null,
  interactionState: "IDLE",
  sceneRotationX: 0,
  sceneRotationY: 0,
  sceneAngularVelocityX: 0,
  sceneAngularVelocityY: 0,
  gestureAvailable: false,
  calibration: [1, 0, 0, 1, 0, 0], // a,b,c,d,tx,ty simple affine
  dwellMs: 400,
};

export function createObject(
  type: H1ObjectType,
  id?: string,
): H1Object {
  const uid =
    id ||
    `h1_${type}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
  const baseScale: Record<H1ObjectType, [number, number, number]> = {
    cube: [0.6, 0.6, 0.6],
    cuboid: [0.9, 0.5, 0.5],
    sphere: [0.55, 0.55, 0.55],
    pyramid: [0.7, 0.7, 0.7],
  };
  return {
    id: uid,
    type,
    position: [0, 0, 0],
    rotation: [0, 0, 0],
    scale: [...baseScale[type]] as [number, number, number],
    color: H1_COLORS[Math.floor(Math.random() * H1_COLORS.length)],
    angularVelocity: [0, 0, 0],
    continuousRotation: false,
    continuousDirection: 1,
    continuousAxis: "y",
  };
}
