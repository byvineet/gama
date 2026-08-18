import { useEffect, useRef, useState } from "react";

function fmt(sec: number) {
  const s = Math.max(0, Math.floor(Number.isFinite(sec) ? sec : 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function resolveEndsAt(data?: Record<string, unknown>): number | null {
  const raw = data?.ends_at ?? data?.endsAt;
  if (raw == null) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return null;
  // Heuristic: if value looks like seconds-since-epoch (< year 2100 in ms scale), treat as seconds
  // Normal ms timestamps for 2020–2100 are ~1.5e12–4e12
  if (n < 1e11) return n * 1000;
  return n;
}

function resolveRemaining(data?: Record<string, unknown>): number {
  const raw = data?.remaining_sec ?? data?.remainingSec ?? data?.total_sec ?? data?.totalSec ?? 0;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

export function Timer({ data }: { data?: Record<string, unknown> }) {
  const endsAt = resolveEndsAt(data);
  const running = data?.running !== false;
  const seedRemaining = resolveRemaining(data);

  // Prefer absolute end time when valid; otherwise pure countdown from remaining
  const [left, setLeft] = useState(() => {
    if (endsAt != null) return Math.max(0, (endsAt - Date.now()) / 1000);
    return seedRemaining;
  });

  // Keep a stable client-side end so interval doesn't depend on re-renders
  const endRef = useRef<number | null>(null);
  const remainingRef = useRef(seedRemaining);

  useEffect(() => {
    const now = Date.now();
    if (endsAt != null) {
      endRef.current = endsAt;
      remainingRef.current = Math.max(0, (endsAt - now) / 1000);
    } else {
      endRef.current = running ? now + seedRemaining * 1000 : null;
      remainingRef.current = seedRemaining;
    }
    setLeft(remainingRef.current);

    if (!running) return;

    const tick = () => {
      if (endRef.current != null) {
        const next = Math.max(0, (endRef.current - Date.now()) / 1000);
        setLeft(next);
      } else {
        setLeft((v) => Math.max(0, v - 0.25));
      }
    };
    tick();
    const id = window.setInterval(tick, 250);
    return () => window.clearInterval(id);
  }, [endsAt, running, seedRemaining]);

  const total =
    Number(data?.total_sec ?? data?.totalSec) ||
    (seedRemaining > 0 ? seedRemaining : left) ||
    1;
  const pct = Math.max(0, Math.min(100, ((total - left) / total) * 100));
  const done = left <= 0;

  return (
    <div className="gc-card gc-timer">
      <div className="gc-card-label">{String(data?.label || "TIMER")}</div>
      <div className={`gc-timer-value ${done ? "done" : ""}`}>{done ? "00:00" : fmt(left)}</div>
      <div className="gc-mini-bar wide">
        <div className="gc-mini-fill" style={{ width: `${done ? 100 : pct}%` }} />
      </div>
      <div className="gc-timer-state">{done ? "Complete" : running ? "Running" : "Paused"}</div>
    </div>
  );
}
