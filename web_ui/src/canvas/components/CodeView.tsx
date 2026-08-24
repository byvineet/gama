import { useState } from "react";

export function CodeView({ data }: { data?: Record<string, unknown> }) {
  const [copied, setCopied] = useState(false);
  const code = String(data?.code || data?.content || data?.text || "").trim();
  const language = String(data?.language || data?.lang || "python").toUpperCase();
  const title = String(data?.title || data?.filename || "Code Snippet");
  const explanation = String(data?.explanation || data?.description || "");
  const output = String(data?.output || data?.result || "");

  const lines = code ? code.split("\n") : [];

  const handleCopy = () => {
    if (!code) return;
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="gc-card gc-code-stage" style={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}>
      {/* Header Bar */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12, paddingBottom: 8, borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: "0.7rem", fontWeight: 700, letterSpacing: "0.08em", padding: "2px 8px", borderRadius: 4, background: "rgba(0, 229, 255, 0.15)", color: "#00E5FF", border: "1px solid rgba(0, 229, 255, 0.3)" }}>
            {language}
          </span>
          <span style={{ fontSize: "0.95rem", fontWeight: 600, color: "#FFFFFF", letterSpacing: "0.02em" }}>
            {title}
          </span>
        </div>
        <button
          onClick={handleCopy}
          style={{
            background: copied ? "rgba(76, 175, 80, 0.2)" : "rgba(255, 255, 255, 0.06)",
            border: `1px solid ${copied ? "#4CAF50" : "rgba(255, 255, 255, 0.12)"}`,
            color: copied ? "#81C784" : "#CCCCCC",
            padding: "4px 10px",
            borderRadius: 6,
            fontSize: "0.75rem",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 4,
            transition: "all 0.2s ease",
          }}
          title="Copy to clipboard"
        >
          {copied ? "✓ Copied" : "📋 Copy"}
        </button>
      </div>

      {/* Code Area with Line Numbers */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          background: "rgba(10, 14, 22, 0.75)",
          borderRadius: 8,
          border: "1px solid rgba(255, 255, 255, 0.05)",
          padding: "12px 14px",
          fontFamily: "'Fira Code', 'JetBrains Mono', 'Consolas', monospace",
          fontSize: "0.82rem",
          lineHeight: "1.55",
          color: "#E0E6ED",
          minHeight: 0,
        }}
      >
        {lines.length > 0 ? (
          lines.map((line, idx) => (
            <div key={idx} style={{ display: "flex", gap: 12 }}>
              <span
                style={{
                  userSelect: "none",
                  color: "rgba(255, 255, 255, 0.25)",
                  width: 28,
                  textAlign: "right",
                  fontSize: "0.75rem",
                }}
              >
                {idx + 1}
              </span>
              <span style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {line || " "}
              </span>
            </div>
          ))
        ) : (
          <div style={{ color: "rgba(255, 255, 255, 0.4)", fontStyle: "italic" }}>
            No code content available.
          </div>
        )}
      </div>

      {/* Optional Explanation & Output Section */}
      {(explanation || output) && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6, fontSize: "0.8rem" }}>
          {explanation && (
            <div style={{ padding: "8px 12px", background: "rgba(0, 229, 255, 0.05)", borderLeft: "3px solid #00E5FF", borderRadius: "0 6px 6px 0", color: "#B0BEC5" }}>
              <strong style={{ color: "#E0F7FA" }}>Insight: </strong>{explanation}
            </div>
          )}
          {output && (
            <div style={{ padding: "6px 10px", background: "rgba(0, 0, 0, 0.4)", borderRadius: 6, border: "1px solid rgba(255,255,255,0.06)", fontFamily: "monospace", color: "#81C784" }}>
              <span style={{ color: "rgba(255,255,255,0.4)" }}>&gt; </span>{output}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
