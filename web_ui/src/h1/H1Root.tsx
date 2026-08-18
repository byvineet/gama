/**
 * H1Root — fullscreen spatial gesture workspace.
 * Completely isolates from Nexus and D2 UI.
 */
import { useEffect, useSyncExternalStore, useCallback } from "react";
import { h1 } from "./core/H1Controller";
import { H1Scene } from "./scene/H1Scene";
import { h1GestureController } from "./gestures/H1GestureController";
import type { H1ObjectType } from "./core/H1State";
import { H1_COLORS } from "./core/H1State";
import "./h1.css";

type Props = {
  onRequestExit?: () => void;
};

export function H1Root({ onRequestExit }: Props) {
  const snap = useSyncExternalStore(h1.subscribe, h1.getSnapshot, h1.getSnapshot);

  useEffect(() => {
    if (snap.active) {
      h1GestureController.start();
      return () => {
        h1GestureController.stop();
      };
    }
    h1GestureController.stop();
    return undefined;
  }, [snap.active]);

  const add = useCallback((type: H1ObjectType) => {
    h1.addObject(type);
  }, []);

  const setColor = useCallback(
    (color: string) => {
      if (snap.selectedId) h1.setObjectColor(snap.selectedId, color);
    },
    [snap.selectedId],
  );

  if (!snap.active) return null;

  const entering = snap.visualState === "entering";
  const exiting = snap.visualState === "exiting";

  return (
    <div
      className={`h1-root ${entering ? "h1-entering" : ""} ${exiting ? "h1-exiting" : ""}`}
      role="application"
      aria-label="H1 spatial workspace"
    >
      <H1Scene snapshot={snap} />

      <div className="h1-hud">
        <div className="h1-brand">
          <span className="h1-badge">H1</span>
          <span className="h1-title">Spatial Workspace</span>
        </div>

        <div className="h1-create">
          <button type="button" className="h1-chip" onClick={() => add("cube")}>
            + Cube
          </button>
          <button type="button" className="h1-chip" onClick={() => add("cuboid")}>
            + Cuboid
          </button>
          <button type="button" className="h1-chip" onClick={() => add("sphere")}>
            + Sphere
          </button>
          <button type="button" className="h1-chip" onClick={() => add("pyramid")}>
            + Pyramid
          </button>
        </div>

        <div className={`h1-colors ${snap.selectedId ? "active" : "inactive"}`}>
          <span className="h1-colors-label">Color</span>
          {H1_COLORS.map((c) => (
            <button
              key={c}
              type="button"
              className="h1-swatch"
              style={{ background: c }}
              disabled={!snap.selectedId}
              onClick={() => setColor(c)}
              title={c}
            />
          ))}
        </div>

        <div className="h1-status">
          {!snap.gestureAvailable && snap.gestureMessage && (
            <span className="h1-hint">{snap.gestureMessage}</span>
          )}
          {snap.interactionState === "MOVE_OBJECT" && (
            <span className="h1-locked">Moving · release to place</span>
          )}
          {snap.interactionState === "RESIZE" && (
            <span className="h1-locked">Resize · both hands pinch</span>
          )}
          {snap.interactionState === "ROTATE_OBJECT" && (
            <span className="h1-locked">Spinning object</span>
          )}
          {snap.interactionState === "ROTATE_SCENE" && (
            <span className="h1-hover">Rotating scene</span>
          )}
          {snap.selectedId && !["MOVE_OBJECT","RESIZE","ROTATE_OBJECT"].includes(snap.interactionState) && (
            <span className="h1-locked">Selected · drag move · swipe spin · fling↓ delete</span>
          )}
          {snap.hoverId && !snap.selectedId && (
            <span className="h1-hover">Ready · pinch to grab</span>
          )}
          {!snap.selectedId && !snap.hoverId && snap.gestureAvailable && snap.interactionState === "IDLE" && (
            <span className="h1-hint">Swipe to spin scene · pinch object to grab</span>
          )}
        </div>

        {onRequestExit && (
          <button
            type="button"
            className="h1-exit"
            onClick={onRequestExit}
            title="Exit H1"
          >
            Exit
          </button>
        )}
      </div>
    </div>
  );
}

export function useH1() {
  return useSyncExternalStore(h1.subscribe, h1.getSnapshot, h1.getSnapshot);
}
