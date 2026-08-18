function Ring({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(100, value));
  const r = 36;
  const c = 2 * Math.PI * r;
  const off = c * (1 - v / 100);
  return (
    <div className="gc-ring">
      <svg viewBox="0 0 100 100" className="gc-ring-svg">
        <circle cx="50" cy="50" r={r} className="gc-ring-track" />
        <circle
          cx="50"
          cy="50"
          r={r}
          className="gc-ring-value"
          strokeDasharray={c}
          strokeDashoffset={off}
        />
        <text x="50" y="54" textAnchor="middle" className="gc-ring-text">
          {Math.round(v)}%
        </text>
      </svg>
      <span className="gc-ring-label">{label}</span>
    </div>
  );
}

export function SystemStatus({ data }: { data?: Record<string, unknown> }) {
  return (
    <div className="gc-card gc-system">
      <div className="gc-card-label">SYSTEM</div>
      <div className="gc-rings">
        <Ring label="CPU" value={Number(data?.cpu ?? 0)} />
        <Ring label="RAM" value={Number(data?.ram ?? 0)} />
        <Ring label="DISK" value={Number(data?.disk ?? 0)} />
      </div>
      {data?.status ? <p className="gc-system-status">{String(data.status)}</p> : null}
      {data?.battery != null && (
        <p className="gc-system-status">Battery {Math.round(Number(data.battery))}%</p>
      )}
    </div>
  );
}
