import { useEffect, useMemo, useRef } from "react";

export type OrbMode =
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "alert"
  | "warning"
  | "sleep"
  | "success"
  | "error"
  | "muted";

type Props = {
  speaking: boolean;
  listening: boolean;
  amplitude: number;
  primary: string;
  statusText?: string;
  muted?: boolean;
  /** When false, animation loop pauses (performance) */
  active?: boolean;
};

function resolveMode(
  primary: string,
  speaking: boolean,
  listening: boolean,
  muted?: boolean,
): OrbMode {
  if (muted) return "muted";
  const p = (primary || "").toUpperCase();
  if (p.includes("ERROR") || p.includes("FAIL")) return "error";
  if (p.includes("SLEEP")) return "sleep";
  if (speaking || p === "SPEAKING") return "speaking";
  if (p === "THINKING" || p === "PROCESSING" || p === "EXECUTING") return "thinking";
  if (listening || p === "LISTENING" || p === "WAKE_WORD") return "listening";
  // WAITING / READY map to idle (standby removed from backend)
  if (p === "SUCCESS") return "success";
  if (p.includes("ALERT")) return "alert";
  return "idle";
}

const COLORS: Record<OrbMode, string> = {
  idle: "#00b4ff",
  listening: "#22d3ee",
  thinking: "#a78bfa",
  speaking: "#38bdf8",
  alert: "#ef4444",
  warning: "#f59e0b",
  sleep: "#64748b",
  success: "#22c55e",
  error: "#f87171",
  muted: "#64748b",
};

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

/** Symmetric base heights for 7 bars (center tallest) — matches reference silhouette */
const BAR_BASE = [0.22, 0.42, 0.68, 1.0, 0.68, 0.42, 0.22];
const BAR_COUNT = BAR_BASE.length;

/**
 * Neon ring + centered equalizer bars (speech-reactive).
 * Layout matches the reference icon; bar heights respond to amplitude.
 * Supports muted state and visibility-based pause for performance.
 */
export function VoiceOrb({
  speaking,
  listening,
  amplitude,
  primary,
  statusText,
  muted = false,
  active = true,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const mode = useMemo(
    () => resolveMode(primary, speaking, listening, muted),
    [primary, speaking, listening, muted],
  );

  const ampTarget = useRef(0);
  const smoothAmp = useRef(0);
  const barLevels = useRef<number[]>(BAR_BASE.map(() => 0.15));
  const modeRef = useRef(mode);
  const colorRef = useRef(COLORS.idle);
  const phase = useRef(0);
  const lastTs = useRef(0);
  const activeRef = useRef(active);

  useEffect(() => {
    modeRef.current = mode;
    colorRef.current = COLORS[mode] || COLORS.idle;
  }, [mode]);

  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  useEffect(() => {
    if (muted) {
      ampTarget.current = 0;
      return;
    }
    const a = Number(amplitude) || 0;
    const gated = a < 0.04 ? 0 : a;
    const shaped = Math.pow(Math.min(1.15, Math.max(0, gated)), 0.8);
    ampTarget.current = shaped;
  }, [amplitude, muted]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;
    let running = true;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const reducedMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const resize = () => {
      const parent = canvas.parentElement;
      // Prefer parent box; fall back to presence stage / viewport so the orb
      // never collapses to a tiny icon when flex height is ambiguous.
      const pw = parent?.clientWidth || 0;
      const ph = parent?.clientHeight || 0;
      const stage = parent?.closest(".presence-stage, .gc-idle-orb-stage, .display-pane, .gama-canvas") as HTMLElement | null;
      const sw = stage?.clientWidth || 0;
      const sh = stage?.clientHeight || 0;
      const vw = Math.min(window.innerWidth * 0.55, 520);
      const vh = Math.min(window.innerHeight * 0.55, 520);
      const available = Math.min(
        pw > 40 ? pw : sw > 40 ? sw : vw,
        ph > 40 ? ph : sh > 40 ? sh : vh,
      );
      // Classic presence size ~380–440px; never smaller than 220 on desktop
      const size = Math.max(220, Math.min(available, 440));
      canvas.width = Math.floor(size * dpr);
      canvas.height = Math.floor(size * dpr);
      canvas.style.width = `${size}px`;
      canvas.style.height = `${size}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined" && canvas.parentElement) {
      ro = new ResizeObserver(() => resize());
      ro.observe(canvas.parentElement);
      const stage = canvas.parentElement.closest(".presence-stage, .gama-canvas");
      if (stage) ro.observe(stage);
    }

    const draw = (ts: number) => {
      if (!running) return;

      // Pause when tab/window hidden or parent requests inactive
      if (!activeRef.current || document.hidden) {
        raf = requestAnimationFrame(draw);
        return;
      }

      const w = canvas.width / dpr;
      const h = canvas.height / dpr;
      const cx = w / 2;
      const cy = h / 2;
      const ringR = Math.min(w, h) * 0.38;

      const dt = lastTs.current ? Math.min(48, ts - lastTs.current) : 16;
      lastTs.current = ts;

      const target = ampTarget.current;
      const rising = target > smoothAmp.current;
      const tau = rising ? 80 : 160;
      const alpha = 1 - Math.exp(-dt / tau);
      smoothAmp.current = lerp(smoothAmp.current, target, alpha);

      const m = modeRef.current;
      const floor =
        m === "sleep" || m === "muted"
          ? 0.08
          : m === "thinking"
            ? 0.18
            : m === "listening"
              ? 0.1
              : 0.06;
      const amp = Math.max(floor, smoothAmp.current);

      if (!reducedMotion) {
        phase.current += (dt / 1000) * (1.4 + amp * 1.7);
      }
      const t = phase.current;
      const color = colorRef.current;

      for (let i = 0; i < BAR_COUNT; i++) {
        const centerDist = Math.abs(i - (BAR_COUNT - 1) / 2) / ((BAR_COUNT - 1) / 2);
        const sway =
          reducedMotion
            ? 0.5
            : 0.5 +
              0.5 *
                Math.sin(t * 1.8 + i * 0.95) *
                Math.sin(t * 0.7 + i * 1.4);
        const speechBoost = amp * (0.75 + (1 - centerDist) * 0.45);
        const idlePulse = floor * (0.7 + 0.3 * Math.sin(t * 0.9 + i * 0.5));
        const targetH = BAR_BASE[i] * Math.max(idlePulse, speechBoost * (0.35 + 0.65 * sway));
        const barTau = rising ? 60 + i * 8 : 132 + i * 12;
        const barAlpha = 1 - Math.exp(-dt / barTau);
        barLevels.current[i] = lerp(barLevels.current[i], targetH, barAlpha);
      }

      ctx.clearRect(0, 0, w, h);

      const ambient = ctx.createRadialGradient(cx, cy, ringR * 0.25, cx, cy, ringR * 1.45);
      ambient.addColorStop(0, hexAlpha(color, 0.12 + amp * 0.1));
      ambient.addColorStop(0.55, hexAlpha(color, 0.04));
      ambient.addColorStop(1, "transparent");
      ctx.fillStyle = ambient;
      ctx.fillRect(0, 0, w, h);

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, ringR, 0, Math.PI * 2);
      ctx.strokeStyle = hexAlpha(color, 0.25);
      ctx.lineWidth = 14;
      ctx.shadowColor = color;
      ctx.shadowBlur = 28 + amp * 10;
      ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, ringR, 0, Math.PI * 2);
      ctx.strokeStyle = color;
      ctx.lineWidth = 3.5;
      ctx.shadowColor = color;
      ctx.shadowBlur = 16;
      ctx.lineCap = "round";
      ctx.stroke();
      ctx.restore();

      ctx.beginPath();
      ctx.arc(cx, cy, ringR - 2, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(2, 8, 22, 0.72)";
      ctx.fill();

      // Muted slash overlay
      if (m === "muted") {
        ctx.save();
        ctx.strokeStyle = "rgba(248, 113, 113, 0.75)";
        ctx.lineWidth = 4;
        ctx.lineCap = "round";
        ctx.shadowColor = "#f87171";
        ctx.shadowBlur = 10;
        const s = ringR * 0.55;
        ctx.beginPath();
        ctx.moveTo(cx - s, cy - s);
        ctx.lineTo(cx + s, cy + s);
        ctx.stroke();
        ctx.restore();
      }

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, ringR - 6, 0, Math.PI * 2);
      ctx.clip();

      const maxBarH = ringR * 0.72;
      const barW = ringR * 0.085;
      const gap = ringR * 0.095;
      const totalW = BAR_COUNT * barW + (BAR_COUNT - 1) * gap;
      const startX = cx - totalW / 2;

      for (let i = 0; i < BAR_COUNT; i++) {
        const level = Math.max(0.06, Math.min(1, barLevels.current[i]));
        const barH = maxBarH * level;
        const x = startX + i * (barW + gap);
        const y = cy - barH / 2;
        const r = barW / 2;

        ctx.save();
        ctx.shadowColor = color;
        ctx.shadowBlur = 14 + amp * 8;
        ctx.fillStyle = hexAlpha(color, 0.35);
        roundRect(ctx, x - 1, y - 1, barW + 2, barH + 2, r + 1);
        ctx.fill();
        ctx.restore();

        const grad = ctx.createLinearGradient(x, y, x, y + barH);
        grad.addColorStop(0, hexAlpha(color, 0.75));
        grad.addColorStop(0.5, color);
        grad.addColorStop(1, hexAlpha(color, 0.75));
        ctx.fillStyle = grad;
        ctx.shadowColor = color;
        ctx.shadowBlur = 10;
        roundRect(ctx, x, y, barW, barH, r);
        ctx.fill();
      }

      ctx.restore();

      raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    return () => {
      running = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      try { ro?.disconnect(); } catch { /* */ }
    };
  }, []);

  const rawStatus = (statusText || "").trim();
  const statusLooksGeneric =
    !rawStatus ||
    /^(waiting|ready|idle|standby)(\.{0,3})?$/i.test(rawStatus);

  const label = !statusLooksGeneric
    ? rawStatus
    : mode === "muted"
      ? "Mic Muted"
      : mode === "speaking"
        ? "Speaking"
        : mode === "listening"
          ? "Listening"
          : mode === "thinking"
            ? "Thinking"
            : mode === "sleep"
              ? "Sleeping"
              : mode === "error"
                ? "Fault"
                : mode === "success"
                  ? "Done"
                  : mode === "alert"
                    ? "Alert"
                    : "Ready";

  const isOfflineLabel = /^offline$/i.test(String(label || "").trim());
  const tone =
    isOfflineLabel
      ? "tone-offline"
      : mode === "muted"
        ? "tone-muted"
        : mode === "listening"
          ? "tone-listen"
          : mode === "speaking"
            ? "tone-speak"
            : mode === "thinking"
              ? "tone-think"
              : mode === "error"
                ? "tone-err"
                : mode === "success"
                  ? "tone-ok"
                  : mode === "sleep"
                    ? "tone-sleep"
                    : "tone-idle";

  return (
    <div className="relative flex flex-col items-center justify-center w-full h-full">
      <canvas ref={canvasRef} className="max-w-full max-h-full" />
      <div className="orb-status-wrap pointer-events-none">
        <div className={`orb-status-pill ${tone}`}>
          <span className="orb-status-dot" />
          <span className="orb-status-text">{label}</span>
        </div>
      </div>
    </div>
  );
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function hexAlpha(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, a))})`;
}
