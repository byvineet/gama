type Props = {
  data?: Record<string, unknown>;
  onConfirm?: (yes: boolean) => void;
};

export function Confirm({ data, onConfirm }: Props) {
  return (
    <div className="gc-card gc-confirm">
      <div className="gc-card-label">CONFIRM</div>
      <h3 className="gc-card-title">{String(data?.title || "Confirm")}</h3>
      <p className="gc-card-body">{String(data?.body || data?.message || "")}</p>
      <div className="gc-confirm-actions">
        <button type="button" className="gc-btn primary" onClick={() => onConfirm?.(true)}>
          Yes
        </button>
        <button type="button" className="gc-btn" onClick={() => onConfirm?.(false)}>
          No
        </button>
      </div>
      <p className="gc-confirm-hint">Or say “yes” / “no”</p>
    </div>
  );
}
