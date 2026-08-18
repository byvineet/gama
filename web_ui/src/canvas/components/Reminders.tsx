export function Reminders({ data, items }: { data?: Record<string, unknown>; items?: unknown[] }) {
  const list = (items || (data?.items as unknown[]) || (data?.reminders as unknown[]) || []) as Array<
    Record<string, unknown>
  >;
  const active = list.filter((r) => !r.done);
  return (
    <div className="gc-card gc-list-card">
      <div className="gc-card-label">REMINDERS</div>
      <h3 className="gc-card-title">{String(data?.title || "Reminders")}</h3>
      {active.length === 0 ? (
        <p className="gc-empty">No active reminders</p>
      ) : (
        <ul className="gc-item-list">
          {active.slice(0, 10).map((r, i) => (
            <li key={String(r.id || i)}>
              <span className="gc-item-kind">{String(r.kind || "reminder").toUpperCase()}</span>
              <div className="gc-item-body">
                <span className="gc-item-name">{String(r.message || r.title || "")}</span>
                {r.when ? <span className="gc-item-sub">{String(r.when)}</span> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
