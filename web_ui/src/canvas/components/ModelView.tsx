/**
 * ModelView — isometric 3D / CAD mesh preview on the Gama canvas.
 * - Auto-rotate (hold or drag pauses)
 * - Drag inside stage: orbit (yaw/pitch)
 * - Shift+drag or right-drag: pan model in view
 * - Wheel / +/- : zoom
 * - Prefer mesh (build123d STL) when present; else parametric primitives
 */

import { useEffect, useMemo, useRef, useState, type ReactNode, type PointerEvent, type WheelEvent } from "react";

type Prim = {
  type?: string;
  x?: number;
  y?: number;
  z?: number;
  w?: number;
  h?: number;
  d?: number;
  r?: number;
  color?: string;
};

type MeshData = {
  vertices?: number[][];
  faces?: number[][];
  color?: string;
  source?: string;
};

type Props = { data?: Record<string, unknown> };

function project(
  x: number,
  y: number,
  z: number,
  yaw: number,
  pitch: number,
  scale: number,
  panX: number,
  panY: number
) {
  const yr = (yaw * Math.PI) / 180;
  const pr = (pitch * Math.PI) / 180;
  const x1 = x * Math.cos(yr) + z * Math.sin(yr);
  const z1 = -x * Math.sin(yr) + z * Math.cos(yr);
  const y1 = y * Math.cos(pr) - z1 * Math.sin(pr);
  const z2 = y * Math.sin(pr) + z1 * Math.cos(pr);
  return {
    x: 200 + panX + x1 * scale,
    y: 160 + panY - y1 * scale,
    depth: z2,
  };
}

function shade(hex: string, f: number): string {
  const h = (hex || "#38bdf8").replace("#", "");
  if (h.length !== 6) return hex || "#38bdf8";
  const r = Math.max(0, Math.min(255, Math.round(parseInt(h.slice(0, 2), 16) * f)));
  const g = Math.max(0, Math.min(255, Math.round(parseInt(h.slice(2, 4), 16) * f)));
  const b = Math.max(0, Math.min(255, Math.round(parseInt(h.slice(4, 6), 16) * f)));
  return `rgb(${r},${g},${b})`;
}

function boxFaces(p: Prim, yaw: number, pitch: number, scale: number, panX: number, panY: number) {
  const x = p.x ?? 0,
    y = p.y ?? 0,
    z = p.z ?? 0;
  const hx = (p.w ?? 1) / 2,
    hy = (p.h ?? 1) / 2,
    hz = (p.d ?? 1) / 2;
  const corners = [
    [x - hx, y - hy, z - hz],
    [x + hx, y - hy, z - hz],
    [x + hx, y + hy, z - hz],
    [x - hx, y + hy, z - hz],
    [x - hx, y - hy, z + hz],
    [x + hx, y - hy, z + hz],
    [x + hx, y + hy, z + hz],
    [x - hx, y + hy, z + hz],
  ].map(([a, b, c]) => project(a, b, c, yaw, pitch, scale, panX, panY));
  const facesIdx = [
    [0, 1, 2, 3],
    [4, 5, 6, 7],
    [0, 1, 5, 4],
    [3, 2, 6, 7],
    [0, 3, 7, 4],
    [1, 2, 6, 5],
  ];
  const factors = [0.55, 0.75, 0.45, 1.0, 0.65, 0.85];
  return facesIdx.map((idx, i) => {
    const pts = idx.map((k) => corners[k]);
    const depth = pts.reduce((s, q) => s + q.depth, 0) / pts.length;
    const points = pts.map((q) => `${q.x.toFixed(1)},${q.y.toFixed(1)}`).join(" ");
    return { points, depth, fill: shade(String(p.color || "#38bdf8"), factors[i]) };
  });
}

function sphereApprox(p: Prim, yaw: number, pitch: number, scale: number, panX: number, panY: number) {
  const c = project(p.x ?? 0, p.y ?? 0, p.z ?? 0, yaw, pitch, scale, panX, panY);
  const r = (p.r ?? 0.4) * scale;
  return { cx: c.x, cy: c.y, r, depth: c.depth, color: String(p.color || "#38bdf8") };
}

function cylinderApprox(p: Prim, yaw: number, pitch: number, scale: number, panX: number, panY: number) {
  const r = p.r ?? 0.35;
  const h = p.h ?? 1;
  const x = p.x ?? 0,
    y = p.y ?? 0,
    z = p.z ?? 0;
  const top = project(x, y + h / 2, z, yaw, pitch, scale, panX, panY);
  const bot = project(x, y - h / 2, z, yaw, pitch, scale, panX, panY);
  const rx = r * scale * 0.9;
  const ry = r * scale * 0.45;
  return {
    top,
    bot,
    rx,
    ry,
    depth: (top.depth + bot.depth) / 2,
    color: String(p.color || "#38bdf8"),
  };
}

export function ModelView({ data }: Props) {
  const prims = (Array.isArray(data?.primitives) ? data!.primitives : []) as Prim[];
  const mesh = (data?.mesh && typeof data.mesh === "object" ? data.mesh : null) as MeshData | null;
  const title = String(data?.title || data?.prompt || "3D Model");
  const baseColor = String((mesh && mesh.color) || data?.color || "#38bdf8");
  const isCad = Boolean(mesh?.vertices?.length && mesh?.faces?.length);

  const [yaw, setYaw] = useState(Number(data?.yaw ?? 38));
  const [pitch, setPitch] = useState(Number(data?.pitch ?? 28));
  const [zoom, setZoom] = useState(Number(data?.zoom ?? 1) || 1);
  const [panX, setPanX] = useState(0);
  const [panY, setPanY] = useState(0);
  const [holding, setHolding] = useState(false);
  const [spinning, setSpinning] = useState(data?.auto_rotate !== false);
  const dragMode = useRef<"orbit" | "pan" | null>(null);
  const lastPtr = useRef({ x: 0, y: 0 });
  const holdRef = useRef(false);

  useEffect(() => {
    if (!spinning || holding) return;
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      if (!holdRef.current && !dragMode.current) {
        setYaw((y) => (y + dt * 28) % 360);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [spinning, holding]);

  const scale = 70 * Math.max(0.45, Math.min(2.4, zoom));

  const elements = useMemo(() => {
    const items: { depth: number; node: ReactNode; key: string }[] = [];

    if (isCad && mesh?.vertices && mesh?.faces) {
      const verts = mesh.vertices;
      const faces = mesh.faces;
      // Cap for SVG performance
      const limit = Math.min(faces.length, 1800);
      for (let i = 0; i < limit; i++) {
        const f = faces[i];
        if (!f || f.length < 3) continue;
        const pts3 = f.slice(0, 3).map((idx) => {
          const v = verts[idx] || [0, 0, 0];
          return project(v[0], v[1], v[2], yaw, pitch, scale, panX, panY);
        });
        const depth = (pts3[0].depth + pts3[1].depth + pts3[2].depth) / 3;
        // Face normal-ish shade from depth + orientation
        const ax = pts3[1].x - pts3[0].x;
        const ay = pts3[1].y - pts3[0].y;
        const bx = pts3[2].x - pts3[0].x;
        const by = pts3[2].y - pts3[0].y;
        const cross = ax * by - ay * bx;
        const factor = 0.55 + Math.max(0, Math.min(0.45, (cross + 400) / 1600));
        const points = pts3.map((q) => `${q.x.toFixed(1)},${q.y.toFixed(1)}`).join(" ");
        items.push({
          depth,
          key: `m${i}`,
          node: (
            <polygon
              key={`m${i}`}
              points={points}
              fill={shade(baseColor, factor)}
              stroke="rgba(15,23,42,0.2)"
              strokeWidth={0.4}
            />
          ),
        });
      }
      return items.sort((a, b) => a.depth - b.depth);
    }

    prims.forEach((p, i) => {
      const t = String(p.type || "box").toLowerCase();
      if (t === "sphere") {
        const s = sphereApprox(p, yaw, pitch, scale, panX, panY);
        items.push({
          depth: s.depth,
          key: `s${i}`,
          node: (
            <g key={`s${i}`}>
              <ellipse
                cx={s.cx}
                cy={s.cy}
                rx={s.r}
                ry={s.r * 0.95}
                fill={shade(s.color, 0.85)}
                stroke={shade(s.color, 1.1)}
                strokeWidth={1.5}
              />
              <ellipse
                cx={s.cx - s.r * 0.25}
                cy={s.cy - s.r * 0.28}
                rx={s.r * 0.28}
                ry={s.r * 0.18}
                fill="#ffffff"
                opacity={0.25}
              />
            </g>
          ),
        });
      } else if (t === "cylinder" || t === "cone") {
        const c = cylinderApprox(p, yaw, pitch, scale, panX, panY);
        const isCone = t === "cone";
        const topRx = isCone ? c.rx * 0.15 : c.rx;
        const topRy = isCone ? c.ry * 0.15 : c.ry;
        items.push({
          depth: c.depth,
          key: `c${i}`,
          node: (
            <g key={`c${i}`}>
              <path
                d={`M ${c.bot.x - c.rx} ${c.bot.y} L ${c.top.x - topRx} ${c.top.y} L ${c.top.x + topRx} ${c.top.y} L ${c.bot.x + c.rx} ${c.bot.y} Z`}
                fill={shade(c.color, 0.7)}
              />
              <ellipse cx={c.bot.x} cy={c.bot.y} rx={c.rx} ry={c.ry} fill={shade(c.color, 0.55)} />
              <ellipse
                cx={c.top.x}
                cy={c.top.y}
                rx={topRx}
                ry={topRy}
                fill={shade(c.color, 0.95)}
                stroke={shade(c.color, 1.1)}
                strokeWidth={1}
              />
            </g>
          ),
        });
      } else {
        boxFaces(p, yaw, pitch, scale, panX, panY)
          .sort((a, b) => a.depth - b.depth)
          .forEach((f, fi) => {
            items.push({
              depth: f.depth,
              key: `b${i}-${fi}`,
              node: (
                <polygon
                  key={`b${i}-${fi}`}
                  points={f.points}
                  fill={f.fill}
                  stroke="rgba(15,23,42,0.35)"
                  strokeWidth={1}
                />
              ),
            });
          });
      }
    });
    return items.sort((a, b) => a.depth - b.depth);
  }, [prims, mesh, isCad, yaw, pitch, scale, panX, panY, baseColor]);

  const onPtrDown = (e: PointerEvent) => {
    e.stopPropagation();
    holdRef.current = true;
    setHolding(true);
    const pan = e.shiftKey || e.button === 2 || e.altKey;
    dragMode.current = pan ? "pan" : "orbit";
    lastPtr.current = { x: e.clientX, y: e.clientY };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  };
  const onPtrMove = (e: PointerEvent) => {
    if (!dragMode.current) return;
    e.stopPropagation();
    const dx = e.clientX - lastPtr.current.x;
    const dy = e.clientY - lastPtr.current.y;
    lastPtr.current = { x: e.clientX, y: e.clientY };
    if (dragMode.current === "orbit") {
      setYaw((y) => y + dx * 0.45);
      setPitch((p) => Math.max(-80, Math.min(80, p - dy * 0.35)));
    } else {
      setPanX((x) => x + dx);
      setPanY((y) => y + dy);
    }
  };
  const onPtrUp = (e: PointerEvent) => {
    e.stopPropagation();
    holdRef.current = false;
    setHolding(false);
    dragMode.current = null;
  };

  const onWheel = (e: WheelEvent) => {
    e.stopPropagation();
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.08 : 0.08;
    setZoom((z) => Math.max(0.45, Math.min(2.4, z + delta)));
  };

  return (
    <div className="gc-card gc-model-card">
      <div className="gc-card-label">
        <span className="gc-model-badge">{isCad ? "CAD" : "3D"}</span> {title.slice(0, 26)}
        {holding ? (
          <span className="gc-model-hold">HOLD</span>
        ) : spinning ? (
          <span className="gc-model-spin">SPIN</span>
        ) : null}
      </div>
      <div
        className="gc-model-stage"
        data-no-drag
        onPointerDown={onPtrDown}
        onPointerMove={onPtrMove}
        onPointerUp={onPtrUp}
        onPointerCancel={onPtrUp}
        onContextMenu={(e) => e.preventDefault()}
        onWheel={onWheel}
        title="Drag to orbit · Shift+drag to pan · Scroll to zoom · Hold pauses spin"
      >
        <svg viewBox="0 0 400 320" className="gc-model-svg" role="img" aria-label={title}>
          <defs>
            <radialGradient id="gcFloor" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgba(56,189,248,0.12)" />
              <stop offset="100%" stopColor="rgba(0,0,0,0)" />
            </radialGradient>
          </defs>
          <ellipse
            cx={200 + panX}
            cy={250 + panY}
            rx={120 * Math.min(1.3, zoom)}
            ry={28 * Math.min(1.2, zoom)}
            fill="url(#gcFloor)"
          />
          {elements.map((e) => e.node)}
        </svg>
      </div>
      <div className="gc-model-controls" data-no-drag>
        <button
          type="button"
          className="gc-model-btn"
          onClick={(e) => {
            e.stopPropagation();
            setZoom((z) => Math.max(0.45, z - 0.15));
          }}
          title="Zoom out"
        >
          −
        </button>
        <button
          type="button"
          className="gc-model-btn"
          onClick={(e) => {
            e.stopPropagation();
            setSpinning((s) => !s);
          }}
          title={spinning ? "Pause spin" : "Resume spin"}
        >
          {spinning ? "❚❚" : "▶"}
        </button>
        <button
          type="button"
          className="gc-model-btn"
          onClick={(e) => {
            e.stopPropagation();
            setPanX(0);
            setPanY(0);
            setPitch(28);
            setYaw(38);
            setZoom(1);
          }}
          title="Reset view"
        >
          ⟲
        </button>
        <span className="gc-model-meta">
          {isCad ? `${mesh?.faces?.length || 0} faces` : `${prims.length} prim`} · {Math.round(zoom * 100)}%
        </span>
        <button
          type="button"
          className="gc-model-btn"
          onClick={(e) => {
            e.stopPropagation();
            setYaw((y) => y - 20);
          }}
          title="Nudge left"
        >
          ↺
        </button>
        <button
          type="button"
          className="gc-model-btn"
          onClick={(e) => {
            e.stopPropagation();
            setYaw((y) => y + 20);
          }}
          title="Nudge right"
        >
          ↻
        </button>
        <button
          type="button"
          className="gc-model-btn"
          onClick={(e) => {
            e.stopPropagation();
            setZoom((z) => Math.min(2.4, z + 0.15));
          }}
          title="Zoom in"
        >
          +
        </button>
      </div>
      <p className="gc-model-hint" data-no-drag>
        Drag orbit · Shift-drag pan · Scroll zoom
      </p>
    </div>
  );
}
