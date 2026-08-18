import { gestureController } from "../gestures/GestureController";
/**
 * GamaD2Bridge — maps high-level Gama events into D2 API.
 */
import { d2 } from "../core/D2Controller";
import type { D2Card } from "../core/D2State";

export function enterD2() {
  d2.enter();
}
export function exitD2() {
  try { gestureController.stop(); } catch { /* */ }
  d2.exit();
}
export function isD2Active() { return d2.isActive(); }

export function syncAgentState(primary: string, speaking: boolean) {
  if (!d2.isActive()) return;
  const p = (primary || "").toUpperCase();
  if (p.includes("ERROR") || p.includes("FAIL")) { d2.setState("error"); return; }
  if (speaking) { d2.setState("working"); return; }
  if (p === "THINKING" || p === "PROCESSING" || p === "EXECUTING") {
    d2.setState("thinking"); return;
  }
  if (p === "LISTENING" || p === "WAKE_WORD") { d2.setState("listening"); return; }
  if (d2.getSnapshot().visualization.type === "none") d2.setState("idle");
}

export function showTasksAsCards(
  tasks: Array<{ id?: string; title?: string; text?: string; status?: string; done?: boolean }>,
) {
  if (!d2.isActive()) return;
  d2.clearCards();
  const pending = tasks.filter((t) => !t.done && String(t.status || "").toLowerCase() !== "done");
  pending.slice(0, 6).forEach((t, i) => {
    d2.showCard({
      id: t.id || `task_${i}`,
      title: t.title || t.text || "Task",
      body: t.status,
      kind: "task",
      priority: 2,
      angle: (i / Math.max(1, pending.length)) * Math.PI * 2,
    });
  });
}

export function showRemindersAsCards(
  reminders: Array<{ id?: string; title?: string; text?: string; when?: string; time?: string }>,
) {
  if (!d2.isActive()) return;
  d2.clearCards();
  reminders.slice(0, 5).forEach((r, i) => {
    d2.showCard({
      id: r.id || `rem_${i}`,
      title: r.title || r.text || "Reminder",
      meta: r.when || r.time,
      kind: "reminder",
      priority: 3,
      angle: (i / Math.max(1, reminders.length)) * Math.PI * 2 + 0.4,
    });
  });
}

export function showNewsAsCards(
  items: Array<{ title?: string; summary?: string; source?: string; url?: string }>,
) {
  if (!d2.isActive()) return;
  d2.clearCards();
  items.slice(0, 5).forEach((n, i) => {
    d2.showCard({
      id: `news_${i}`,
      title: n.title || "Headline",
      body: n.summary,
      meta: n.source,
      kind: "news",
      priority: 1,
      angle: (i / Math.max(1, items.length)) * Math.PI * 2 + 1.0,
    });
  });
}

export function showGenericCards(cards: Omit<D2Card, "id" | "createdAt">[]) {
  if (!d2.isActive()) return;
  d2.clearCards();
  cards.slice(0, 6).forEach((c) => d2.showCard(c));
}

export function visualizeCpu(percent: number) {
  if (!d2.isActive()) return;
  d2.visualize({ type: "cpu", value: percent, label: "CPU" });
}

export function visualizeRam(percent: number) {
  if (!d2.isActive()) return;
  d2.visualize({ type: "ram", value: percent, label: "Memory" });
}

export function clearD2Content() {
  d2.clearCards();
  d2.clearVisualization();
}
