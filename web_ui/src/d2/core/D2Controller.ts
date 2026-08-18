/**
 * D2Controller — public API Gama uses to talk to D2.
 * Never exposes Three.js / MediaPipe internals.
 */
import {
  DEFAULT_D2_STATE,
  D2_THEME_COLORS,
  D2_THEME_ORDER,
  D2_THEME_STORAGE_KEY,
  MAX_VISIBLE_CARDS,
  type D2Card,
  type D2StateSnapshot,
  type D2ThemeId,
  type D2VisualState,
  type D2Visualization,
} from "./D2State";
import { d2Events } from "./D2Events";

type Listener = (snap: D2StateSnapshot) => void;

class D2ControllerImpl {
  private state: D2StateSnapshot = { ...DEFAULT_D2_STATE, cards: [] };
  private listeners = new Set<Listener>();
  private gamaAccent = "#008FFF";
  private transitionTimer: number | null = null;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = (): D2StateSnapshot => this.state;

  private notify() {
    const snap = this.state;
    this.listeners.forEach((fn) => {
      try { fn(snap); } catch (e) { console.warn("[D2Controller]", e); }
    });
  }

  private set(partial: Partial<D2StateSnapshot>) {
    this.state = { ...this.state, ...partial };
    this.notify();
  }

  setGamaAccent(color: string) {
    this.gamaAccent = color || "#008FFF";
    if (this.state.themeId === "gama") {
      this.set({ primaryColor: this.gamaAccent });
      d2Events.emit("D2_THEME_CHANGED", { themeId: "gama", color: this.gamaAccent });
    }
  }

  getGamaAccent() { return this.gamaAccent; }

  enter() {
    if (this.state.active && this.state.visualState !== "exiting") return;
    if (this.transitionTimer != null) {
      window.clearTimeout(this.transitionTimer);
      this.transitionTimer = null;
    }
    let themeId: D2ThemeId = "gama";
    try {
      const stored = localStorage.getItem(D2_THEME_STORAGE_KEY) as D2ThemeId | null;
      if (stored && D2_THEME_ORDER.includes(stored)) themeId = stored;
    } catch { /* */ }
    const primaryColor = themeId === "gama" ? this.gamaAccent : D2_THEME_COLORS[themeId];
    this.set({
      active: true,
      visualState: "entering",
      dispersion: 0,
      exploded: false,
      themeId,
      primaryColor,
      zoom: 1,
      rotationX: 0,
      rotationY: 0,
      cards: [],
      visualization: { type: "none" },
    });
    d2Events.emit("D2_ENTER");
    d2Events.emit("D2_STATE_CHANGED", { visualState: "entering" });
    this.transitionTimer = window.setTimeout(() => {
      this.transitionTimer = null;
      if (this.state.active) {
        this.set({ visualState: "idle" });
        d2Events.emit("D2_STATE_CHANGED", { visualState: "idle" });
      }
    }, 550);
  }

  exit() {
    if (!this.state.active) return;
    if (this.transitionTimer != null) {
      window.clearTimeout(this.transitionTimer);
      this.transitionTimer = null;
    }
    this.set({ visualState: "exiting" });
    d2Events.emit("D2_STATE_CHANGED", { visualState: "exiting" });
    d2Events.emit("D2_EXIT");
    this.transitionTimer = window.setTimeout(() => {
      this.transitionTimer = null;
      this.set({
        active: false,
        visualState: "idle",
        cards: [],
        visualization: { type: "none" },
        zoom: 1,
        rotationX: 0,
        rotationY: 0,
      });
    }, 480);
  }

  isActive() { return this.state.active; }

  setState(visualState: D2VisualState) {
    if (!this.state.active) return;
    if (visualState === this.state.visualState) return;
    this.set({ visualState });
    d2Events.emit("D2_STATE_CHANGED", { visualState });
  }

  setTheme(themeId: D2ThemeId) {
    if (!D2_THEME_ORDER.includes(themeId)) return;
    const primaryColor = themeId === "gama" ? this.gamaAccent : D2_THEME_COLORS[themeId];
    this.set({ themeId, primaryColor });
    try { localStorage.setItem(D2_THEME_STORAGE_KEY, themeId); } catch { /* */ }
    d2Events.emit("D2_THEME_CHANGED", { themeId, color: primaryColor });
  }

  cycleTheme(direction: 1 | -1 = 1) {
    const idx = D2_THEME_ORDER.indexOf(this.state.themeId);
    const next = D2_THEME_ORDER[(idx + direction + D2_THEME_ORDER.length) % D2_THEME_ORDER.length];
    this.setTheme(next);
  }

  resetTheme() { this.setTheme("gama"); }

  showCard(card: Omit<D2Card, "id" | "createdAt"> & { id?: string }) {
    if (!this.state.active) return null;
    const id = card.id || `d2c_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const full: D2Card = {
      ...card,
      id,
      createdAt: Date.now(),
      angle: card.angle ?? Math.random() * Math.PI * 2,
      radius: card.radius ?? 0.55 + Math.random() * 0.2,
    };
    let cards = [...this.state.cards, full];
    if (cards.length > MAX_VISIBLE_CARDS) {
      cards = cards
        .sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || b.createdAt - a.createdAt)
        .slice(0, MAX_VISIBLE_CARDS);
    }
    this.set({ cards });
    d2Events.emit("D2_CARD_CREATE", full);
    return id;
  }

  removeCard(id: string) {
    const cards = this.state.cards.filter((c) => c.id !== id);
    if (cards.length === this.state.cards.length) return;
    this.set({ cards });
    d2Events.emit("D2_CARD_REMOVE", { id });
  }

  clearCards() {
    if (!this.state.cards.length) return;
    this.set({ cards: [] });
    d2Events.emit("D2_CARDS_CLEAR");
  }

  visualize(viz: D2Visualization) {
    if (!this.state.active) return;
    this.set({ visualization: viz, visualState: "displaying" });
    d2Events.emit("D2_VISUALIZATION_START", viz);
    d2Events.emit("D2_STATE_CHANGED", { visualState: "displaying" });
  }

  clearVisualization() {
    this.set({ visualization: { type: "none" }, visualState: "idle" });
    d2Events.emit("D2_VISUALIZATION_END");
    d2Events.emit("D2_STATE_CHANGED", { visualState: "idle" });
  }

  setZoom(zoom: number) {
    const z = Math.max(0.55, Math.min(2.2, zoom));
    if (Math.abs(z - this.state.zoom) < 0.001) return;
    // Mutate without notify — D2Scene polls getSnapshot every frame.
    // Notifying would re-render React on every pinch frame and cause lag.
    this.state = { ...this.state, zoom: z };
    d2Events.emit("D2_ZOOM", { zoom: z });
  }

  setRotation(rx: number, ry: number) {
    // High-frequency path: no React notify (scene reads snapshot directly)
    this.state = { ...this.state, rotationX: rx, rotationY: ry };
  }

  /** Continuous spread 0..1 from open-palm distance (no React notify). */
  setDispersion(d: number) {
    const v = Math.max(0, Math.min(1, d));
    if (Math.abs(v - this.state.dispersion) < 0.004) return;
    this.state = { ...this.state, dispersion: v };
    d2Events.emit("D2_DISPERSION", { dispersion: v });
  }

  /** Clap toggle: explode outward or reform. */
  toggleExploded() {
    const exploded = !this.state.exploded;
    this.state = {
      ...this.state,
      exploded,
      // reforming clears continuous spread so palms start from assembled
      dispersion: exploded ? this.state.dispersion : 0,
    };
    d2Events.emit("D2_EXPLODE", { exploded });
  }

  setExploded(exploded: boolean) {
    if (this.state.exploded === exploded) return;
    this.state = {
      ...this.state,
      exploded,
      dispersion: exploded ? this.state.dispersion : 0,
    };
    d2Events.emit("D2_EXPLODE", { exploded });
  }

  setGestureAvailable(available: boolean, message?: string) {
    this.set({ gestureAvailable: available, gestureMessage: message });
  }

  reset() {
    this.clearCards();
    this.clearVisualization();
    this.set({ zoom: 1, rotationX: 0, rotationY: 0, dispersion: 0, exploded: false, visualState: "idle" });
  }
}

/** Singleton — one D2 runtime per app */
export const d2 = new D2ControllerImpl();
