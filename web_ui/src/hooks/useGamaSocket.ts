import { useCallback, useEffect, useRef, useState } from "react";
import {
  EMPTY_SNAPSHOT,
  type GamaSnapshot,
  type LogEntry,
  type ReminderItem,
  type AlertItem,
  type GoalItem,
  type TaskItem,
} from "../types/gama";
import type { DisplayEnvelope, DisplayState } from "../types/display";
import { DEFAULT_DISPLAY } from "../types/display";
import { displayStore, legacyEnvelopeToCommands } from "../canvas/DisplayStore";
import { isDisplayCommand, type DisplayCommand } from "../canvas/DisplayProtocol";
import { saveCurrentLayout, loadLayout, listLayouts } from "../canvas/layoutStorage";

const DEFAULT_WS =
  import.meta.env.VITE_GAMA_WS_URL ?? "ws://127.0.0.1:8765/ws";

let _revision = 0;
function nextRev(explicit?: number): number {
  if (typeof explicit === "number" && explicit > _revision) {
    _revision = explicit;
    return _revision;
  }
  _revision += 1;
  return _revision;
}

/** Always produce a fresh state — previous mode is ignored so panels switch freely. */
function applyDisplayEnvelope(env: DisplayEnvelope, _prev: DisplayState): DisplayState {
  if (env.close || env.mode === "orb") {
    return { ...DEFAULT_DISPLAY, revision: nextRev(env.revision) };
  }

  const rev = nextRev(env.revision);
  const mode = env.mode;

  if (mode === "timer" && env.timer) {
    const remaining = Number(env.timer.remainingSec ?? 0);
    const running = env.timer.running !== false;
    let endsAt = env.timer.endsAt;
    if (endsAt != null && Number.isFinite(Number(endsAt))) {
      endsAt = Number(endsAt);
      // If value looks like seconds-since-epoch, promote to ms
      if (endsAt > 0 && endsAt < 1e11) endsAt = endsAt * 1000;
    } else {
      endsAt = running && remaining > 0 ? Date.now() + remaining * 1000 : undefined;
    }
    return {
      mode: "timer",
      timer: {
        id: env.timer.id ?? `tm_${Date.now()}`,
        label: env.timer.label ?? "Timer",
        remainingSec: remaining,
        endsAt,
        running,
      },
      info: null,
      enrollment: null,
      title: env.title ?? env.timer.label ?? "Timer",
      userOpened: true,
      revision: rev,
    };
  }

  if (mode === "enrollment" && env.enrollment) {
    return {
      mode: "enrollment",
      timer: null,
      info: null,
      enrollment: env.enrollment,
      title: env.title ?? env.enrollment.title,
      userOpened: true,
      revision: rev,
    };
  }

  if (mode === "info" && env.info) {
    return {
      mode: "info",
      timer: null,
      info: env.info,
      enrollment: null,
      title: env.title ?? env.info.title,
      userOpened: true,
      revision: rev,
    };
  }

  if (mode === "weather" && env.weather) {
    return {
      mode: "weather",
      timer: null,
      info: null,
      enrollment: null,
      weather: env.weather,
      confirm: null,
      title: env.title ?? env.weather.location ?? "Weather",
      userOpened: true,
      revision: rev,
    };
  }

  if (mode === "canvas" && env.canvas) {
    return {
      mode: "canvas",
      timer: null,
      info: null,
      enrollment: null,
      weather: null,
      confirm: null,
      canvas: env.canvas,
      title: env.title ?? env.canvas.title ?? "Display",
      userOpened: true,
      revision: rev,
    };
  }

  if (mode === "confirm" && env.confirm) {
    return {
      mode: "confirm",
      timer: null,
      info: null,
      enrollment: null,
      weather: null,
      confirm: env.confirm,
      title: env.title ?? env.confirm.title,
      userOpened: true,
      revision: rev,
    };
  }

  if (
    mode === "reminders" ||
    mode === "alerts" ||
    mode === "goals" ||
    mode === "tasks"
  ) {
    return {
      mode,
      timer: null,
      info: null,
      enrollment: null,
      weather: null,
      confirm: null,
      title: env.title ?? mode.charAt(0).toUpperCase() + mode.slice(1),
      userOpened: true,
      revision: rev,
    };
  }

  return _prev;
}

export function useGamaSocket(url: string = DEFAULT_WS) {
  const [snapshot, setSnapshot] = useState<GamaSnapshot>(EMPTY_SNAPSHOT);
  const [connected, setConnected] = useState(false);
  const [display, setDisplay] = useState<DisplayState>(DEFAULT_DISPLAY);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const lastHandledLogRef = useRef<string>("");

  const applyEnvelope = useCallback((msg: { type?: string; data?: unknown; channel?: string; action?: string }) => {
    const type = (msg.type || "").toLowerCase();
    const data = msg.data as Record<string, unknown> | unknown;


    // D2 spatial interface control (secondary mode — never auto-activates)
    if (type === "d2" && data && typeof data === "object") {
      const d = data as Record<string, unknown>;
      const action = String(d.action || "").toLowerCase();
      import("../d2").then((mod) => {
        if (action === "enter" || action === "open" || action === "show") {
          // mutual exclusion with H1
          import("../h1").then((h) => { try { h.exitH1(); } catch { /* */ } }).catch(() => {});
          mod.enterD2();
        } else if (action === "exit" || action === "close" || action === "hide") {
          mod.exitD2();
        } else if (action === "show_tasks" && Array.isArray(d.tasks)) {
          mod.showTasksAsCards(d.tasks as never[]);
        } else if (action === "show_reminders" && Array.isArray(d.reminders)) {
          mod.showRemindersAsCards(d.reminders as never[]);
        } else if (action === "show_news" && Array.isArray(d.items)) {
          mod.showNewsAsCards(d.items as never[]);
        } else if (action === "visualize_cpu") {
          mod.visualizeCpu(Number(d.value ?? 0));
        } else if (action === "visualize_ram") {
          mod.visualizeRam(Number(d.value ?? 0));
        } else if (action === "clear") {
          mod.clearD2Content();
        } else if (action === "set_state" && typeof d.state === "string") {
          mod.d2.setState(d.state as never);
        }
      }).catch(() => { /* D2 optional */ });
      return;
    }

    // H1 spatial gesture workspace (mutually exclusive with D2)
    if (type === "h1" && data && typeof data === "object") {
      const d = data as Record<string, unknown>;
      const action = String(d.action || "").toLowerCase();
      import("../h1").then((mod) => {
        if (action === "enter" || action === "open" || action === "show" || action === "enable" || action === "start") {
          import("../d2").then((d2m) => { try { d2m.exitD2(); } catch { /* */ } }).catch(() => {});
          mod.h1.enter();
        } else if (action === "exit" || action === "close" || action === "hide" || action === "stop" || action === "leave") {
          mod.exitH1();
        }
      }).catch(() => { /* H1 optional */ });
      return;
    }

    // New Gama Canvas display protocol (accept several shapes)
    if (
      msg.channel === "display" ||
      type === "display_cmd" ||
      type === "canvas" ||
      (typeof (msg as { action?: string }).action === "string" &&
        (msg as { scene?: unknown }).scene != null)
    ) {
      const payload = (
        msg.channel === "display" || type === "display_cmd" || type === "canvas"
          ? msg
          : msg
      ) as DisplayCommand;
      if (isDisplayCommand(payload) || (payload as { action?: string }).action) {
        const act = String((payload as { action?: string }).action || "");
        if (act === "save_layout") {
          const name = String((payload as { name?: string }).name || "default");
          saveCurrentLayout(name);
          return;
        }
        if (act === "load_layout") {
          const name = String((payload as { name?: string }).name || "default");
          loadLayout(name);
          return;
        }
        if (act === "list_layouts") {
          console.info("[GAMA] Saved layouts:", listLayouts());
          return;
        }
        if (isDisplayCommand(payload)) {
          displayStore.apply(payload);
        }
      }
    }
    if (type === "display" && data && typeof data === "object" && "action" in (data as object)) {
      if (isDisplayCommand(data as DisplayCommand)) {
        displayStore.apply(data as DisplayCommand);
      }
    }
    // Batch of commands
    if (type === "display_batch" && Array.isArray(data)) {
      for (const c of data) {
        if (isDisplayCommand(c)) displayStore.apply(c as DisplayCommand);
      }
      return;
    }

    // Legacy display envelope → canvas commands (skip if already a protocol scene)
    if (type === "display" && data && typeof data === "object") {
      const env = data as DisplayEnvelope;
      const canvasObj = (env as { canvas?: { id?: string; type?: string } }).canvas;
      // Avoid duplicate INFO card when backend sent a structured scene as canvas payload
      if (canvasObj && canvasObj.type && canvasObj.type !== "canvas" && canvasObj.id) {
        if (!displayStore.getScene(canvasObj.id)) {
          displayStore.apply({
            action: "show",
            scene: canvasObj as never,
          });
        }
      } else {
        for (const c of legacyEnvelopeToCommands(env as unknown as Record<string, unknown>)) {
          // Don't add a second scene of the same type if one is already open
          const sid = c.scene?.id;
          if (sid && displayStore.getScene(sid)) continue;
          if (c.scene?.type && displayStore.getScenes().some((s) => s.type === c.scene!.type && s.type !== "idle")) {
            continue;
          }
          displayStore.apply(c);
        }
      }
      setDisplay((prev) => applyDisplayEnvelope(env, prev));
      return;
    }

    setSnapshot((prev) => {
      if (type === "snapshot" && data && typeof data === "object") {
        const snap = data as GamaSnapshot;
        const log = (snap.log || []).filter((e) => {
          const r = String(e.role || "").toLowerCase();
          return r === "user" || r === "gama";
        });
        if (snap.display && typeof snap.display === "object") {
          const d = snap.display as DisplayEnvelope;
          if (d.close || d.mode) {
            for (const c of legacyEnvelopeToCommands(d as unknown as Record<string, unknown>)) {
              displayStore.apply(c);
            }
            setDisplay((p) => applyDisplayEnvelope(d, p));
          }
        }
        return {
          ...prev,
          ...snap,
          log,
          reminders: (snap.reminders as ReminderItem[] | undefined) ?? prev.reminders,
          alerts: (snap.alerts as AlertItem[] | undefined) ?? prev.alerts,
          goals: (snap.goals as GoalItem[] | undefined) ?? prev.goals,
          tasks: (snap.tasks as TaskItem[] | undefined) ?? prev.tasks,
        };
      }
      if (type === "state" && data && typeof data === "object") {
        return { ...prev, ...(data as Partial<GamaSnapshot>) };
      }
      if (type === "goals" && Array.isArray(data)) {
        return { ...prev, goals: data as GoalItem[] };
      }
      if (type === "tasks" && Array.isArray(data)) {
        return { ...prev, tasks: data as TaskItem[] };
      }
      if (type === "reminders") {
        // bridge may send {reminders,alarms,timers} or a flat array
        if (Array.isArray(data)) {
          return { ...prev, reminders: data as ReminderItem[] };
        }
        if (data && typeof data === "object") {
          const d = data as {
            reminders?: ReminderItem[];
            alarms?: ReminderItem[];
            timers?: ReminderItem[];
          };
          const flat = [
            ...(d.reminders || []),
            ...(d.alarms || []),
            ...(d.timers || []),
          ];
          return { ...prev, reminders: flat };
        }
      }
      if (type === "alerts" && Array.isArray(data)) {
        return { ...prev, alerts: data as AlertItem[] };
      }
      if (type === "log" && data && typeof data === "object") {
        const entry = data as LogEntry;
        const role = String(entry.role || "").toLowerCase();
        if (role !== "user" && role !== "gama") {
          console.info(`[GAMA:${role}]`, entry.text);
          return prev;
        }
        return { ...prev, log: [...(prev.log || []), entry].slice(-80) };
      }
      if (type === "amplitude" && data && typeof data === "object") {
        const level = Number((data as { level?: number }).level ?? 0);
        return { ...prev, amplitude: level };
      }
      if (type === "camera_vision" && data && typeof data === "object") {
        const en = Boolean((data as { enabled?: boolean }).enabled);
        return { ...prev, camera_vision: en };
      }
      if (type === "gesture" && data && typeof data === "object") {
        const d = data as { enabled?: boolean; name?: string };
        const en = Boolean(d.enabled);
        return {
          ...prev,
          gesture_enabled: en,
          gesture_name: en ? String(d.name || "nexus") : "",
          gesture_frame: "",
          // Nexus gestures use hidden MediaPipe — never show stage camera preview
          camera_vision: en ? false : prev.camera_vision,
        };
      }
      if (type === "sysstats" && data && typeof data === "object") {
        const d = data as {
          cpu?: number;
          ram?: number;
          disk?: number;
          battery?: number | null;
          battery_charging?: boolean;
          wifi?: boolean;
        };
        return {
          ...prev,
          cpu: Number(d.cpu ?? prev.cpu),
          ram: Number(d.ram ?? prev.ram),
          disk: Number(d.disk ?? prev.disk),
          battery: d.battery === undefined ? prev.battery : d.battery,
          battery_charging: Boolean(d.battery_charging ?? prev.battery_charging),
          wifi: d.wifi === undefined ? prev.wifi : Boolean(d.wifi),
        };
      }
      return prev;
    });
  }, []);

  const pendingChatRef = useRef<string[]>([]);

  const flushPendingChat = useCallback((ws: WebSocket) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const queued = pendingChatRef.current.splice(0, pendingChatRef.current.length);
    for (const msg of queued) {
      try {
        ws.send(JSON.stringify({ type: "chat", text: msg }));
      } catch (err) {
        console.warn("[GAMA] flush chat failed", err);
        pendingChatRef.current.unshift(msg);
        break;
      }
    }
  }, []);

  const sendChat = useCallback((text: string) => {
    const t = text.trim();
    if (!t) return false;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      // Queue until reconnect (common right after page refresh)
      pendingChatRef.current.push(t);
      console.warn("[GAMA] WebSocket not open — chat queued until reconnect");
      return true;
    }
    try {
      ws.send(JSON.stringify({ type: "chat", text: t }));
      return true;
    } catch (err) {
      pendingChatRef.current.push(t);
      console.warn("[GAMA] chat send failed — queued", err);
      return true;
    }
  }, []);

  const sendDisplayEvent = useCallback(
    (sceneId: string, event: string, elementId?: string, value?: unknown) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(
        JSON.stringify({
          channel: "display",
          event,
          scene_id: sceneId,
          element_id: elementId,
          value,
        }),
      );
      return true;
    },
    [],
  );


  const sendGestureAction = useCallback((action: string) => {
    const a = (action || "").trim().toLowerCase();
    if (!a) return false;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify({ type: "gesture_action", action: a }));
      return true;
    } catch {
      return false;
    }
  }, []);

  const sendMute = useCallback((muted: boolean) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn("[GAMA] WebSocket not open — cannot send mute");
      return false;
    }
    ws.send(JSON.stringify({ type: "mute", muted: Boolean(muted) }));
    // Optimistic local update so UI feels instant
    setSnapshot((prev) => ({ ...prev, mic_muted: Boolean(muted) }));
    return true;
  }, []);

  const closeDisplay = useCallback(() => {
    setDisplay({ ...DEFAULT_DISPLAY, revision: nextRev() });
  }, []);

  const showDisplay = useCallback((env: DisplayEnvelope) => {
    setDisplay((prev) => applyDisplayEnvelope(env, prev));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const connect = () => {
      if (cancelled) return;
      try {
        const ws = new WebSocket(url);
        wsRef.current = ws;
        ws.onopen = () => {
          console.info("[GAMA] WebSocket connected:", url);
          setConnected(true);
          retryRef.current = 0;
          try {
            ws.send(JSON.stringify({ type: "refresh" }));
          } catch { /* ignore */ }
          // Deliver any chats typed during disconnect / page refresh
          flushPendingChat(ws);
        };
        ws.onmessage = (ev) => {
          try {
            applyEnvelope(JSON.parse(String(ev.data)));
          } catch {
            /* ignore */
          }
        };
        ws.onclose = () => {
          setConnected(false);
          wsRef.current = null;
          const delay = Math.min(8000, 600 * Math.pow(1.6, retryRef.current++));
          timerRef.current = window.setTimeout(connect, delay);
        };
        ws.onerror = () => {
          try {
            ws.close();
          } catch {
            /* ignore */
          }
        };
      } catch {
        timerRef.current = window.setTimeout(connect, 2000);
      }
    };
    connect();
    return () => {
      cancelled = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
      wsRef.current?.close();
    };
  }, [url, applyEnvelope, flushPendingChat]);

  // Local chat mirror for instant UI (backend also pushes via display_stage)
  useEffect(() => {
    const last = snapshot.log[snapshot.log.length - 1];
    if (!last || String(last.role).toLowerCase() !== "user") return;
    const text = String(last.text || "").trim();
    if (!text) return;
    const fp = `${last.ts ?? ""}|${text}`;
    if (fp === lastHandledLogRef.current) return;
    lastHandledLogRef.current = fp;
    const lower = text.toLowerCase();

    if (
      /^(close|hide|dismiss|clear)\s+(display|screen|it|that|panel)?\s*$/i.test(lower) ||
      /^(go\s+back|return\s+to\s+(orb|home)|exit\s+display)\s*$/i.test(lower)
    ) {
      setDisplay({ ...DEFAULT_DISPLAY, revision: nextRev() });
      return;
    }

    if (/^(show|display|open)\s+(my\s+)?reminders?\b/i.test(lower)) {
      setDisplay({
        mode: "reminders",
        timer: null,
        info: null,
        enrollment: null,
        title: "Reminders",
        userOpened: true,
        revision: nextRev(),
      });
      return;
    }
    if (/^(show|display|open)\s+(my\s+)?(alerts?|warnings?)\b/i.test(lower)) {
      setDisplay({
        mode: "alerts",
        timer: null,
        info: null,
        enrollment: null,
        title: "Alerts",
        userOpened: true,
        revision: nextRev(),
      });
      return;
    }
    if (/^(show|display|open)\s+(my\s+)?goals?\b/i.test(lower)) {
      setDisplay({
        mode: "goals",
        timer: null,
        info: null,
        enrollment: null,
        title: "Goals",
        userOpened: true,
        revision: nextRev(),
      });
      return;
    }
    if (/^(show|display|open)\s+(my\s+)?(tasks?|task\s+queue|queue)\b/i.test(lower)) {
      setDisplay({
        mode: "tasks",
        timer: null,
        info: null,
        enrollment: null,
        weather: null,
        confirm: null,
        title: "Tasks",
        userOpened: true,
        revision: nextRev(),
      });
      return;
    }
    if (/^(show|display|open)\s+(me\s+)?(?:the\s+)?weather\b/i.test(lower)) {
      // Backend will push structured weather; optimistic placeholder
      setDisplay({
        mode: "weather",
        timer: null,
        info: null,
        enrollment: null,
        weather: { location: "…", condition: "Fetching", hours: [] },
        confirm: null,
        title: "Weather",
        userOpened: true,
        revision: nextRev(),
      });
      return;
    }

    if (/^(show|display|open)\s+(?:me\s+)?(?:the\s+)?(?:3[- ]?day\s+)?forecast\b/i.test(lower)
        || /^(show|display)\s+(?:me\s+)?(?:the\s+)?weather\s+forecast\b/i.test(lower)) {
      setDisplay({
        mode: "weather",
        weather: { location: "…", condition: "Fetching forecast", hours: [], days: [], mode: "forecast" },
        timer: null,
        info: null,
        enrollment: null,
        confirm: null,
        canvas: null,
        title: "Forecast",
        userOpened: true,
        revision: nextRev(),
      });
      return;
    }

    // "write X on the display" / "put this on display" — local optimistic text
    const writeMatch = lower.match(
      /^(?:write|put|show|display)\s+(?:this\s+)?(?:on\s+(?:the\s+)?(?:display|screen)\s*:?\s*)(.+)$/i,
    ) || lower.match(
      /^(?:write|put)\s+(.+?)\s+on\s+(?:the\s+)?(?:display|screen)\s*$/i,
    );
    if (writeMatch) {
      const body = (writeMatch[1] || "").trim();
      if (body) {
        setDisplay({
          mode: "canvas",
          canvas: {
            align: "center",
            blocks: [{ type: "text", content: body, size: "md", align: "center" }],
          },
          timer: null,
          info: null,
          enrollment: null,
          weather: null,
          confirm: null,
          title: "Display",
          userOpened: true,
          revision: nextRev(),
        });
        return;
      }
    }

    const timerMatch = lower.match(
      /^(show|start|set)\s+(?:a\s+)?timer\s+(?:for\s+)?(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)?\b/i,
    );
    if (timerMatch) {
      let sec = parseInt(timerMatch[2], 10);
      const unit = (timerMatch[3] || "s").toLowerCase();
      if (unit.startsWith("m")) sec *= 60;
      if (unit.startsWith("h")) sec *= 3600;
      setDisplay({
        mode: "timer",
        timer: {
          id: `tm_${Date.now()}`,
          label: "Timer",
          remainingSec: sec,
          endsAt: Date.now() + sec * 1000,
          running: true,
        },
        info: null,
        enrollment: null,
        title: "Timer",
        userOpened: true,
        revision: nextRev(),
      });
    }
  }, [snapshot.log]);

  return {
    snapshot,
    connected,
    sendChat,
    sendGestureAction,
    sendMute,
    sendDisplayEvent,
    display,
    closeDisplay,
    showDisplay,
  };
}
