"""
core/fast_intent.py — Fast Intent Router
==========================================
Bypasses Gemini entirely for deterministic, low-security commands (spec
section 5: "Never use the LLM for deterministic commands").

How it fits the Gemini-Live-audio architecture
--------------------------------------------------------------------
Gama streams mic audio to Gemini Live, which does transcription +
reasoning + tool-selection server-side. There is no separate local STT
pipeline that feeds a classic text intent router.

Wake-word spotting uses a single offline Vosk model (wake_word/engines/
vosk_engine.py) that is grammar-locked to the wake phrase + interrupt
words. A second unconstrained Vosk recognizer was intentionally removed
(perf audit): it duplicated model load, RAM, and CPU while awake and
did not improve end-to-end latency enough to justify the cost.

Instead, match_fast_intent() is called on transcripts that already exist:

  1. Local / owner-verified transcripts (e.g. from the wake pipeline or
     any local ASR path) — preferred, lowest latency.
  2. Gemini's own input transcription on the receive path — safety net
     so deterministic settings (volume, brightness, interruption toggles)
     still execute if the local path missed them.

When a rule matches, we call the real tool via the same `_execute_tool`
dispatch used everywhere else, so confirmation codes and DESTRUCTIVE-tier
checks still apply. We only skip the LLM's reasoning/tool-selection
round-trip, never any security layer.

Gemini still receives the utterance and may try to call the same tool;
`already_fast_routed()` / mark_fast_routed() let `_execute_tool` skip
re-running the side effect and return the cached result so Gemini only
narrates a short confirmation.

Scope is intentionally conservative: LOW-security tools only
(open app, volume, brightness, search, weather, battery/system status,
clipboard read, media control, time, basic calculator, etc.). Ambiguous
requests are left to Gemini.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from utils.logger import get_logger
from utils.perf import timed

log = get_logger(__name__)


# ---------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------

@dataclass
class IntentRule:
    label: str
    pattern: "re.Pattern[str]"
    tool: str
    args_fn: Callable[["re.Match[str]"], dict]


def _open_app_args(m: "re.Match[str]") -> dict:
    name = m.group(1).strip()
    words = name.split()
    connector_words = {"and", "then", "while", "after", "before", "with", "mode"}
    file_indicators = {"pdf", "doc", "docx", "file", "document", "spreadsheet", "sheet", "notes", "presentation"}
    file_extensions = (".pdf", ".docx", ".doc", ".txt", ".csv", ".xlsx", ".xls", ".pptx", ".ppt", ".png", ".jpg", ".jpeg", ".py", ".json", ".zip")
    name_lower = name.lower()

    if any(name_lower.endswith(ext) for ext in file_extensions) or any(w.lower() in file_indicators for w in words):
        raise ValueError(f"'{name}' looks like a file or document request, not a bare app name")

    if len(words) > 4 or connector_words.intersection(words):
        raise ValueError(f"'{name}' looks like a multi-clause request, not a bare app name")
    return {"app_name": name}


def _word_to_num(text: str) -> int:
    """Convert small English number words to int. Returns -1 if unrecognised."""
    _MAP = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
        "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
        "fifty": 50, "sixty": 60, "ninety": 90,
    }
    t = text.strip().lower()
    if t.isdigit():
        return int(t)
    parts = t.split()
    total = 0
    for p in parts:
        if p in _MAP:
            total += _MAP[p]
    return total if total > 0 else -1


def _reminder_args(m: "re.Match[str]") -> dict:
    """Extract {message, in_minutes} from a 'remind me' match."""
    raw_qty = m.group("qty") or ""
    raw_unit = (m.group("unit") or "minute").lower()
    raw_msg = (m.group("msg") or "").strip().strip(".,!?")
    if not raw_msg:
        raise ValueError("no message")
    qty = _word_to_num(raw_qty)
    if qty <= 0:
        raise ValueError("bad quantity")
    minutes = qty * 60 if raw_unit.startswith("h") else qty
    return {"action": "set", "message": raw_msg, "in_minutes": minutes}


def _timer_args(m: "re.Match[str]") -> dict:
    """Extract {minutes / seconds} from a 'set a timer' match.
    Accepts group names qty/unit OR tqty/tunit (two separate rule variants)."""
    raw_qty = (m.group("qty") if "qty" in m.groupdict() else m.group("tqty") or "").strip()
    raw_unit = (m.group("unit") if "unit" in m.groupdict() else m.group("tunit") or "minute").lower()
    qty = _word_to_num(raw_qty)
    if qty <= 0:
        raise ValueError("bad quantity")
    if raw_unit.startswith("s"):
        return {"action": "timer", "minutes": 0, "seconds": qty}
    minutes = qty * 60 if raw_unit.startswith("h") else qty
    return {"action": "timer", "minutes": minutes, "seconds": 0}


def _protocol_args(m: "re.Match[str]") -> dict:
    """Build args for an instant local 'execute/start/run protocol <id>'
    match. <id> can be digits ('17') or a small spoken number word
    ('seventeen') — both normalize to the same 'protocol 17' identifier
    inside actions/protocol_engine.py, so either phrasing hits the same
    stored routine. Anything else (a named protocol like 'alpha') passes
    through as-is.
    """
    raw = m.group(1).strip()
    num = _word_to_num(raw)
    identifier = str(num) if num > 0 else raw
    return {"action": "run", "identifier": identifier}




def _rules() -> list:
    _NUM = r"(?P<qty>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty|sixty)"
    _UNIT = r"(?P<unit>hours?|hrs?|minutes?|mins?|seconds?|secs?)"

    return [
        # ── Volume ────────────────────────────────────────────────────
        IntentRule(
            "volume_up",
            re.compile(r"\b(volume up|turn (the )?volume up|increase (the )?volume|louder)\b"),
            "computer_settings",
            lambda m: {"action": "volume_up"},
        ),
        IntentRule(
            "volume_down",
            re.compile(r"\b(volume down|turn (the )?volume down|decrease (the )?volume|lower (the )?volume|quieter)\b"),
            "computer_settings",
            lambda m: {"action": "volume_down"},
        ),
        IntentRule(
            "mute",
            re.compile(r"\b(mute|unmute)\b"),
            "computer_settings",
            lambda m: {"action": "mute"},
        ),
        # ── Brightness ────────────────────────────────────────────────
        IntentRule(
            "brightness_up",
            re.compile(r"\b(brightness up|increase (the )?brightness|brighter)\b"),
            "computer_settings",
            lambda m: {"action": "brightness_up"},
        ),
        IntentRule(
            "brightness_down",
            re.compile(r"\b(brightness down|decrease (the )?brightness|dimmer|dim (the )?screen)\b"),
            "computer_settings",
            lambda m: {"action": "brightness_down"},
        ),
        IntentRule(
            "brightness_set",
            re.compile(r"\bbrightness to (\d{1,3})\b"),
            "computer_settings",
            lambda m: {"action": "brightness", "value": m.group(1)},
        ),
        # ── Camera — explicit rule BEFORE generic open_app to prevent ─────
        # "open camera" from fuzzy-matching "explorer" or other aliases.
        IntentRule(
            "open_camera",
            re.compile(r"^(?:open|launch|start) (?:the )?(?:windows )?(?:camera|webcam)$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "camera"},
        ),

        # ── Protocols (JARVIS-style custom routines) ────────────────────
        # Instant local execution — no Gemini round-trip — for the
        # common "execute/start/run protocol <id>" phrasing. Must come
        # BEFORE the generic "open/launch/start <name>" catch-all below,
        # otherwise "start protocol 17" gets swallowed as an app-open
        # request (app_name="protocol 17") instead of matching here.
        # Creating, deleting, or listing protocols still goes through
        # Gemini calling the protocol_engine tool directly (see
        # core/tool_declarations.py), since those need the fuller
        # natural-language step description.
        IntentRule(
            "execute_protocol",
            re.compile(
                r"^(?:execute|start|run|activate|engage|initiate)\s+(?:protocol\s+)?([a-z0-9_\-\s]+)$",
                re.IGNORECASE,
            ),
            "protocol_engine",
            _protocol_args,
        ),

        # ── Cancel / stop running task ─────────────────────────────────
        # These are handled with higher priority than Gemini so the user
        # can immediately abort a long-running background task by barging
        # in and saying "cancel" without waiting for a Gemini round-trip.
        IntentRule(
            "cancel_task",
            re.compile(
                r"^(?:cancel|stop|abort)(?: (?:that|this|it|the task|current task|running task|everything|all))?$",
                re.IGNORECASE,
            ),
            "_direct_task_cancel",   # handled specially in _on_fast_intent_text
            lambda m: {},
        ),
        IntentRule(
            "stop_that",
            re.compile(r"^(?:stop that|cancel that|abort that|stop it now|cancel it|abort it)$", re.IGNORECASE),
            "_direct_task_cancel",
            lambda m: {},
        ),

        # ── Create folder ──────────────────────────────────────────────
        # Offline-safe: maps spoken folder-creation commands to the
        # file_controller tool so they work even without Gemini/internet.
        IntentRule(
            "create_folder",
            re.compile(
                r"^(?:create|make|new) (?:a )?folder (?:called |named )?['\"]?([a-z0-9][a-z0-9 _-]{0,60}?)['\"]?$",
                re.IGNORECASE,
            ),
            "file_controller",
            lambda m: {"action": "create_folder", "path": m.group(1).strip()},
        ),

        # ── Index folder ──────────────────────────────────────────────
        IntentRule(
            "index_folder",
            re.compile(
                r"^(?:index|reindex|refresh index)(?: (?:the|my))?(?: folder)?(?: (?:called|named))?\s+([a-z0-9 _-]{1,60}?)(?:\s+(?:folder|directory))?$",
                re.IGNORECASE,
            ),
            "knowledge_action",
            lambda m: {
                "action": "reindex" if "reindex" in m.group(0).lower() else "index_now",
                "folders": [m.group(1).strip()],
            },
        ),

        # ── Open file / document ──────────────────────────────────────
        IntentRule(
            "open_file_ext",
            re.compile(
                r"^(?:open|launch|read|view|show) (?:the |my )?(.+?\.(?:pdf|docx?|txt|csv|xlsx?|pptx?|png|jpe?g|py|json|zip|mp3|mp4|html?))$",
                re.IGNORECASE,
            ),
            "knowledge_action",
            lambda m: {"action": "open", "path": m.group(1).strip()},
        ),
        IntentRule(
            "open_file_keyword",
            re.compile(
                r"^(?:open|launch|read|view|show) (?:the |my )?(.+?\s+(?:pdf|doc|docx|file|document|spreadsheet|notes|image|photo))$",
                re.IGNORECASE,
            ),
            "knowledge_action",
            lambda m: {"action": "open", "path": m.group(1).strip()},
        ),

        # ── Open new window ───────────────────────────────────────────
        IntentRule(
            "open_new_window",
            re.compile(
                r"^(?:open|launch|start) (?:a )?new window (?:of |for )?(?:the )?([a-z0-9][a-z0-9 ]{0,30}?)$|"
                r"^(?:open|launch|start) (?:the )?([a-z0-9][a-z0-9 ]{0,30}?) (?:in )?(?:a )?new window$",
                re.IGNORECASE,
            ),
            "open_app",
            lambda m: {"app_name": (m.group(1) or m.group(2)).strip(), "new_window": True},
        ),
        # ── Open app ──────────────────────────────────────────────────
        IntentRule(
            "open_app",
            re.compile(r"^(?:open|launch|start) (?:the )?([a-z0-9][a-z0-9 ]{0,30}?)$"),
            "open_app",
            lambda m: _open_app_args(m),
        ),
        # ── JARVIS Diagnostics ────────────────────────────────────────
        IntentRule(
            "jarvis_diagnostics",
            re.compile(
                r"\b(?:run (?:a )?diagnostic|system diagnostic|jarvis status report|full diagnostic|run system check)\b",
                re.IGNORECASE,
            ),
            "system_info",
            lambda m: {"action": "stats"},
        ),
        # ── Window Snap / Layout ──────────────────────────────────────
        IntentRule(
            "snap_window_left",
            re.compile(r"\b(?:snap|move) (?:the )?window (?:to the )?left\b", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "snap_left"},
        ),
        IntentRule(
            "snap_window_right",
            re.compile(r"\b(?:snap|move) (?:the )?window (?:to the )?right\b", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "snap_right"},
        ),
        IntentRule(
            "maximize_window",
            re.compile(r"\b(?:maximize|max) (?:the )?window\b", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "maximize"},
        ),
        # ── Search / weather / battery / system ───────────────────────
        IntentRule(
            "search",
            re.compile(r"^(?:search(?: (?:for|google))?|google|look up|look for) (.+)$"),
            "edge_search",
            lambda m: {"query": m.group(1).strip()},
        ),
        IntentRule(
            "weather",
            re.compile(r"^(?:what'?s|what is|check) (?:the )?weather(?: (?:in|for) (.+))?$"),
            "weather_action",
            lambda m: {"city": (m.group(1) or "").strip()},
        ),
        IntentRule(
            "battery",
            re.compile(r"\bbattery (?:level|status|percentage)\b|\bhow(?:'s| is) (?:my |the )?battery\b"),
            "system_info",
            lambda m: {"action": "battery"},
        ),
        IntentRule(
            "get_time",
            re.compile(r"\b(?:what time is it|what'?s the time|tell me the time|current time)\b", re.IGNORECASE),
            "system_info",
            lambda m: {"action": "time"},
        ),
        IntentRule(
            "get_date",
            re.compile(r"\b(?:what'?s the date|what is the date|today'?s date|what day is it)\b", re.IGNORECASE),
            "system_info",
            lambda m: {"action": "time"},
        ),
        IntentRule(
            "system_status",
            re.compile(r"\b(system status|cpu usage|ram usage|system stats)\b"),
            "system_status",
            lambda m: {},
        ),
        IntentRule(
            "read_clipboard",
            re.compile(r"\b(read|what'?s on) (?:the |my )?clipboard\b"),
            "clipboard",
            lambda m: {"action": "read"},
        ),
        IntentRule(
            "clipboard_history",
            re.compile(r"\b(?:clipboard history|what did i copy|recent clipboard|show clipboard history)\b", re.I),
            "clipboard",
            lambda m: {"action": "history"},
        ),
        IntentRule(
            "clipboard_paste_nth",
            re.compile(
                r"\b(?:paste|use)\s+(?:the\s+)?"
                r"(?P<nth>first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|\d+)"
                r"(?:\s+(?:thing|item|entry|link|text))?(?:\s+i\s+copied)?\b",
                re.I,
            ),
            "clipboard",
            lambda m: {
                "action": "paste",
                "index": {
                    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
                    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
                }.get(m.group("nth").lower(), m.group("nth")),
            },
        ),
        IntentRule(
            "clipboard_paste_last",
            re.compile(r"\b(?:paste|use)\s+(?:what i copied|the last (?:thing|item) i copied|last clipboard)\b", re.I),
            "clipboard",
            lambda m: {"action": "paste", "index": 1},
        ),
        IntentRule(
            "what_time",
            re.compile(r"\b(?:what(?:'s| is) the time|what time is it|current time)\b", re.I),
            "system_info",
            lambda m: {"action": "time"},
        ),
        IntentRule(
            "system_status_fast",
            re.compile(r"\b(?:system status|cpu (?:and )?ram|how(?:'s| is) (?:my )?pc)\b", re.I),
            "system_status",
            lambda m: {},
        ),
        IntentRule(
            "screen_glance",
            re.compile(
                r"\b(?:what(?:'s| is) on (?:my )?screen|look at (?:my )?screen|"
                r"describe (?:my )?screen|glance at (?:the )?screen)\b",
                re.I,
            ),
            "screen_process",
            lambda m: {"action": "describe"},
        ),
        IntentRule(
            "dnd_on",
            re.compile(r"\b(?:don'?t disturb|do not disturb|leave me alone)\b", re.I),
            "project_context",
            lambda m: {"action": "dnd", "minutes": 90},
        ),
        IntentRule(
            "dnd_off",
            re.compile(r"\b(?:i(?:'m| am) (?:free|available)|clear do not disturb|dnd off)\b", re.I),
            "project_context",
            lambda m: {"action": "clear_dnd"},
        ),
        IntentRule(
            "set_project",
            re.compile(
                r"\b(?:i(?:'m| am) working on|set (?:active )?project(?: to)?|switch to project|my project is)\s+(.+)$",
                re.I,
            ),
            "project_context",
            lambda m: {"action": "set", "name": m.group(1).strip()},
        ),
        IntentRule(
            "open_file_named",
            re.compile(
                r"\b(?:open|find and open)\s+(?:the\s+)?(?P<q>.+?\.(?:pdf|docx?|xlsx?|csv|png|jpe?g|txt|md|py))\b",
                re.I,
            ),
            "file_find",
            lambda m: {"action": "open", "query": m.group("q").strip()},
        ),
        IntentRule(
            "find_file",
            re.compile(r"\b(?:find|locate|search for)\s+(?:the\s+)?(?:file\s+)?(?P<q>.{3,80})$", re.I),
            "file_find",
            lambda m: {"action": "find", "query": m.group("q").strip()},
        ),
        IntentRule(
            "lock_pc",
            re.compile(r"^lock (?:the )?(?:pc|computer|screen)$"),
            "computer_settings",
            lambda m: {"action": "lock"},
        ),
        # ── Screenshot ────────────────────────────────────────────────
        IntentRule(
            "screenshot",
            re.compile(r"\b(take (?:a )?screenshot|screenshot|capture (?:the )?screen)\b"),
            "computer_settings",
            lambda m: {"action": "screenshot"},
        ),
        # ── Music Engine — natural-language music commands ───────────────
        # Routes "play Believer", "pause music", "next song", "volume up",
        # "what's playing", etc. straight to the new music_engine tool.
        # The music engine has its own intent parser, so we just pass the
        # matched command text through.
        #
        # "current"/"this"/"my" are accepted qualifiers on pause/resume/
        # stop/restart (e.g. "pause current song") and a compound
        # "stop/pause ... and play X" is recognized too — both used to
        # fall through every rule here (only bare "the music/song/
        # playback" matched), which sent them all the way to the cloud
        # LLM: slow, and the LLM would treat "current song" as a literal
        # track title to search for instead of a transport command.
        IntentRule(
            "music_command",
            re.compile(
                r"^(?P<command>(?:play|spotify)\s+(?:this|that|it)(?:\s+(?:song|track|music))?|"
                r"(?:stop|pause)\b.*?\band\s+play\s+.{2,120}|"
                r"(?:play|spotify)\s+.{2,120}|"
                r"pause(?:\s+(?:the|current|this|my)?\s*(?:music|song|track|playback))?|"
                r"resume(?:\s+(?:the|current|this|my)?\s*(?:music|song|track|playback))?|"
                r"resume\s+it|continue\s+it|play\s+it|pause\s+it|stop\s+it|"
                r"continue(?:\s+playing)?|unpause|"
                r"(?:next|skip)(?:\s+(?:song|track|this|current))?|"
                r"(?:previous|prev|go\s+back|last\s+song)|"
                r"stop(?:\s+(?:the|current|this|my)?\s*(?:music|song|track|playback))?|"
                r"restart(?:\s+(?:the|current|this|my)?\s*(?:music|song|track|playback))?|"
                r"(?:volume\s+(?:up|down)|turn\s+(?:the\s+)?volume\s+(?:up|down)|louder|quieter)|"
                r"mute|unmute|"
                r"what(?:'s|\s+is)\s+playing(?:\s+now)?|"
                r"shuffle|"
                r"repeat(?:\s+(?:this\s+)?(?:song|track|all|queue|off))?)$",
                re.IGNORECASE,
            ),
            "music_engine",
            lambda m: {"command": m.group("command")},
        ),
        # ── Reminder ──────────────────────────────────────────────────
        # "remind me in 5 minutes to drink water"
        # "set a reminder in 10 minutes for take medicine"
        IntentRule(
            "reminder_set",
            re.compile(
                r"(?:remind me|set (?:a )?reminder)\s+"
                r"(?:in\s+)?" + _NUM + r"\s*" + _UNIT +
                r"\s*(?:to|for|about)?\s*(?P<msg>.{3,80})",
                re.IGNORECASE,
            ),
            "reminder",
            _reminder_args,
        ),
        # ── Timer ─────────────────────────────────────────────────────
        # Pattern A: "10 minute timer" / "set a 10 minute timer"
        IntentRule(
            "timer_set",
            re.compile(
                r"(?:(?:set|start)\s+(?:a\s+)?)?" + _NUM + r"\s*" + _UNIT + r"\s*timer",
                re.IGNORECASE,
            ),
            "reminder",
            _timer_args,
        ),
        # Pattern B: "timer for 5 minutes" / "start a timer for 5 minutes"
        # Uses different named groups (tqty/tunit) to avoid redefinition error.
        IntentRule(
            "timer_set_b",
            re.compile(
                r"(?:(?:set|start)\s+(?:a\s+)?)?timer\s+for\s+"
                r"(?P<tqty>\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
                r"eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty|sixty)"
                r"\s*(?P<tunit>hours?|hrs?|minutes?|mins?|seconds?|secs?)",
                re.IGNORECASE,
            ),
            "reminder",
            _timer_args,
        ),
        # ── Class schedule ────────────────────────────────────────────
        # "do I have class today" / "what's my schedule" / "any class today"
        IntentRule(
            "class_today",
            re.compile(
                r"\b(?:do i have|is there|any|what'?s my|what is my|show me my|what are my)\s+"
                r"(?:class(?:es)?|schedule|classes today)\b"
                r"|(?:class(?:es)? today|today'?s (?:class(?:es)?|schedule))\b",
                re.IGNORECASE,
            ),
            "class_schedule",
            lambda m: {"action": "today"},
        ),
        # "what's my next class" / "next class"
        IntentRule(
            "class_next",
            re.compile(
                r"\b(?:next class|what(?:'?s| is) (?:my )?next class|when is (?:my )?next class)\b",
                re.IGNORECASE,
            ),
            "class_schedule",
            lambda m: {"action": "next"},
        ),
        # ── Show desktop / minimize all ───────────────────────────────

        # ── Display stage (HUD presence panels) ───────────────────────
        IntentRule(
            "display_close",
            re.compile(
                r"^(?:close|hide|dismiss|clear)\s+(?:the\s+)?(?:display|screen|panel|it|that)\s*$"
                r"|^(?:go\s+back(?:\s+to\s+(?:the\s+)?(?:orb|standby))?|return\s+to\s+(?:the\s+)?(?:orb|standby)|exit\s+display)\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "close"},
        ),
        IntentRule(
            "display_reminders",
            re.compile(
                r"^(?:show|display|open)\s+(?:me\s+)?(?:my\s+)?reminders?(?:\s+on\s+(?:the\s+)?(?:display|screen))?\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "reminders"},
        ),
        IntentRule(
            "display_alerts",
            re.compile(
                r"^(?:show|display|open)\s+(?:me\s+)?(?:my\s+)?(?:alerts?|warnings?)(?:\s+on\s+(?:the\s+)?(?:display|screen))?\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "alerts"},
        ),
        IntentRule(
            "display_goals",
            re.compile(
                r"^(?:show|display|open)\s+(?:me\s+)?(?:my\s+)?goals?(?:\s+on\s+(?:the\s+)?(?:display|screen))?\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "goals"},
        ),
        IntentRule(
            "display_weather",
            re.compile(
                r"^(?:show|display|open|what(?:'?s| is))?\s*(?:me\s+)?(?:the\s+)?weather(?:\s+(?:in|for)\s+(?P<city>.+))?\s*$"
                r"|^what(?:'?s| is)\s+the\s+weather(?:\s+(?:in|for)\s+(?P<city2>.+))?\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "weather", "city": (m.groupdict().get("city") or m.groupdict().get("city2") or "") or ""},
        ),
        IntentRule(
            "display_forecast",
            re.compile(
                r"^(?:show|display|open)\s+(?:me\s+)?(?:the\s+)?(?:3[- ]?day\s+)?(?:weather\s+)?forecast(?:\s+(?:for|in)\s+(?P<city>.+))?\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "forecast", "city": (m.groupdict().get("city") or "")},
        ),
        IntentRule(
            "display_write",
            re.compile(
                r"^(?:write|put|show)\s+(?:this\s+)?(?:on\s+(?:the\s+)?(?:display|screen)\s*:?\s*)(?P<text>.+)$"
                r"|^(?:write|put)\s+(?P<text2>.+?)\s+on\s+(?:the\s+)?(?:display|screen)\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "write", "text": (m.groupdict().get("text") or m.groupdict().get("text2") or "").strip()},
        ),
        IntentRule(
            "display_tasks",
            re.compile(
                r"^(?:show|display|open)\s+(?:me\s+)?(?:my\s+)?(?:tasks?|task\s+queue|queue)(?:\s+on\s+(?:the\s+)?(?:display|screen))?\s*$",
                re.IGNORECASE,
            ),
            "display_stage",
            lambda m: {"action": "tasks"},
        ),

        IntentRule(
            "show_desktop",
            re.compile(r"^(?:show desktop|minimize all|minimize everything|show the desktop)$", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "show_desktop"},
        ),

        # ── Close / kill application ──────────────────────────────────
        IntentRule(
            "close_app",
            re.compile(r"^(?:close|quit|exit|kill) (?:the )?([a-z0-9][a-z0-9 ]{0,28}?)$", re.IGNORECASE),
            "process_manager",
            lambda m: {"action": "close_window", "name": m.group(1).strip()},
        ),
        IntentRule(
            "kill_process",
            re.compile(r"^(?:force kill|force close|terminate) (?:the )?([a-z0-9][a-z0-9 ]{0,28}?)$", re.IGNORECASE),
            "process_manager",
            lambda m: {"action": "kill", "name": m.group(1).strip()},
        ),

        # ── Open system folders ───────────────────────────────────────
        IntentRule(
            "open_downloads",
            re.compile(r"\b(?:open|go to|show) (?:my |the )?downloads?(?: folder)?\b", re.IGNORECASE),
            "file_controller",
            lambda m: {"action": "open_folder", "path": "downloads"},
        ),
        IntentRule(
            "open_desktop",
            re.compile(r"\b(?:open|go to|show) (?:the )?desktop\b", re.IGNORECASE),
            "file_controller",
            lambda m: {"action": "open_folder", "path": "desktop"},
        ),
        IntentRule(
            "open_documents",
            re.compile(r"\b(?:open|go to|show) (?:my |the )?documents?(?: folder)?\b", re.IGNORECASE),
            "file_controller",
            lambda m: {"action": "open_folder", "path": "documents"},
        ),

        # ── Open system tools ─────────────────────────────────────────
        IntentRule(
            "open_task_manager",
            re.compile(r"\b(?:open|launch|start) (?:the )?task manager\b", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "taskmgr"},
        ),
        IntentRule(
            "open_control_panel",
            re.compile(r"\b(?:open|launch) (?:the )?control panel\b", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "control panel"},
        ),
        IntentRule(
            "open_settings",
            re.compile(r"^(?:open|launch) (?:windows )?settings$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "settings"},
        ),
        IntentRule(
            "open_device_manager",
            re.compile(r"\b(?:open|launch) (?:the )?device manager\b", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "device manager"},
        ),
        IntentRule(
            "open_cmd",
            re.compile(r"^(?:open|launch|start) (?:a |the )?(?:command prompt|cmd|terminal)$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "cmd"},
        ),
        IntentRule(
            "open_powershell",
            re.compile(r"^(?:open|launch|start) (?:a |the )?powershell$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "powershell"},
        ),
        IntentRule(
            "open_notepad",
            re.compile(r"^(?:open|launch) notepad$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "notepad"},
        ),
        IntentRule(
            "open_calculator",
            re.compile(r"^(?:open|launch) (?:the )?calc(?:ulator)?$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "calculator"},
        ),
        IntentRule(
            "open_explorer",
            re.compile(r"^(?:open|launch) (?:file |windows )?explorer$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "explorer"},
        ),
        IntentRule(
            "open_paint",
            re.compile(r"^(?:open|launch) (?:ms )?paint$", re.IGNORECASE),
            "open_app",
            lambda m: {"app_name": "mspaint"},
        ),

        # ── Shutdown / Restart / Sleep PC ─────────────────────────────
        # These go through the security gate (DESTRUCTIVE tier) — fast
        # intent only skips the Gemini reasoning round-trip, not safety.
        IntentRule(
            "shutdown_pc",
            re.compile(r"^(?:shut ?down|power off)(?: (?:the )?(?:pc|computer|system))?$", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "shutdown"},
        ),
        IntentRule(
            "restart_pc",
            re.compile(r"^(?:restart|reboot)(?: (?:the )?(?:pc|computer|system))?$", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "restart"},
        ),
        # (Self-restart rule removed with the restart_self tool — routing
        # "restart yourself" to an unregistered tool returned "Unknown tool".
        # It now falls through to Gemini for a natural spoken reply.)
        IntentRule(
            "sleep_pc",
            re.compile(
                r"^(?:sleep|hibernate)(?: (?:the )?(?:pc|computer|system))?$"
                r"|^put (?:the )?(?:pc|computer|system) (?:to )?sleep$",
                re.IGNORECASE,
            ),
            "computer_settings",
            lambda m: {"action": "sleep_pc"},
        ),

        # ── Screen recorder ───────────────────────────────────────────
        IntentRule(
            "record_start",
            re.compile(r"\b(?:start|begin) (?:screen )?recording\b|^record (?:the )?screen$", re.IGNORECASE),
            "screen_recorder",
            lambda m: {"action": "start"},
        ),
        IntentRule(
            "record_stop",
            re.compile(r"\b(?:stop|end|finish) (?:screen )?recording\b", re.IGNORECASE),
            "screen_recorder",
            lambda m: {"action": "stop"},
        ),

        # ── Process listing ───────────────────────────────────────────
        IntentRule(
            "list_processes",
            re.compile(r"\b(?:list|show) (?:running )?processes\b|^what'?s running$", re.IGNORECASE),
            "process_manager",
            lambda m: {"action": "top"},
        ),

        # ── Empty recycle bin ─────────────────────────────────────────
        IntentRule(
            "empty_recycle_bin",
            re.compile(r"\b(?:empty|clear) (?:the )?recycle bin\b", re.IGNORECASE),
            "computer_settings",
            lambda m: {"action": "empty_recycle_bin"},
        ),

        # ── Clipboard ─────────────────────────────────────────────────
        IntentRule(
            "clipboard_clear",
            re.compile(r"\b(?:clear|empty|wipe) (?:the )?clipboard\b", re.IGNORECASE),
            "clipboard",
            lambda m: {"action": "clear"},
        ),

        # ── Notes ─────────────────────────────────────────────────────
        IntentRule(
            "notes_list",
            re.compile(r"^(?:show|list|read) (?:my )?notes?$", re.IGNORECASE),
            "notes",
            lambda m: {"action": "list"},
        ),

        # ── Barge-in / interruption toggle ────────────────────────────
        # "turn barge-in on/off", "turn interruption on/off",
        # "enable/disable interruption", "don't interrupt me", etc.
        IntentRule(
            "barge_in_enable",
            re.compile(
                r"^(?:turn|switch|enable|activate) (?:on )?(?:barge[- ]?in|interruption)(?: on)?$"
                r"|^(?:enable|activate) (?:barge[- ]?in|interruption)$"
                r"|^(?:allow|let me) interrupt(?: you)?$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {"action": "barge_in", "enabled": True},
        ),
        IntentRule(
            "barge_in_disable",
            re.compile(
                r"^(?:turn|switch|disable|deactivate) (?:off )?(?:barge[- ]?in|interruption)(?: off)?$"
                r"|^(?:turn|switch) (?:barge[- ]?in|interruption) off$"
                r"|^(?:disable|deactivate) (?:barge[- ]?in|interruption)$"
                r"|^(?:don'?t|do not) (?:listen|interrupt)(?: me)?(?: while (?:you(?:'re| are)? speaking))?$"
                r"|^(?:stop|no) interrupt(?:ing|ion)?(?: me)?$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {"action": "barge_in", "enabled": False},
        ),

        # ── Listening sensitivity ─────────────────────────────────────
        # "set listening sensitivity to 70%"
        # "increase / decrease listening sensitivity"
        IntentRule(
            "sensitivity_set",
            re.compile(
                r"^(?:set|change|adjust) (?:listening )?sensitivity(?: level)? to "
                r"(?P<pct>\d{1,3})\s*(?:percent|%)?$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {"action": "listening_sensitivity", "value": int(m.group("pct"))},
        ),
        IntentRule(
            "sensitivity_increase",
            re.compile(
                r"^(?:increase|raise|boost|turn up) (?:listening )?sensitivity(?:\s+level)?$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {"action": "increase_sensitivity"},
        ),
        IntentRule(
            "sensitivity_decrease",
            re.compile(
                r"^(?:decrease|lower|reduce|turn down) (?:listening )?sensitivity(?:\s+level)?$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {"action": "decrease_sensitivity"},
        ),

        # ── Personality percentage ─────────────────────────────────────
        # "set honesty to 80%"  /  "set humor to 60 percent"
        # "set talkativeness to 40"
        IntentRule(
            "personality_pct",
            re.compile(
                r"^(?:set|change|adjust) "
                r"(?P<trait>humor|honesty|talkativeness|professionality|professionalism)\s+"
                r"(?:to\s+)?(?P<pct>\d{1,3})\s*(?:percent|%)?$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {
                "action": "set_personality",
                "trait": m.group("trait").lower().replace("professionalism", "professionality"),
                "level": int(m.group("pct")),
            },
        ),
        # Legacy: "set honesty to high/medium/low"
        IntentRule(
            "personality_level",
            re.compile(
                r"^(?:set|change|adjust|make|turn) "
                r"(?P<trait>humor|honesty|talkativeness|professionality|professionalism)\s+"
                r"(?:to\s+)?(?P<level>low|medium|high|very high|very low|off|max|min)$",
                re.IGNORECASE,
            ),
            "user_settings",
            lambda m: {
                "action": "set_personality",
                "trait": m.group("trait").lower().replace("professionalism", "professionality"),
                "level": m.group("level").lower(),
            },
        ),
    ]


_RULES = _rules()


@timed("Intent")
def match_fast_intent(text: str) -> Optional[Tuple[str, dict, str]]:
    """Return (tool_name, args, matched_label) or None. <20ms budget."""
    text = re.sub(r"[.,!?]+$", "", (text or "").strip().lower())
    if not text:
        return None
    for rule in _RULES:
        m = rule.pattern.search(text)
        if m:
            try:
                args = rule.args_fn(m)
            except Exception:
                continue
            return rule.tool, args, rule.label
    return None


# ---------------------------------------------------------------------
# Dedup cache — prevents Gemini's own (redundant) tool call for the
# same command a moment later from double-executing a side effect.
# ---------------------------------------------------------------------

_DEDUP_TTL_SECONDS = 8.0
_recent: Dict[str, Tuple[float, str]] = {}


def _fingerprint(tool: str, args: dict) -> str:
    return tool + "|" + "|".join(f"{k}={args[k]}" for k in sorted(args))


def mark_fast_routed(tool: str, args: dict, result: str) -> None:
    _recent[_fingerprint(tool, args)] = (time.monotonic(), result)
    cutoff = time.monotonic() - _DEDUP_TTL_SECONDS
    for k in [k for k, (ts, _) in _recent.items() if ts < cutoff]:
        _recent.pop(k, None)


_FAILURE_MARKERS = (
    "couldn't find", "could not find", "tool failed", "failed to",
    "not found", "unable to", "unknown", "not supported",
)


def is_failure_result(result: Optional[str]) -> bool:
    """Best-effort check for whether a fast-routed tool's result string
    describes a failure rather than a success (e.g. open_app's "Couldn't
    find app or file '...'"). Used so a failed fast-intent attempt never
    gets cached/dedup'd — a genuine failure should let Gemini's own
    follow-up (e.g. falling back to a web search) actually run, instead
    of silently returning the same cached failure again."""
    if not result:
        return False
    low = result.lower()
    return any(marker in low for marker in _FAILURE_MARKERS)


def already_fast_routed(tool: str, args: dict) -> Optional[str]:
    """Returns the cached result string if this exact call was already
    fast-routed within the last few seconds, else None."""
    entry = _recent.get(_fingerprint(tool, args))
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > _DEDUP_TTL_SECONDS:
        _recent.pop(_fingerprint(tool, args), None)
        return None
    return result

