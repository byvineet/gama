export function Alerts({ data, items }: { data?: Record<string, unknown>; items?: unknown[] }) {
  const list = (items || (data?.items as unknown[]) || (data?.alerts as unknown[]) || []) as Array<
    Record<string, unknown>
  >;
  return (
    <div className="gc-card gc-alerts">
      <div className="gc-card-label">ALERTS</div>
      {list.length === 0 ? (
        <p className="gc-empty">All clear</p>
      ) : (
        <ul className="gc-item-list">
          {list.slice(0, 8).map((a, i) => {
            const level = String(a.level || "info").toLowerCase();
            return (
              <li key={String(a.id || i)} className={`gc-alert-row level-${level}`}>
                <span className="gc-item-kind">{level.toUpperCase()}</span>
                <div className="gc-item-body">
                  <span className="gc-item-name">{String(a.title || a.message || "")}</span>
                  {a.message && a.title ? (
                    <span className="gc-item-sub">{String(a.message)}</span>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
