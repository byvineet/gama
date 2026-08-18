export function Progress({ data }: { data?: Record<string, unknown> }) {
  const value = Math.max(0, Math.min(100, Number(data?.value ?? data?.progress ?? 0)));
  return (
    <div className="gc-card">
      <div className="gc-card-label">PROGRESS</div>
      <h3 className="gc-card-title">{String(data?.title || data?.label || "Progress")}</h3>
      <div className="gc-progress-big">
        <div className="gc-mini-bar tall wide">
          <div className="gc-mini-fill" style={{ width: `${value}%` }} />
        </div>
        <span className="gc-progress-val">{Math.round(value)}%</span>
      </div>
      {data?.detail ? <p className="gc-card-body">{String(data.detail)}</p> : null}
    </div>
  );
}
