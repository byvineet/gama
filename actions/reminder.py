"""
actions/reminder.py — Gama Reminder, Alarm & Timer System
==========================================================
Features:
  - Reminders: "remind me in 5 minutes to drink water"
  - Alarms: "set alarm for 7:30 AM"
  - Timers: "set a timer for 10 minutes"
  - List/cancel active items
  - Desktop notifications + sound alerts
  - SPEAKS the message aloud via Gemini when fired
  - WAKES GAMA from sleep when fired (so it can speak)

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_reminders: List[Dict] = []
_alarms:    List[Dict] = []
_timers:    List[Dict] = []
_lock = threading.Lock()

# Callback: sends a text message to Gemini to speak aloud.
_speak_callback: Optional[Callable[[str], None]] = None
# Callback: wakes GAMA from sleep before speaking.
_wake_callback:  Optional[Callable[[], None]]    = None
# Callback: tells the UI dashboard to refresh immediately (e.g. right after
# a reminder/alarm/timer is set/cancelled) instead of waiting for the next
# periodic tick.
_ui_refresh_callback: Optional[Callable[[], None]] = None


def set_speak_callback(callback: Callable[[str], None]) -> None:
    global _speak_callback
    _speak_callback = callback
    logger.info("Reminder speak callback registered.")


def set_ui_refresh_callback(callback: Callable[[], None]) -> None:
    """Register a callback that immediately refreshes the dashboard UI.

    Called by main.py/ui.py so the CLASS/REMINDER panel updates the instant
    a reminder, alarm, or timer is set or cancelled — not only after the
    next 60s clock tick or after the item fires.
    """
    global _ui_refresh_callback
    _ui_refresh_callback = callback
    logger.info("Reminder UI-refresh callback registered.")


def _notify_ui_refresh() -> None:
    if _ui_refresh_callback is not None:
        try:
            _ui_refresh_callback()
        except Exception:
            logger.debug("UI refresh callback failed", exc_info=True)


def set_wake_callback(callback: Callable[[], None]) -> None:
    """Register a callback that wakes GAMA from sleep when a reminder fires.

    Called by main.py so timers and alarms always wake the assistant before
    announcing — preventing the 'GAMA sleeps and misses its own timer' bug.
    """
    global _wake_callback
    _wake_callback = callback
    logger.info("Reminder wake callback registered.")


def get_active_count() -> int:
    """Return the total number of active (not-yet-fired) items.

    Used by the auto-sleep watcher to block sleep while reminders are pending.
    """
    with _lock:
        count  = sum(1 for r in _reminders if not r["done"])
        count += sum(1 for a in _alarms    if not a["done"])
        count += sum(1 for t in _timers    if not t["done"])
    return count


def get_next_reminder_summary() -> str:
    """Return a short human-readable summary of the next due item."""
    now = datetime.now()
    next_item = None
    min_diff = float("inf")
    with _lock:
        for r in _reminders:
            if not r["done"]:
                diff = (r["remind_at"] - now).total_seconds()
                if 0 <= diff < min_diff:
                    min_diff = diff
                    mins = max(1, int(diff / 60))
                    next_item = f"'{r['message']}' in {mins}m"
        for t in _timers:
            if not t["done"]:
                diff = (t["end_at"] - now).total_seconds()
                if 0 <= diff < min_diff:
                    min_diff = diff
                    mins = max(1, int(diff / 60))
                    next_item = f"Timer '{t['label']}' in {mins}m"
    return next_item or ""



def get_next_due_seconds() -> Optional[float]:
    """Return seconds until the soonest pending reminder / alarm / timer.

    Returns ``None`` when nothing is pending.  Used by the auto-sleep watcher
    so it can block standby only when a near-term item is about to fire —
    a far-future reminder must NOT suppress standby indefinitely.
    """
    now = datetime.now()
    candidates: List[float] = []
    with _lock:
        for r in _reminders:
            if not r["done"]:
                candidates.append((r["remind_at"] - now).total_seconds())
        for a in _alarms:
            if not a["done"]:
                candidates.append((a["alarm_at"] - now).total_seconds())
        for t in _timers:
            if not t["done"]:
                candidates.append((t["end_at"] - now).total_seconds())
    # Filter out already-overdue items (they're mid-fire) and return minimum
    future = [s for s in candidates if s > 0]
    return min(future) if future else None


def reminder(action: str = "set", **kwargs) -> str:
    """Main entry point for reminders, alarms, and timers."""
    action = (action or "set").lower().strip()

    if action == "set":
        return _set_reminder(kwargs.get("message", ""), kwargs.get("in_minutes", 0))
    if action == "alarm":
        return _set_alarm(kwargs.get("time", ""), kwargs.get("message", "Alarm"))
    if action == "timer":
        return _set_timer(kwargs.get("minutes", 0), kwargs.get("seconds", 0),
                          kwargs.get("message", "Timer done"))
    if action == "list":
        return _list_all()
    if action == "list_reminders":
        return _list_reminders()
    if action == "list_alarms":
        return _list_alarms()
    if action == "list_timers":
        return _list_timers()
    if action in ("cancel", "delete", "remove"):
        return _cancel(kwargs.get("id", 0), kwargs.get("type", "reminder"))
    if action in ("cancel_all", "delete_all", "remove_all"):
        return _cancel_all(kwargs.get("type", ""))
    return f"Unknown reminder action: {action}. Use: set, alarm, timer, list, cancel, delete."


# ── Reminders ──────────────────────────────────────────────────────────────────
def _set_reminder(message: str, in_minutes: int = 0) -> str:
    message = (message or "").strip()
    if not message:
        return "What should I remind you about?"
    try:
        minutes = int(in_minutes or 0)
    except Exception:
        minutes = 0
    if minutes <= 0:
        return "Please specify how many minutes from now."

    remind_at = datetime.now() + timedelta(minutes=minutes)
    with _lock:
        rid = _next_id(_reminders)
        _reminders.append({
            "id": rid, "message": message,
            "remind_at": remind_at, "done": False,
        })
    threading.Thread(target=_fire_reminder, args=(rid, minutes), daemon=True).start()
    _notify_ui_refresh()
    return (f"Reminder set for {minutes} minute{'s' if minutes != 1 else ''} from now "
            f"({remind_at.strftime('%I:%M %p')}): {message}")


# ── Alarms ─────────────────────────────────────────────────────────────────────
def _set_alarm(time_str: str, message: str = "Alarm") -> str:
    time_str = (time_str or "").strip()
    if not time_str:
        return "What time should I set the alarm for? (e.g. '7:30 AM' or '14:30')"
    try:
        alarm_time = _parse_time(time_str)
        if alarm_time is None:
            return f"Couldn't parse time '{time_str}'. Try '7:30 AM' or '14:30'."
        now = datetime.now()
        alarm_dt = now.replace(hour=alarm_time.hour, minute=alarm_time.minute,
                               second=0, microsecond=0)
        if alarm_dt <= now:
            alarm_dt += timedelta(days=1)
        seconds_until = (alarm_dt - now).total_seconds()
        with _lock:
            aid = _next_id(_alarms)
            _alarms.append({
                "id": aid, "message": message,
                "alarm_at": alarm_dt, "done": False,
            })
        threading.Thread(target=_fire_alarm, args=(aid, seconds_until), daemon=True).start()
        _notify_ui_refresh()
        h = int(seconds_until // 3600)
        m = int((seconds_until % 3600) // 60)
        return (f"Alarm set for {alarm_dt.strftime('%I:%M %p on %B %d')}: {message}. "
                f"That's in {h}h {m}m.")
    except Exception as exc:
        return f"Alarm setup failed: {exc}"


def _parse_time(time_str: str) -> Optional[datetime]:
    """Parse a time string like '7:30 AM', '14:30', '7am'."""
    import re
    time_str = time_str.strip().upper()
    # "7:30 AM" or "14:30"
    m = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM)?$', time_str)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return datetime.now().replace(hour=hour, minute=minute)
    # "7am" or "7 PM"
    m = re.match(r'^(\d{1,2})\s*(AM|PM)$', time_str)
    if m:
        hour, ampm = int(m.group(1)), m.group(2)
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            return datetime.now().replace(hour=hour, minute=0)
    return None


# ── Timers ─────────────────────────────────────────────────────────────────────
def _set_timer(minutes: int = 0, seconds: int = 0, message: str = "Timer done") -> str:
    try:
        minutes = int(minutes or 0)
        seconds = int(seconds or 0)
    except Exception:
        minutes = seconds = 0
    total_seconds = minutes * 60 + seconds
    if total_seconds <= 0:
        return "Please specify a duration (e.g. '10 minutes')."

    with _lock:
        tid    = _next_id(_timers)
        end_at = datetime.now() + timedelta(seconds=total_seconds)
        _timers.append({"id": tid, "message": message, "end_at": end_at, "done": False})

    threading.Thread(target=_fire_timer, args=(tid, total_seconds), daemon=True).start()
    _notify_ui_refresh()
    # Also project a live countdown on the HUD display stage when available
    try:
        from actions.display_stage import project_timer_on_display
        project_timer_on_display(total_seconds, label=message or "Timer", running=True)
    except Exception:
        pass
    parts = []
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return f"Timer set for {' and '.join(parts)}. I'll notify you when it's done."


# ── List & cancel ──────────────────────────────────────────────────────────────
def _list_all() -> str:
    parts = []
    for fn in (_list_reminders, _list_alarms, _list_timers):
        s = fn()
        if "No active" not in s:
            parts.append(s)
    return "\n\n".join(parts) if parts else "No active reminders, alarms, or timers."


def _list_reminders() -> str:
    with _lock:
        active = [r for r in _reminders if not r["done"]]
    if not active:
        return "No active reminders."
    lines = ["Active reminders:"]
    for r in active:
        lines.append(f"  #{r['id']} at {r['remind_at'].strftime('%I:%M %p')} — {r['message']}")
    return "\n".join(lines)


def _list_alarms() -> str:
    with _lock:
        active = [a for a in _alarms if not a["done"]]
    if not active:
        return "No active alarms."
    lines = ["Active alarms:"]
    for a in active:
        lines.append(f"  #{a['id']} at {a['alarm_at'].strftime('%I:%M %p')} — {a['message']}")
    return "\n".join(lines)


def _list_timers() -> str:
    with _lock:
        active = [t for t in _timers if not t["done"]]
    if not active:
        return "No active timers."
    lines = ["Active timers:"]
    now = datetime.now()
    for t in active:
        remaining = max(0.0, (t["end_at"] - now).total_seconds())
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        lines.append(f"  #{t['id']} — {t['message']} ({mins}m {secs}s remaining)")
    return "\n".join(lines)


def _cancel(rid: int, item_type: str = "reminder") -> str:
    """Cancel-and-delete a single item on demand — it's fully removed from
    the list, not just flagged done, so it stops showing up anywhere."""
    try:
        rid = int(rid)
    except Exception:
        return "Invalid ID."
    item_type = item_type.lower().strip()
    target = {"reminder": _reminders, "alarm": _alarms, "timer": _timers}.get(item_type, _reminders)
    with _lock:
        for item in list(target):
            if item["id"] == rid:
                item["done"] = True
                target.remove(item)
                _notify_ui_refresh()
                return f"{item_type.capitalize()} #{rid} cancelled and removed."
    return f"{item_type.capitalize()} #{rid} not found."


def _cancel_all(item_type: str = "") -> str:
    """Cancel-and-delete every active item of a type (or all types), on demand."""
    item_type = item_type.lower().strip()
    count = 0
    with _lock:
        if item_type in ("", "reminder"):
            removed = [r for r in _reminders if not r["done"]]
            count += len(removed)
            _reminders[:] = [r for r in _reminders if r["done"]]
        if item_type in ("", "alarm"):
            removed = [a for a in _alarms if not a["done"]]
            count += len(removed)
            _alarms[:] = [a for a in _alarms if a["done"]]
        if item_type in ("", "timer"):
            removed = [t for t in _timers if not t["done"]]
            count += len(removed)
            _timers[:] = [t for t in _timers if t["done"]]
    if count:
        _notify_ui_refresh()
    return f"Cancelled and removed {count} item(s)."


# ── Fire callbacks (daemon threads) ───────────────────────────────────────────
def _fire_reminder(rid: int, minutes: int) -> None:
    time.sleep(max(0, minutes * 60))
    fired = None
    with _lock:
        for r in _reminders:
            if r["id"] == rid and not r["done"]:
                r["done"] = True
                fired = r
                break
        if fired is not None:
            # Auto-remove from the list now that it has completed.
            _reminders.remove(fired)
    if fired is not None:
        _notify("Reminder", fired["message"])


def _fire_alarm(aid: int, seconds_until: float) -> None:
    time.sleep(max(0, seconds_until))
    fired = None
    with _lock:
        for a in _alarms:
            if a["id"] == aid and not a["done"]:
                a["done"] = True
                fired = a
                break
        if fired is not None:
            # Auto-remove from the list now that it has completed.
            _alarms.remove(fired)
    if fired is not None:
        _notify("Alarm", fired["message"], alarm_sound=True)


def _fire_timer(tid: int, seconds: int) -> None:
    time.sleep(max(0, seconds))
    fired = None
    with _lock:
        for t in _timers:
            if t["id"] == tid and not t["done"]:
                t["done"] = True
                fired = t
                break
        if fired is not None:
            # Auto-remove from the list now that it has completed.
            _timers.remove(fired)
    if fired is not None:
        _notify("Timer", fired["message"])


def _notify(title: str, message: str, alarm_sound: bool = False) -> None:
    """Desktop notification + sound + speak (via _speak_callback / Arbitrator).

    Intentionally does NOT call _wake_callback here. _speak_callback (wired
    to main.py's _speak_via_session) already handles waking GAMA just long
    enough to announce and then automatically returning to sleep — based on
    its own accurate "was GAMA actually asleep" check. Calling the wake
    callback here first used to flip GAMA into a *permanent* awake state
    before that check ran, which broke the auto-resleep behaviour and
    silently left GAMA awake (sometimes with nothing actually spoken, if the
    resulting live-session round trip didn't produce audio). GAMA should
    only ever enter a persistent awake/listening state via wake-word
    detection — reminders/alarms/timers/class-watcher events must only ever
    "poke it awake to say one line, then back to sleep."
    """
    from state_engine.arbitrator import arbitrator
    from state_engine.user_state import PriorityLevel

    prio = PriorityLevel.P1_URGENT if alarm_sound else PriorityLevel.P2_NORMAL
    arbitrator.dispatch(
        title=f"GAMA {title}",
        message=message,
        priority=prio,
        category=title.lower(),
        speak=True,
    )

    # 2. Sound beeps
    try:
        import platform
        if platform.system() == "Windows":
            import winsound
            if alarm_sound:
                for _ in range(5):
                    for freq in [1000, 1200, 800]:
                        winsound.Beep(freq, 200)
                    time.sleep(0.1)
            else:
                for freq in [800, 1000, 1200]:
                    winsound.Beep(freq, 180)
                    time.sleep(0.08)
    except Exception:
        pass

    # 3. Speak — _speak_callback wakes GAMA (temporarily, if asleep) and
    # handles the announce-then-resleep cycle itself; see the docstring above.
    speak_text = f"{title}: {message}"
    if _speak_callback is not None:
        try:
            _speak_callback(speak_text)
            logger.info(f"Reminder spoke via Gemini: {speak_text}")
        except Exception as exc:
            logger.error(f"Speak callback failed: {exc}")

    logger.info(f"{title} fired: {message}")


def _next_id(items: List[Dict]) -> int:
    if not items:
        return 1
    return max(item["id"] for item in items) + 1


def fire_notification(title: str, message: str, alarm_sound: bool = False) -> None:
    """Public wrapper around the reminder-firing pipeline (notify + sound +
    wake GAMA + speak via Gemini). Lets other modules — e.g. class_schedule's
    study reminders — reuse the exact same alert behavior as reminders,
    alarms, and timers without duplicating the logic.
    """
    _notify(title, message, alarm_sound=alarm_sound)


__all__ = ["reminder", "set_speak_callback", "set_wake_callback", "get_active_count",
           "get_next_due_seconds", "get_next_reminder_summary", "fire_notification"]