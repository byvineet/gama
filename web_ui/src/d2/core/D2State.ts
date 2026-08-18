/**
 * D2State — central state for the D2 spatial interface.
 * Isolated from Gama's primary Nexus state.
 */

export type D2VisualState =
  | "idle"
  | "listening"
  | "thinking"
  | "working"
  | "displaying"
  | "error"
  | "entering"
  | "exiting";

export type D2ThemeId =
  | "gama"
  | "cyan"
  | "blue"
  | "green"
  | "white"
  | "amber"
  | "violet";

export interface D2Card {
  id: string;
  title: string;
  body?: string;
  meta?: string;
  kind?: "task" | "reminder" | "news" | "info" | "system" | "research" | "generic";
  priority?: number;
  createdAt: number;
  angle?: number;
  radius?: number;
}

export interface D2Visualization {
  type: "cpu" | "ram" | "network" | "tasks" | "custom" | "none";
  value?: number;
  label?: string;
  data?: Record<string, unknown>;
}

export interface D2StateSnapshot {
  active: boolean;
  visualState: D2VisualState;
  themeId: D2ThemeId;
  primaryColor: string;
  zoom: number;
  rotationX: number;
  rotationY: number;
  /** 0 = assembled, 1 = fully spread (open-palm push apart). */
  dispersion: number;
  /** Clap toggles explosion of particles / shell. */
  exploded: boolean;
  cards: D2Card[];
  visualization: D2Visualization;
  gestureAvailable: boolean;
  gestureMessage?: string;
}

export const D2_THEME_COLORS: Record<D2ThemeId, string> = {
  gama: "#008FFF",
  cyan: "#22d3ee",
  blue: "#3b82f6",
  green: "#34d399",
  white: "#e2e8f0",
  amber: "#fbbf24",
  violet: "#a78bfa",
};

export const D2_THEME_ORDER: D2ThemeId[] = [
  "gama", "cyan", "blue", "green", "white", "amber", "violet",
];

export const MAX_VISIBLE_CARDS = 8;

export const DEFAULT_D2_STATE: D2StateSnapshot = {
  active: false,
  visualState: "idle",
  themeId: "gama",
  primaryColor: "#008FFF",
  zoom: 1,
  rotationX: 0,
  rotationY: 0,
  dispersion: 0,
  exploded: false,
  cards: [],
  visualization: { type: "none" },
  gestureAvailable: true,
};

export const D2_THEME_STORAGE_KEY = "gama.d2.themeId";
