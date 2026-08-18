/**
 * Persist Gama Nexus layouts in localStorage and restore on demand.
 */

import type { SceneNode } from "./DisplayProtocol";
import { displayStore } from "./DisplayStore";

const KEY = "gama.canvas.layouts.v1";

export type SavedLayout = {
  name: string;
  savedAt: number;
  scenes: SceneNode[];
};

function readAll(): Record<string, SavedLayout> {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return {};
    const obj = JSON.parse(raw);
    return obj && typeof obj === "object" ? obj : {};
  } catch {
    return {};
  }
}

function writeAll(all: Record<string, SavedLayout>) {
  try {
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    /* quota */
  }
}

export function saveCurrentLayout(name: string): SavedLayout {
  const scenes = displayStore
    .getScenes()
    .filter((s) => s.type !== "idle")
    .map((s) => ({
      id: s.id,
      type: s.type,
      layer: s.layer,
      position: s.position,
      size: s.size,
      data: s.data,
      title: s.title,
      style: s.style,
    }));
  const entry: SavedLayout = { name, savedAt: Date.now(), scenes };
  const all = readAll();
  all[name] = entry;
  writeAll(all);
  return entry;
}

export function loadLayout(name: string): boolean {
  const all = readAll();
  const entry = all[name];
  if (!entry || !Array.isArray(entry.scenes)) return false;
  displayStore.clear();
  for (const sc of entry.scenes) {
    if (!sc?.id || !sc?.type) continue;
    displayStore.apply({ action: "show", scene: sc });
  }
  return true;
}

export function listLayouts(): string[] {
  return Object.keys(readAll()).sort();
}

export function deleteLayout(name: string): boolean {
  const all = readAll();
  if (!(name in all)) return false;
  delete all[name];
  writeAll(all);
  return true;
}
