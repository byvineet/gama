"""
actions/calendar_action.py — Google Calendar integration
============================================================
Voice/tool-facing calendar: "what's on my schedule today", "add a
meeting tomorrow at 3pm", "move my 2pm to 4pm", "cancel my dentist
appointment". Talks to the real Google Calendar API v3 over plain
REST calls (`requests`, already a dependency) — no heavyweight
google-api-python-client SDK needed for the handful of endpoints used
here.

Auth is handled entirely by actions/google_calendar_auth.py (OAuth
PKCE, same pattern as Spotify). Every function here degrades to a
clear "not connected" message rather than raising if the user hasn't
run the one-time login yet.

Datetimes: the model is expected to resolve natural language ("tomorrow
3pm", "next Monday") into full ISO 8601 datetimes itself (it already
has the current date/time in its context) and pass those directly —
this avoids reimplementing NLP date parsing and matches how Gemini
function-calling is used elsewhere in this codebase (e.g.
actions/reminder.py's explicit in_minutes / time fields).

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.http_pool import get_session, HTTP_TIMEOUT

from actions.google_calendar_auth import get_access_token_sync, is_configured, has_refresh_token

log = get_logger(__name__)
logger = log  # back-compat alias
API_BASE = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = HTTP_TIMEOUT  # 5s strict — pooled/keep-alive session, see utils/http_pool.py


def _timezone_name() -> str:
    """IANA timezone Gama treats as 'local' for calendar reads/writes —
    defaults to Asia/Kolkata (IST). Google's API rejects dateTime values
    with no UTC offset unless a timeZone is also supplied, so every
    create/update below sends this explicitly rather than relying on
    the machine's OS timezone (which may be misconfigured or UTC on a
    server/VM). Override via "timezone" in config/api_keys.json."""
    try:
        from utils.paths import user_data_path
        import json as _json
        with open(user_data_path("config/api_keys.json"), "r", encoding="utf-8") as f:
            data = _json.load(f)
        return str(data.get("timezone") or "Asia/Kolkata").strip()
    except Exception:
        return "Asia/Kolkata"


def _tzinfo():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(_timezone_name())
    except Exception:
        from zoneinfo import ZoneInfo as _ZI
        return _ZI("Asia/Kolkata")


def _holiday_calendar_id() -> Optional[str]:
    """Calendar ID Gama reads for 'is there a holiday this month'-style
    questions. Holiday calendars are public Google calendars — the
    normal OAuth token works fine against them without the user having
    to explicitly add the calendar to their account. Configure via
    "holiday_calendar_id" in config/api_keys.json (e.g.
    'en.indian#holiday@group.v.calendar.google.com')."""
    try:
        from utils.paths import user_data_path
        import json as _json
        with open(user_data_path("config/api_keys.json"), "r", encoding="utf-8") as f:
            data = _json.load(f)
        cal_id = data.get("holiday_calendar_id")
        return str(cal_id).strip() if cal_id else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Low-level REST helpers
# ---------------------------------------------------------------------------
def _headers() -> Optional[dict]:
    token = get_access_token_sync()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _not_connected_message() -> str:
    if not is_configured():
        return ("Google Calendar isn't set up yet — I need a Google OAuth Client "
                "ID and Secret in config/api_keys.json. See actions/google_calendar_auth.py "
                "for setup steps.")
    if not has_refresh_token():
        return ("Google Calendar isn't connected yet — run "
                "'python scripts/google_calendar_login.py' once to connect your account.")
    return "I couldn't reach Google Calendar right now — your login may need to be refreshed."


def _local_now() -> datetime:
    return datetime.now(_tzinfo())


def _fmt_event_time(ev: dict) -> str:
    start = ev.get("start", {})
    if "dateTime" in start:
        try:
            dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(_tzinfo())
            return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
        except Exception:
            return start["dateTime"]
    return start.get("date", "unknown date") + " (all day)"


def _summarize_event(ev: dict) -> str:
    title = ev.get("summary", "(no title)")
    when = _fmt_event_time(ev)
    loc = f" @ {ev['location']}" if ev.get("location") else ""
    return f"{title} — {when}{loc} [id: {ev.get('id', '')[:8]}]"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _normalize_range_time(value: str) -> str:
    """RFC3339-ify a timeMin/timeMax value for Google's API. The model
    often passes bare 'YYYY-MM-DDTHH:MM:SS' with no UTC offset, which
    Google rejects with HTTP 400 (same underlying issue _google_time_field
    solves for create/update — this is the read-path equivalent)."""
    value = (value or "").strip()
    if not value:
        return value
    if len(value) == 10 and value.count("-") == 2:
        # bare date -> midnight local
        value = value + "T00:00:00"
    if value.endswith("Z") or "+" in value[10:] or value.count("-") > 2:
        # already has an offset (Z, +05:30, or -05:00 after the date part)
        return value
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tzinfo())
        return dt.isoformat()
    except Exception:
        return value


def _list_events(time_min: Optional[str], time_max: Optional[str], max_results: int = 10) -> str:
    headers = _headers()
    if not headers:
        return _not_connected_message()

    if not time_min:
        time_min = _local_now().isoformat()
    else:
        time_min = _normalize_range_time(time_min)
    if not time_max:
        time_max = (_local_now() + timedelta(days=7)).isoformat()
    else:
        time_max = _normalize_range_time(time_max)

    try:
        resp = get_session().get(
            f"{API_BASE}/calendars/primary/events",
            headers=headers, timeout=_TIMEOUT,
            params={
                "timeMin": time_min, "timeMax": time_max,
                "singleEvents": "true", "orderBy": "startTime",
                "maxResults": max_results,
            },
        )
    except Exception as exc:
        return f"Network error reaching Google Calendar: {exc}"

    if resp.status_code != 200:
        return f"Google Calendar returned an error (HTTP {resp.status_code})."

    items = resp.json().get("items", [])
    if not items:
        return "Nothing on the calendar in that range."
    return "\n".join(_summarize_event(ev) for ev in items)


def _holidays(time_min: Optional[str], time_max: Optional[str], max_results: int = 20) -> str:
    """List holidays from the configured public holiday calendar (see
    _holiday_calendar_id), NOT the user's primary calendar — Google
    doesn't put holidays on 'primary' unless the user manually added
    the holiday calendar to their list, which most people never do.
    Defaults to the current month if no range is given, since that's
    the common phrasing ('is there any holiday this month')."""
    cal_id = _holiday_calendar_id()
    if not cal_id:
        return ("No holiday calendar is configured — add \"holiday_calendar_id\" to "
                "config/api_keys.json (e.g. \"en.indian#holiday@group.v.calendar.google.com\" "
                "for Indian holidays; Google publishes one per country).")

    headers = _headers()
    if not headers:
        return _not_connected_message()

    if not time_min or not time_max:
        now = _local_now()
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_of_month.month == 12:
            next_month = start_of_month.replace(year=start_of_month.year + 1, month=1)
        else:
            next_month = start_of_month.replace(month=start_of_month.month + 1)
        time_min = time_min or start_of_month.isoformat()
        time_max = time_max or next_month.isoformat()
    else:
        time_min = _normalize_range_time(time_min)
        time_max = _normalize_range_time(time_max)

    import urllib.parse
    try:
        resp = get_session().get(
            f"{API_BASE}/calendars/{urllib.parse.quote(cal_id, safe='')}/events",
            headers=headers, timeout=_TIMEOUT,
            params={
                "timeMin": time_min, "timeMax": time_max,
                "singleEvents": "true", "orderBy": "startTime",
                "maxResults": max_results,
            },
        )
    except Exception as exc:
        return f"Network error reaching Google Calendar: {exc}"

    if resp.status_code == 404:
        return f"Couldn't find the holiday calendar \"{cal_id}\" — check holiday_calendar_id in config/api_keys.json."
    if resp.status_code != 200:
        return f"Google Calendar returned an error (HTTP {resp.status_code}) while fetching holidays."

    items = resp.json().get("items", [])
    if not items:
        return "No holidays found in that range."
    return "\n".join(f"{ev.get('summary', '(unnamed holiday)')} — {_fmt_event_time(ev)}" for ev in items)



def _today() -> str:
    now = _local_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return _list_events(start.isoformat(), end.isoformat(), max_results=20)


def _next() -> str:
    headers = _headers()
    if not headers:
        return _not_connected_message()
    try:
        resp = get_session().get(
            f"{API_BASE}/calendars/primary/events",
            headers=headers, timeout=_TIMEOUT,
            params={
                "timeMin": _local_now().isoformat(), "singleEvents": "true",
                "orderBy": "startTime", "maxResults": 1,
            },
        )
    except Exception as exc:
        return f"Network error reaching Google Calendar: {exc}"
    if resp.status_code != 200:
        return f"Google Calendar returned an error (HTTP {resp.status_code})."
    items = resp.json().get("items", [])
    if not items:
        return "Nothing else on the calendar."
    return "Next up: " + _summarize_event(items[0])


def _google_time_field(value: str) -> dict:
    """Build a Google Calendar API start/end object. Google rejects a bare
    dateTime with no UTC offset unless timeZone is also given, so this
    always attaches the configured IANA zone (Asia/Kolkata by default)
    — this is what was missing before and caused HTTP 400 'Missing time
    zone definition for start time.'"""
    value = (value or "").strip()
    if len(value) == 10 and value.count("-") == 2:
        return {"date": value}
    # Already has an explicit UTC offset (e.g. ends in +05:30 or Z) —
    # Google accepts that with or without timeZone, but we still pass
    # it for consistency/clarity across events.
    return {"dateTime": value, "timeZone": _timezone_name()}


def _create(title: str, start: str, end: str = "", location: str = "",
            description: str = "", attendees: str = "") -> str:
    if not title or not start:
        return "I need at least a title and a start time to create an event."
    if not end:
        try:
            start_dt = datetime.fromisoformat(start)
            end = (start_dt + timedelta(hours=1)).isoformat()
        except Exception:
            return "I couldn't understand that start time — please give it in ISO 8601 format."

    headers = _headers()
    if not headers:
        return _not_connected_message()

    body = {
        "summary": title,
        "start": _google_time_field(start),
        "end": _google_time_field(end),
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": e.strip()} for e in attendees.split(",") if e.strip()]

    try:
        resp = get_session().post(f"{API_BASE}/calendars/primary/events",
                              headers=headers, json=body, timeout=_TIMEOUT)
    except Exception as exc:
        return f"Couldn't reach Google Calendar ({exc})."

    if resp.status_code not in (200, 201):
        return f"Google Calendar couldn't create the event (HTTP {resp.status_code}): {resp.text[:200]}"

    ev = resp.json()
    return f"Added \"{title}\" — {_fmt_event_time(ev)}."


def _find_event_id(query: str) -> Optional[dict]:
    """Best-effort: resolve a short id prefix OR a title search to one
    upcoming event, so voice commands don't require the user to know a
    real event ID. Window is intentionally wide (a year back, two years
    forward) — a 60-day window used to make anything more than ~2
    months out (e.g. a birthday set today for October) unfindable for
    update/delete, which silently broke those actions."""
    headers = _headers()
    if not headers:
        return None
    try:
        resp = get_session().get(
            f"{API_BASE}/calendars/primary/events",
            headers=headers, timeout=_TIMEOUT,
            params={
                "timeMin": (_local_now() - timedelta(days=365)).isoformat(),
                "timeMax": (_local_now() + timedelta(days=730)).isoformat(),
                "singleEvents": "true", "orderBy": "startTime",
                "q": query, "maxResults": 10,
            },
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    items = resp.json().get("items", [])
    if not items:
        return None
    # Prefer an exact short-id match if the caller passed one.
    for ev in items:
        if ev.get("id", "").startswith(query):
            return ev
    # Otherwise prefer the closest event to "now" among title matches —
    # Google's 'q' full-text search can return multiple loose matches,
    # and the nearest one in time is the most likely intended target.
    now = _local_now()

    def _dist(ev):
        raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tzinfo())
            return abs((dt - now).total_seconds())
        except Exception:
            return float("inf")

    items.sort(key=_dist)
    return items[0]


def _update(event_query: str, title: str = "", start: str = "", end: str = "",
            location: str = "") -> str:
    if not (title or start or end or location):
        return "Tell me what to change — a new title, time, or location."

    headers = _headers()
    if not headers:
        return _not_connected_message()

    ev = _find_event_id(event_query)
    if not ev:
        return f"I couldn't find an event matching \"{event_query}\"."

    body = {}
    if title:
        body["summary"] = title
    if start:
        body["start"] = _google_time_field(start)
    if end:
        body["end"] = _google_time_field(end)
    if location:
        body["location"] = location

    try:
        resp = get_session().patch(f"{API_BASE}/calendars/primary/events/{ev['id']}",
                               headers=headers, json=body, timeout=_TIMEOUT)
    except Exception as exc:
        return f"Network error reaching Google Calendar: {exc}"

    if resp.status_code != 200:
        return f"Google Calendar couldn't update the event (HTTP {resp.status_code})."

    updated = resp.json()
    new_start = updated.get("start", {}).get("dateTime") or updated.get("start", {}).get("date", "")
    new_end = updated.get("end", {}).get("dateTime") or updated.get("end", {}).get("date", "")
    return f"Updated \"{updated.get('summary', ev.get('summary'))}\" — {_fmt_event_time(updated)}."


def _delete(event_query: str) -> str:
    headers = _headers()
    if not headers:
        return _not_connected_message()

    ev = _find_event_id(event_query)
    if not ev:
        return f"I couldn't find an event matching \"{event_query}\"."

    try:
        resp = get_session().delete(f"{API_BASE}/calendars/primary/events/{ev['id']}",
                                headers=headers, timeout=_TIMEOUT)
    except Exception as exc:
        return f"Network error reaching Google Calendar: {exc}"

    if resp.status_code not in (200, 204):
        return f"Google Calendar couldn't cancel the event (HTTP {resp.status_code})."
    return f"Cancelled \"{ev.get('summary', 'that event')}\"."


def _status() -> str:
    if not is_configured():
        return "Google Calendar isn't configured — no Client ID/Secret set."
    if not has_refresh_token():
        return "Google Calendar isn't connected yet. Run scripts/google_calendar_login.py."
    return "Google Calendar is connected and ready."


def _sync() -> str:
    return "Using Google Calendar only; local sync removed."


_KNOWN_ACTIONS = (
    "status", "today", "next", "list", "upcoming", "range",
    "holidays", "holiday",
    "create", "update", "reschedule", "move",
    "delete", "cancel", "remove", "sync",
)


def _normalize_action(action: str) -> str:
    """The model sometimes glues two plausible action names together
    (e.g. 'list/upcoming', 'list,upcoming', 'list upcoming') instead of
    picking one. Rather than bouncing that back as an error and burning
    a whole extra round trip, split on common separators and use the
    first token that's actually a known action."""
    action = (action or "today").lower().strip()
    if action in _KNOWN_ACTIONS:
        return action
    import re
    for token in re.split(r"[\s/,;|+]+", action):
        if token in _KNOWN_ACTIONS:
            return token
    return action


# ---------------------------------------------------------------------------
# Tool entrypoint
# ---------------------------------------------------------------------------
def calendar_action(action: str = "today", **kwargs) -> str:
    action = _normalize_action(action)

    if action == "status":
        return _status()
    if action == "today":
        return _today()
    if action == "next":
        return _next()
    if action in ("list", "upcoming", "range"):
        return _list_events(kwargs.get("time_min"), kwargs.get("time_max"),
                             max_results=int(kwargs.get("max_results", 10) or 10))
    if action in ("holidays", "holiday"):
        return _holidays(kwargs.get("time_min"), kwargs.get("time_max"),
                          max_results=int(kwargs.get("max_results", 20) or 20))
    if action == "create":
        return _create(
            kwargs.get("title", ""), kwargs.get("start", ""), kwargs.get("end", ""),
            kwargs.get("location", ""), kwargs.get("description", ""),
            kwargs.get("attendees", ""),
        )
    if action in ("update", "reschedule", "move"):
        return _update(
            kwargs.get("event_query", ""), kwargs.get("title", ""),
            kwargs.get("start", ""), kwargs.get("end", ""), kwargs.get("location", ""),
        )
    if action in ("delete", "cancel", "remove"):
        return _delete(kwargs.get("event_query", ""))
    if action == "sync":
        return _sync()

    return ("Unknown calendar action. Use: status, today, next, list, holidays, "
            "create, update, delete, sync.")


__all__ = ["calendar_action"]
