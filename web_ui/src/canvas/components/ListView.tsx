export function ListView({ data }: { data?: Record<string, unknown> }) {
  const items = (Array.isArray(data?.items) ? data!.items : []) as Array<
    string | { label: string; value?: string; done?: boolean }
  >;
  return (
    <div className="gc-card gc-list-card">
      <div className="gc-card-label">LIST</div>
      <h3 className="gc-card-title">{String(data?.title || "List")}</h3>
      <ul className="gc-item-list">
        {items.map((it, i) => {
          if (typeof it === "string") {
            return (
              <li key={i}>
                <span className="gc-item-dot" />
                <span>{it}</span>
              </li>
            );
          }
          return (
            <li key={i} className={it.done ? "done" : ""}>
              <span className={`gc-item-dot ${it.done ? "ok" : ""}`} />
              <div className="gc-item-body">
                <span className="gc-item-name">{it.label}</span>
                {it.value ? <span className="gc-item-sub">{it.value}</span> : null}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
