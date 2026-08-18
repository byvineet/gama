/**
 * GamaCanvas — Gama's visual output channel.
 * Supports free placement (position 0–1) and mouse drag-to-rearrange.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { displayStore } from "./DisplayStore";
import { SceneRenderer } from "./SceneRenderer";
import type { SceneNode } from "./DisplayProtocol";
import type { ActiveScene } from "./DisplayStore";

type Props = {
  clock: string;
  dateStr: string;
  statusText?: string;
  listening?: boolean;
  speaking?: boolean;
  muted?: boolean;
  amplitude?: number;
  primary?: string;
  offline?: boolean;
  tasks?: unknown[];
  goals?: unknown[];
  reminders?: unknown[];
  alerts?: unknown[];
  cpu?: number;
  ram?: number;
  disk?: number;
  onConfirm?: (yes: boolean) => void;
  onDisplayEvent?: (sceneId: string, event: string, elementId?: string, value?: unknown) => void;
};

function buildIdleScene(): SceneNode {
  return {
    id: "gama-idle",
    type: "idle",
    layer: 0,
    transition: { enter: "fade", duration: 400 },
  };
}

type DragState = {
  id: string;
  startX: number;
  startY: number;
  origX: number;
  origY: number;
};
type ResizeState = {
  id: string;
  startX: number;
  startY: number;
  origW: number;
  origH: number;
};

export function GamaCanvas(props: Props) {
  const rev = useSyncExternalStore(displayStore.subscribe, displayStore.getSnapshot);
  const scenes = useMemo(() => displayStore.getScenes(), [rev]);
  const rootRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [resize, setResize] = useState<ResizeState | null>(null);

  useEffect(() => {
    displayStore.ensureIdle(buildIdleScene());
  }, [rev]);

  const content = scenes.filter((s) => s.type !== "idle" || scenes.length === 1);
  const hasMain = content.some((s) => s.type !== "idle");

  // Free layout if any scene has explicit position, or more than one content card
  const freeLayout =
    hasMain &&
    (content.filter((s) => s.type !== "idle").length > 1 ||
      content.some((s) => s.position && (s.position.x != null || s.position.y != null)));

  const ctx = {
    clock: props.clock,
    dateStr: props.dateStr,
    statusText: props.statusText,
    listening: props.listening,
    speaking: props.speaking,
    muted: props.muted,
    amplitude: props.amplitude,
    primary: props.primary,
    offline: props.offline,
    snapshotExtras: {
      tasks: props.tasks,
      goals: props.goals,
      reminders: props.reminders,
      alerts: props.alerts,
      cpu: props.cpu,
      ram: props.ram,
      disk: props.disk,
    },
    onConfirm: props.onConfirm,
    onEvent: props.onDisplayEvent,
  };

  const onPointerDown = useCallback(
    (e: React.PointerEvent, scene: ActiveScene) => {
      if (scene.type === "idle") return;
      // Only primary button / touch
      if (e.button !== 0) return;
      // Ignore interactive controls inside the card
      const t = e.target as HTMLElement;
      if (t.closest("button, a, input, textarea, [data-no-drag]")) return;

      const root = rootRef.current;
      if (!root) return;
      const rect = root.getBoundingClientRect();
      const px = scene.position?.x;
      const py = scene.position?.y;
      // Default stacked positions if none set
      const origX = typeof px === "number" ? px : 0.5;
      const origY = typeof py === "number" ? py : 0.35;

      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      setDrag({
        id: scene.id,
        startX: e.clientX,
        startY: e.clientY,
        origX,
        origY,
      });
      e.preventDefault();
    },
    [],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const root = rootRef.current;
      if (!root) return;
      const rect = root.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) return;
      if (drag) {
        const dx = (e.clientX - drag.startX) / rect.width;
        const dy = (e.clientY - drag.startY) / rect.height;
        const x = Math.max(0.08, Math.min(0.92, drag.origX + dx));
        const y = Math.max(0.08, Math.min(0.92, drag.origY + dy));
        displayStore.apply({
          action: "update",
          scene_id: drag.id,
          position: { x, y },
        } as Parameters<typeof displayStore.apply>[0]);
      } else if (resize) {
        const dx = (e.clientX - resize.startX) / rect.width;
        const dy = (e.clientY - resize.startY) / rect.height;
        const w = Math.max(0.18, Math.min(0.95, resize.origW + dx));
        const h = Math.max(0.15, Math.min(0.95, resize.origH + dy));
        displayStore.apply({
          action: "update",
          scene_id: resize.id,
          size: { w, h },
        } as Parameters<typeof displayStore.apply>[0]);
      }
    },
    [drag, resize],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      try {
        (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
      } catch {
        /* */
      }
      if (drag) {
        const scene = displayStore.getScene(drag.id);
        if (scene?.position && props.onDisplayEvent) {
          props.onDisplayEvent(drag.id, "moved", undefined, scene.position);
        }
      }
      if (resize) {
        const scene = displayStore.getScene(resize.id);
        if (scene?.size && props.onDisplayEvent) {
          props.onDisplayEvent(resize.id, "resized", undefined, scene.size);
        }
      }
      setDrag(null);
      setResize(null);
    },
    [drag, resize, props],
  );

  return (
    <div
      ref={rootRef}
      className={`gama-canvas ${hasMain ? "has-content" : "is-idle"} ${freeLayout ? "free-layout" : "stack-layout"}`}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <div className="gc-layers">
        {content.map((scene) => {
          const positioned =
            freeLayout ||
            (scene.position && (scene.position.x != null || scene.position.y != null));
          const style: React.CSSProperties = positioned
            ? {
                position: "absolute",
                left: `${(scene.position?.x ?? 0.5) * 100}%`,
                top: `${(scene.position?.y ?? 0.4) * 100}%`,
                transform: "translate(-50%, -50%)",
                width: scene.size?.w != null ? `${scene.size.w * 100}%` : undefined,
                height: scene.size?.h != null ? `${scene.size.h * 100}%` : undefined,
                maxWidth: scene.size?.w != null ? undefined : "min(420px, 92%)",
                maxHeight: scene.size?.h != null ? undefined : "min(70%, 520px)",
                zIndex: 10 + (scene.layer || 0),
                cursor: scene.type === "idle" ? "default" : drag?.id === scene.id ? "grabbing" : "grab",
                touchAction: "none",
                display: "flex",
                flexDirection: "column",
              }
            : scene.type === "idle"
              ? {
                  width: "100%",
                  height: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }
              : {
                  maxWidth: "min(480px, 100%)",
                  margin: "0 auto",
                };

          return (
            <div
              key={`${scene.id}-${scene.updatedAt}`}
              className={`gc-scene-wrap ${drag?.id === scene.id ? "dragging" : ""} ${resize?.id === scene.id ? "resizing" : ""}`}
              style={style}
              onPointerDown={(e) => onPointerDown(e, scene)}
              data-scene-id={scene.id}
            >
              <SceneRenderer scene={scene} ctx={ctx} />
              {scene.type !== "idle" ? (
                <div
                  className="gc-resize"
                  title="Resize"
                  onPointerDown={(e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    const root = rootRef.current;
                    if (!root) return;
                    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
                    setResize({
                      id: scene.id,
                      startX: e.clientX,
                      startY: e.clientY,
                      origW: scene.size?.w ?? 0.38,
                      origH: scene.size?.h ?? 0.35,
                    });
                  }}
                />
              ) : null}
            </div>
          );
        })}
      </div>
      {hasMain ? (
        <div className="gc-hint" aria-hidden>
          Drag to move · corner to resize · “save this display as Morning”
        </div>
      ) : null}
    </div>
  );
}

export { displayStore };
