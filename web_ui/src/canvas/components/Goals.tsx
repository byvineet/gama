export function Goals({ data, items }: { data?: Record<string, unknown>; items?: unknown[] }) {
  const list = (items || (data?.items as unknown[]) || (data?.goals as unknown[]) || []) as Array<
    Record<string, unknown>
  >;
  return (
    <div className="gc-card gc-list-card">
      <div className="gc-card-label">GOALS</div>
      <h3 className="gc-card-title">{String(data?.title || "Goals")}</h3>
      {list.length === 0 ? (
        <p className="gc-empty">No goals yet</p>
      ) : (
        <ul className="gc-item-list">
          {list.slice(0, 10).map((g, i) => {
            const pct = Math.max(0, Math.min(100, Number(g.progress_pct ?? 0)));
            return (
              <li key={String(g.id || i)} className="gc-goal-row">
                <div className="gc-item-body">
                  <span className="gc-item-name">{String(g.title || "")}</span>
                  <span className="gc-item-sub">{String(g.status || "")}</span>
                </div>
                <div className="gc-mini-bar">
                  <div className="gc-mini-fill" style={{ width: `${pct}%` }} />
                </div>
                <span className="gc-mini-pct">{Math.round(pct)}%</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
