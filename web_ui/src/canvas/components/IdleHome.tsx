import { VoiceOrb } from "../../components/VoiceOrb";

type Props = {
  clock: string;
  dateStr: string;
  statusText?: string;
  listening?: boolean;
  speaking?: boolean;
  muted?: boolean;
  nextHint?: string;
  amplitude?: number;
  primary?: string;
  offline?: boolean;
};

/**
 * Default idle presence: classic Voice Orb.
 * Offline keeps the orb visible (grey sleep style) — never blanks the canvas.
 */
export function IdleHome({
  statusText,
  listening,
  speaking,
  muted,
  nextHint,
  amplitude = 0,
  primary = "IDLE",
  offline = false,
}: Props) {
  const state = offline
    ? "offline"
    : muted
      ? "muted"
      : speaking
        ? "speak"
        : listening
          ? "listen"
          : "idle";

  // SLEEP → grey ring (not muted, which draws a red slash and looks "broken")
  const orbPrimary = offline ? "SLEEP" : String(primary || "IDLE");
  const orbStatus = offline ? "Offline" : muted ? "Mic Muted" : statusText;

  return (
    <div
      className={`gc-idle gc-idle-orb${offline ? " is-offline" : ""}`}
      data-state={state}
    >
      <div className="gc-idle-ambient" data-state={state} />
      <div className="gc-idle-glow" data-state={state} />
      <div className="gc-idle-orb-stage">
        <VoiceOrb
          speaking={!offline && Boolean(speaking)}
          listening={!offline && Boolean(listening)}
          amplitude={offline ? 0 : Number(amplitude) || 0}
          primary={orbPrimary}
          statusText={orbStatus}
          muted={!offline && Boolean(muted)}
          active
        />
      </div>
    </div>
  );
}
