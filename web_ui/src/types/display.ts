/** Central presence-stage modes (voice-orb area). */
export type DisplayMode =
  | "orb"
  | "reminders"
  | "alerts"
  | "goals"
  | "tasks"
  | "timer"
  | "enrollment"
  | "weather"
  | "confirm"
  | "canvas"
  | "info";

export interface TimerDisplay {
  id: string;
  label: string;
  remainingSec: number;
  endsAt?: number;
  running: boolean;
}

export interface InfoCard {
  title: string;
  body: string;
  meta?: string;
}

export interface EnrollmentState {
  kind: "voice" | "face" | "mic";
  title: string;
  instruction?: string;
  step?: number;
  total?: number;
  progress?: number;
  status?: string;
  recording?: boolean;
}

export interface WeatherHour {
  time: string;
  temp_c?: number | null;
  condition?: string;
  emoji?: string;
  chance_of_rain?: number | null;
}

export interface WeatherDay {
  date: string;
  condition?: string;
  emoji?: string;
  max_c?: number | null;
  min_c?: number | null;
  chance_of_rain?: number | null;
}

export interface WeatherCard {
  location: string;
  temp_c?: number | null;
  condition?: string;
  emoji?: string;
  feels_c?: number | null;
  humidity?: number | null;
  wind_kph?: number | null;
  hours?: WeatherHour[];
  days?: WeatherDay[];
  mode?: "current" | "forecast";
  summary?: string;
  error?: string;
}

export interface ConfirmCard {
  id: string;
  title: string;
  body: string;
  action?: string;
  level?: "destructive" | "sensitive" | "info";
}

/** Single block on the freeform HUD canvas. */
export interface HudBlock {
  id?: string;
  type: "text" | "image" | "link" | "badge" | "divider";
  content?: string;
  title?: string;
  align?: "left" | "center" | "right";
  size?: "sm" | "md" | "lg" | "xl";
  weight?: "normal" | "medium" | "bold";
  color?: string;
  width?: string;
}

/** Freeform canvas Gama can write to arbitrarily. */
export interface HudCanvas {
  title?: string;
  align?: "left" | "center" | "right";
  blocks: HudBlock[];
}

export interface DisplayState {
  mode: DisplayMode;
  timer?: TimerDisplay | null;
  info?: InfoCard | null;
  enrollment?: EnrollmentState | null;
  weather?: WeatherCard | null;
  confirm?: ConfirmCard | null;
  canvas?: HudCanvas | null;
  title?: string;
  userOpened?: boolean;
  revision: number;
}

export const DEFAULT_DISPLAY: DisplayState = {
  mode: "orb",
  timer: null,
  info: null,
  enrollment: null,
  weather: null,
  confirm: null,
  canvas: null,
  userOpened: false,
  revision: 0,
};

export interface DisplayEnvelope {
  mode?: DisplayMode;
  timer?: Partial<TimerDisplay>;
  info?: InfoCard;
  enrollment?: EnrollmentState;
  weather?: WeatherCard;
  confirm?: ConfirmCard;
  canvas?: HudCanvas;
  title?: string;
  close?: boolean;
  revision?: number;
}
