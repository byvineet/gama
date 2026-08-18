type Props = { cpu?: number; ram?: number; disk?: number };

function Metric({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  return (
    <div className="metric-card">
      <div className="metric-top">
        <span className="metric-label">{label}</span>
        <span className="metric-val">{Math.round(v)}%</span>
      </div>
      <div className="metric-bar">
        <div className="metric-fill" style={{ width: `${v}%` }} />
      </div>
    </div>
  );
}

export function SystemStats({ cpu = 0, ram = 0, disk = 0 }: Props) {
  return (
    <div className="metrics-row">
      <Metric label="CPU" value={cpu} />
      <Metric label="RAM" value={ram} />
      <Metric label="DISK" value={disk} />
    </div>
  );
}
