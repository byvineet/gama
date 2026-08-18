/**
 * Image / live-stream scene for the Gama canvas.
 * Supports http(s), data:, relative paths, and continuous MJPEG streams
 * (e.g. http://127.0.0.1:8766/stream for Live Camera).
 */

function safeSrc(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s) return null;
  const low = s.toLowerCase();
  if (low.startsWith("javascript:") || low.startsWith("vbscript:") || low.startsWith("data:text/html")) {
    return null;
  }
  if (
    low.startsWith("https://") ||
    low.startsWith("http://") ||
    low.startsWith("data:image/") ||
    low.startsWith("/") ||
    low.startsWith("./") ||
    low.startsWith("blob:")
  ) {
    return s;
  }
  if (!/^[a-z][a-z0-9+.-]*:/i.test(s)) {
    return s;
  }
  return null;
}

function isLiveStream(src: string): boolean {
  const low = src.toLowerCase();
  return (
    low.includes("/stream") ||
    low.includes("mjpeg") ||
    low.includes(":8766") ||
    low.includes("multipart")
  );
}

export function ImageView({ data }: { data?: Record<string, unknown> }) {
  const src = safeSrc(data?.src || data?.image || data?.image_url || data?.url || data?.href);
  const caption = String(data?.caption || data?.title || data?.alt || "");
  const alt = String(data?.alt || caption || "Image");
  const live = src ? isLiveStream(src) : false;

  if (!src) {
    return (
      <div className="gc-card">
        <div className="gc-card-label">IMAGE</div>
        <p className="gc-empty">No valid image source</p>
      </div>
    );
  }

  return (
    <div className={`gc-card gc-image-card${live ? " gc-live-camera" : ""}`}>
      <div className="gc-card-label">
        {live ? (
          <>
            <span className="gc-live-dot" /> LIVE CAMERA
          </>
        ) : (
          "IMAGE"
        )}
      </div>
      <div className={`gc-image-frame${live ? " gc-image-frame-live" : ""}`}>
        {/*
          MJPEG multipart streams must NOT use loading="lazy" and should avoid
          React key churn so the browser keeps a single native decoder session.
        */}
        <img
          src={src}
          alt={alt}
          className={`gc-image${live ? " gc-image-live" : ""}`}
          draggable={false}
          decoding="async"
          // eslint-disable-next-line react/no-unknown-property
          {...(live ? {} : { loading: "lazy" as const })}
        />
      </div>
      {caption ? <p className="gc-image-caption">{caption}</p> : null}
    </div>
  );
}
