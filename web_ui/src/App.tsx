import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { BrowserCamera } from "./components/BrowserCamera";
import { GamaCanvas } from "./canvas/GamaCanvas";
import { displayStore } from "./canvas/DisplayStore";
import { ConversationLog } from "./components/ConversationLog";
import { SystemStats } from "./components/SystemStats";
import { TopIcons } from "./components/TopIcons";
import { useGamaSocket } from "./hooks/useGamaSocket";
import { D2Root, d2, syncAgentState, exitD2 } from "./d2";
import { H1Root, h1, exitH1 } from "./h1";

type LayoutMode = "full" | "companion" | "compact";

const LAYOUT_KEY = "gama.layoutMode";

function formatDate(d: Date): string {
  try {
    return d.toLocaleDateString(undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  } catch {
    return "";
  }
}

function formatClock(d: Date): string {
  try {
    return d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return "";
  }
}

export default function App() {
  const { snapshot, connected, sendChat, sendMute, sendDisplayEvent, closeDisplay } = useGamaSocket();
  const handleConfirm = (yes: boolean) => {
    sendChat(yes ? "yes" : "no");
    closeDisplay();
    displayStore.clear(3); // clear alert/confirm layer
  };

  const [now, setNow] = useState(() => new Date());
  const [layout, setLayout] = useState<LayoutMode>(() => {
    try {
      const s = localStorage.getItem(LAYOUT_KEY);
      if (s === "full" || s === "companion" || s === "compact") return s;
    } catch { /* */ }
    return "full";
  });
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);

  // Optimistic mute: prefer local toggle, sync from snapshot when available
  const [localMuted, setLocalMuted] = useState(false);
  const muted = snapshot.mic_muted ?? localMuted;

  const toggleMute = useCallback(() => {
    const next = !muted;
    setLocalMuted(next);
    sendMute(next);
  }, [muted, sendMute]);

  // Keyboard shortcuts: G+M = mute, G+L = cycle layout
  // Hold G then press M or L (within 900ms), or press them together.
  useEffect(() => {
    let gDown = false;
    let gTimer: number | null = null;

    const clearG = () => {
      gDown = false;
      if (gTimer != null) {
        window.clearTimeout(gTimer);
        gTimer = null;
      }
    };

    const onKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || (e.target as HTMLElement)?.isContentEditable) {
        return;
      }

      if (e.key === "g" || e.key === "G") {
        if (!e.repeat) {
          gDown = true;
          if (gTimer != null) window.clearTimeout(gTimer);
          gTimer = window.setTimeout(clearG, 900);
        }
        return;
      }

      if (!gDown) return;

      if (e.key === "m" || e.key === "M") {
        e.preventDefault();
        clearG();
        toggleMute();
        return;
      }
      if (e.key === "l" || e.key === "L") {
        e.preventDefault();
        clearG();
        const order: LayoutMode[] = ["full", "companion", "compact"];
        setLayout((prev) => order[(order.indexOf(prev) + 1) % order.length]);
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === "g" || e.key === "G") clearG();
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      clearG();
    };
  }, [toggleMute]);

  useEffect(() => {
    if (typeof snapshot.mic_muted === "boolean") {
      setLocalMuted(snapshot.mic_muted);
    }
  }, [snapshot.mic_muted]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 15_000);
    return () => window.clearInterval(id);
  }, []);

  // Page Visibility — pause heavy animations when hidden
  useEffect(() => {
    const onVis = () => setPageVisible(!document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);



  // Persist layout
  useEffect(() => {
    try {
      localStorage.setItem(LAYOUT_KEY, layout);
    } catch { /* */ }
    document.documentElement.dataset.layout = layout;
  }, [layout]);





  const listening =
    !muted &&
    !snapshot.speaking &&
    ["LISTENING", "WAKE_WORD", "IDLE"].includes(
      String(snapshot.primary || "").toUpperCase(),
    );

  const dateStr = useMemo(() => formatDate(now), [now]);
  const clock = useMemo(() => formatClock(now), [now]);

  const showConversation = layout === "full";
  const showStats = layout === "full" || layout === "companion";

  // D2 secondary spatial interface — never auto-activates
  const d2Snap = useSyncExternalStore(d2.subscribe, d2.getSnapshot, d2.getSnapshot);
  const d2Active = d2Snap.active;

  // H1 spatial gesture workspace — mutually exclusive with D2
  const h1Snap = useSyncExternalStore(h1.subscribe, h1.getSnapshot, h1.getSnapshot);
  const h1Active = h1Snap.active;

  useEffect(() => {
    if (d2Active) {
      syncAgentState(String(snapshot.primary || "IDLE"), Boolean(snapshot.speaking));
    }
  }, [d2Active, snapshot.primary, snapshot.speaking]);

  // Nexus gestures run on the backend system camera (vision/gesture_engine),
  // not in the browser — so they keep working with the UI tab closed.


  const handleSend = useCallback(
    (text: string) => {
      const t = text.trim().toLowerCase();
      // H1 spatial workspace (check before D2 — mutual exclusion)
      if (
        /\b(open h1|start h1|enable h1|show h1|enter h1|h1 mode|spatial workspace)\b/.test(t)
      ) {
        if (d2.isActive()) exitD2();
        h1.enter();
        sendChat(text);
        return;
      }
      if (
        /\b(close h1|exit h1|stop h1|leave h1)\b/.test(t)
      ) {
        exitH1();
        sendChat(text);
        return;
      }
      if (
        /\b(switch to d2|enter d2|open d2|d2 mode)\b/.test(t)
      ) {
        if (h1.isActive()) exitH1();
        d2.enter();
        sendChat(text);
        return;
      }
      if (
        /\b(exit d2|leave d2|close d2|return to nexus|switch back|back to nexus)\b/.test(t)
      ) {
        if (h1.isActive()) exitH1();
        else exitD2();
        sendChat(text);
        return;
      }
      if (d2.isActive()) {
        if (/\b(show (my )?tasks|list tasks)\b/.test(t)) {
          const tasks = (snapshot.tasks || []) as Array<{
            id?: string; title?: string; text?: string; status?: string; done?: boolean;
          }>;
          import("./d2").then((m) => m.showTasksAsCards(tasks));
          sendChat(text);
          return;
        }
        if (/\b(show (my )?reminders|list reminders)\b/.test(t)) {
          const rems = (snapshot.reminders || []) as Array<{
            id?: string; title?: string; text?: string; when?: string; time?: string;
          }>;
          import("./d2").then((m) => m.showRemindersAsCards(rems));
          sendChat(text);
          return;
        }
        if (/\b(clear (the )?(tasks|cards|news|reminders)|clear d2)\b/.test(t)) {
          import("./d2").then((m) => m.clearD2Content());
          sendChat(text);
          return;
        }
        if (/\b(visualize|show) (my )?cpu\b/.test(t)) {
          import("./d2").then((m) => m.visualizeCpu(Number(snapshot.cpu || 0)));
          sendChat(text);
          return;
        }
        if (/\b(visualize|show) (my )?(ram|memory)\b/.test(t)) {
          import("./d2").then((m) => m.visualizeRam(Number(snapshot.ram || 0)));
          sendChat(text);
          return;
        }
      }
      sendChat(text);
    },
    [sendChat, snapshot.tasks, snapshot.reminders, snapshot.cpu, snapshot.ram],
  );

  const gamaAccent = "#00b4ff";


  const overlayActive = d2Active || h1Active;

  return (
    <>
      <div
        className={`app-shell layout-${layout} ${!connected ? "is-offline" : ""}`}
        style={
          overlayActive
            ? { opacity: 0, pointerEvents: "none", visibility: "hidden", transition: "opacity 0.4s ease" }
            : { transition: "opacity 0.35s ease" }
        }
        aria-hidden={overlayActive}
      >
      <header className="top-strip">
        <div className="top-brand">
          <span className="top-brand-name">G A M A</span>
          <span className="top-brand-tag">GENUINELY A MODERN ASSISTANT</span>
        </div>

        <div className="top-center-time">
          <span className="top-clock">{clock}</span>
          <span className="top-date">{dateStr}</span>
        </div>

        <TopIcons
          connected={connected}
          wifi={snapshot.wifi !== false}
          battery={snapshot.battery}
          batteryCharging={Boolean(snapshot.battery_charging)}
          muted={muted}
          onToggleMute={toggleMute}
          layout={layout}
          onLayoutChange={setLayout}
        />
      </header>

      <main className="deck">
        {showConversation && (
          <section className="deck-dialogue panel">
            <ConversationLog
              entries={snapshot.log}
              onSend={handleSend}
              connected={connected}
            />
          </section>
        )}

        <section className="deck-presence">
          <div
            className={`presence-stage panel${
              snapshot.camera_vision ? " presence-stage-camera" : ""
            }`}
          >
            <BrowserCamera
              enabled={Boolean(snapshot.camera_vision)}
              fillStage={Boolean(snapshot.camera_vision)}
            />
            {/* Hide voice orb / canvas content while camera fills the display */}
            {!snapshot.camera_vision ? (
              <GamaCanvas
                clock={clock}
                dateStr={dateStr}
                statusText={
                  !connected
                    ? "Offline"
                    : muted
                      ? "Mic Muted"
                      : snapshot.status_text
                }
                listening={connected && listening}
                speaking={connected && snapshot.speaking}
                muted={muted || !connected}
                amplitude={connected ? snapshot.amplitude : 0}
                primary={connected ? String(snapshot.primary || "IDLE") : "SLEEP"}
                offline={!connected}
                tasks={snapshot.tasks}
                goals={snapshot.goals}
                reminders={snapshot.reminders}
                alerts={snapshot.alerts}
                cpu={snapshot.cpu}
                ram={snapshot.ram}
                disk={snapshot.disk}
                onConfirm={handleConfirm}
                onDisplayEvent={(sceneId, event, elementId) =>
                  sendDisplayEvent?.(sceneId, event, elementId)
                }
              />
            ) : null}
          </div>
          {showStats && (
            <SystemStats cpu={snapshot.cpu} ram={snapshot.ram} disk={snapshot.disk} />
          )}
        </section>
      </main>
      </div>

      <D2Root gamaAccent={gamaAccent} onRequestExit={() => exitD2()} />
      <H1Root onRequestExit={() => exitH1()} />
    </>
  );
}
