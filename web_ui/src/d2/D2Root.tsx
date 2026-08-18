/**
 * D2Root — fullscreen secondary spatial interface.
 * Mounted when D2 mode is active. Hides all normal Gama UI.
 */
import { useEffect, useSyncExternalStore } from "react";
import { d2 } from "./core/D2Controller";
import { D2Scene } from "./scene/D2Scene";
import { CardRenderer } from "./cards/CardRenderer";
import { gestureController } from "./gestures/GestureController";
import "./d2.css";

type Props = {
  gamaAccent?: string;
  onRequestExit?: () => void;
};

export function D2Root({ gamaAccent = "#008FFF", onRequestExit }: Props) {
  const snap = useSyncExternalStore(d2.subscribe, d2.getSnapshot, d2.getSnapshot);

  useEffect(() => {
    d2.setGamaAccent(gamaAccent);
  }, [gamaAccent]);

  // MediaPipe lifecycle: start/stop ONLY when D2 active flag changes.
  // Do NOT depend on visualState — entering/idle/exiting must not restart camera.
  useEffect(() => {
    if (snap.active) {
      gestureController.start();
      return () => {
        gestureController.stop();
      };
    }
    gestureController.stop();
    return undefined;
  }, [snap.active]);

  if (!snap.active) return null;

  const entering = snap.visualState === "entering";
  const exiting = snap.visualState === "exiting";

  return (
    <div
      className={`d2-root ${entering ? "d2-entering" : ""} ${exiting ? "d2-exiting" : ""}`}
      role="application"
      aria-label="D2 spatial interface"
    >
      <header className="d2-topbar">
        <div className="d2-topbar-left">
          <span className="d2-brand">GAMA</span>
        </div>
        <div className="d2-topbar-center">
          {!snap.gestureAvailable && snap.gestureMessage && (
            <span className="d2-gesture-hint">{snap.gestureMessage}</span>
          )}
        </div>
        <div className="d2-topbar-right">
          <span className="d2-mode-badge">D2</span>
          {onRequestExit && (
            <button
              type="button"
              className="d2-exit-btn"
              onClick={onRequestExit}
              title="Return to Nexus"
            >
              Exit
            </button>
          )}
        </div>
      </header>

      <D2Scene snapshot={snap} quality="low" />

      <CardRenderer
        cards={snap.cards}
        primaryColor={snap.primaryColor}
        onDismiss={(id) => d2.removeCard(id)}
      />

      {snap.visualization.type !== "none" && (
        <div className="d2-viz-label">
          {snap.visualization.label || snap.visualization.type.toUpperCase()}
          {typeof snap.visualization.value === "number" && (
            <span> · {Math.round(snap.visualization.value)}%</span>
          )}
        </div>
      )}
    </div>
  );
}

export function useD2() {
  return useSyncExternalStore(d2.subscribe, d2.getSnapshot, d2.getSnapshot);
}
