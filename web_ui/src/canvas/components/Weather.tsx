export function Weather({ data }: { data?: Record<string, unknown> }) {
  const loc = String(data?.location || "—");
  const temp = data?.temperature ?? data?.temp_c;
  const cond = String(data?.condition || "");
  const humidity = data?.humidity;
  const wind = data?.wind_kph ?? data?.wind;
  const high = data?.high;
  const low = data?.low;
  const forecast = Array.isArray(data?.forecast) ? data!.forecast as Array<Record<string, unknown>> : [];

  return (
    <div className="gc-card gc-weather">
      <div className="gc-card-label">WEATHER</div>
      <div className="gc-weather-main">
        <div className="gc-weather-temp">
          {temp != null ? `${Math.round(Number(temp))}°` : "—"}
        </div>
        <div className="gc-weather-side">
          <div className="gc-weather-loc">{loc}</div>
          <div className="gc-weather-cond">{cond}</div>
          {(high != null || low != null) && (
            <div className="gc-weather-range">
              {high != null && <span>H {Math.round(Number(high))}°</span>}
              {low != null && <span>L {Math.round(Number(low))}°</span>}
            </div>
          )}
        </div>
      </div>
      <div className="gc-weather-meta">
        {humidity != null && <span>Humidity {Number(humidity)}%</span>}
        {wind != null && <span>Wind {Number(wind)} km/h</span>}
      </div>
      {forecast.length > 0 && (
        <div className="gc-weather-forecast">
          {forecast.slice(0, 5).map((f, i) => (
            <div key={i} className="gc-weather-day">
              <span>{String(f.day || f.date || "")}</span>
              <span>
                {f.high != null ? Math.round(Number(f.high)) : "—"}°
                {f.low != null ? ` / ${Math.round(Number(f.low))}°` : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
