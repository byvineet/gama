"""
actions/class_schedule.py — Gama Class Schedule & Study Reminders
====================================================================
Tracks Vineet's Physics Wallah "Arjuna JEE 2027" batch timetable (he is
personally preparing for JEE 2028) and proactively reminds him 10 minutes
before, 5 minutes before, and right when each class starts — offering to
open the PW live-class link. Classes run Monday–Saturday; Sunday is off.

Times are stored and shown in 12-hour format (e.g. "4:00 PM").
Parsing accepts both 12-hour and legacy 24-hour strings.

The timetable lives in config/class_schedule.json so it can be edited
without touching code (via the class_schedule tool, action=set_day).

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = get_logger(__name__)
logger = log  # back-compat alias
PW_LIVE_URL = "https://www.pw.live/study-v2/study"


def _now_ist() -> datetime:
    """Current wall-clock time in India, as a naive datetime."""
    try:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
    except Exception:
        from datetime import timezone
        ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).replace(tzinfo=None)




BASE_DIR = _get_base_dir()
SCHEDULE_PATH = BASE_DIR / "config" / "class_schedule.json"

# Default timetable — 12-hour format
_DEFAULT_SCHEDULE: Dict[str, List[Dict]] = {
    "monday": [
        {"start": "4:00 PM", "end": "6:00 PM", "subject": "Chemistry"},
        {"start": "6:15 PM", "end": "8:00 PM", "subject": "Mathematics"},
    ],
    "tuesday": [
        {"start": "4:00 PM", "end": "6:00 PM", "subject": "Physics"},
        {"start": "6:15 PM", "end": "8:00 PM", "subject": "Mathematics"},
    ],
    "wednesday": [
        {"start": "4:00 PM", "end": "6:00 PM", "subject": "Mathematics"},
        {"start": "6:15 PM", "end": "8:00 PM", "subject": "Physics"},
    ],
    "thursday": [
        {"start": "4:00 PM", "end": "6:00 PM", "subject": "Mathematics"},
        {"start": "6:15 PM", "end": "8:00 PM", "subject": "Physics"},
    ],
    "friday": [
        {"start": "4:00 PM", "end": "6:00 PM", "subject": "Chemistry"},
        {"start": "6:15 PM", "end": "8:00 PM", "subject": "Physics"},
    ],
    "saturday": [
        {"start": "4:00 PM", "end": "6:00 PM", "subject": "Chemistry"},
        {"start": "6:15 PM", "end": "8:00 PM", "subject": "Physics"},
    ],
    "sunday": [],
}

_WEEKDAYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]

_lock = threading.Lock()
_fired_today: set = set()
_watcher_thread: Optional[threading.Thread] = None
_watcher_running = False
_CHECK_INTERVAL_SECONDS = 20


def _parse_time_to_hm(time_str: str) -> Tuple[int, int]:
    """Parse '4:00 PM', '4:00PM', '16:00', '4pm' → (hour 0-23, minute)."""
    s = (time_str or "").strip()
    if not s:
        raise ValueError("empty time")

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])$", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3).upper()
        if hour < 1 or hour > 12 or minute > 59:
            raise ValueError(f"invalid 12h time: {time_str}")
        if ampm == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute

    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"invalid 24h time: {time_str}")
        return hour, minute

    raise ValueError(f"unrecognized time: {time_str}")


def _format_12h(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{minute:02d} {ampm}"


def _to_12h_str(time_str: str) -> str:
    try:
        h, m = _parse_time_to_hm(str(time_str))
        return _format_12h(h, m)
    except Exception:
        return str(time_str or "")


def _parse_datetime_on(date: datetime, time_str: str) -> datetime:
    h, m = _parse_time_to_hm(str(time_str))
    return date.replace(hour=h, minute=m, second=0, microsecond=0)


def _normalize_entry(entry: Dict) -> Dict:
    out = dict(entry)
    if out.get("start"):
        out["start"] = _to_12h_str(out["start"])
    if out.get("end"):
        out["end"] = _to_12h_str(out["end"])
    return out


def _normalize_schedule(schedule: Dict) -> Dict[str, List[Dict]]:
    normalized: Dict[str, List[Dict]] = {}
    for day in _WEEKDAYS:
        entries = schedule.get(day, [])
        if not isinstance(entries, list):
            entries = []
        normalized[day] = [
            _normalize_entry(c) for c in entries if isinstance(c, dict)
        ]
    return normalized


def _load_schedule() -> Dict[str, List[Dict]]:
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _normalize_schedule(raw)
    except Exception:
        return json.loads(json.dumps(_DEFAULT_SCHEDULE))


def _save_schedule(schedule: Dict) -> None:
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_schedule(schedule)
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)


def ensure_schedule_file() -> None:
    """Write default timetable or migrate legacy 24h times to 12h."""
    if not SCHEDULE_PATH.exists():
        _save_schedule(_DEFAULT_SCHEDULE)
        return
    try:
        schedule = _load_schedule()
        _save_schedule(schedule)
    except Exception:
        pass


def class_schedule(action: str = "today", **kwargs) -> str:
    """Main entry point for viewing/editing Vineet's class timetable."""
    action = (action or "today").lower().strip()
    ensure_schedule_file()
    schedule = _load_schedule()

    if action == "today":
        return _describe_day(schedule, _now_ist().strftime("%A").lower())
    if action == "tomorrow":
        tomorrow_day = (_now_ist() + timedelta(days=1)).strftime("%A").lower()
        return _describe_day(schedule, tomorrow_day)
    if action == "week":
        return _describe_week(schedule)
    if action == "next":
        return _next_class(schedule)
    if action == "set_day":
        day = (kwargs.get("day") or "").lower().strip()
        classes = kwargs.get("classes")
        if day not in schedule:
            return f"'{day}' isn't a valid weekday."
        if not isinstance(classes, list):
            return (
                "Provide 'classes' as a list of {start, end, subject} entries "
                "(12-hour times like '4:00 PM')."
            )
        schedule[day] = [
            _normalize_entry(c) if isinstance(c, dict) else c for c in classes
        ]
        _save_schedule(schedule)
        with _lock:
            _fired_today.clear()
        return f"Updated {day.capitalize()}'s schedule."
    return _describe_day(schedule, _now_ist().strftime("%A").lower())


def _describe_day(schedule: Dict, day: str) -> str:
    classes = schedule.get(day, [])
    if not classes:
        return f"No classes on {day.capitalize()} — holiday!"
    lines = [f"{day.capitalize()}'s classes:"]
    for c in classes:
        start = _to_12h_str(c.get("start", ""))
        end = _to_12h_str(c.get("end", ""))
        lines.append(f"  {start}–{end} — {c.get('subject')}")
    return "\n".join(lines)


def _describe_week(schedule: Dict) -> str:
    lines = ["Weekly class schedule:"]
    for day in _WEEKDAYS:
        classes = schedule.get(day, [])
        if not classes:
            lines.append(f"  {day.capitalize()}: Holiday")
            continue
        parts = [
            f"{_to_12h_str(c.get('start'))}–{_to_12h_str(c.get('end'))} {c.get('subject')}"
            for c in classes
        ]
        lines.append(f"  {day.capitalize()}: " + ", ".join(parts))
    return "\n".join(lines)


def _next_class(schedule: Dict) -> str:
    now = _now_ist()
    for day_offset in range(0, 8):
        check_date = now + timedelta(days=day_offset)
        day = check_date.strftime("%A").lower()
        for c in schedule.get(day, []):
            try:
                day_base = check_date.replace(hour=0, minute=0, second=0, microsecond=0)
                start_dt = _parse_datetime_on(day_base, c.get("start", ""))
            except Exception:
                continue
            if start_dt > now:
                delta = start_dt - now
                hrs, rem = divmod(int(delta.total_seconds()), 3600)
                mins = rem // 60
                when = (
                    "today" if day_offset == 0
                    else ("tomorrow" if day_offset == 1 else day.capitalize())
                )
                start_disp = _to_12h_str(c.get("start", ""))
                return (
                    f"Next class: {c.get('subject')} at {start_disp} {when} "
                    f"(in {hrs}h {mins}m)."
                )
    return "No upcoming classes found."


def start_watcher() -> None:
    global _watcher_thread, _watcher_running
    if _watcher_running:
        return
    ensure_schedule_file()
    _watcher_running = True
    _watcher_thread = threading.Thread(
        target=_watch_loop, name="gama-class-watcher", daemon=True
    )
    _watcher_thread.start()
    logger.info("Class schedule watcher started.")


def stop_watcher() -> None:
    global _watcher_running
    _watcher_running = False


def _watch_loop() -> None:
    from actions.reminder import fire_notification
    while _watcher_running:
        try:
            _check_once(fire_notification)
        except Exception as exc:
            logger.error(f"Class watcher tick failed: {exc}")
        time.sleep(_CHECK_INTERVAL_SECONDS)


def _check_once(notify) -> None:
    from state_engine.arbitrator import arbitrator
    from state_engine.user_state import PriorityLevel, UserState, user_state_manager

    now = _now_ist()
    today_key = now.strftime("%Y-%m-%d")
    weekday = now.strftime("%A").lower()
    schedule = _load_schedule()
    classes = schedule.get(weekday, [])

    in_any_class = False

    for c in classes:
        subject = c.get("subject", "class")
        start_str = c.get("start")
        end_str = c.get("end")
        if not start_str:
            continue
        try:
            day_base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_dt = _parse_datetime_on(day_base, start_str)
            end_dt = (
                _parse_datetime_on(day_base, end_str)
                if end_str
                else start_dt + timedelta(hours=2)
            )
        except ValueError:
            continue

        if start_dt <= now <= end_dt:
            in_any_class = True

        for offset_min, tag in ((10, "10min"), (5, "5min"), (0, "start")):
            fire_at = start_dt - timedelta(minutes=offset_min)
            fire_key = f"{today_key}|{start_str}|{tag}"
            with _lock:
                if fire_key in _fired_today:
                    continue
                if not (
                    fire_at
                    <= now
                    < fire_at + timedelta(seconds=_CHECK_INTERVAL_SECONDS + 10)
                ):
                    continue
                _fired_today.add(fire_key)

            start_disp = _to_12h_str(start_str)
            if tag == "start":
                message = (
                    f"Your {subject} class has started ({start_disp}). "
                    f"Should I open Physics Wallah ({PW_LIVE_URL}) for you, Vineet?"
                )
                prio = PriorityLevel.P1_URGENT
            else:
                message = (
                    f"{offset_min} minutes left for your {subject} class "
                    f"(starts {start_disp})."
                )
                prio = PriorityLevel.P2_NORMAL

            arbitrator.dispatch(
                title="Class Reminder",
                message=message,
                priority=prio,
                category="class",
                speak=True,
            )

    current_state = user_state_manager.get_state()
    if in_any_class and current_state != UserState.IN_CLASS:
        user_state_manager.set_state(UserState.IN_CLASS, source="class_schedule")
    elif not in_any_class and current_state == UserState.IN_CLASS:
        user_state_manager.set_state(UserState.IDLE, source="class_schedule")
        arbitrator.flush_debrief_queue()

    with _lock:
        stale = [k for k in _fired_today if not k.startswith(today_key)]
        for k in stale:
            _fired_today.discard(k)


__all__ = [
    "class_schedule",
    "start_watcher",
    "stop_watcher",
    "ensure_schedule_file",
    "PW_LIVE_URL",
]
