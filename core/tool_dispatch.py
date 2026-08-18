"""
core/tool_dispatch.py — Tool Dispatch Layer (extracted from main.py, C3 refactor)
==================================================================================
Owns every action-module binding (lazy-imported) and the full tool-execution
pipeline: `_execute_tool` (module-level router used by tool declarations'
lambdas), `_register_tools` (populates `core.tool_registry`), and
`_execute_tool_impl` (the ConfidenceScorer-gated entry point called by
GamaAssistant on every Gemini/fast-intent tool call).

This used to live inline in `main.py` (~650 lines). Extracted so `main.py`
stays focused on session/audio/UI orchestration, and so tool wiring can be
read, tested, and modified independently.

Public surface used by main.py:
    - `set_active_assistant(assistant)` — call once GamaAssistant is constructed
    - `_execute_tool_impl(name, args)` — the dispatch entry point
    - all the individual action bindings below (open_app, web_search, ...) —
      re-exported for the few call sites in main.py that still call them
      directly outside the dispatch path (e.g. fast-intent shortcuts).
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from datetime import datetime

from utils.logger import get_logger
from utils.perf import PerfTimer, turn as _perf_turn, report as _perf_report

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Improvement #7 — read-only tool auto-parallel promotion
# ---------------------------------------------------------------------------
# Tools in this set are side-effect-free reads: they can safely run
# concurrently with each other in a GoalPlanner multi-step goal even when
# Gemini omits the parallel_safe flag.  Write/mutate tools are NOT listed
# here — they stay sequential by default.
_PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset({
    "display_stage", "d2_mode", "weather_action", "system_info", "system_status",
    "goal_tracker", "reminder",
    "weather_report",
    "web_search",
    "web_reader",
    "system_info",
    "notes",           # read-only note lookup
    "process_manager", # read-only process list
    "calendar_action", # read-only calendar queries (not create/delete)
    "edge_search",
    "class_schedule",
    "self_diagnostics",
    "recall_memory",
    "memory_search",
    "get_world_context",
    "desktop_context",
    "file_processor",
})


def _lazy_import(module_path: str, attr: str):
    """Return a callable proxy that imports `module_path` on first use."""
    box: dict = {}

    def _resolve():
        obj = box.get("obj")
        if obj is None:
            obj = getattr(importlib.import_module(module_path), attr)
            box["obj"] = obj
        return obj

    def _wrapper(*args, **kwargs):
        return _resolve()(*args, **kwargs)

    _wrapper.__name__ = attr
    return _wrapper


# Eager imports actually referenced inside this module's dispatch logic.
from memory import facade as memory_facade
from actions.calendar_action import calendar_action
from actions.desktop_notify import desktop_notify
from voice.soundscape import sound_action as _sound_action_handler
from voice.event_voice import event_voice_action as _event_voice_handler
from actions.desktop_context import desktop_context

open_app              = _lazy_import("actions.open_app", "open_app")
web_search            = _lazy_import("actions.web_search", "web_search")
edge_search           = _lazy_import("actions.edge_search", "edge_search")
class_schedule        = _lazy_import("actions.class_schedule", "class_schedule")
computer_settings     = _lazy_import("actions.computer_settings", "computer_settings")
edith_analyze_screen  = _lazy_import("actions.edith_vision", "edith_analyze_screen")
webcam_process        = _lazy_import("actions.screen_processor", "webcam_process")
screen_agent          = _lazy_import("actions.screen_agent", "screen_agent")
file_processor        = _lazy_import("actions.file_processor", "file_processor")
web_reader            = _lazy_import("actions.web_reader", "web_reader")
file_controller       = _lazy_import("actions.file_controller", "file_controller")
weather_action        = _lazy_import("actions.weather_report", "weather_action")
self_awareness         = _lazy_import("actions.self_awareness", "self_awareness")
reminder              = _lazy_import("actions.reminder", "reminder")
set_confirmation_code = _lazy_import("actions.confirmation", "set_confirmation_code")
notes                 = _lazy_import("actions.notes", "notes")
system_info           = _lazy_import("actions.system_info", "system_info")
get_system_status     = _lazy_import("actions.system_info", "get_system_status")
clipboard             = _lazy_import("actions.clipboard", "clipboard")
utilities             = _lazy_import("actions.utilities", "utilities")
display_stage         = _lazy_import("actions.display_stage", "display_stage")
canvas_visual         = _lazy_import("actions.canvas_visual", "canvas_visual")
email_sender          = _lazy_import("actions.email_sender", "email_sender")
telegram_sender       = _lazy_import("actions.telegram_sender", "telegram_sender")
process_manager       = _lazy_import("actions.process_manager", "process_manager")
startup_manager       = _lazy_import("actions.startup_manager", "startup_manager")
user_settings_action  = _lazy_import("actions.user_settings_action", "user_settings")
media_controller       = _lazy_import("actions.media_controller", "media_controller")
get_music_controller   = _lazy_import("music.controller", "MusicController")
browser_control       = _lazy_import("actions.browser_control", "browser_control")
keyboard_actions      = _lazy_import("actions.keyboard_actions", "keyboard_actions")
mouse_actions         = _lazy_import("actions.mouse_actions", "mouse_actions")
computer_agent        = _lazy_import("actions.computer_agent", "computer_agent")
ui_automation         = _lazy_import("actions.ui_automation", "ui_automation")
advanced_automation   = _lazy_import("actions.advanced_automation", "advanced_automation")
automation_run        = _lazy_import("automation.engine", "run_goal")
terminal_command      = _lazy_import("actions.terminal", "terminal_command")
knowledge_action      = _lazy_import("actions.knowledge_action", "knowledge_action")
goal_tracker          = _lazy_import("actions.goal_tracker", "goal_tracker")
protocol_engine        = _lazy_import("actions.protocol_engine", "protocol_engine")
file_find             = _lazy_import("actions.file_find", "file_find")
project_context_action = _lazy_import("memory.project_context", "project_context_action")


# Music Engine singleton — created on first use so startup isn't delayed.
_music_engine_controller = None

def _get_music_engine():
    global _music_engine_controller
    if _music_engine_controller is None:
        _music_engine_controller = get_music_controller()
    return _music_engine_controller


_ACTIVE_ASSISTANT = None  # set via set_active_assistant() once GamaAssistant is constructed; lets module-level
                           # tool dispatch (e.g. voice_profile enroll) gate mic forwarding


def get_active_assistant():
    """Return the live GamaAssistant instance, or None."""
    return _ACTIVE_ASSISTANT


def set_active_assistant(assistant) -> None:
    """Called once from main.py's main() after GamaAssistant is constructed.

    Replaces the old pattern of main.py reaching into this module's global
    directly (`tool_dispatch._ACTIVE_ASSISTANT = assistant`), which works but
    is fragile; a setter keeps the module's internal state changes explicit
    and in one place.
    """
    global _ACTIVE_ASSISTANT
    _ACTIVE_ASSISTANT = assistant


def _execute_tool(name: str, args: dict) -> str:
    """Route a tool call to the appropriate action module.

    NOTE: For functions that take `action` as their first positional arg,
    we must pop `action` out of args before passing **kwargs — otherwise
    Python raises "got multiple values for argument 'action'".
    """
    with PerfTimer(f"Tool:{name}"), PerfTimer("Tool"):
        from core.fast_intent import already_fast_routed
        cached = already_fast_routed(name, args)
        if cached is not None:
            log.info(f"Tool '{name}' already executed via fast intent router — skipping duplicate call.")
            return cached
        # Wrap with ExecutionQueue: result verification + retry on transient
        # failure + outcome recording in ConfidenceScorer + CircuitBreaker.
        from core.execution_queue import exec_queue
        eq_result = exec_queue.run(name, args, _execute_tool_impl)
        result = eq_result.result
        _update_working_memory(name, args, result)
        _record_tool_category(name)
        return result


def _record_tool_category(name: str) -> None:
    """Perf audit item #2: track which activity categories this session
    has actually touched, so a later Live reconnect can send Gemini a
    trimmed tool list instead of all 60+ schemas every time. Best-effort
    and silently skipped if there's no active assistant or the tool has
    no category mapping (untagged/ALWAYS tools don't need tracking)."""
    try:
        if _ACTIVE_ASSISTANT is None:
            return
        from core.tool_declarations import TOOL_CATEGORIES
        tags = TOOL_CATEGORIES.get(name)
        if tags:
            _ACTIVE_ASSISTANT._recent_tool_categories.update(tags)
    except Exception:
        log.debug(f"Tool-category tracking skipped for '{name}'", exc_info=True)


# Tool name -> (working-memory slot, arg key to read the value from).
# Deliberately conservative: only tools with an unambiguous "this is now
# the current X" meaning are listed. Extend this table as new tools are
# added — it never raises if an entry is missing.
_WORKING_MEMORY_SLOT_MAP: dict[str, tuple[str, str]] = {
    "open_app": ("app", "app_name"),
    "file_processor": ("file", "path"),
    # code_helper removed
    "web_search": ("goal", "query"),
    "knowledge_action": ("file", "path"),
}


def _update_working_memory(name: str, args: dict, result: str) -> None:
    """Best-effort: after a tool runs, record what it acted on so a later
    'summarize it' / 'email it' / 'delete it' can resolve the pronoun
    without asking the user to repeat themselves (spec section 2 & 3)."""
    try:
        if name == "music_engine":
            # Use the controller's own state rather than the raw command
            # text — "pause music" shouldn't overwrite the 'song' slot
            # with the literal string "pause music". This keeps 'song'
            # accurate across play/pause/resume/next/previous so a later
            # "pause it" / "what song is this" resolves correctly even if
            # it has to go through the LLM instead of the fast-intent path.
            try:
                state = _get_music_engine()._state.get()
                if state.last_query:
                    from context_engine import working_memory
                    working_memory.set_slot("song", state.last_query)
            except Exception:
                log.debug("Working memory update skipped for music_engine", exc_info=True)
            return
        mapping = _WORKING_MEMORY_SLOT_MAP.get(name)
        if not mapping:
            return
        slot, arg_key = mapping
        value = args.get(arg_key)
        if not value or (isinstance(result, str) and result.startswith("Tool failed")):
            return
        from context_engine import working_memory
        working_memory.set_slot(slot, str(value))
    except Exception:
        log.debug(f"Working memory update skipped for tool '{name}'", exc_info=True)


# ---------------------------------------------------------------------------
# Tool Registry — registers all handlers (replaces the 300-line if-elif chain)
# ---------------------------------------------------------------------------

def _kw(args: dict) -> dict:
    """Strip the 'action' key from args dict (used by action-based tools)."""
    return {k: v for k, v in args.items() if k != "action"}


def _act(args: dict, default: str = "") -> str:
    """Return the 'action' value from args, with a default."""
    return args.get("action", default)


def _live_vision_tool(args: dict) -> str:
    """Enable/disable continuous Live vision.

    HUD camera = browser getUserMedia (instant).
    Gemini frames = background OpenCV (not shown on display).
    """
    from vision.live_vision import live_vision_action
    action = (args or {}).get("action", "status")
    mode = (args or {}).get("mode", "camera")
    camera_index = int((args or {}).get("camera_index", 0) or 0)
    return live_vision_action(action, mode=mode, camera_index=camera_index)



def _register_tools() -> None:
    """
    Register every tool handler with the ToolRegistry.

    Called once at module-load time after all lazy imports are defined.
    Replaces the 300-line if-elif chain in the old _execute_tool_impl.
    Adding a new tool now requires only one register() call here — not
    an edit to a dispatch chain.
    """
    from core.tool_registry import tool_registry
    from core.confidence import ActionRisk as R

    # ── Search ────────────────────────────────────────────────────────────────

    _SEARCH_KWS = (
        "search", "google", "look up online", "look it up", "browse",
        "find online", "web search", "on the internet", "on the web",
        "search online", "search the web", "search for it", "search it",
    )
    _BROWSER_KWS = (
        "search", "google", "browse", "open edge", "edge search",
        "look up online", "look it up", "find online", "on the internet",
        "on the web", "search online", "search the web",
    )

    def _web_search_handler(args):
        last_text = (getattr(_ACTIVE_ASSISTANT, "_last_input_transcript", "") or "").lower()
        if not any(kw in last_text for kw in _SEARCH_KWS):
            return (
                "Web search is disabled by user preference. "
                "Answer using your own knowledge and training data instead. "
                "If the user specifically wants a web search they will say so explicitly."
            )
        return web_search(args.get("query", ""), args.get("mode", "search"),
                          args.get("items"), args.get("aspect", "specs"))

    def _edge_search_handler(args):
        last_text = (getattr(_ACTIVE_ASSISTANT, "_last_input_transcript", "") or "").lower()
        if not any(kw in last_text for kw in _BROWSER_KWS):
            return (
                "Browser search is disabled by user preference. "
                "Answer using your own knowledge and training data instead. "
                "If the user specifically wants to search in Edge they will say so explicitly."
            )
        return edge_search(args.get("query", ""), args.get("new_tab", True))

    # H5 FIX — Disambiguate search tools so Gemini picks deterministically:
    #   web_search  = silent background search, NO browser window opened.
    #                 Use for: "search for X", "look up Y", "find Z online"
    #                 where the user wants a result spoken back, not a tab opened.
    #   edge_search = opens the Edge browser and loads a search results page.
    #                 Use ONLY when user explicitly asks to "open Edge", "browse to",
    #                 "show me in the browser", or "open a new tab".
    #   RULE: Default to web_search. Only use edge_search when user asks to
    #         open a browser / tab explicitly.
    tool_registry.register("web_search", _web_search_handler, risk=R.SAFE,
                            description=(
                                "Silent background web search — returns a spoken result, "
                                "does NOT open any browser window. "
                                "Use for: 'search for X', 'look up Y', 'what is Z'. "
                                "PREFER THIS over edge_search unless user explicitly asks "
                                "to open a browser tab."
                            ),
                            category="search")
    tool_registry.register("edge_search", _edge_search_handler, risk=R.SAFE,
                            description=(
                                "Opens the Microsoft Edge browser and performs a visible search. "
                                "Use ONLY when user explicitly says 'open Edge', 'browse to', "
                                "'show me in the browser', or 'open a tab'. "
                                "Do NOT use for silent/spoken-result searches — use web_search instead."
                            ),
                            category="search")

    # ── App / process control ─────────────────────────────────────────────────

    tool_registry.register("open_app",
        lambda args: open_app(args.get("app_name", ""), new_window=bool(args.get("new_window", False))),
        risk=R.LOW, description="Launch a desktop application.", category="app_control")

    tool_registry.register("computer_agent",
        lambda args: computer_agent(_act(args, "execute"), **_kw(args)),
        risk=R.HIGH, description="Autonomous desktop agent.", category="automation")
    tool_registry.register("process_manager",
        lambda args: process_manager(_act(args, "list"), **_kw(args)),
        risk=R.MEDIUM, description="List/kill system processes.", category="system")
    tool_registry.register("startup_manager",
        lambda args: startup_manager(_act(args, "list"), **_kw(args)),
        risk=R.MEDIUM, description="Manage startup programs.", category="system")

    # ── System / settings ─────────────────────────────────────────────────────

    tool_registry.register("computer_settings",
        lambda args: computer_settings(_act(args), args.get("value", "")),
        risk=R.MEDIUM, description="Adjust system settings (volume, brightness, etc.)", category="system")
    tool_registry.register("system_status",
        lambda args: (lambda s: (
            f"CPU: {s.get('cpu_percent','?')}%, "
            f"RAM: {s.get('ram_percent','?')}%, "
            f"Uptime: {s.get('uptime','?')}"
        ))(get_system_status()),
        risk=R.SAFE, description="Quick system metrics.", category="system")
    tool_registry.register("system_info",
        lambda args: system_info(_act(args, "overview"), **_kw(args)),
        risk=R.SAFE, description="Detailed system information.", category="system")
    tool_registry.register("desktop_context",
        lambda args: desktop_context(_act(args, "status"), **_kw(args)),
        risk=R.SAFE, description="Query active desktop state.", category="system")

    # ── Automation / terminal ─────────────────────────────────────────────────

    tool_registry.register("automation_engine",
        lambda args: automation_run(args.get("goal", ""),
                                    confirmation_code=args.get("confirmation_code")),
        risk=R.HIGH, description="Goal-based automation engine.", category="automation")
    tool_registry.register("terminal_command",
        lambda args: terminal_command(_act(args, "run"), **_kw(args)),
        risk=R.HIGH, description="Run terminal commands.", category="automation")
    tool_registry.register("advanced_automation",
        lambda args: advanced_automation(_act(args, "quick_action"), **_kw(args)),
        risk=R.HIGH, description="Advanced window/UI automation.", category="automation")
    tool_registry.register("ui_automation",
        lambda args: ui_automation(_act(args, "list"), **_kw(args)),
        risk=R.MEDIUM, description="UI element inspection/interaction.", category="automation")
    tool_registry.register("keyboard_actions",
        lambda args: keyboard_actions(_act(args, "type"), **_kw(args)),
        risk=R.MEDIUM, description="Keyboard input actions.", category="automation")
    tool_registry.register("mouse_actions",
        lambda args: mouse_actions(_act(args, "click"), **_kw(args)),
        risk=R.MEDIUM, description="Mouse input actions.", category="automation")
    def _self_awareness_handler(args):
        act = (_act(args, "about") or "about").lower().strip()
        return self_awareness(act, **_kw(args))

    tool_registry.register("self_awareness", _self_awareness_handler,
        risk=R.HIGH, description="Introspect and edit Gama's own source code/project.",
        category="assistant")



    # ── Screen / camera ───────────────────────────────────────────────────────

    tool_registry.register("edith_screen_vision",
        lambda args: edith_analyze_screen(args.get("prompt", "What am I looking at?"), args.get("target_window_only", False)),
        risk=R.SAFE, description="E.D.I.T.H. Tactical Vision Engine & OCR screen analysis.", category="vision")
    tool_registry.register("live_vision",
        lambda args: _live_vision_tool(args),
        risk=R.SAFE, description="Continuous Gemini Live desktop/camera vision stream.", category="vision")
    tool_registry.register("webcam_process",
        lambda args: webcam_process(args.get("prompt", "What do you see?")),
        risk=R.SAFE, description="Describe webcam view.", category="vision")
    tool_registry.register("screen_agent",
        lambda args: screen_agent(_act(args, "visual_task"), **_kw(args)),
        risk=R.SAFE, description="Agent-driven screen analysis/action.", category="vision")

    # ── File management ───────────────────────────────────────────────────────

    tool_registry.register("file_processor",
        lambda args: file_processor(
            args.get("path", ""),
            args.get("action", "auto"),
            args.get("instruction", ""),
            show_on_nexus=bool(args.get("show_on_nexus") or args.get("on_nexus") or args.get("nexus")),
            max_chars=args.get("max_chars") or 120_000,
        ),
        risk=R.LOW, description="Read/process file content without opening windows.", category="files")
    tool_registry.register("web_reader",
        lambda args: web_reader(
            url=args.get("url") or args.get("link") or "",
            action=args.get("action", "read"),
            mode=args.get("mode", "main_content"),
            max_chars=args.get("max_chars") or 12_000,
            use_playwright=bool(args.get("use_playwright") or args.get("playwright")),
            show_on_nexus=bool(args.get("show_on_nexus") or args.get("on_nexus") or args.get("nexus")),
            title=args.get("title") or "",
        ),
        risk=R.LOW,
        description="Fetch and extract clean content from a weblink without opening a browser window.",
        category="search")
    tool_registry.register("file_controller",
        lambda args: file_controller(_act(args), **_kw(args)),
        risk=R.MEDIUM, description="Create/move/delete files and folders.", category="files")


    tool_registry.register("knowledge_action",
        lambda args: knowledge_action(_act(args), **_kw(args)),
        risk=R.SAFE, description="Search/index local knowledge base.", category="files")

    # ── Memory ────────────────────────────────────────────────────────────────
    # C2 (GAMA_ARCHITECTURE_AUDIT.md): save_memory and remember used to write
    # into two different backends (memory_manager JSON vs long_term.db) with
    # no coordination. Both now go through memory/facade.py's single funnel.

    def _save_memory_handler(args):
        memory_facade.set_preference(
            args.get("category", "notes"),
            args.get("key", "general"),
            args.get("value", ""),
        )
        return "Saved."

    tool_registry.register("save_memory", _save_memory_handler,
        risk=R.LOW, description="Persist a structured category/key/value in memory.", category="memory")
    def _remember_handler(args):
        # Bug fix: memory_facade.remember_fact() returns an int (the SQLite
        # row id) by design — it's a data-layer function, not a tool handler.
        # This lambda used to return that int straight through, but every
        # tool handler must return str (ToolEntry's Callable[..., str]
        # contract) since _execute_tool_impl does result.startswith(...) on
        # whatever comes back. That made 'remember' crash on every call.
        text = (
            args.get("text")
            or args.get("fact")
            or args.get("content")
            or ""
        )
        text = str(text).strip()
        if not text:
            return "Nothing to remember."
        project = args.get("project") or None
        memory_facade.remember_fact(
            text,
            project=project,
            temporary=bool(args.get("temporary", False)),
        )
        # Refresh project last_update_ts so activity_sentinel stays quiet
        # while progress is being recorded by the model.
        try:
            from memory.project_context import note_project_update, get_active_project
            active = get_active_project()
            if project or (
                active and active.get("name")
                and str(active["name"]).lower() in text.lower()
            ):
                note_project_update(text)
        except Exception:
            pass
        return "Got it — I'll remember that."

    tool_registry.register("remember", _remember_handler,
        risk=R.LOW, description="Store a natural-language fact (model-chosen, durable only).", category="memory")
    tool_registry.register("recall_memory",
        lambda args: memory_facade.recall(args.get("query", ""), project=args.get("project") or None),
        risk=R.SAFE, description="Recall a stored fact or preference.", category="memory")
    tool_registry.register("forget_memory",
        lambda args: memory_facade.forget_fact(args.get("query", ""), project=args.get("project") or None),
        risk=R.MEDIUM, description="Delete a stored fact.", category="memory")

    # ── Code / knowledge ──────────────────────────────────────────────────────



    # ── Communication ─────────────────────────────────────────────────────────

    tool_registry.register("email_sender",
        lambda args: email_sender(_act(args, "send"), **_kw(args)),
        risk=R.MEDIUM, description="Send / manage emails.", category="communication")

    tool_registry.register("telegram_sender",
        lambda args: telegram_sender(_act(args, "status"), **_kw(args)),
        risk=R.MEDIUM, description="Send Telegram messages / setup bot",
        category="communication")

    # ── Media / music ─────────────────────────────────────────────────────────
    # H5 FIX — Eliminate non-deterministic music tool selection.
    #   music_engine    = PREFERRED for ALL music commands (play, pause, skip,
    #                     volume, shuffle, repeat, what's playing, etc.)
    #                     It has its own intent parser and handles everything.
        #                     user asks specifically for "local files" or music_engine fails.
    #   media_controller = System transport API (pause/play any system media).
    #                     Only use for non-music media (e.g. pause a podcast app,
    #                     control system volume on non-music sources) or as fallback
    #                     when music_engine is unavailable.
    #   ROUTING RULE: Always call music_engine first for any music/audio request.

    tool_registry.register("music_engine",
        lambda args: _get_music_engine().handle(args.get("command", "")),
        risk=R.LOW,
        description=(
            "PREFERRED music tool — handles ALL music commands: play, pause, "
            "resume, skip, previous, stop, shuffle, repeat, volume, "
            "'what's playing', and natural-language queries like "
            "'play Believer by Imagine Dragons'. Always use this tool first "
            "for any music or audio playback request."
        ),
        category="media")

    tool_registry.register("media_controller",
        lambda args: media_controller(_act(args, "now_playing"), **_kw(args)),
        risk=R.LOW,
        description=(
            "System media transport control (Windows Media Session API). "
            "Use for non-music media sources (podcasts, videos) or to "
            "query/control the currently active system media player as a "
            "fallback. For music commands, use music_engine instead."
        ),
        category="media")
    tool_registry.register("browser_control",
        lambda args: browser_control(_act(args, "open"), **_kw(args)),
        risk=R.LOW, description="Control the browser via Playwright.", category="media")

    # ── Productivity ──────────────────────────────────────────────────────────

    tool_registry.register("calendar_action",
        lambda args: calendar_action(_act(args, "today"), **_kw(args)),
        risk=R.LOW, description="Read/write calendar events.", category="productivity")
    tool_registry.register("reminder",
        lambda args: reminder(_act(args, "set"), **_kw(args)),
        risk=R.LOW, description="Set/list/cancel reminders.", category="productivity")
    tool_registry.register("notes",
        lambda args: notes(_act(args, "create"), **_kw(args)),
        risk=R.LOW, description="Create/read/search notes.", category="productivity")
    tool_registry.register("goal_tracker",
        lambda args: goal_tracker(_act(args, "list"), **_kw(args)),
        risk=R.LOW, description="Track long-term goals.", category="productivity")
    tool_registry.register("protocol_engine",
        lambda args: protocol_engine(_act(args, "list"), **_kw(args)),
        risk=R.MEDIUM,
        description="Create/run/delete/list JARVIS-style numbered or named custom protocols ('execute protocol 17').",
        category="automation")
    tool_registry.register("class_schedule",
        lambda args: class_schedule(_act(args, "today"), **_kw(args)),
        risk=R.SAFE, description="Query the class timetable.", category="productivity")
    tool_registry.register("session_restore",
        lambda args: __import__("actions.session_restore", fromlist=["session_restore_action"]).session_restore_action(
            action=args.get("action", "load"),
            apps=args.get("apps") or None,
        ),
        risk=R.LOW, description="Save/restore open app sessions.", category="productivity")
    # ── Utilities ─────────────────────────────────────────────────────────────

    tool_registry.register("clipboard",
        lambda args: clipboard(_act(args, "read"), **_kw(args)),
        risk=R.LOW, description="Read/write clipboard and smart history.", category="utilities")
    tool_registry.register("file_find",
        lambda args: file_find(_act(args, "find"), **_kw(args)),
        risk=R.LOW, description="Find and open files by name/intent.", category="files")
    tool_registry.register("project_context",
        lambda args: project_context_action(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Set/clear active project and DND.", category="productivity")
    tool_registry.register("utilities",
        lambda args: utilities(_act(args, "joke"), **_kw(args)),
        risk=R.SAFE, description="Miscellaneous utilities (jokes, timers, etc.)", category="utilities")
    tool_registry.register("display_stage",
        lambda args: display_stage(_act(args, "show"), **_kw(args)),
        risk=R.SAFE,
        description=(
            "Gama Canvas: show weather/tasks/goals/reminders/system/timer/image/"
            "custom SVG, compose multi-panel views, move/resize, save/load layouts, or clear."
        ),
        category="utilities")
    tool_registry.register("d2_mode",
        lambda args: __import__("actions.d2_mode", fromlist=["d2_mode"]).d2_mode(
            **{**(args or {}), "action": (args or {}).get("action", "status")}
        ),
        risk=R.SAFE,
        description=(
            "D2 secondary card/orb interface (NOT Nexus, NOT H1). Only call when the user "
            "explicitly asks to switch to D2 / enter D2 / open D2 / D2 mode. "
            "Do NOT call for H1 or spatial workspace. Never activate D2 automatically. "
            "Actions: enter, exit, show_tasks, show_reminders, show_news, visualize_cpu, "
            "visualize_ram, clear, status."
        ),
        category="utilities")

    tool_registry.register("canvas_visual",
        lambda args: canvas_visual(_act(args, "generate"), **_kw(args)),
        risk=R.SAFE,
        description="Flash-Lite premium visual generator for complex JARVIS-style canvas HUDs.",
        category="utilities")
    tool_registry.register("weather_action",
        lambda args: weather_action(args.get("city", ""), args.get("forecast", False)),
        risk=R.SAFE, description="Current weather / forecast.", category="utilities")
    tool_registry.register("notification_manager",
        lambda args: _lazy_import("actions.notification_manager", "notification_manager")(
            _act(args, "status"), **_kw(args)
        ),
        risk=R.SAFE,
        description="Desktop / system notification controls (show, list, clear, on/off).",
        category="utilities")
    tool_registry.register("desktop_notify",
        lambda args: desktop_notify(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Desktop (OS) notifications.", category="utilities")
    tool_registry.register("sound_action",
        lambda args: _sound_action_handler(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Test/configure GAMA's alert & UI sounds.", category="utilities")
    tool_registry.register("event_voice",
        lambda args: _event_voice_handler(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Speak GAMA's own alert/success/failure events out loud.", category="utilities")

    # ── User settings (voice-configurable runtime toggles) ────────────────────
    # Handles: personality %, barge-in on/off, listening sensitivity,
    # proactive suggestions, wake greeting, voice verification.

    tool_registry.register("user_settings",
        lambda args: user_settings_action(_act(args, "status"), **_kw(args)),
        risk=R.LOW,
        description=(
            "Adjust Gama's runtime settings by voice. Actions: "
            "set_personality (trait + value 0–100%), "
            "barge_in (enabled true/false), "
            "listening_sensitivity (value 10–100%), "
            "increase_sensitivity, decrease_sensitivity, "
            "wake_greeting, voice_verification, status."
        ),
        category="assistant")

    # ── Security / assistant control ─────────────────────────────────────────

    def _set_voice_handler(args):
        if _ACTIVE_ASSISTANT is not None:
            return _ACTIVE_ASSISTANT.set_voice(args.get("voice", "male"))
        return "Voice switching unavailable."

    def _shutdown_handler(args):
        if _ACTIVE_ASSISTANT is not None:
            _ACTIVE_ASSISTANT.schedule_shutdown()
            return "GAMA is shutting down. Goodbye."
        return "Shutdown requested, but assistant instance is unavailable."

    def _credential_status_handler(args):
        from security.credential_store import available, list_secret_names, _backend
        names = list_secret_names()
        if not available():
            return ("Secure credential storage isn't available right now (no DPAPI or "
                    "encryption backend found) — API keys are still read from the plain "
                    "config file.")
        if not names:
            return "Secure credential storage is active, but nothing has been migrated into it yet."
        return (f"Secure credential storage is active (backend: {_backend}). "
                f"Stored securely: {', '.join(names)}. Values are never exposed here.")

    tool_registry.register("set_voice", _set_voice_handler,
        risk=R.LOW, description="Switch GAMA's TTS voice.", category="assistant")
    tool_registry.register("shutdown_assistant", _shutdown_handler,
        risk=R.HIGH, description="Shut down the GAMA process.", category="assistant")
    tool_registry.register("set_confirmation_code",
        lambda args: set_confirmation_code(args.get("code", "")),
        risk=R.MEDIUM, description="Set the security confirmation code.", category="assistant")
    tool_registry.register("credential_status", _credential_status_handler,
        risk=R.SAFE, description="Check secure credential storage status.", category="assistant")

    # ── Image generation ──────────────────────────────────────────────────────

    def _generate_image_handler(args):
        from actions.image_gen import generate_image as _gen_img
        from voice.speech_manager import Priority as _Prio

        def _speak_cb(text: str, *, kind: str = "ack") -> None:
            if _ACTIVE_ASSISTANT is not None:
                _ACTIVE_ASSISTANT._speak_exact(text, priority=_Prio.ACK, kind=kind)

        # Default: save + show on display stage. Open system viewer only if asked.
        open_file = bool(args.get("open_file") or args.get("open") or args.get("open_image"))
        show = args.get("show_on_canvas")
        if show is None:
            show = args.get("show_on_display")
        if show is None:
            show = True
        return _gen_img(
            prompt=args.get("prompt", ""),
            speak_fn=_speak_cb,
            width=int(args.get("width") or 1024),
            height=int(args.get("height") or 1024),
            open_file=open_file,
            show_on_canvas=bool(show),
        )

    tool_registry.register("generate_image", _generate_image_handler,
        risk=R.LOW, description="Generate an AI image, save it, and show it on the display stage.", category="media")



    # ── Task queue removed (managed by core execution queue) ─────────────────

    # ── Explicit wait/pause ──────────────────────────────────────────────────
    # Multi-step voice requests ("open Spotify, wait 2 seconds, then play
    # music") were being executed with no actual pause between tool calls —
    # Gemini had no tool that could block, so it just fired the next call
    # immediately. This gives it one: a real, synchronous time.sleep() that
    # blocks the turn for the requested duration before returning, so any
    # step that follows really does happen after the wait, not concurrently
    # with it. Capped at 5 minutes so a bad transcription can't hang forever.
    # Phase 2 plugin system — drop files in plugins/
    try:
        from core.plugin_loader import load_plugins
        load_plugins(register=True)
    except Exception as _plug_exc:
        log.warning(f"[plugins] load failed: {_plug_exc}")



    tool_registry.register("telegram_sender",
        lambda args: telegram_sender(_act(args, "status"), **_kw(args)),
        risk=R.MEDIUM, description="Send Telegram messages / setup bot",
        category="communication")

    # ── Media / music ─────────────────────────────────────────────────────────
    # H5 FIX — Eliminate non-deterministic music tool selection.
    #   music_engine    = PREFERRED for ALL music commands (play, pause, skip,
    #                     volume, shuffle, repeat, what's playing, etc.)
    #                     It has its own intent parser and handles everything.
        #                     user asks specifically for "local files" or music_engine fails.
    #   media_controller = System transport API (pause/play any system media).
    #                     Only use for non-music media (e.g. pause a podcast app,
    #                     control system volume on non-music sources) or as fallback
    #                     when music_engine is unavailable.
    #   ROUTING RULE: Always call music_engine first for any music/audio request.

    tool_registry.register("music_engine",
        lambda args: _get_music_engine().handle(args.get("command", "")),
        risk=R.LOW,
        description=(
            "PREFERRED music tool — handles ALL music commands: play, pause, "
            "resume, skip, previous, stop, shuffle, repeat, volume, "
            "'what's playing', and natural-language queries like "
            "'play Believer by Imagine Dragons'. Always use this tool first "
            "for any music or audio playback request."
        ),
        category="media")

    tool_registry.register("media_controller",
        lambda args: media_controller(_act(args, "now_playing"), **_kw(args)),
        risk=R.LOW,
        description=(
            "System media transport control (Windows Media Session API). "
            "Use for non-music media sources (podcasts, videos) or to "
            "query/control the currently active system media player as a "
            "fallback. For music commands, use music_engine instead."
        ),
        category="media")
    tool_registry.register("browser_control",
        lambda args: browser_control(_act(args, "open"), **_kw(args)),
        risk=R.LOW, description="Control the browser via Playwright.", category="media")

    # ── Productivity ──────────────────────────────────────────────────────────

    tool_registry.register("calendar_action",
        lambda args: calendar_action(_act(args, "today"), **_kw(args)),
        risk=R.LOW, description="Read/write calendar events.", category="productivity")
    tool_registry.register("reminder",
        lambda args: reminder(_act(args, "set"), **_kw(args)),
        risk=R.LOW, description="Set/list/cancel reminders.", category="productivity")
    tool_registry.register("notes",
        lambda args: notes(_act(args, "create"), **_kw(args)),
        risk=R.LOW, description="Create/read/search notes.", category="productivity")
    tool_registry.register("goal_tracker",
        lambda args: goal_tracker(_act(args, "list"), **_kw(args)),
        risk=R.LOW, description="Track long-term goals.", category="productivity")
    tool_registry.register("protocol_engine",
        lambda args: protocol_engine(_act(args, "list"), **_kw(args)),
        risk=R.MEDIUM,
        description="Create/run/delete/list JARVIS-style numbered or named custom protocols ('execute protocol 17').",
        category="automation")
    tool_registry.register("class_schedule",
        lambda args: class_schedule(_act(args, "today"), **_kw(args)),
        risk=R.SAFE, description="Query the class timetable.", category="productivity")
    tool_registry.register("session_restore",
        lambda args: __import__("actions.session_restore", fromlist=["session_restore_action"]).session_restore_action(
            action=args.get("action", "load"),
            apps=args.get("apps") or None,
        ),
        risk=R.LOW, description="Save/restore open app sessions.", category="productivity")
    # ── Utilities ─────────────────────────────────────────────────────────────

    tool_registry.register("clipboard",
        lambda args: clipboard(_act(args, "read"), **_kw(args)),
        risk=R.LOW, description="Read/write clipboard and smart history.", category="utilities")
    tool_registry.register("file_find",
        lambda args: file_find(_act(args, "find"), **_kw(args)),
        risk=R.LOW, description="Find and open files by name/intent.", category="files")
    tool_registry.register("project_context",
        lambda args: project_context_action(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Set/clear active project and DND.", category="productivity")
    tool_registry.register("utilities",
        lambda args: utilities(_act(args, "joke"), **_kw(args)),
        risk=R.SAFE, description="Miscellaneous utilities (jokes, timers, etc.)", category="utilities")
    tool_registry.register("display_stage",
        lambda args: display_stage(_act(args, "show"), **_kw(args)),
        risk=R.SAFE,
        description=(
            "Gama Canvas: show weather/tasks/goals/reminders/system/timer/image/"
            "custom SVG, compose multi-panel views, move/resize, save/load layouts, or clear."
        ),
        category="utilities")
    tool_registry.register("d2_mode",
        lambda args: __import__("actions.d2_mode", fromlist=["d2_mode"]).d2_mode(
            **{**(args or {}), "action": (args or {}).get("action", "status")}
        ),
        risk=R.SAFE,
        description=(
            "D2 secondary card/orb interface (NOT Nexus, NOT H1). Only call when the user "
            "explicitly asks to switch to D2 / enter D2 / open D2 / D2 mode. "
            "Do NOT call for H1 or spatial workspace. Never activate D2 automatically. "
            "Actions: enter, exit, show_tasks, show_reminders, show_news, visualize_cpu, "
            "visualize_ram, clear, status."
        ),
        category="utilities")

    tool_registry.register("canvas_visual",
        lambda args: canvas_visual(_act(args, "generate"), **_kw(args)),
        risk=R.SAFE,
        description="Flash-Lite premium visual generator for complex JARVIS-style canvas HUDs.",
        category="utilities")
    tool_registry.register("weather_action",
        lambda args: weather_action(args.get("city", ""), args.get("forecast", False)),
        risk=R.SAFE, description="Current weather / forecast.", category="utilities")
    tool_registry.register("notification_manager",
        lambda args: _lazy_import("actions.notification_manager", "notification_manager")(
            _act(args, "status"), **_kw(args)
        ),
        risk=R.SAFE,
        description="Desktop / system notification controls (show, list, clear, on/off).",
        category="utilities")
    tool_registry.register("desktop_notify",
        lambda args: desktop_notify(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Desktop (OS) notifications.", category="utilities")
    tool_registry.register("sound_action",
        lambda args: _sound_action_handler(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Test/configure GAMA's alert & UI sounds.", category="utilities")
    tool_registry.register("event_voice",
        lambda args: _event_voice_handler(_act(args, "status"), **_kw(args)),
        risk=R.LOW, description="Speak GAMA's own alert/success/failure events out loud.", category="utilities")

    # ── User settings (voice-configurable runtime toggles) ────────────────────
    # Handles: personality %, barge-in on/off, listening sensitivity,
    # proactive suggestions, wake greeting, voice verification.

    tool_registry.register("user_settings",
        lambda args: user_settings_action(_act(args, "status"), **_kw(args)),
        risk=R.LOW,
        description=(
            "Adjust Gama's runtime settings by voice. Actions: "
            "set_personality (trait + value 0–100%), "
            "barge_in (enabled true/false), "
            "listening_sensitivity (value 10–100%), "
            "increase_sensitivity, decrease_sensitivity, "
            "wake_greeting, voice_verification, status."
        ),
        category="assistant")

    # ── Security / assistant control ─────────────────────────────────────────

    def _set_voice_handler(args):
        if _ACTIVE_ASSISTANT is not None:
            return _ACTIVE_ASSISTANT.set_voice(args.get("voice", "male"))
        return "Voice switching unavailable."

    def _shutdown_handler(args):
        if _ACTIVE_ASSISTANT is not None:
            _ACTIVE_ASSISTANT.schedule_shutdown()
            return "GAMA is shutting down. Goodbye."
        return "Shutdown requested, but assistant instance is unavailable."

    def _credential_status_handler(args):
        from security.credential_store import available, list_secret_names, _backend
        names = list_secret_names()
        if not available():
            return ("Secure credential storage isn't available right now (no DPAPI or "
                    "encryption backend found) — API keys are still read from the plain "
                    "config file.")
        if not names:
            return "Secure credential storage is active, but nothing has been migrated into it yet."
        return (f"Secure credential storage is active (backend: {_backend}). "
                f"Stored securely: {', '.join(names)}. Values are never exposed here.")

    tool_registry.register("set_voice", _set_voice_handler,
        risk=R.LOW, description="Switch GAMA's TTS voice.", category="assistant")
    tool_registry.register("shutdown_assistant", _shutdown_handler,
        risk=R.HIGH, description="Shut down the GAMA process.", category="assistant")
    tool_registry.register("set_confirmation_code",
        lambda args: set_confirmation_code(args.get("code", "")),
        risk=R.MEDIUM, description="Set the security confirmation code.", category="assistant")
    tool_registry.register("credential_status", _credential_status_handler,
        risk=R.SAFE, description="Check secure credential storage status.", category="assistant")

    # ── Image generation ──────────────────────────────────────────────────────

    def _generate_image_handler(args):
        from actions.image_gen import generate_image as _gen_img
        from voice.speech_manager import Priority as _Prio

        def _speak_cb(text: str, *, kind: str = "ack") -> None:
            if _ACTIVE_ASSISTANT is not None:
                _ACTIVE_ASSISTANT._speak_exact(text, priority=_Prio.ACK, kind=kind)

        # Default: save + show on display stage. Open system viewer only if asked.
        open_file = bool(args.get("open_file") or args.get("open") or args.get("open_image"))
        show = args.get("show_on_canvas")
        if show is None:
            show = args.get("show_on_display")
        if show is None:
            show = True
        return _gen_img(
            prompt=args.get("prompt", ""),
            speak_fn=_speak_cb,
            width=int(args.get("width") or 1024),
            height=int(args.get("height") or 1024),
            open_file=open_file,
            show_on_canvas=bool(show),
        )

    tool_registry.register("generate_image", _generate_image_handler,
        risk=R.LOW, description="Generate an AI image, save it, and show it on the display stage.", category="media")



    # ── Task queue removed (managed by core execution queue) ─────────────────

    # (The explicit `wait` tool was removed — a time.sleep() tool could pin a
    # Live turn for up to 5 minutes and was never reachable from the filtered
    # declaration set anyway. Sequenced pauses should be composed by Gemini
    # across separate tool calls.)

    # Phase 2 plugin system — drop files in plugins/
    try:
        from core.plugin_loader import load_plugins
        load_plugins(register=True)
    except Exception as _plug_exc:
        log.warning(f"[plugins] load failed: {_plug_exc}")



    n = len(tool_registry.list_names())
    log.info(
        f"[ToolRegistry] {n} tools registered. "
        f"Health: {tool_registry.health_summary()}"
    )


_register_tools()


def _sanitize_tool_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    sanitized = {}
    for k, v in args.items():
        if isinstance(v, str):
            sanitized[k] = v.replace("\x00", "").strip()
        else:
            sanitized[k] = v
    return sanitized


def _execute_tool_impl(name: str, args: dict) -> str:
    """Dispatch a tool call via the ToolRegistry (O(1) lookup).

    Only DESTRUCTIVE actions may be blocked (confirmation code / voice).
    All other tools run immediately with zero blockage — Gemini Live
    decides via function calling + TOOL_DECLARATIONS.
    """
    args = _sanitize_tool_args(args)
    from core.tool_registry import tool_registry as _tr
    from core.confidence import ActionRisk, confidence_scorer

    entry = _tr.get_entry(name)
    risk  = entry.risk if entry else ActionRisk.LOW
    name_l = (name or "").lower().strip()

    # Explicit policy from actions.confirmation — only DESTRUCTIVE
    try:
        from actions.confirmation import requires_confirmation, verify_confirmation_code
    except Exception:
        requires_confirmation = lambda a: False
        def verify_confirmation_code(c): return "VERIFIED"

    preconfirmed = bool(
        args.get("confirmation_code")
        or args.get("confirmed")
        or args.get("user_confirmed")
    )

    # ONLY destructive / permanent actions require a confirmation code.
    if requires_confirmation(name_l) or risk == ActionRisk.DESTRUCTIVE:
        if not preconfirmed:
            return (
                f"CONFIRM_REQUIRED: '{name}' is permanent / destructive. "
                f"Provide the confirmation code (and verbal yes) to proceed."
            )
        try:
            code = str(args.get("confirmation_code") or "")
            v = verify_confirmation_code(code)
            if v != "VERIFIED":
                return v
        except Exception:
            if not args.get("confirmation_code"):
                return "CONFIRM_REQUIRED: confirmation code needed."

    # Everything else runs freely (no MEDIUM/HIGH confidence gates).
    result = _tr.dispatch(name, args)
    try:
        confidence_scorer.record_outcome(name, not str(result).startswith("Tool failed"))
    except Exception:
        pass
    return result


# Short spoken acknowledgments for operations expected to take longer than
# ~500ms — spoken immediately so GAMA never goes silently busy, per spec.