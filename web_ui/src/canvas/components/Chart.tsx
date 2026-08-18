export function Chart({ data }: { data?: Record<string, unknown> }) {
  const series = (Array.isArray(data?.series) ? data!.series : []) as Array<{
    label: string;
    value: number;
    color?: string;
  }>;
  const max = Math.max(1, ...series.map((s) => Number(s.value) || 0));
  const kind = String(data?.kind || "bar");

  return (
    <div className="gc-card gc-chart">
      <div className="gc-card-label">CHART</div>
      <h3 className="gc-card-title">{String(data?.title || "")}</h3>
      {kind === "ring" ? (
        <div className="gc-chart-rings">
          {series.slice(0, 4).map((s, i) => {
            const v = Math.max(0, Math.min(100, Number(s.value)));
            return (
              <div key={i} className="gc-chart-ring-item">
                <div className="gc-mini-bar">
                  <div
                    className="gc-mini-fill"
                    style={{ width: `${v}%`, background: s.color || undefined }}
                  />
                </div>
                <span>
                  {s.label} · {Math.round(v)}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="gc-bars">
          {series.slice(0, 12).map((s, i) => {
            const h = ((Number(s.value) || 0) / max) * 100;
            return (
              <div key={i} className="gc-bar-col" title={`${s.label}: ${s.value}`}>
                <div
                  className="gc-bar"
                  style={{ height: `${h}%`, background: s.color || undefined }}
                />
                <span className="gc-bar-label">{s.label}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
