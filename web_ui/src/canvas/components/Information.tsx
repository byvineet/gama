export function Information({ data }: { data?: Record<string, unknown> }) {
  const title = String(data?.title || "Information");
  const body = String(data?.content || data?.body || "");
  const meta = Array.isArray(data?.metadata)
    ? (data!.metadata as string[])
    : Array.isArray(data?.items)
      ? (data!.items as string[]).map(String)
      : [];
  return (
    <div className="gc-card gc-info">
      <div className="gc-card-label">INFO</div>
      <h3 className="gc-card-title">{title}</h3>
      {body ? <p className="gc-card-body">{body}</p> : null}
      {meta.length > 0 && (
        <ul className="gc-meta-list">
          {meta.map((m, i) => (
            <li key={i}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
