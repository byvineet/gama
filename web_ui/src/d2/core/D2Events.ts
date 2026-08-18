export type D2EventType =
  | "D2_ENTER" | "D2_EXIT" | "D2_STATE_CHANGED"
  | "D2_CARD_CREATE" | "D2_CARD_UPDATE" | "D2_CARD_REMOVE" | "D2_CARDS_CLEAR"
  | "D2_VISUALIZATION_START" | "D2_VISUALIZATION_UPDATE" | "D2_VISUALIZATION_END"
  | "D2_THEME_CHANGED" | "D2_GESTURE" | "D2_ZOOM" | "D2_ROTATE"
  | "D2_DISPERSION" | "D2_EXPLODE";

export interface D2Event {
  type: D2EventType;
  payload?: unknown;
  ts: number;
}

type Listener = (event: D2Event) => void;

class D2EventBus {
  private listeners = new Map<D2EventType | "*", Set<Listener>>();

  on(type: D2EventType | "*", fn: Listener): () => void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(fn);
    return () => this.listeners.get(type)?.delete(fn);
  }

  emit(type: D2EventType, payload?: unknown): void {
    const event: D2Event = { type, payload, ts: Date.now() };
    for (const key of [type, "*" as const]) {
      this.listeners.get(key)?.forEach((fn) => {
        try { fn(event); } catch (e) { console.warn("[D2Events]", type, e); }
      });
    }
  }
}

export const d2Events = new D2EventBus();
