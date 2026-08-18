export type PrimaryState = string;

export interface LogEntry {
  role: "user" | "gama" | "system" | string;
  text: string;
  ts?: number;
  time?: string;
}

export interface TaskItem {
  task_id: string;
  name: string;
  status: string;
  current_step?: string;
  progress_pct?: number | null;
  waiting?: boolean;
  waiting_reason?: string;
  error?: string;
}

export interface ReminderItem {
  id: number | string;
  message: string;
  when?: string;
  done?: boolean;
  kind: "reminder" | "alarm" | "timer" | string;
}

export interface GoalItem {
  id: number | string;
  title: string;
  description?: string;
  status?: string;
  progress_pct?: number;
  deadline?: string;
}

export interface AlertItem {
  id: string;
  title: string;
  message: string;
  level?: string;
  ts?: number;
  time?: string;
}

export interface GamaSnapshot {
  primary: PrimaryState;
  activity: string;
  mood: string;
  status_text: string;
  speaking: boolean;
  amplitude: number;
  awake: boolean;
  owner_name?: string;
  log: LogEntry[];
  goals: GoalItem[];
  tasks?: TaskItem[];
  reminders?: ReminderItem[];
  alerts?: AlertItem[];
  gesture_enabled: boolean;
  gesture_name: string;
  gesture_frame: string;
  /** Browser getUserMedia camera panel (instant HUD preview) */
  camera_vision?: boolean;
  cpu: number;
  ram: number;
  disk: number;
  battery: number | null;
  battery_charging: boolean;
  wifi: boolean;
  /** Microphone muted (UI + backend) */
  mic_muted?: boolean;
  ts: number;
  display?: import("./display").DisplayEnvelope | null;
}

export const EMPTY_SNAPSHOT: GamaSnapshot = {
  primary: "IDLE",
  activity: "NONE",
  mood: "NORMAL",
  status_text: "Ready",
  speaking: false,
  amplitude: 0,
  awake: false,
  owner_name: "Sir",
  log: [],
  goals: [],
  tasks: [],
  reminders: [],
  alerts: [],
  gesture_enabled: false,
  gesture_name: "",
  gesture_frame: "",
  camera_vision: false,
  cpu: 0,
  ram: 0,
  disk: 0,
  battery: null,
  battery_charging: false,
  wifi: true,
  mic_muted: false,
  ts: 0,
  display: null,
};
