/**
 * H1Scene — Three.js spatial workspace over live camera.
 * Performance: pixelRatio 1, ~30 FPS, persistent geometries.
 */
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { h1 } from "../core/H1Controller";
import type { H1Object, H1StateSnapshot } from "../core/H1State";
import { h1GestureController } from "../gestures/H1GestureController";

type Props = {
  snapshot: H1StateSnapshot;
};

const FRAME_MS = 1000 / 30;

function createGeometry(type: H1Object["type"]): THREE.BufferGeometry {
  switch (type) {
    case "cube":
      return new THREE.BoxGeometry(1, 1, 1);
    case "cuboid":
      return new THREE.BoxGeometry(1, 1, 1);
    case "sphere":
      return new THREE.SphereGeometry(0.5, 24, 18);
    case "pyramid": {
      const geo = new THREE.ConeGeometry(0.55, 1, 4);
      geo.rotateY(Math.PI / 4);
      return geo;
    }
    default:
      return new THREE.BoxGeometry(1, 1, 1);
  }
}

function makeMaterial(color: string, emissiveIntensity = 0.15): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({
    color: new THREE.Color(color),
    emissive: new THREE.Color(color),
    emissiveIntensity,
    metalness: 0.15,
    roughness: 0.45,
    transparent: true,
    opacity: 0.92,
  });
}

export function H1Scene({ snapshot }: Props) {
  const mountRef = useRef<HTMLDivElement>(null);
  const videoBgRef = useRef<HTMLVideoElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const meshesRef = useRef<Map<string, THREE.Mesh>>(new Map());
  const frameRef = useRef(0);
  const groupRef = useRef<THREE.Group | null>(null);

  useEffect(() => {
    const el = mountRef.current;
    if (!el || !snapshot.active) return;

    const scene = new THREE.Scene();

    const camera = new THREE.PerspectiveCamera(
      40,
      el.clientWidth / Math.max(1, el.clientHeight),
      0.1,
      40,
    );
    camera.position.set(0, 0.15, 4.2);
    camera.lookAt(0, 0, 0);

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
      console.warn("[H1Scene] WebGL unavailable", e);
      h1.setGestureAvailable(false, "Spatial rendering unavailable");
      return;
    }
    renderer.setPixelRatio(1);
    renderer.setSize(el.clientWidth, el.clientHeight, false);
    renderer.setClearColor(0x000000, 0);
    el.appendChild(renderer.domElement);
    rendererRef.current = renderer;

    scene.add(new THREE.AmbientLight(0xffffff, 0.4));
    const key = new THREE.DirectionalLight(0xffffff, 0.7);
    key.position.set(2, 3, 4);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x88aaff, 0.25);
    fill.position.set(-2, 1, 2);
    scene.add(fill);

    const root = new THREE.Group();
    scene.add(root);
    groupRef.current = root;

    const grid = new THREE.GridHelper(6, 12, 0x1a3a4a, 0x0d1f2a);
    grid.position.y = -1.1;
    (grid.material as THREE.Material).opacity = 0.35;
    (grid.material as THREE.Material).transparent = true;
    root.add(grid);

    const onResize = () => {
      if (!el || !renderer) return;
      const w = el.clientWidth;
      const h = Math.max(1, el.clientHeight);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
      h1GestureController.setSceneRefs(
        camera,
        meshesRef.current as unknown as Map<string, THREE.Object3D>,
        w,
        h,
      );
    };
    window.addEventListener("resize", onResize);
    onResize();

    let lastFrameTs = 0;
    let lastLogicTs = performance.now();

    const animate = (now: number) => {
      frameRef.current = requestAnimationFrame(animate);
      if (now - lastFrameTs < FRAME_MS) return;
      lastFrameTs = now - ((now - lastFrameTs) % FRAME_MS);

      const dt = Math.min(0.05, (now - lastLogicTs) / 1000);
      lastLogicTs = now;

      const snap = h1.getSnapshot();
      if (!snap.active) return;

      h1.tick(dt);
      syncMeshes(snap, root);

      root.rotation.x = snap.sceneRotationX;
      root.rotation.y = snap.sceneRotationY;

      h1GestureController.setSceneRefs(
        camera,
        meshesRef.current as unknown as Map<string, THREE.Object3D>,
        el.clientWidth,
        el.clientHeight,
      );

      renderer.render(scene, camera);
    };
    frameRef.current = requestAnimationFrame(animate);

    return () => {
      cancelAnimationFrame(frameRef.current);
      window.removeEventListener("resize", onResize);
      meshesRef.current.forEach((m) => {
        m.geometry.dispose();
        (m.material as THREE.Material).dispose();
      });
      meshesRef.current.clear();
      renderer.dispose();
      if (renderer.domElement.parentNode) {
        renderer.domElement.parentNode.removeChild(renderer.domElement);
      }
      rendererRef.current = null;
      groupRef.current = null;
    };
  }, [snapshot.active]);

  useEffect(() => {
    if (!snapshot.active) return;
    const id = window.setInterval(() => {
      const v = h1GestureController.getVideo();
      if (v && videoBgRef.current && videoBgRef.current.srcObject !== v.srcObject) {
        videoBgRef.current.srcObject = v.srcObject;
        videoBgRef.current.play().catch(() => {});
      }
    }, 400);
    return () => clearInterval(id);
  }, [snapshot.active]);

  function syncMeshes(snap: H1StateSnapshot, root: THREE.Group) {
    const live = new Set(snap.objects.map((o) => o.id));

    meshesRef.current.forEach((mesh, id) => {
      if (!live.has(id)) {
        root.remove(mesh);
        mesh.geometry.dispose();
        (mesh.material as THREE.Material).dispose();
        meshesRef.current.delete(id);
      }
    });

    for (const obj of snap.objects) {
      let mesh = meshesRef.current.get(obj.id);
      if (!mesh) {
        const geo = createGeometry(obj.type);
        const mat = makeMaterial(obj.color);
        mesh = new THREE.Mesh(geo, mat);
        mesh.userData.h1Id = obj.id;
        root.add(mesh);
        meshesRef.current.set(obj.id, mesh);
      }

      mesh.position.set(...obj.position);
      mesh.rotation.set(...obj.rotation);
      mesh.scale.set(...obj.scale);

      const mat = mesh.material as THREE.MeshStandardMaterial;
      const isSelected = snap.selectedId === obj.id;
      const isHover = snap.hoverId === obj.id && !isSelected;

      const targetColor = new THREE.Color(obj.color);
      if (!mat.color.equals(targetColor)) {
        mat.color.copy(targetColor);
        mat.emissive.copy(targetColor);
      }

      if (isSelected) {
        mat.emissiveIntensity = 0.45;
        mat.opacity = 1;
      } else if (isHover) {
        mat.emissiveIntensity = 0.28;
        mat.opacity = 0.95;
      } else {
        mat.emissiveIntensity = 0.12;
        mat.opacity = 0.9;
      }
    }
  }

  return (
    <div className="h1-scene-wrap" ref={mountRef}>
      <video
        ref={videoBgRef}
        className="h1-camera-bg"
        playsInline
        muted
        autoPlay
      />
    </div>
  );
}
