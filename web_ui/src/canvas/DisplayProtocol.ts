/**
 * Gama Display Protocol — structured visual output channel.
 * Python (or any backend) describes WHAT to show; React renders it.
 * Never eval / never arbitrary HTML or JS from the model.
 */

export type DisplayAction =
  | "show"
  | "update"
  | "replace"
  | "remove"
  | "clear"
  | "push"
  | "pop"
  | "animate"
  | "save_layout"
  | "load_layout"
  | "list_layouts";

export type SceneType =
  | "idle"
  | "weather"
  | "tasks"
  | "goals"
  | "reminders"
  | "alerts"
  | "calendar"
  | "timer"
  | "pomodoro"
  | "clock"
  | "time"
  | "music"
  | "system"
  | "status"
  | "execution"
  | "search"
  | "notes"
  | "information"
  | "table"
  | "list"
  | "chart"
  | "progress"
  | "card"
  | "gauge"
  | "metric"
  | "timeline"
  | "confirm"
  | "notification"
  | "image"
  | "code"           // dynamic code view with syntax styling & line numbers
  | "workflow"       // autonomous pipeline & file organization progress
  | "scene"          // composite container
  | "custom_svg"     // declarative SVG
  | "model_3d"       // parametric isometric 3D
  | "dsl";           // visual DSL composition

export type TransitionName =
  | "none"
  | "fade"
  | "slide"
  | "scale"
  | "reveal"
  | "scan"
  | "pulse"
  | "glow"
  | "dissolve";

export interface TransitionSpec {
  enter?: TransitionName;
  update?: TransitionName;
  exit?: TransitionName;
  duration?: number; // ms
}

export type SceneLayer = 0 | 1 | 2 | 3 | 4;

export interface SceneStyle {
  opacity?: number;
  accent?: string;
  background?: string;
  padding?: string | number;
  align?: "start" | "center" | "end" | "stretch";
}

export interface SceneBase {
  id: string;
  type: SceneType;
  layer?: SceneLayer;
  /** 0–1 relative placement within canvas (optional layout hints) */
  position?: { x?: number; y?: number };
  size?: { w?: number; h?: number };
  data?: Record<string, unknown>;
  children?: SceneNode[];
  animation?: TransitionSpec;
  transition?: TransitionSpec;
  duration?: number; // auto-remove after ms
  style?: SceneStyle;
  interactive?: boolean;
  title?: string;
}

export type SceneNode = SceneBase;

export interface DisplayCommand {
  channel?: "display";
  action: DisplayAction;
  scene?: SceneNode;
  scene_id?: string;
  /** Partial data merge for update */
  data?: Record<string, unknown>;
  animation?: TransitionSpec;
  /** stack name for push/pop */
  stack?: string;
  position?: { x?: number; y?: number };
  size?: { w?: number; h?: number };
  layer?: number;
}

export interface DisplayEvent {
  channel: "display";
  event: string;
  scene_id: string;
  element_id?: string;
  value?: unknown;
}

/** Built-in data shapes (loose — components read what they need) */
export interface WeatherData {
  location?: string;
  temperature?: number;
  feels_like?: number;
  condition?: string;
  humidity?: number;
  wind_kph?: number;
  high?: number;
  low?: number;
  icon?: string;
  forecast?: Array<{ day?: string; high?: number; low?: number; condition?: string }>;
}

export interface TimerData {
  label?: string;
  remaining_sec?: number;
  total_sec?: number;
  running?: boolean;
  ends_at?: number;
}

export interface SystemData {
  cpu?: number;
  ram?: number;
  disk?: number;
  network?: string;
  battery?: number;
  status?: string;
}

export interface InformationData {
  title?: string;
  content?: string;
  body?: string;
  metadata?: string[];
  items?: string[];
}

export interface ListData {
  title?: string;
  items?: Array<string | { label: string; value?: string; done?: boolean }>;
}

export interface ChartData {
  title?: string;
  series?: Array<{ label: string; value: number; color?: string }>;
  kind?: "bar" | "ring" | "line";
}

/** SVG primitive types (safe subset) */
export type SvgPrimitiveType =
  | "g"
  | "text"
  | "line"
  | "circle"
  | "ellipse"
  | "rect"
  | "path"
  | "polygon"
  | "polyline"
  | "image";

export interface SvgElement {
  type: SvgPrimitiveType;
  id?: string;
  // geometry
  x?: number;
  y?: number;
  cx?: number;
  cy?: number;
  r?: number;
  rx?: number;
  ry?: number;
  width?: number;
  height?: number;
  x1?: number;
  y1?: number;
  x2?: number;
  y2?: number;
  d?: string;
  points?: string;
  // text
  text?: string;
  // presentation (string values only — no JS)
  fill?: string;
  stroke?: string;
  strokeWidth?: number | string;
  opacity?: number | string;
  fontSize?: number | string;
  fontFamily?: string;
  fontWeight?: string | number;
  textAnchor?: "start" | "middle" | "end";
  transform?: string;
  className?: string;
  children?: SvgElement[];
  // image only — must be data: or relative path, never javascript:
  href?: string;
}

export interface CustomSvgData {
  viewBox?: string;
  width?: string | number;
  height?: string | number;
  elements?: SvgElement[];
  background?: string;
}

export function isDisplayCommand(msg: unknown): msg is DisplayCommand {
  if (!msg || typeof msg !== "object") return false;
  const m = msg as Record<string, unknown>;
  const action = m.action;
  if (typeof action !== "string") return false;
  const valid: DisplayAction[] = [
    "show", "update", "replace", "remove", "clear", "push", "pop", "animate",
  ];
  return valid.includes(action as DisplayAction);
}
