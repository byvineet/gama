export function Tasks({ data, items }: { data?: Record<string, unknown>; items?: unknown[] }) {
  const list = (items || (data?.items as unknown[]) || (data?.tasks as unknown[]) || []) as Array<
    Record<string, unknown> | string
  >;
  const title = String(data?.title || "Tasks");
  return (
    <div className="gc-card gc-list-card">
      <div className="gc-card-label">TASKS</div>
      <h3 className="gc-card-title">{title}</h3>
      {list.length === 0 ? (
        <p className="gc-empty">No active tasks</p>
      ) : (
        <ul className="gc-item-list">
          {list.slice(0, 12).map((t, i) => {
            if (typeof t === "string") {
              return (
                <li key={i}>
                  <span className="gc-item-dot" />
                  <span>{t}</span>
                </li>
              );
            }
            const name = String(t.name || t.title || t.task || "");
            const status = String(t.status || "");
            const pct = t.progress_pct != null ? Number(t.progress_pct) : null;
            return (
              <li key={String(t.task_id || t.id || i)}>
                <span className={`gc-item-dot ${status.toLowerCase()}`} />
                <div className="gc-item-body">
                  <span className="gc-item-name">{name}</span>
                  <span className="gc-item-sub">
                    {status}
                    {pct != null ? ` · ${Math.round(pct)}%` : ""}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
