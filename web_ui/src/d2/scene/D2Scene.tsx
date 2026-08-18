/**
 * D2Scene — Three.js fullscreen spatial environment (performance-tuned).
 * - pixelRatio forced to 1
 * - render capped ~30 FPS
 * - camera.lookAt only when zoom changes
 */
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { Orb } from "./Orb";
import { d2 } from "../core/D2Controller";
import type { D2StateSnapshot } from "../core/D2State";

type Props = {
  snapshot: D2StateSnapshot;
  quality?: "high" | "low";
};

const FRAME_MS = 1000 / 30; // ~30 FPS cap

export function D2Scene({ snapshot, quality = "low" }: Props) {
  const mountRef = useRef<HTMLDivElement>(null);
  const orbRef = useRef<Orb | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const frameRef = useRef<number>(0);

  useEffect(() => {
    const el = mountRef.current;
    if (!el || !snapshot.active) return;

    const scene = new THREE.Scene();

    const camera = new THREE.PerspectiveCamera(
      42,
      el.clientWidth / Math.max(1, el.clientHeight),
      0.1,
      50,
    );
    camera.position.set(0, 0.12, 3.6);
    camera.lookAt(0, 0, 0);
    let lastCamZ = camera.position.z;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: false,
        alpha: true,
        powerPreference: "high-performance",
        stencil: false,
        depth: true,
      });
    } catch (e) {
      console.warn("[D2Scene] WebGL unavailable", e);
      d2.setGestureAvailable(false, "Spatial rendering unavailable");
      return;
    }
    renderer.setPixelRatio(1);
    renderer.setSize(el.clientWidth, el.clientHeight, false);
    renderer.setClearColor(0x000000, 0);
    el.appendChild(renderer.domElement);
    rendererRef.current = renderer;

    scene.add(new THREE.AmbientLight(0xffffff, 0.35));

    const orb = new Orb();
    orb.setColor(snapshot.primaryColor);
    scene.add(orb.group);
    orbRef.current = orb;

    const onResize = () => {
      if (!el || !renderer) return;
      const w = el.clientWidth;
      const h = Math.max(1, el.clientHeight);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
    };
    window.addEventListener("resize", onResize);

    let lastFrameTs = 0;
    let lastLogicTs = performance.now();
    let lastVisualState = snapshot.visualState;
    let lastVizKey = JSON.stringify(snapshot.visualization);

    const animate = (now: number) => {
      frameRef.current = requestAnimationFrame(animate);

      // Cap rendering to ~30 FPS
      if (now - lastFrameTs < FRAME_MS) return;
      // absorb drift so we don't spiral
      lastFrameTs = now - ((now - lastFrameTs) % FRAME_MS);

      const dt = Math.min(0.05, (now - lastLogicTs) / 1000);
      lastLogicTs = now;

      const snap = d2.getSnapshot();
      if (!snap.active) return;

      if (snap.visualState !== lastVisualState) {
        orb.setVisualState(snap.visualState);
        lastVisualState = snap.visualState;
      }
      const vizKey = `${snap.visualization.type}|${snap.visualization.value}|${snap.visualization.label || ""}`;
      if (vizKey !== lastVizKey) {
        orb.setVisualization(snap.visualization);
        lastVizKey = vizKey;
      }

      orb.update(dt, snap.zoom, snap.rotationX, snap.rotationY, snap.dispersion ?? 0, !!snap.exploded);

      const camZ = 3.6 - (snap.zoom - 1) * 0.5;
      if (Math.abs(camZ - lastCamZ) > 0.0005) {
        camera.position.z = camZ;
        camera.lookAt(0, 0, 0);
        lastCamZ = camZ;
      }

      renderer.render(scene, camera);
    };
    frameRef.current = requestAnimationFrame(animate);

    return () => {
      cancelAnimationFrame(frameRef.current);
      window.removeEventListener("resize", onResize);
      orb.dispose();
      orbRef.current = null;
      renderer.dispose();
      if (renderer.domElement.parentNode === el) {
        el.removeChild(renderer.domElement);
      }
      rendererRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot.active, quality]);

  useEffect(() => {
    const orb = orbRef.current;
    if (!orb) return;
    orb.setColor(snapshot.primaryColor);
    orb.setVisualState(snapshot.visualState);
    orb.setVisualization(snapshot.visualization);
  }, [snapshot.primaryColor, snapshot.visualState, snapshot.visualization]);

  if (!snapshot.active) return null;

  return (
    <div
      ref={mountRef}
      className="d2-scene"
      style={{
        position: "absolute",
        inset: 0,
        zIndex: 1,
        overflow: "hidden",
        background:
          "radial-gradient(ellipse at 50% 45%, #0a1220 0%, #03060c 55%, #010204 100%)",
      }}
    />
  );
}
