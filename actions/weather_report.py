"""
actions/weather_report.py — Gama Weather Report (Mark style)
Primary source: WeatherAPI.com via RapidAPI (direct HTTP, deterministic,
no LLM round-trip). Falls back to the original Gemini-grounded-search
method if no RapidAPI key is configured or the HTTP call fails for any
reason — so this never regresses an existing working setup.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import sys
from pathlib import Path
from typing import Optional

from utils.http_pool import get_session, HTTP_TIMEOUT

log = get_logger(__name__)
logger = log  # back-compat alias
BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

_WEATHERAPI_HOST = "weatherapi-com.p.rapidapi.com"
_WEATHERAPI_CURRENT_URL = f"https://{_WEATHERAPI_HOST}/current.json"
_WEATHERAPI_FORECAST_URL = f"https://{_WEATHERAPI_HOST}/forecast.json"
_HTTP_TIMEOUT = HTTP_TIMEOUT  # 5s — fail fast onto the Gemini fallback rather than hang


def _get_gemini_api_key() -> str:
    """Legacy plaintext read, kept only as the last-resort fallback path
    inside _weather_via_gemini(). Prefer core.config_manager.config
    everywhere else — this file avoids importing it at module load time
    to keep actions/ modules import-cheap (see main.py's lazy-import
    convention for the actions package)."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _get_weather_api_key() -> str:
    try:
        from core.config_manager import config
        return config.weather_rapidapi_key()
    except Exception:
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("weather_rapidapi_key", "")
        except Exception:
            return ""


DEFAULT_LOCATION = "Orai, Uttar Pradesh, India (postal code 285001)"
_DEFAULT_LOCATION_QUERY = "Orai"


def _condition_emoji(text: str) -> str:
    t = (text or "").lower()
    if "thunder" in t:
        return "⛈️"
    if "snow" in t or "blizzard" in t or "sleet" in t:
        return "❄️"
    if "rain" in t or "drizzle" in t:
        return "🌧️"
    if "overcast" in t:
        return "☁️"
    if "cloud" in t or "partly" in t:
        return "⛅"
    if "mist" in t or "fog" in t or "haze" in t:
        return "🌫️"
    if "clear" in t or "sunny" in t:
        return "☀️"
    return "🌡️"


def _weather_via_rapidapi(city: str, forecast: bool, api_key: str) -> Optional[str]:
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": _WEATHERAPI_HOST,
        "Content-Type": "application/json",
    }
    try:
        if forecast:
            resp = get_session().get(
                _WEATHERAPI_FORECAST_URL,
                headers=headers,
                params={"q": city, "days": 3, "aqi": "no", "alerts": "no"},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            loc = data.get("location", {})
            days = data.get("forecast", {}).get("forecastday", [])
            if not days:
                return None
            loc_name = loc.get("name") or city
            lines = [f"3-day forecast for {loc_name}:"]
            for d in days:
                date = d.get("date", "")
                day = d.get("day", {})
                cond = (day.get("condition", {}) or {}).get("text", "")
                hi = day.get("maxtemp_c")
                lo = day.get("mintemp_c")
                rain_chance = day.get("daily_chance_of_rain")
                emoji = _condition_emoji(cond)
                line = f"{date}: {emoji} {cond}, {lo}\u2013{hi}\u00b0C"
                if rain_chance is not None:
                    line += f", {rain_chance}% chance of rain"
                lines.append(line)
            return "\n".join(lines)
        else:
            resp = get_session().get(
                _WEATHERAPI_CURRENT_URL,
                headers=headers,
                params={"q": city},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            loc = data.get("location", {})
            cur = data.get("current", {})
            if not cur:
                return None
            loc_name = loc.get("name") or city
            cond = (cur.get("condition", {}) or {}).get("text", "")
            temp_c = cur.get("temp_c")
            feels_c = cur.get("feelslike_c")
            humidity = cur.get("humidity")
            wind_kph = cur.get("wind_kph")
            emoji = _condition_emoji(cond)
            parts = [f"{emoji} It's {cond.lower()} in {loc_name}, {temp_c}\u00b0C"]
            if feels_c is not None and feels_c != temp_c:
                parts[0] += f" (feels like {feels_c}\u00b0C)"
            parts[0] += "."
            extras = []
            if humidity is not None:
                extras.append(f"humidity {humidity}%")
            if wind_kph is not None:
                extras.append(f"wind {wind_kph} km/h")
            if extras:
                parts.append("Also: " + ", ".join(extras) + ".")
            return " ".join(parts)
    except Exception as exc:
        logger.debug(f"WeatherAPI.com fetch failed, falling back to Gemini: {exc}")
        return None


def _weather_via_gemini(city: str, forecast: bool) -> str:
    try:
        from google import genai
        client = genai.Client(api_key=_get_gemini_api_key())
        if forecast:
            prompt = (f"Give me a 3-day weather forecast for {city}. "
                      f"Include high/low temps in Celsius and conditions for each day.")
        else:
            prompt = (f"What is the current weather in {city}? "
                      f"Include temperature in Celsius, condition, humidity, wind. "
                      f"Be concise (2-3 sentences). Always use Celsius, never Fahrenheit.")
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config={"tools": [{"google_search": {}}]},
        )
        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text += part.text
        return text.strip() or "No weather data available."
    except Exception as exc:
        return f"Weather fetch failed: {exc}"


def weather_action(city: str = "", forecast: bool = False) -> str:
    """Get current weather or forecast for a city.

    Tries WeatherAPI.com (RapidAPI) first — fast, deterministic, no LLM
    call — and only falls back to the Gemini-grounded-search method if
    no key is configured or the HTTP call fails for any reason.
    """
    requested_city = (city or "").strip()
    api_city = requested_city or _DEFAULT_LOCATION_QUERY
    gemini_city = requested_city or DEFAULT_LOCATION

    api_key = _get_weather_api_key()
    if api_key:
        result = _weather_via_rapidapi(api_city, forecast, api_key)
        if result:
            return result

    return _weather_via_gemini(gemini_city, forecast)




def weather_card(city: str = "", forecast: bool = False) -> dict:
    """Structured weather for the HUD.

    forecast=False → current + next ~6 hours
    forecast=True  → current + 3-day daily forecast
    """
    api_city = (city or "").strip() or _DEFAULT_LOCATION_QUERY
    api_key = _get_weather_api_key()
    if not api_key:
        text = _weather_via_gemini(api_city, bool(forecast))
        return {
            "location": api_city,
            "temp_c": None,
            "condition": text[:120] if text else "Unavailable",
            "emoji": "🌡️",
            "feels_c": None,
            "humidity": None,
            "wind_kph": None,
            "hours": [],
            "days": [],
            "mode": "forecast" if forecast else "current",
            "source": "gemini",
            "summary": text,
        }

    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": _WEATHERAPI_HOST,
        "Content-Type": "application/json",
    }
    try:
        days_param = 3 if forecast else 1
        resp = get_session().get(
            _WEATHERAPI_FORECAST_URL,
            headers=headers,
            params={"q": api_city, "days": days_param, "aqi": "no", "alerts": "no"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        loc = data.get("location", {}) or {}
        cur = data.get("current", {}) or {}
        days_raw = (data.get("forecast") or {}).get("forecastday") or []

        loc_name = loc.get("name") or api_city
        cond = ((cur.get("condition") or {}).get("text")) or ""
        emoji = _condition_emoji(cond)

        hours: list = []
        days_out: list = []

        if forecast:
            for d in days_raw[:3]:
                day = d.get("day") or {}
                dcond = ((day.get("condition") or {}).get("text")) or ""
                days_out.append({
                    "date": d.get("date") or "",
                    "condition": dcond,
                    "emoji": _condition_emoji(dcond),
                    "max_c": day.get("maxtemp_c"),
                    "min_c": day.get("mintemp_c"),
                    "chance_of_rain": day.get("daily_chance_of_rain"),
                })
        else:
            hours_raw = (days_raw[0].get("hour") if days_raw else None) or []
            from datetime import datetime
            now_h = None
            try:
                localtime = loc.get("localtime") or ""
                now_h = int(localtime.split(" ")[1].split(":")[0])
            except Exception:
                now_h = datetime.now().hour
            for h in hours_raw:
                try:
                    tstr = str(h.get("time") or "")
                    hour_num = int(tstr.split(" ")[1].split(":")[0])
                except Exception:
                    continue
                if now_h is not None and hour_num < now_h:
                    continue
                hcond = ((h.get("condition") or {}).get("text")) or ""
                hours.append({
                    "time": tstr.split(" ")[-1][:5] if tstr else f"{hour_num:02d}:00",
                    "temp_c": h.get("temp_c"),
                    "condition": hcond,
                    "emoji": _condition_emoji(hcond),
                    "chance_of_rain": h.get("chance_of_rain"),
                })
                if len(hours) >= 6:
                    break

        return {
            "location": loc_name,
            "temp_c": cur.get("temp_c"),
            "condition": cond,
            "emoji": emoji,
            "feels_c": cur.get("feelslike_c"),
            "humidity": cur.get("humidity"),
            "wind_kph": cur.get("wind_kph"),
            "hours": hours,
            "days": days_out,
            "mode": "forecast" if forecast else "current",
            "source": "weatherapi",
        }
    except Exception as exc:
        logger.warning("weather_card failed: %s", exc)
        return {"error": str(exc), "location": api_city, "hours": [], "days": [], "mode": "forecast" if forecast else "current"}


__all__ = ["weather_action", "weather_card"]