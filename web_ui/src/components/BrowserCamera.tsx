import { useCallback, useEffect, useRef, useState } from "react";

const POS_KEY = "gama.cameraPreview.pos";
const SIZE_KEY = "gama.cameraPreview.size";

type Pos = { x: number; y: number };
type Size = { w: number; h: number };

const DEFAULT_SIZE: Size = { w: 420, h: 280 };
const MIN_W = 240;
const MIN_H = 160;

function loadPos(): Pos | null {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (typeof p?.x === "number" && typeof p?.y === "number") return p;
  } catch {
    /* */
  }
  return null;
}

function loadSize(): Size {
  try {
    const raw = localStorage.getItem(SIZE_KEY);
    if (!raw) return DEFAULT_SIZE;
    const s = JSON.parse(raw);
    if (typeof s?.w === "number" && typeof s?.h === "number") {
      return { w: Math.max(MIN_W, s.w), h: Math.max(MIN_H, s.h) };
    }
  } catch {
    /* */
  }
  return DEFAULT_SIZE;
}

/**
 * Instant smooth camera preview via the browser's getUserMedia API.
 * Resizeable + moveable within the presence stage. When enabled, the
 * voice orb is hidden and this panel fills the display state.
 *
 * Gemini Live vision runs on the backend separately; this component is
 * display-only and does not stream frames to the model.
 */
export function BrowserCamera({
  enabled,
  fillStage = false,
}: {
  enabled: boolean;
  /** When true, panel expands to fill the presence stage (orb hidden). */
  fillStage?: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string>("");
  const [ready, setReady] = useState(false);
  const [pos, setPos] = useState<Pos>(() => loadPos() ?? { x: 24, y: 24 });
  const [size, setSize] = useState<Size>(() => loadSize());
  const dragRef = useRef<{
    kind: "move" | "resize";
    startX: number;
    startY: number;
    origX: number;
    origY: number;
    origW: number;
    origH: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function start() {
      setError("");
      setReady(false);
      if (!enabled) return;
      if (!navigator.mediaDevices?.getUserMedia) {
        setError("Camera API not available in this browser");
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: {
            facingMode: "user",
            width: { ideal: 1280 },
            height: { ideal: 720 },
            frameRate: { ideal: 30 },
          },
        });
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        const el = videoRef.current;
        if (el) {
          el.srcObject = stream;
          el.style.transform = "scaleX(-1)";
          await el.play().catch(() => {});
          setReady(true);
        }
      } catch (e: unknown) {
        const msg =
          e && typeof e === "object" && "name" in e
            ? String((e as { name?: string }).name)
            : "error";
        if (msg === "NotAllowedError" || msg === "PermissionDeniedError") {
          setError("Camera permission denied — allow access in the browser");
        } else if (msg === "NotFoundError") {
          setError("No camera found");
        } else {
          setError("Could not open camera");
        }
      }
    }

    function stop() {
      const stream = streamRef.current;
      streamRef.current = null;
      if (stream) {
        stream.getTracks().forEach((t) => t.stop());
      }
      const el = videoRef.current;
      if (el) {
        el.srcObject = null;
      }
      setReady(false);
    }

    if (enabled) {
      start();
    } else {
      stop();
    }

    return () => {
      cancelled = true;
      stop();
    };
  }, [enabled]);

  useEffect(() => {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(pos));
    } catch {
      /* */
    }
  }, [pos]);

  useEffect(() => {
    try {
      localStorage.setItem(SIZE_KEY, JSON.stringify(size));
    } catch {
      /* */
    }
  }, [size]);

  const onPointerMove = useCallback((e: PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (d.kind === "move") {
      setPos({
        x: Math.max(0, d.origX + dx),
        y: Math.max(0, d.origY + dy),
      });
    } else {
      setSize({
        w: Math.max(MIN_W, d.origW + dx),
        h: Math.max(MIN_H, d.origH + dy),
      });
    }
  }, []);

  const onPointerUp = useCallback(() => {
    dragRef.current = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
  }, [onPointerMove]);

  const startDrag = (kind: "move" | "resize", e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragRef.current = {
      kind,
      startX: e.clientX,
      startY: e.clientY,
      origX: pos.x,
      origY: pos.y,
      origW: size.w,
      origH: size.h,
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  };

  if (!enabled) return null;

  const style: React.CSSProperties = fillStage
    ? {
        position: "absolute",
        inset: 12,
        width: "auto",
        height: "auto",
        zIndex: 20,
      }
    : {
        position: "absolute",
        left: pos.x,
        top: pos.y,
        width: size.w,
        height: size.h,
        zIndex: 20,
      };

  return (
    <div
      ref={panelRef}
      className={`browser-camera-panel${fillStage ? " browser-camera-fill" : ""}`}
      style={style}
    >
      <div
        className="browser-camera-header"
        onPointerDown={(e) => {
          if (!fillStage) startDrag("move", e);
        }}
        title={fillStage ? "Live camera" : "Drag to move"}
        style={{ cursor: fillStage ? "default" : "grab" }}
      >
        <span className="gc-live-dot" />
        <span>LIVE CAMERA</span>
        <span className="browser-camera-badge">{ready ? "BROWSER" : "…"}</span>
      </div>
      <div className="browser-camera-frame">
        <video
          ref={videoRef}
          className="browser-camera-video"
          playsInline
          muted
          autoPlay
        />
        {error ? <div className="browser-camera-error">{error}</div> : null}
        {!ready && !error ? (
          <div className="browser-camera-wait">Waiting for camera…</div>
        ) : null}
      </div>
      <div className="browser-camera-caption">
        {fillStage
          ? "Full display · Gemini sees via Live vision"
          : "Drag header to move · corner to resize · Gemini sees via background vision"}
      </div>
      {!fillStage ? (
        <div
          className="browser-camera-resize"
          onPointerDown={(e) => startDrag("resize", e)}
          title="Resize"
        />
      ) : null}
    </div>
  );
}
