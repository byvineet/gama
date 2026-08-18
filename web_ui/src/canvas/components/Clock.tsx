import { useEffect, useState } from "react";

function pad(n: number) {
  return String(n).padStart(2, "0");
}

function formatTime(d: Date, hour12: boolean): string {
  try {
    return d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12,
    });
  } catch {
    const h = d.getHours();
    const m = d.getMinutes();
    const s = d.getSeconds();
    if (hour12) {
      const h12 = h % 12 || 12;
      const ampm = h < 12 ? "AM" : "PM";
      return `${pad(h12)}:${pad(m)}:${pad(s)} ${ampm}`;
    }
    return `${pad(h)}:${pad(m)}:${pad(s)}`;
  }
}

function formatDate(d: Date): string {
  try {
    return d.toLocaleDateString(undefined, {
      weekday: "long",
      day: "numeric",
      month: "long",
      year: "numeric",
    });
  } catch {
    return d.toDateString();
  }
}

export function Clock({ data }: { data?: Record<string, unknown> }) {
  const hour12 = data?.hour12 !== false && data?.hour24 !== true;
  const showSeconds = data?.show_seconds !== false && data?.showSeconds !== false;
  const showDate = data?.show_date !== false && data?.showDate !== false;
  const label = String(data?.label || data?.title || "TIME");
  const timezone = data?.timezone != null ? String(data.timezone) : undefined;

  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const tick = () => setNow(new Date());
    tick();
    // Update every second for realtime feel
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, []);

  let timeStr: string;
  let dateStr: string;
  try {
    if (timezone) {
      timeStr = now.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: showSeconds ? "2-digit" : undefined,
        hour12,
        timeZone: timezone,
      });
      dateStr = now.toLocaleDateString(undefined, {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
        timeZone: timezone,
      });
    } else {
      timeStr = showSeconds
        ? formatTime(now, hour12)
        : now.toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
            hour12,
          });
      dateStr = formatDate(now);
    }
  } catch {
    timeStr = formatTime(now, hour12);
    dateStr = formatDate(now);
  }

  return (
    <div className="gc-card gc-timer gc-clock">
      <div className="gc-card-label">{label}</div>
      <div className="gc-timer-value">{timeStr}</div>
      {showDate ? <div className="gc-clock-date">{dateStr}</div> : null}
      {timezone ? <div className="gc-timer-state">{timezone}</div> : null}
    </div>
  );
}
