type LayoutMode = "full" | "companion" | "compact";

type Props = {
  connected: boolean;
  wifi?: boolean;
  battery?: number | null;
  batteryCharging?: boolean;
  muted?: boolean;
  onToggleMute?: () => void;
  layout?: LayoutMode;
  onLayoutChange?: (mode: LayoutMode) => void;
};

export function TopIcons({
  connected,
  wifi = true,
  battery = null,
  batteryCharging = false,
  muted = false,
  onToggleMute,
  layout = "full",
  onLayoutChange,
}: Props) {
  const bat = battery == null ? null : Math.round(Math.max(0, Math.min(100, battery)));

  return (
    <div className="top-icons">
      {/* Mute mic */}
      <button
        type="button"
        className={`top-icon-btn ${muted ? "muted" : "on"}`}
        title={muted ? "Unmute microphone (G+M)" : "Mute microphone (G+M)"}
        onClick={onToggleMute}
        aria-pressed={muted}
        aria-label={muted ? "Unmute microphone" : "Mute microphone"}
      >
        {muted ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <rect x="9" y="2" width="6" height="11" rx="3" />
            <path d="M5 11a7 7 0 0 0 10.5 6.1" />
            <path d="M12 18v4" />
            <path d="M4 4l16 16" strokeWidth="2" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <rect x="9" y="2" width="6" height="11" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0" />
            <path d="M12 18v4" />
          </svg>
        )}
      </button>

      {/* Layout cycle (full → companion → compact) */}
      {onLayoutChange && (
        <button
          type="button"
          className="top-icon-btn on"
          title={`Layout: ${layout} (G+L to cycle)`}
          onClick={() => {
            const order: LayoutMode[] = ["full", "companion", "compact"];
            const next = order[(order.indexOf(layout) + 1) % order.length];
            onLayoutChange(next);
          }}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            {layout === "full" && (
              <>
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M3 9h18" />
              </>
            )}
            {layout === "companion" && (
              <>
                <rect x="6" y="3" width="12" height="18" rx="2" />
                <path d="M6 8h12" />
              </>
            )}
            {layout === "compact" && <rect x="8" y="8" width="8" height="8" rx="1.5" />}
          </svg>
        </button>
      )}

      {/* Connection / Offline */}
       <span
        className={`top-icon ${connected ? "on" : "off"}`}
        title={connected ? "Bridge connected" : "Bridge offline"}
      >
        <svg width="20" height="20" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M5 12.5a10 10 0 0 1 14 0" />
          <path d="M8.5 15.5a5.5 5.5 0 0 1 7 0" />
          <circle cx="12" cy="19" r="1.2" fill="currentColor" stroke="none" />
        </svg>
      </span>

      {/* Battery */}
      <span
        className={`top-icon ${bat == null ? "on" : bat < 20 ? "warn" : "on"}`}
        title={bat == null ? "Battery N/A" : `${bat}%${batteryCharging ? " (charging)" : ""}`}
      >
        <svg width="18" height="16" viewBox="0 0 28 16" fill="none" stroke="currentColor" strokeWidth="1.6">
          <rect x="1" y="3" width="22" height="10" rx="2" />
          <rect x="23.5" y="6" width="2.5" height="4" rx="0.5" fill="currentColor" stroke="none" />
          {bat != null && (
            <rect
              x="3"
              y="5"
              width={Math.max(1, (18 * bat) / 100)}
              height="6"
              rx="1"
              fill="currentColor"
              stroke="none"
              opacity="0.85"
            />
          )}
        </svg>
        {batteryCharging && <span className="charge-bolt">⚡</span>}
      </span>

    </div>
  );
}