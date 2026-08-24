"""
actions/telegram_sender.py — Gama Telegram Messenger
======================================================
Send Telegram messages, Live native-audio voice notes, and files via Bot API.

Use cases
---------
* Text: "send me a Telegram saying the build finished"
* Voice (Live native audio): "send a voice message on Telegram saying …"
* Schedule voice (pre-synth): "send voice message regarding X at 7 PM"
* Schedule content: "voice note on Telegram about tomorrow's class schedule"
* Critical alerts when enabled

Credentials
-----------
* Bot token  → encrypted credential store
* Chat ID    → config/api_keys.json
* alerts_on  → config/api_keys.json boolean

Author : Gama / Vineet Machchal
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
from typing import Any, Dict, List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
_CRED_BOT_TOKEN = "telegram_bot_token"
_CFG_CHAT_ID = "telegram_chat_id"
_CFG_ALERTS = "telegram_alerts_enabled"

_ALERT_COOLDOWN_S = 120.0
_alert_last: dict[str, float] = {}
_alert_lock = threading.Lock()

# Scheduled voice notes: pre-synthesized audio, fire at exact time
_scheduled_lock = threading.Lock()
_scheduled_voices: List[Dict[str, Any]] = []
_sched_id = 0
_sched_watcher_started = False




BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
VOICE_CACHE_DIR = BASE_DIR / "storage" / "telegram_voice_notes"


def _read_cfg() -> dict:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cfg(data: dict) -> None:
    API_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = API_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(API_CONFIG_PATH)


def _get_bot_token() -> str:
    try:
        from security.credential_store import get_secret, available as store_available
        if store_available():
            tok = get_secret(_CRED_BOT_TOKEN)
            if tok:
                return tok.strip()
    except Exception as exc:
        logger.debug(f"telegram: credential store read failed: {exc}")
    cfg = _read_cfg()
    return (cfg.get("telegram_bot_token") or "").strip()


def _get_chat_id() -> str:
    cfg = _read_cfg()
    return str(cfg.get(_CFG_CHAT_ID) or "").strip()


def _alerts_enabled() -> bool:
    cfg = _read_cfg()
    return bool(cfg.get(_CFG_ALERTS, False))


def is_configured() -> bool:
    return bool(_get_bot_token() and _get_chat_id())


def _api_call(method: str, payload: dict, timeout: float = 15.0) -> dict:
    import requests

    token = _get_bot_token()
    if not token:
        raise RuntimeError("Telegram bot token not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json=payload, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        desc = data.get("description") or resp.text
        raise RuntimeError(f"Telegram API error: {desc}")
    return data.get("result") or {}


def _send_message(chat_id: str, text: str, parse_mode: Optional[str] = None) -> str:
    if not text or not str(text).strip():
        return "What message should I send?"
    if not chat_id:
        return "No Telegram chat_id configured. Run setup first."

    payload = {
        "chat_id": chat_id,
        "text": str(text).strip()[:4096],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        _api_call("sendMessage", payload)
        return "OK. Telegram text message sent."
    except Exception as exc:
        logger.warning(f"telegram send failed: {exc}")
        return f"Telegram send failed: {exc}"


def _send_document(chat_id: str, file_path: str, caption: str = "") -> str:
    from pathlib import Path
    import requests

    if not chat_id:
        return "No Telegram chat_id configured. Run setup first."
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        return f"File not found: {file_path}"
    size = path.stat().st_size
    if size > 48 * 1024 * 1024:
        return f"File too large for Telegram ({size // (1024*1024)} MB). Keep under ~45 MB."
    token = _get_bot_token()
    if not token:
        return "Telegram bot token not configured."
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with path.open("rb") as fh:
            files = {"document": (path.name, fh)}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = str(caption)[:1024]
            resp = requests.post(url, data=data, files=files, timeout=120)
        payload = resp.json()
        if not payload.get("ok"):
            return f"Telegram send file failed: {payload.get('description') or resp.text}"
        return f"Sent file '{path.name}' via Telegram."
    except Exception as exc:
        logger.warning(f"telegram sendDocument failed: {exc}")
        return f"Telegram send file failed: {exc}"


def _send_voice_file(chat_id: str, audio_path: Path, caption: str = "", delete_after: bool = False) -> str:
    """Upload an already-synthesized audio file as a Telegram voice note."""
    import requests

    if not chat_id:
        return "No Telegram chat_id configured. Run setup first."
    path = Path(audio_path)
    if not path.is_file():
        return f"Voice file not found: {audio_path}"

    token = _get_bot_token()
    if not token:
        return "Telegram bot token not configured."

    is_ogg = path.suffix.lower() == ".ogg"
    method = "sendVoice" if is_ogg else "sendAudio"
    field = "voice" if is_ogg else "audio"
    url = f"https://api.telegram.org/bot{token}/{method}"
    cap = (caption or "").strip()

    try:
        with path.open("rb") as fh:
            files = {field: (path.name, fh)}
            data = {"chat_id": str(chat_id)}
            if cap:
                data["caption"] = cap[:1024]
            resp = requests.post(url, data=data, files=files, timeout=120)
        payload = resp.json()
        if not payload.get("ok"):
            url2 = f"https://api.telegram.org/bot{token}/sendDocument"
            with path.open("rb") as fh:
                files = {"document": (path.name, fh)}
                data = {"chat_id": str(chat_id)}
                if cap:
                    data["caption"] = cap[:1024]
                resp = requests.post(url2, data=data, files=files, timeout=120)
            payload = resp.json()
            if not payload.get("ok"):
                return f"Telegram voice send failed: {payload.get('description') or resp.text}"
        return "Voice message sent on Telegram (Live native audio)."
    except Exception as exc:
        logger.warning(f"telegram voice upload failed: {exc}")
        return f"Telegram voice send failed: {exc}"
    finally:
        if delete_after:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _send_voice(chat_id: str, text: str, caption: str = "", voice_name: str = "") -> str:
    """Synthesize with Live native audio and send immediately."""
    text = (text or "").strip()
    if not text:
        return "What should the voice message say?"
    if not chat_id:
        return "No Telegram chat_id configured. Run setup first."

    try:
        from voice.live_voice_note import synthesize_live_voice_note
        audio_path = synthesize_live_voice_note(text, voice_name=voice_name or None)
    except Exception as exc:
        logger.warning("telegram Live voice synthesis failed: %s", exc)
        return f"Could not synthesize Live voice note: {exc}"

    return _send_voice_file(chat_id, Path(audio_path), caption=caption, delete_after=True)


# ---------------------------------------------------------------------------
# Dynamic, flexible context & content resolution (schedule, memory, notes, etc.)
# ---------------------------------------------------------------------------

_REFERENTIAL_PHRASES = frozenset({
    "", "it", "that", "this", "them", "those", "send it", "send that", "send this",
    "the message", "message", "the voice note", "voice note", "voice message",
    "what you said", "what you just said", "what we discussed", "the schedule",
    "schedule", "class schedule", "my schedule", "my classes",
})

_SCHEDULE_KEYWORDS = frozenset({
    "class_schedule", "schedule", "classes", "timetable", "class", "lectures",
    "class_timetable", "today_schedule", "tomorrow_schedule",
})


def _resolve_schedule_day(day_str: str = "", fallback_text: str = "") -> str:
    """Extract or determine the schedule day, defaulting to 'today' (never 'tomorrow')."""
    d = (day_str or "").lower().strip()
    if d in ("today", "tomorrow", "week", "next"):
        return d

    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for w in weekdays:
        if w in d:
            return w

    text_l = (fallback_text or "").lower()
    if "tomorrow" in text_l:
        return "tomorrow"
    if "today" in text_l or "todays" in text_l or "today's" in text_l:
        return "today"
    if "week" in text_l or "weekly" in text_l:
        return "week"
    if "next" in text_l:
        return "next"
    for w in weekdays:
        if w in text_l:
            return w

    # Default is always TODAY
    return "today"


def _content_from_class_schedule(day: str = "today", is_voice: bool = False) -> str:
    """Pull real class schedule from class_schedule module."""
    day = _resolve_schedule_day(day)
    try:
        from actions.class_schedule import class_schedule, _load_schedule, _describe_day, ensure_schedule_file
        ensure_schedule_file()
        if day in ("today", "tomorrow", "week", "next"):
            body = class_schedule(action=day)
        else:
            schedule = _load_schedule()
            if day in schedule:
                body = _describe_day(schedule, day)
            else:
                body = class_schedule(action="today")
    except Exception as exc:
        logger.warning("class_schedule lookup failed: %s", exc)
        return f"I could not load your class schedule ({exc})."

    if not is_voice:
        return body

    label = day if day in ("today", "tomorrow", "week", "next") else day.capitalize()
    if day == "next":
        return f"Sir, {body}"
    if day == "week":
        return f"Sir, here is your weekly class schedule. {body}"
    if day == "today":
        return f"Sir, here is your class schedule for today. {body}"
    return f"Sir, here is your class schedule for {label}. {body}"


def _resolve_context_fallback() -> str:
    """Retrieve conversational context or working memory if nothing explicit is given."""
    # 1. Check working memory slots
    try:
        from context_engine import working_memory
        for slot in ("task", "goal", "file", "project"):
            val = working_memory.get_slot(slot)
            if val:
                return f"Regarding your {slot}: {val}"
    except Exception:
        pass

    # 2. Check conversation state facts / goal
    try:
        from core.assistant_runtime import runtime
        conv = runtime.conversation
        if conv.recent_facts:
            return conv.recent_facts[-1]
        if conv.current_goal:
            return f"Active goal: {conv.current_goal}"
        if conv.active_task:
            return f"Active task: {conv.active_task}"
    except Exception:
        pass

    # 3. Check world model last message
    try:
        from core.world_model import world
        snap = world.snapshot() if hasattr(world, "snapshot") else None
        if snap and getattr(snap, "conversation", None):
            last_asst = snap.conversation.get("last_assistant_message")
            if last_asst:
                return str(last_asst)
    except Exception:
        pass

    return ""


def _resolve_content(is_voice: bool = False, **kwargs) -> str:
    """Build the message or voice-note text flexibly and context-aware.

    Handles explicit messages, regarding/topic parameters, schedule lookups
    (defaulting to today), reminders/tasks/notes, or contextual fallbacks.
    """
    raw_msg = str(
        kwargs.get("message")
        or kwargs.get("text")
        or kwargs.get("body")
        or ""
    ).strip()

    regarding = str(
        kwargs.get("regarding")
        or kwargs.get("topic")
        or kwargs.get("about")
        or ""
    ).lower().strip().replace("-", "_").replace(" ", "_")

    day_arg = str(
        kwargs.get("day")
        or kwargs.get("when")
        or kwargs.get("schedule_day")
        or ""
    ).lower().strip()

    use_schedule = kwargs.get("use_schedule") in (True, "true", "1", "yes", 1)

    # 1. Schedule request via regarding, use_schedule, or day
    if regarding in _SCHEDULE_KEYWORDS or use_schedule:
        resolved_day = _resolve_schedule_day(day_arg or regarding, fallback_text=raw_msg)
        return _content_from_class_schedule(resolved_day, is_voice=is_voice)

    # 2. Reminders / Goals / Notes / Clipboard / Screen topic
    if regarding in ("reminder", "reminders"):
        try:
            from actions.reminder import reminder
            res = reminder(action="list")
            return f"Your active reminders:\n{res}" if not is_voice else f"Sir, here are your active reminders: {res}"
        except Exception:
            pass
    elif regarding in ("goal", "goals", "active_goals", "task", "tasks"):
        try:
            from actions.goal_tracker import goal_tracker
            res = goal_tracker(action="list", status="active")
            return f"Active goals:\n{res}" if not is_voice else f"Sir, your active goals: {res}"
        except Exception:
            pass
    elif regarding in ("clipboard", "copied"):
        try:
            from actions.clipboard import clipboard
            return str(clipboard(action="get"))
        except Exception:
            pass

    # 3. If explicit message is provided and NOT a bare referential phrase
    raw_msg_lower = raw_msg.lower().strip(" .!?,:;'\"")
    if raw_msg and raw_msg_lower not in _REFERENTIAL_PHRASES:
        # Check if the message is purely pointing at schedule lookup without providing the schedule details
        if raw_msg_lower in (
            "class schedule", "my class schedule", "today's class schedule", "todays class schedule",
            "tomorrow's class schedule", "tomorrows class schedule", "my classes", "my timetable",
            "classes today", "classes tomorrow", "today schedule", "tomorrow schedule"
        ):
            resolved_day = _resolve_schedule_day(day_arg, fallback_text=raw_msg_lower)
            return _content_from_class_schedule(resolved_day, is_voice=is_voice)

        # Otherwise, the LLM or user provided the concrete message/content — respect it!
        return raw_msg

    # 4. If raw_msg was referential (e.g. "send it", "send this") or empty
    fallback = _resolve_context_fallback()
    if fallback:
        if is_voice and not fallback.lower().startswith("sir"):
            return f"Sir, {fallback}"
        return fallback

    return raw_msg


# Backwards-compatibility aliases
def _resolve_spoken_text(**kwargs) -> str:
    return _resolve_content(is_voice=True, **kwargs)


def _script_from_class_schedule(day: str = "today") -> str:
    return _content_from_class_schedule(day=day, is_voice=True)


# ---------------------------------------------------------------------------
# Scheduled voice notes (pre-synthesize, send exactly at time)
# ---------------------------------------------------------------------------

def _next_sched_id() -> int:
    global _sched_id
    with _scheduled_lock:
        _sched_id += 1
        return _sched_id


def _parse_at_time(time_str: str) -> Optional[datetime]:
    """Parse '7:30 AM', '19:30', '7pm' → next datetime (local)."""
    s = (time_str or "").strip().upper()
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", s)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            now = datetime.now()
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
    m = re.match(r"^(\d{1,2})\s*(AM|PM)$", s)
    if m:
        hour, ampm = int(m.group(1)), m.group(2)
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            now = datetime.now()
            dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
    return None


def _ensure_sched_watcher() -> None:
    global _sched_watcher_started
    with _scheduled_lock:
        if _sched_watcher_started:
            return
        _sched_watcher_started = True
    t = threading.Thread(target=_sched_watch_loop, name="gama-tg-voice-sched", daemon=True)
    t.start()
    logger.info("Telegram scheduled-voice watcher started.")


def _sched_watch_loop() -> None:
    while True:
        try:
            now = datetime.now()
            due: List[Dict[str, Any]] = []
            with _scheduled_lock:
                for item in _scheduled_voices:
                    if item.get("done"):
                        continue
                    fire_at = item.get("fire_at")
                    if fire_at and fire_at <= now:
                        item["done"] = True
                        due.append(dict(item))
            for item in due:
                path = Path(item.get("audio_path") or "")
                chat = str(item.get("chat_id") or _get_chat_id())
                caption = str(item.get("caption") or "")
                result = _send_voice_file(chat, path, caption=caption, delete_after=True)
                logger.info(
                    "[telegram] scheduled voice id=%s result=%s",
                    item.get("id"),
                    result[:80],
                )
            # prune done
            with _scheduled_lock:
                _scheduled_voices[:] = [x for x in _scheduled_voices if not x.get("done")]
        except Exception as exc:
            logger.error("scheduled voice watcher error: %s", exc)
        time.sleep(1.0)  # 1s resolution for on-time delivery


def _schedule_voice_note(
    text: str,
    *,
    chat_id: str = "",
    at: str = "",
    in_minutes: float = 0,
    caption: str = "",
    voice_name: str = "",
) -> str:
    """Pre-synthesize Live audio now; upload exactly at the target time."""
    chat = (chat_id or _get_chat_id()).strip()
    if not chat:
        return "No Telegram chat_id configured. Run setup first."
    text = (text or "").strip()
    if not text:
        return "What should the scheduled voice message say?"

    fire_at: Optional[datetime] = None
    if at:
        fire_at = _parse_at_time(str(at))
        if fire_at is None:
            return f"Couldn't parse time '{at}'. Try '7:30 AM' or '19:30'."
    else:
        try:
            mins = float(in_minutes or 0)
        except Exception:
            mins = 0
        if mins <= 0:
            return "Provide at='7:00 PM' or in_minutes=N for a scheduled voice note."
        fire_at = datetime.now() + timedelta(minutes=mins)

    seconds_until = (fire_at - datetime.now()).total_seconds()
    if seconds_until < 2:
        return _send_voice(chat, text, caption=caption, voice_name=voice_name)

    # Pre-synthesize NOW so send at Y has no Live delay
    try:
        from voice.live_voice_note import synthesize_live_voice_note
        VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        audio_path = synthesize_live_voice_note(
            text,
            voice_name=voice_name or None,
            out_dir=VOICE_CACHE_DIR,
        )
    except Exception as exc:
        logger.warning("pre-synth scheduled voice failed: %s", exc)
        return f"Could not pre-synthesize voice note: {exc}"

    sid = _next_sched_id()
    with _scheduled_lock:
        _scheduled_voices.append({
            "id": sid,
            "fire_at": fire_at,
            "audio_path": str(audio_path),
            "chat_id": chat,
            "caption": caption,
            "text": text,
            "done": False,
        })
    _ensure_sched_watcher()

    h = int(seconds_until // 3600)
    m = int((seconds_until % 3600) // 60)
    s = int(seconds_until % 60)
    wait = f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")
    return (
        f"Scheduled Telegram voice note #{sid} for {fire_at.strftime('%I:%M %p on %b %d')} "
        f"(in {wait}). Audio is pre-synthesized and will send on time with no Live delay."
    )


def _list_scheduled_voices() -> str:
    with _scheduled_lock:
        pending = [x for x in _scheduled_voices if not x.get("done")]
    if not pending:
        return "No scheduled Telegram voice notes."
    lines = ["Scheduled Telegram voice notes:"]
    for x in pending:
        fire_at = x.get("fire_at")
        when = fire_at.strftime("%I:%M %p on %b %d") if fire_at else "?"
        preview = (x.get("text") or "")[:60]
        lines.append(f"  #{x.get('id')} at {when} — {preview}")
    return "\n".join(lines)


def _cancel_scheduled_voice(sid: int) -> str:
    try:
        sid = int(sid)
    except Exception:
        return "Provide a valid scheduled voice id."
    removed = None
    with _scheduled_lock:
        for x in _scheduled_voices:
            if x.get("id") == sid and not x.get("done"):
                x["done"] = True
                removed = x
                break
    if not removed:
        return f"No pending scheduled voice note with id {sid}."
    try:
        Path(removed.get("audio_path") or "").unlink(missing_ok=True)
    except Exception:
        pass
    return f"Cancelled scheduled Telegram voice note #{sid}."


def send_telegram_alert(message: str, kind: str = "alert", force: bool = False) -> bool:
    try:
        if not force and not _alerts_enabled():
            return False
        if not is_configured():
            return False

        msg = (message or "").strip()
        if not msg:
            return False

        if not force:
            now = time.monotonic()
            with _alert_lock:
                last = _alert_last.get(kind, 0.0)
                if now - last < _ALERT_COOLDOWN_S:
                    return False
                _alert_last[kind] = now

        clean = msg
        for prefix in ("[SYSTEM_ALERT]", "[SYSTEM]", "[PROACTIVE_SUGGESTION]"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):].strip()
        for cut in ("Briefly and casually", "State this in ONE short", "Do not acknowledge"):
            idx = clean.find(cut)
            if idx > 20:
                clean = clean[:idx].strip().rstrip(".")

        text = f"⚠ Gama alert\n{clean}"
        result = _send_message(_get_chat_id(), text)
        ok = "sent" in result.lower() and "fail" not in result.lower()
        if ok:
            logger.info(f"[telegram] alert sent kind={kind}: {clean[:80]}")
        return ok
    except Exception as exc:
        logger.debug(f"[telegram] send_telegram_alert failed: {exc}")
        return False


def telegram_sender(action: str = "status", **kwargs) -> str:
    """Telegram messenger tool."""
    action = (action or "status").lower().strip().replace("-", "_").replace(" ", "_")

    if action in ("setup", "configure", "link"):
        return _setup(
            kwargs.get("bot_token") or kwargs.get("token") or "",
            kwargs.get("chat_id") or kwargs.get("chatid") or "",
        )

    if action in ("send", "message", "msg", "send_message", "text"):
        chat = str(kwargs.get("chat_id") or kwargs.get("chatid") or _get_chat_id()).strip()
        msg = _resolve_content(is_voice=False, **kwargs)
        return _send_message(chat, msg)

    if action in ("send_voice", "voice", "voice_note", "voice_message", "send_voice_note"):
        chat = str(kwargs.get("chat_id") or kwargs.get("chatid") or _get_chat_id()).strip()
        msg = _resolve_content(is_voice=True, **kwargs)
        return _send_voice(
            chat,
            msg,
            caption=str(kwargs.get("caption") or ""),
            voice_name=str(kwargs.get("voice_name") or kwargs.get("voice") or ""),
        )

    if action in (
        "schedule_voice", "schedule_voice_note", "send_voice_at", "voice_at",
    ):
        chat = str(kwargs.get("chat_id") or kwargs.get("chatid") or _get_chat_id()).strip()
        msg = _resolve_content(is_voice=True, **kwargs)
        return _schedule_voice_note(
            msg,
            chat_id=chat,
            at=str(kwargs.get("at") or kwargs.get("time") or kwargs.get("when") or ""),
            in_minutes=kwargs.get("in_minutes") or kwargs.get("minutes") or 0,
            caption=str(kwargs.get("caption") or ""),
            voice_name=str(kwargs.get("voice_name") or kwargs.get("voice") or ""),
        )

    if action in ("list_scheduled", "list_voice_schedule"):
        return _list_scheduled_voices()

    if action in ("cancel_scheduled", "cancel_voice"):
        return _cancel_scheduled_voice(kwargs.get("id") or kwargs.get("schedule_id") or 0)

    if action in ("send_file", "send_document", "send_pdf", "upload", "file"):
        chat = str(kwargs.get("chat_id") or kwargs.get("chatid") or _get_chat_id()).strip()
        path = kwargs.get("path") or kwargs.get("file") or kwargs.get("file_path") or ""
        if not path:
            query = kwargs.get("query") or kwargs.get("name") or ""
            if query:
                try:
                    from actions.context_resolver import resolve_file_reference
                    res = resolve_file_reference(str(query))
                    if res and res.path and res.path.exists():
                        path = str(res.path)
                except Exception:
                    pass
        if not path:
            return "Which file should I send on Telegram? Provide path or name."
        return _send_document(chat, str(path), caption=kwargs.get("caption") or kwargs.get("message") or "")

    if action in ("test", "ping"):
        if not is_configured():
            return (
                "Telegram is not configured yet. Create a bot with @BotFather, "
                "message it, grab your chat_id from getUpdates, then say: "
                "setup telegram with token <token> and chat id <id>."
            )
        return _send_message(_get_chat_id(), "✓ Gama Telegram link is working.")

    if action in ("enable_alerts", "alerts_on", "enable"):
        if not is_configured():
            return "Configure Telegram first (setup with bot token + chat id)."
        cfg = _read_cfg()
        cfg[_CFG_ALERTS] = True
        _write_cfg(cfg)
        return (
            "Telegram critical alerts are ON. I'll push battery/network/"
            "high-priority system alerts to your Telegram chat."
        )

    if action in ("disable_alerts", "alerts_off", "disable"):
        cfg = _read_cfg()
        cfg[_CFG_ALERTS] = False
        _write_cfg(cfg)
        return "Telegram critical alerts are OFF."

    if action in ("status", "info"):
        token_ok = bool(_get_bot_token())
        chat = _get_chat_id()
        alerts = _alerts_enabled()
        with _scheduled_lock:
            n_sched = sum(1 for x in _scheduled_voices if not x.get("done"))
        if not token_ok and not chat:
            return (
                "Telegram is not set up. Steps:\n"
                "1) Message @BotFather → /newbot → copy the token\n"
                "2) Open your bot and send any message\n"
                "3) Visit https://api.telegram.org/bot<TOKEN>/getUpdates for chat.id\n"
                "4) Say: setup telegram with token <token> and chat id <id>"
            )
        parts = [
            f"token={'saved' if token_ok else 'missing'}",
            f"chat_id={chat or 'missing'}",
            f"alerts={'ON' if alerts else 'OFF'}",
            f"scheduled_voice={n_sched}",
        ]
        return "Telegram status: " + ", ".join(parts) + "."

    return (
        "Unknown Telegram action. Use: setup, send, send_voice, schedule_voice, "
        "list_scheduled, cancel_scheduled, send_file, test, status, "
        "enable_alerts, disable_alerts."
    )


def _setup(bot_token: str, chat_id: str) -> str:
    bot_token = (bot_token or "").strip()
    chat_id = str(chat_id or "").strip()

    if not bot_token and not chat_id:
        return (
            "Need both bot_token and chat_id. "
            "Create a bot via @BotFather, message it, then get chat_id from getUpdates."
        )
    if not bot_token:
        return "Please provide the bot token from @BotFather."
    if not chat_id:
        return (
            "Please provide your chat_id. After messaging the bot, open "
            "https://api.telegram.org/bot<TOKEN>/getUpdates and copy chat.id."
        )

    if ":" not in bot_token or len(bot_token) < 30:
        return "That doesn't look like a Telegram bot token (expected digits:letters)."

    from security.credential_store import set_secret, available as store_available

    if not store_available():
        return (
            "Secure credential store is unavailable (need pywin32 or cryptography). "
            "Token was NOT saved."
        )
    if not set_secret(_CRED_BOT_TOKEN, bot_token):
        return "Failed to save bot token in the secure store."

    try:
        cfg = _read_cfg()
        cfg[_CFG_CHAT_ID] = chat_id
        cfg.pop("telegram_bot_token", None)
        _write_cfg(cfg)
    except Exception as exc:
        logger.warning(f"telegram: could not update config JSON: {exc}")
        return f"Token saved, but failed to store chat_id: {exc}"

    try:
        me = _api_call("getMe", {})
        uname = me.get("username") or "bot"
        result = _send_message(chat_id, f"✓ Gama linked to @{uname}. Telegram is ready.")
        if "fail" in result.lower():
            return (
                f"Saved credentials for @{uname}, but test message failed: {result}. "
                "Make sure you have started a chat with the bot."
            )
        return (
            f"Telegram configured for @{uname} (chat {chat_id}). "
            "Say 'enable telegram alerts' if you want critical system alerts pushed here, "
            "or 'send a telegram saying …' anytime."
        )
    except Exception as exc:
        return (
            f"Credentials saved, but API check failed: {exc}. "
            "Double-check the token and that you've messaged the bot at least once."
        )


__all__ = ["telegram_sender", "send_telegram_alert", "is_configured"]
