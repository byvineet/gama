/**
 * Gama Core — lightweight holographic brain / main core for D2.
 * Wireframe cortex shell, neural arcs, icosa nucleus, sparse dust & debris.
 */
import * as THREE from "three";
import type { D2VisualState, D2Visualization } from "../core/D2State";

const DUST_COUNT = 28;
const DEBRIS_COUNT = 6;
const SYNAPSE_COUNT = 10;
const NEURAL_ARCS = 4;
const LAT_RINGS = 6;
const MERIDIANS = 8;

const CODE_SNIPPETS = [
  "GAMA.CORE", "neural.fire", ">> SYNC", "ACK", "heap:ok", "ctx.bind",
  "0xCORE", ">>> RDY", "think()", "attn.q", "embed", "fn main",
  "async {}", "spawn()", "AES", "IRQ", "mem.ok", "mutex",
  "DMA", "fork()", "pipe", "REG", "HTTP/2", "impl Core",
];

interface DebrisOrbit {
  orbitR: number;
  speed: number;
  tiltX: number;
  tiltZ: number;
  phase: number;
}

interface SpriteDrift {
  phi: number;
  theta: number;
  r: number;
  speed: number;
}

function lineMat(color: THREE.Color, opacity: number) {
  return new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
}

function latRing(radius: number, lat: number, segs = 56) {
  const r = radius * Math.cos(lat);
  const y = radius * Math.sin(lat);
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i <= segs; i++) {
    const a = (i / segs) * Math.PI * 2;
    pts.push(new THREE.Vector3(r * Math.cos(a), y, r * Math.sin(a)));
  }
  return new THREE.BufferGeometry().setFromPoints(pts);
}

function meridian(radius: number, lon: number, segs = 56) {
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i <= segs; i++) {
    const lat = (i / segs) * Math.PI - Math.PI / 2;
    pts.push(
      new THREE.Vector3(
        radius * Math.cos(lat) * Math.cos(lon),
        radius * Math.sin(lat),
        radius * Math.cos(lat) * Math.sin(lon),
      ),
    );
  }
  return new THREE.BufferGeometry().setFromPoints(pts);
}

/** Quadratic-ish arc from core toward shell (neural pathway). */
function neuralArc(seed: number, R: number): THREE.BufferGeometry {
  const phi0 = ((seed * 1.7) % 1) * Math.PI - Math.PI / 2;
  const theta0 = ((seed * 2.3) % 1) * Math.PI * 2;
  const phi1 = phi0 + (Math.sin(seed * 5.1) * 0.55);
  const theta1 = theta0 + (Math.cos(seed * 3.7) * 0.9);
  const pts: THREE.Vector3[] = [];
  const segs = 24;
  for (let i = 0; i <= segs; i++) {
    const t = i / segs;
    const r = 0.18 + t * (R - 0.18);
    const phi = phi0 + (phi1 - phi0) * t;
    const theta = theta0 + (theta1 - theta0) * t;
    // slight outward bulge mid-arc
    const bulge = Math.sin(t * Math.PI) * 0.12;
    pts.push(
      new THREE.Vector3(
        (r + bulge) * Math.cos(phi) * Math.cos(theta),
        (r + bulge) * Math.sin(phi),
        (r + bulge) * Math.cos(phi) * Math.sin(theta),
      ),
    );
  }
  return new THREE.BufferGeometry().setFromPoints(pts);
}

export class Orb {
  readonly group = new THREE.Group();
  private outerShell = new THREE.Group();
  private neuralGroup = new THREE.Group();
  private coreGroup = new THREE.Group();
  private dust: THREE.Points;
  private debris: THREE.Mesh[] = [];
  private synapses: THREE.Mesh[] = [];
  private textGroup = new THREE.Group();
  private scanRing1: THREE.Mesh;
  private scanRing2: THREE.Mesh;
  private icoWire: THREE.LineSegments;
  private icoWireInner: THREE.LineSegments;
  private coreSphere: THREE.Mesh;
  private glowSphere: THREE.Mesh;
  private nucleusRing: THREE.Line;
  private primary = new THREE.Color("#008FFF");
  private mid = new THREE.Color("#008FFF");
  private dim = new THREE.Color("#008FFF");
  private visualState: D2VisualState = "idle";
  private viz: D2Visualization = { type: "none" };
  private t = 0;
  private intensity = 1;
  private enterProgress = 0;
  private lastColorHex = "";
  private lastVizKey = "";
  private frameCount = 0;
  private R1 = 1.08;
  private R3 = 0.28;
  /** Original dust positions for dispersion / explode. */
  private dustHome: Float32Array | null = null;
  /** Smoothed visual dispersion (includes explode boost). */
  private dispVisual = 0;

  constructor() {
    this.mid.copy(this.primary).multiplyScalar(0.78);
    this.dim.copy(this.primary).multiplyScalar(0.32);

    // ——— CORTEX SHELL (wireframe brain envelope) ———
    for (let i = 0; i < LAT_RINGS; i++) {
      const lat = ((i / (LAT_RINGS - 1)) * 2 - 1) * (Math.PI / 2) * 0.9;
      const major = i % 2 === 0;
      this.outerShell.add(
        new THREE.Line(
          latRing(this.R1, lat, major ? 48 : 32),
          lineMat(major ? this.mid : this.dim, major ? 0.42 : 0.11),
        ),
      );
    }
    for (let i = 0; i < MERIDIANS; i++) {
      const lon = (i / MERIDIANS) * Math.PI * 2;
      const major = i % 3 === 0;
      this.outerShell.add(
        new THREE.Line(
          meridian(this.R1, lon, major ? 48 : 32),
          lineMat(major ? this.mid : this.dim, major ? 0.48 : 0.1),
        ),
      );
    }
    // Double equator — "core belt"
    for (const off of [-0.03, 0, 0.03]) {
      this.outerShell.add(
        new THREE.Line(
          latRing(this.R1, off, 96),
          lineMat(this.primary, 0.55 - Math.abs(off) * 8),
        ),
      );
    }
    this.group.add(this.outerShell);

    // ——— NEURAL PATHWAYS (core → cortex) ———
    for (let i = 0; i < NEURAL_ARCS; i++) {
      this.neuralGroup.add(
        new THREE.Line(neuralArc(i + 1.3, this.R1 * 0.95), lineMat(this.mid, 0.28)),
      );
    }
    this.group.add(this.neuralGroup);

    // ——— NUCLEUS: dual icosa + glow (Gama's brain core) ———
    const icoOuter = new THREE.IcosahedronGeometry(this.R3, 1);
    this.icoWire = new THREE.LineSegments(
      new THREE.EdgesGeometry(icoOuter),
      lineMat(this.primary, 0.9),
    );
    this.coreGroup.add(this.icoWire);

    const icoInner = new THREE.IcosahedronGeometry(this.R3 * 0.55, 0);
    this.icoWireInner = new THREE.LineSegments(
      new THREE.EdgesGeometry(icoInner),
      lineMat(this.primary, 0.7),
    );
    this.coreGroup.add(this.icoWireInner);

    this.coreSphere = new THREE.Mesh(
      new THREE.SphereGeometry(0.14, 16, 16),
      new THREE.MeshBasicMaterial({
        color: this.primary,
        transparent: true,
        opacity: 0.22,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    );
    this.coreGroup.add(this.coreSphere);

    this.glowSphere = new THREE.Mesh(
      new THREE.SphereGeometry(0.5, 16, 16),
      new THREE.MeshBasicMaterial({
        color: this.primary,
        transparent: true,
        opacity: 0.06,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    );
    this.coreGroup.add(this.glowSphere);

    // Nucleus ring (orbital plane of the core)
    const ringPts: THREE.Vector3[] = [];
    for (let i = 0; i <= 64; i++) {
      const a = (i / 64) * Math.PI * 2;
      ringPts.push(new THREE.Vector3(Math.cos(a) * 0.42, 0, Math.sin(a) * 0.42));
    }
    this.nucleusRing = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(ringPts),
      lineMat(this.primary, 0.4),
    );
    this.nucleusRing.rotation.x = 0.35;
    this.coreGroup.add(this.nucleusRing);
    this.group.add(this.coreGroup);

    // ——— SYNAPSE NODES on cortex ———
    const nodeGeo = new THREE.SphereGeometry(0.022, 8, 8);
    for (let i = 0; i < SYNAPSE_COUNT; i++) {
      const mat = new THREE.MeshBasicMaterial({
        color: this.primary,
        transparent: true,
        opacity: 0.65,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(nodeGeo, mat);
      const phi = Math.acos(2 * ((i + 0.5) / SYNAPSE_COUNT) - 1);
      const theta = Math.PI * (1 + Math.sqrt(5)) * i;
      mesh.position.set(
        this.R1 * Math.sin(phi) * Math.cos(theta),
        this.R1 * Math.cos(phi),
        this.R1 * Math.sin(phi) * Math.sin(theta),
      );
      mesh.userData = { phase: Math.random() * Math.PI * 2, base: mesh.position.clone() };
      this.synapses.push(mesh);
      this.group.add(mesh);
    }

    // ——— DUST ———
    const dustPos = new Float32Array(DUST_COUNT * 3);
    for (let i = 0; i < DUST_COUNT; i++) {
      const r = 0.45 + Math.random() * 1.3;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      dustPos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      dustPos[i * 3 + 1] = r * Math.cos(phi);
      dustPos[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
    }
    const dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute("position", new THREE.BufferAttribute(dustPos, 3));
    this.dustHome = dustPos.slice(0);
    this.dust = new THREE.Points(
      dustGeo,
      new THREE.PointsMaterial({
        color: this.primary,
        size: 0.011,
        transparent: true,
        opacity: 0.5,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        sizeAttenuation: true,
      }),
    );
    this.group.add(this.dust);

    // ——— DEBRIS (orbiting fragments) ———
    const debrisGeos = [
      new THREE.IcosahedronGeometry(0.016, 0),
      new THREE.TetrahedronGeometry(0.018, 0),
      new THREE.OctahedronGeometry(0.014, 0),
    ];
    for (let i = 0; i < DEBRIS_COUNT; i++) {
      const geo = debrisGeos[i % debrisGeos.length];
      const mat = new THREE.MeshBasicMaterial({
        color: this.primary,
        transparent: true,
        opacity: 0.35 + Math.random() * 0.35,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.userData = {
        orbitR: 1.3 + Math.random() * 0.45,
        speed: 0.12 + Math.random() * 0.28,
        tiltX: (Math.random() - 0.5) * 0.7,
        tiltZ: (Math.random() - 0.5) * 0.5,
        phase: Math.random() * Math.PI * 2,
      } satisfies DebrisOrbit;
      this.debris.push(mesh);
      this.group.add(mesh);
    }

    // ——— CODE SPRITES (sparse telemetry) ———
    for (let i = 0; i < 6; i++) {
      const text = CODE_SNIPPETS[i % CODE_SNIPPETS.length];
      const canvas = document.createElement("canvas");
      canvas.width = 128;
      canvas.height = 24;
      const ctx = canvas.getContext("2d")!;
      ctx.font = "bold 11px Courier New, monospace";
      ctx.fillStyle = `rgba(160, 210, 255, ${0.35 + Math.random() * 0.4})`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, 64, 12);
      const tex = new THREE.CanvasTexture(canvas);
      tex.minFilter = THREE.LinearFilter;
      const sp = new THREE.Sprite(
        new THREE.SpriteMaterial({
          map: tex,
          transparent: true,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        }),
      );
      const phi = Math.acos(2 * Math.random() - 1);
      const theta = Math.random() * Math.PI * 2;
      const r = 0.65 + Math.random() * 0.65;
      sp.scale.set(0.26, 0.05, 1);
      sp.userData = {
        phi,
        theta,
        r,
        speed: (0.06 + Math.random() * 0.1) * (Math.random() > 0.5 ? 1 : -1),
      } satisfies SpriteDrift;
      this.textGroup.add(sp);
    }
    this.group.add(this.textGroup);

    // ——— SCAN RINGS ———
    const makeScan = (radius: number, thickness: number) => {
      const geo = new THREE.RingGeometry(radius - thickness, radius, 64);
      const mat = new THREE.MeshBasicMaterial({
        color: this.primary,
        transparent: true,
        opacity: 0.2,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.rotation.x = Math.PI / 2;
      return mesh;
    };
    this.scanRing1 = makeScan(this.R1, 0.012);
    this.scanRing2 = makeScan(this.R3 * 1.5, 0.01);
    this.group.add(this.scanRing1, this.scanRing2);

    this.group.scale.setScalar(0.001);
  }

  setColor(hex: string) {
    if (hex === this.lastColorHex) return;
    this.lastColorHex = hex;
    this.primary.set(hex);
    this.mid.copy(this.primary).multiplyScalar(0.78);
    this.dim.copy(this.primary).multiplyScalar(0.32);

    const applyLine = (obj: THREE.Object3D, color: THREE.Color) => {
      if (obj instanceof THREE.Line || obj instanceof THREE.LineSegments) {
        (obj.material as THREE.LineBasicMaterial).color.copy(color);
      }
    };
    this.outerShell.traverse((o) => applyLine(o, this.mid));
    this.neuralGroup.traverse((o) => applyLine(o, this.mid));
    (this.icoWire.material as THREE.LineBasicMaterial).color.copy(this.primary);
    (this.icoWireInner.material as THREE.LineBasicMaterial).color.copy(this.primary);
    (this.nucleusRing.material as THREE.LineBasicMaterial).color.copy(this.primary);
    (this.coreSphere.material as THREE.MeshBasicMaterial).color.copy(this.primary);
    (this.glowSphere.material as THREE.MeshBasicMaterial).color.copy(this.primary);
    (this.dust.material as THREE.PointsMaterial).color.copy(this.primary);
    (this.scanRing1.material as THREE.MeshBasicMaterial).color.copy(this.primary);
    (this.scanRing2.material as THREE.MeshBasicMaterial).color.copy(this.primary);
    this.debris.forEach((d) => {
      (d.material as THREE.MeshBasicMaterial).color.copy(this.primary);
    });
    this.synapses.forEach((s) => {
      (s.material as THREE.MeshBasicMaterial).color.copy(this.primary);
    });
  }

  setVisualState(state: D2VisualState) {
    if (state === this.visualState) return;
    this.visualState = state;
  }
  setVisualization(viz: D2Visualization) {
    this.viz = viz;
  }
  setIntensity(v: number) {
    this.intensity = Math.max(0.3, Math.min(1.5, v));
  }

  update(dt: number, zoom: number, rotX: number, rotY: number, dispersion = 0, exploded = false) {
    this.t += dt;
    this.frameCount++;
    const heavy = this.frameCount % 2 === 0;
    const state = this.visualState;

    if (state === "entering") {
      this.enterProgress = Math.min(1, this.enterProgress + dt / 0.55);
    } else if (state === "exiting") {
      this.enterProgress = Math.max(0, this.enterProgress - dt / 0.45);
    } else {
      this.enterProgress = 1;
    }
    const ease =
      this.enterProgress < 0.5
        ? 2 * this.enterProgress * this.enterProgress
        : 1 - Math.pow(-2 * this.enterProgress + 2, 2) / 2;
    const baseScale = 0.02 + ease * 0.98;

    let spin = 0.05;
    if (state === "thinking") spin = 0.16;
    else if (state === "working") spin = 0.26;
    else if (state === "listening") spin = 0.09;
    else if (state === "displaying") spin = 0.11;
    else if (state === "error") spin = 0.035;
    else if (state === "entering") spin = 0.2;

    this.group.rotation.y = rotY + this.t * spin;
    this.group.rotation.x = rotX * 0.45 + Math.sin(this.t * 0.22) * 0.028;
    this.group.scale.setScalar(baseScale * zoom);

    this.outerShell.rotation.y += dt * 0.1;
    this.outerShell.rotation.x = Math.sin(this.t * 0.07) * 0.035;
    this.neuralGroup.rotation.y -= dt * 0.06;

    // Dual nucleus counter-rotate
    this.icoWire.rotation.x += dt * 0.5;
    this.icoWire.rotation.y += dt * 0.65;
    this.icoWireInner.rotation.x -= dt * 0.8;
    this.icoWireInner.rotation.z += dt * 0.45;
    this.nucleusRing.rotation.z += dt * 0.35;

    const active = state === "thinking" || state === "working";
    const surge = active
      ? 0.14 + Math.sin(this.t * 3.2) * 0.11
      : 0.035 + Math.sin(this.t * 1.1) * 0.025;
    this.coreSphere.scale.setScalar(1 + surge);
    (this.coreSphere.material as THREE.MeshBasicMaterial).opacity =
      (0.16 + surge * 0.4) * this.intensity;
    this.glowSphere.scale.setScalar(1 + surge * 0.95);
    (this.glowSphere.material as THREE.MeshBasicMaterial).opacity =
      (0.045 + surge * 0.09) * this.intensity;
    this.icoWire.scale.setScalar(1 + surge * 0.45);
    this.icoWireInner.scale.setScalar(1 + surge * 0.3);

    // Synapses pulse (every other frame)
    if (heavy) for (let i = 0; i < this.synapses.length; i++) {
      const s = this.synapses[i];
      const phase = (s.userData.phase as number) + this.t * (active ? 3.5 : 1.4);
      const pulse = 0.45 + 0.4 * Math.max(0, Math.sin(phase));
      (s.material as THREE.MeshBasicMaterial).opacity = pulse * this.intensity;
      const base = s.userData.base as THREE.Vector3;
      const breath = 1 + Math.sin(phase * 0.5) * 0.03;
      s.position.copy(base).multiplyScalar(breath);
    }

    if (heavy) for (const d of this.debris) {
      const u = d.userData as DebrisOrbit;
      const a = this.t * u.speed + u.phase;
      d.position.set(
        u.orbitR * (1 + this.dispVisual * 1.8) * Math.cos(a) * Math.cos(u.tiltX),
        u.orbitR * Math.sin(u.tiltX) * Math.sin(a * 0.8) +
          Math.sin(a * 0.3 + u.tiltZ) * 0.12,
        u.orbitR * Math.sin(a) * Math.cos(u.tiltZ),
      );
      d.rotation.x += dt * 0.85;
      d.rotation.z += dt * 0.55;
    }

    if (heavy) this.textGroup.children.forEach((sp) => {
      const u = sp.userData as SpriteDrift;
      u.theta += u.speed * dt;
      sp.position.set(
        u.r * Math.sin(u.phi) * Math.cos(u.theta),
        u.r * Math.cos(u.phi),
        u.r * Math.sin(u.phi) * Math.sin(u.theta),
      );
    });

    const scanY1 = Math.sin(this.t * 0.4) * this.R1;
    this.scanRing1.position.y = scanY1;
    const scanS1 =
      Math.sqrt(Math.max(0, this.R1 * this.R1 - scanY1 * scanY1)) / this.R1;
    this.scanRing1.scale.set(scanS1, scanS1, 1);
    (this.scanRing1.material as THREE.MeshBasicMaterial).opacity =
      0.2 * scanS1 * this.intensity;

    const scanY2 = Math.sin(this.t * 0.6 + 1.6) * this.R3 * 1.15;
    this.scanRing2.position.y = scanY2;
    const r2 = this.R3 * 1.5;
    const scanS2 = Math.sqrt(Math.max(0, r2 * r2 - scanY2 * scanY2)) / r2;
    this.scanRing2.scale.set(scanS2, scanS2, 1);
    (this.scanRing2.material as THREE.MeshBasicMaterial).opacity =
      0.16 * scanS2 * this.intensity;

    this.dust.rotation.y += dt * 0.035;


    // ——— Dispersion / explode (gesture-driven) ———
    // Open palms push apart → dispersion 0..1; clap → exploded boost.
    const targetDisp = exploded
      ? Math.max(dispersion, 0.92)
      : dispersion;
    // Smooth so clap isn't an instant pop — similar energy to palms pulling together.
    const lerp = 1 - Math.exp(-(exploded ? 3.2 : 5.5) * dt);
    this.dispVisual += (targetDisp - this.dispVisual) * lerp;
    const D = this.dispVisual;
    const shellSpread = 1 + D * 1.55;
    const neuralSpread = 1 + D * 1.15;
    const dustSpread = 1 + D * 2.4;
    this.outerShell.scale.setScalar(shellSpread);
    this.neuralGroup.scale.setScalar(neuralSpread);
    this.textGroup.scale.setScalar(1 + D * 1.3);
    // Dust points expand from home positions
    if (this.dustHome) {
      const pos = this.dust.geometry.getAttribute("position") as THREE.BufferAttribute;
      const arr = pos.array as Float32Array;
      const home = this.dustHome;
      for (let i = 0; i < home.length; i++) {
        arr[i] = home[i] * dustSpread;
      }
      pos.needsUpdate = true;
    }
    // Debris fly farther out on their orbits
    if (heavy) {
      for (const d of this.debris) {
        const u = d.userData as DebrisOrbit;
        if (u && typeof u.orbitR === "number") {
          // orbit applied below uses u.orbitR — boost radius via mesh scale parent offset
          d.scale.setScalar(1 + D * 0.85);
        }
      }
    }
    // Core stays more coherent; slight shrink when fully exploded for contrast
    const coreMul = 1 - D * 0.12;

    let expand = 1;
    if (this.viz.type === "cpu" && typeof this.viz.value === "number") {
      expand = 0.9 + (this.viz.value / 100) * 0.35;
    } else if (this.viz.type === "ram" && typeof this.viz.value === "number") {
      expand = 0.88 + (this.viz.value / 100) * 0.4;
    }
    this.coreGroup.scale.setScalar(expand * (1 - this.dispVisual * 0.12));
  }

  dispose() {
    this.group.traverse((obj) => {
      if (
        obj instanceof THREE.Mesh ||
        obj instanceof THREE.Points ||
        obj instanceof THREE.Line ||
        obj instanceof THREE.LineSegments ||
        obj instanceof THREE.Sprite
      ) {
        const g = (obj as THREE.Mesh).geometry;
        g?.dispose();
        const m = (obj as THREE.Mesh).material;
        if (Array.isArray(m))
          m.forEach((x) => {
            (x as THREE.SpriteMaterial).map?.dispose();
            x.dispose();
          });
        else if (m) {
          (m as THREE.SpriteMaterial).map?.dispose();
          m.dispose();
        }
      }
    });
  }
}
