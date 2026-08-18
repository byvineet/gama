import type { ActiveScene } from "./DisplayStore";
import { Weather } from "./components/Weather";
import { Tasks } from "./components/Tasks";
import { Goals } from "./components/Goals";
import { Reminders } from "./components/Reminders";
import { Alerts } from "./components/Alerts";
import { Timer } from "./components/Timer";
import { Clock } from "./components/Clock";
import { SystemStatus } from "./components/SystemStatus";
import { Information } from "./components/Information";
import { ListView } from "./components/ListView";
import { Chart } from "./components/Chart";
import { Progress } from "./components/Progress";
import { Confirm } from "./components/Confirm";
import { ImageView } from "./components/ImageView";
import { IdleHome } from "./components/IdleHome";
import { SVGRenderer } from "./custom/SVGRenderer";
import { ModelView } from "./components/ModelView";
import { enterClass } from "./animation/transitions";

type Ctx = {
  clock: string;
  dateStr: string;
  statusText?: string;
  listening?: boolean;
  speaking?: boolean;
  muted?: boolean;
  amplitude?: number;
  primary?: string;
  offline?: boolean;
  snapshotExtras?: {
    tasks?: unknown[];
    goals?: unknown[];
    reminders?: unknown[];
    alerts?: unknown[];
    cpu?: number;
    ram?: number;
    disk?: number;
  };
  onConfirm?: (yes: boolean) => void;
  onEvent?: (sceneId: string, event: string, elementId?: string) => void;
};

export function SceneRenderer({ scene, ctx }: { scene: ActiveScene; ctx: Ctx }) {
  const data = scene.data || {};
  const t = scene.transition || scene.animation;
  const cls = `gc-scene layer-${scene.layer} ${enterClass(t?.enter)}`;

  const body = (() => {
    switch (scene.type) {
      case "idle":
        return (
          <IdleHome
            clock={ctx.clock}
            dateStr={ctx.dateStr}
            statusText={ctx.statusText}
            listening={ctx.listening}
            speaking={ctx.speaking}
            muted={ctx.muted}
            amplitude={ctx.amplitude}
            primary={ctx.primary}
            offline={ctx.offline}
            nextHint={String(data.next_hint || data.hint || "")}
          />
        );
      case "weather":
        return <Weather data={data} />;
      case "tasks":
        return <Tasks data={data} items={ctx.snapshotExtras?.tasks} />;
      case "goals":
        return <Goals data={data} items={ctx.snapshotExtras?.goals} />;
      case "reminders":
        return <Reminders data={data} items={ctx.snapshotExtras?.reminders} />;
      case "alerts":
      case "notification":
        return <Alerts data={data} items={ctx.snapshotExtras?.alerts} />;
      case "timer":
      case "pomodoro":
        return <Timer data={data} />;
      case "clock":
      case "time":
        return <Clock data={data} />;
      case "system":
      case "status":
        return (
          <SystemStatus
            data={{
              cpu: data.cpu ?? ctx.snapshotExtras?.cpu,
              ram: data.ram ?? ctx.snapshotExtras?.ram,
              disk: data.disk ?? ctx.snapshotExtras?.disk,
              ...data,
            }}
          />
        );
      case "information":
      case "notes":
      case "card":
      case "execution":
      case "search":
        return <Information data={data} />;
      case "list":
      case "timeline":
        return <ListView data={data} />;
      case "table":
        return <ListView data={data} />;
      case "chart":
      case "gauge":
      case "metric":
        return <Chart data={data} />;
      case "progress":
        return <Progress data={data} />;
      case "confirm":
        return <Confirm data={data} onConfirm={ctx.onConfirm} />;
      case "image":
        return <ImageView data={data} />;
      case "custom_svg":
        return <SVGRenderer data={data} />;
      case "model_3d":
        return <ModelView data={data} />;
      case "dsl":
      case "scene":
        // composite: render children as nested cards, or custom_svg elements in data
        if (data.elements) return <SVGRenderer data={data} />;
        if (scene.children?.length) {
          return (
            <div className="gc-composite">
              {scene.children.map((ch) => (
                <SceneRenderer
                  key={ch.id}
                  scene={{
                    ...ch,
                    layer: ch.layer ?? scene.layer,
                    createdAt: scene.createdAt,
                    updatedAt: scene.updatedAt,
                  }}
                  ctx={ctx}
                />
              ))}
            </div>
          );
        }
        return <Information data={data} />;
      case "music":
      case "calendar":
        return (
          <Information
            data={{
              title: scene.title || scene.type,
              content: String(data.summary || data.content || data.body || JSON.stringify(data)),
            }}
          />
        );
      default:
        return (
          <Information
            data={{
              title: scene.title || scene.type,
              content: String(data.content || data.body || ""),
              metadata: Object.keys(data).slice(0, 6).map((k) => `${k}: ${String(data[k])}`),
            }}
          />
        );
    }
  })();

  return (
    <div
      className={cls}
      data-scene-id={scene.id}
      data-scene-type={scene.type}
      style={{
        opacity: scene.style?.opacity,
        ["--gc-accent" as string]: scene.style?.accent,
        width: "100%",
        height: "100%",
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
      }}
    >
      {body}
    </div>
  );
}
