export function WorkflowView({ data }: { data?: Record<string, unknown> }) {
  const title = String(data?.title || "Workflow Status");
  const summary = String(data?.summary || data?.content || data?.body || "");
  const status = String(data?.status || "completed").toUpperCase();
  const steps = Array.isArray(data?.steps)
    ? (data!.steps as Array<string | { name?: string; status?: string }>)
    : Array.isArray(data?.items)
      ? (data!.items as string[])
      : [];
  const stats = (data?.stats && typeof data?.stats === "object") ? (data!.stats as Record<string, unknown>) : null;

  return (
    <div className="gc-card gc-workflow-stage" style={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12, paddingBottom: 8, borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: "0.7rem", fontWeight: 700, letterSpacing: "0.08em", padding: "2px 8px", borderRadius: 4, background: status === "COMPLETED" ? "rgba(76, 175, 80, 0.15)" : "rgba(255, 179, 0, 0.15)", color: status === "COMPLETED" ? "#81C784" : "#FFB300", border: `1px solid ${status === "COMPLETED" ? "rgba(76, 175, 80, 0.3)" : "rgba(255, 179, 0, 0.3)"}` }}>
            {status}
          </span>
          <h3 style={{ margin: 0, fontSize: "1rem", fontWeight: 600, color: "#FFFFFF", letterSpacing: "0.02em" }}>
            {title}
          </h3>
        </div>
        <span style={{ fontSize: "0.75rem", color: "rgba(255, 255, 255, 0.4)", fontFamily: "monospace" }}>
          ⚡ WORKFLOW
        </span>
      </div>

      {/* Summary message */}
      {summary && (
        <div style={{ marginBottom: 12, padding: "8px 12px", background: "rgba(255, 255, 255, 0.03)", borderRadius: 6, border: "1px solid rgba(255, 255, 255, 0.06)", fontSize: "0.85rem", color: "#CFD8DC", lineHeight: 1.45 }}>
          {summary}
        </div>
      )}

      {/* Metric Cards if stats available */}
      {stats && Object.keys(stats).length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: `repeat(${Math.min(Object.keys(stats).length, 3)}, 1fr)`, gap: 8, marginBottom: 12 }}>
          {Object.entries(stats).map(([k, v]) => (
            <div key={k} style={{ padding: "8px 10px", background: "rgba(0, 229, 255, 0.04)", border: "1px solid rgba(0, 229, 255, 0.12)", borderRadius: 6, textAlign: "center" }}>
              <div style={{ fontSize: "0.65rem", textTransform: "uppercase", color: "rgba(255,255,255,0.4)", letterSpacing: "0.05em", marginBottom: 2 }}>
                {k.replace(/_/g, " ")}
              </div>
              <div style={{ fontSize: "1.1rem", fontWeight: 700, color: "#00E5FF", fontFamily: "monospace" }}>
                {String(v)}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Steps / Category Pills List */}
      <div style={{ flex: 1, overflowY: "auto", minHeight: 0, display: "flex", flexDirection: "column", gap: 6 }}>
        {steps.length > 0 ? (
          steps.map((st, i) => {
            const label = typeof st === "string" ? st : st.name || JSON.stringify(st);
            return (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: "8px 12px",
                  background: "rgba(255, 255, 255, 0.025)",
                  borderRadius: 6,
                  border: "1px solid rgba(255, 255, 255, 0.04)",
                  fontSize: "0.82rem",
                  color: "#ECEFF1",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#00E5FF", boxShadow: "0 0 8px #00E5FF" }} />
                  <span>{label}</span>
                </div>
                <span style={{ fontSize: "0.75rem", color: "rgba(255, 255, 255, 0.35)", fontFamily: "monospace" }}>
                  #{i + 1}
                </span>
              </div>
            );
          })
        ) : (
          <div style={{ color: "rgba(255, 255, 255, 0.4)", fontStyle: "italic", fontSize: "0.8rem", textAlign: "center", padding: "16px 0" }}>
            Workflow running smoothly.
          </div>
        )}
      </div>
    </div>
  );
}
