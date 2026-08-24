"""
core/live_session.py — Gemini Live session lifecycle — extracted from GamaAssistant (Phase 1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger
from core.text_sanitize import (
    _sanitize_spoken_text,
    _clean_transcript,
    _is_reasoning_echo,
    _explicit_reasoning_requested,
    _dedupe_repeated_sentences,
    _spoken_dedup_check_and_mark,
)
from core.tool_declarations import TOOL_DECLARATIONS, get_filtered_declarations

log = get_logger(__name__)

# Mirrored from main.py to avoid circular imports after Phase-1 extraction.
SEND_SAMPLE_RATE = 16000
LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
# Rolling JSONL for crash-safe session persistence (same path main.py
# replays on startup).
_SESSION_ROLLING_PATH = "memory/session_rolling.jsonl"

try:
    from core.interrupt_calibration import _WAKE_ACK_LINE
except Exception:
    _WAKE_ACK_LINE = "Yes, Sir?"


class LiveSessionController:
    """Gemini Live session lifecycle — extracted from GamaAssistant (Phase 1)."""

    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    def _build_config(self):
        """Build the LiveConnectConfig.

        Latency optimisation — context caching
        ----------------------------------------
        memory + desktop + fusion are the expensive parts (file I/O, LLM
        summarisation). They change at most every few minutes, so we rebuild
        them at most once every 5 minutes and reuse the cache for all
        reconnects in between. Only the [CURRENT DATE & TIME] header is
        regenerated fresh each call (it's a single datetime.now() — free).
        """
        asst = self._asst
        import time as _time
        from google.genai import types
        from datetime import datetime

        _CACHE_TTL = 300  # seconds between full context rebuilds

        # Latency-first: do NOT rebuild memory/desktop/fusion for the system
        # instruction. Those live behind on-demand tools (desktop_context,
        # recall_memory, …). Only cache the static system prompt text.
        now_mono = _time.monotonic()
        if (
            not hasattr(asst, "_ctx_cache_sysprompt")
            or (now_mono - getattr(asst, "_ctx_cache_ts", 0)) > _CACHE_TTL
        ):
            # NOTE: never `from main import …` here — when the app runs as
            # `python main.py` that re-executes main.py under a second module
            # name ("main" vs "__main__"), duplicating every module-level
            # side effect. The loader lives in core/prompt_loader.py.
            from core.prompt_loader import load_system_prompt
            asst._ctx_cache_sysprompt = load_system_prompt()
            asst._ctx_cache_fused = ""  # kept for compatibility; unused in prompt
            asst._ctx_cache_ts = now_mono
            log.debug("System prompt cache rebuilt (lean — no memory/desktop inject).")
        fused = ""

        try:
            from zoneinfo import ZoneInfo
            _now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            from datetime import timezone, timedelta
            _now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        time_str = _now_ist.strftime("%A, %B %d, %Y — %I:%M %p IST").replace(" 0", " ")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Always speak times in 12-hour IST format (e.g. '9:41 PM'), never 24-hour.\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Working Memory is rebuilt fresh every call (never cached) — it
        # tracks the current goal/task/file/project/etc. and the last few
        # things acted on, which change far more often than the 5-minute
        # memory/desktop context cache above. This is what lets "it" /
        # "that" / "them" resolve naturally turn to turn.
        try:
            from context_engine import working_memory
            wm_str = working_memory.as_prompt_block()
        except Exception as exc:
            log.debug(f"Working memory block skipped (non-fatal): {exc}")
            wm_str = ""

        # World Model active-goal/task block — also rebuilt fresh every
        # call (task status changes mid-turn). Lean by design: see
        # World Model.tasks_and_goal_prompt_block() docstring for why it
        # doesn't repeat the desktop_str/working_memory fields.
        try:
            from core.world_model import world as _world
            world_str = _world.tasks_and_goal_prompt_block()
        except Exception as exc:
            log.debug(f"World Model block skipped (non-fatal): {exc}")
            world_str = ""

        # Active long-horizon goals (goal_tracker) — so Gama can reference
        # and occasionally check in on projects/goals without being asked.
        goals_str = ""
        try:
            from actions.goal_tracker import goal_tracker as _gt
            _glist = _gt(action="list", status="active")
            if _glist and "no active" not in str(_glist).lower() and len(str(_glist).strip()) > 8:
                goals_str = "[ACTIVE GOALS / PROJECTS]\n" + str(_glist)[:900]
        except Exception as exc:
            log.debug(f"Goals block skipped (non-fatal): {exc}")

        # ── Latency-first context policy ────────────────────────────────────
        # Memory recall (recall_for_prompt), layered/episodic memory search,
        # and workflow-learner predictions were previously computed here on
        # every connect and then discarded by the block below. Removed: the
        # model reaches all of that through on-demand tools instead
        # (recall_memory / memory_search / desktop_context / get_world_context).

        # ── Phase 1: Health Monitor — surface degraded modules ───────────────
        # Lets Gama route around failures (e.g. TTS fallback, offline mode)
        # rather than silently trying a broken subsystem.
        health_str = ""
        try:
            from core.health_monitor import health_monitor as _hm
            _failed_mods = _hm.get_failed_modules()
            if _failed_mods:
                health_str = (
                    f"[HEALTH MONITOR] DEGRADED modules: {', '.join(_failed_mods)}. "
                    "Route around these — use available fallbacks."
                )
        except Exception:
            pass

        # ── C2 fix: total system-prompt character budget ──────────────────
        # Prevents unbounded growth of the assembled system_instruction
        # across long sessions. Without a cap, the layered-memory and fused
        # context blocks accumulate over time and will eventually exceed
        # Gemini Live's effective prompt window, degrading response quality.
        #
        # Strategy: fixed sections (time, working memory, world model, core
        # system prompt) are always included in full. The four variable
        # sections are trimmed in order of decreasing criticality:
        #   workflow_pred → layered_memory → query_memory → fused
        # so the least-essential context is cut first.
        #
        # Budget: 20 000 chars ≈ 5 000 tokens — well within Gemini's 1M
        # context window but large enough to hold a full session's context.
        # Ref: GAMA_ARCHITECTURE_AUDIT.md § Critical Issues C2
        # ── System-prompt size budget (latency-first) ─────────────────────────
        # Long memory + desktop summaries are NO LONGER stuffed into every
        # system instruction. The model must call on-demand tools:
        #   desktop_context, recall_memory, memory_search, get_world_context
        # Keep only: time + tiny working-memory + lean world/goals + core prompt.
        # Budget ~8k chars ≈ 2k tokens for fast first-token.
        _SYSPROMPT_CHAR_BUDGET = 8_000

        def _sysprompt_trim(s: str, limit: int) -> str:
            """Hard-truncate a prompt section to `limit` chars, appending ellipsis."""
            return s if len(s) <= limit else s[:limit].rstrip() + "…"

        # Cap optional blocks tightly
        if world_str:
            world_str = _sysprompt_trim(world_str, 600)
        if goals_str:
            goals_str = _sysprompt_trim(goals_str, 400)
        if wm_str:
            wm_str = _sysprompt_trim(wm_str, 800)

        _on_demand_hint = (
            "[CONTEXT TOOLS — USE ON DEMAND]\n"
            "Do NOT assume full memory or desktop state is in this prompt.\n"
            "When you need past facts, preferences, or long-term memory → call recall_memory or memory_search.\n"
            "When you need what is on screen / open apps / focused window → call desktop_context.\n"
            "When you need world/goal status beyond the tiny block above → call get_world_context.\n"
            "Keep answers grounded; call a tool rather than inventing context.\n"
        )

        _fixed_chars = (
            len(time_ctx)
            + len(wm_str)
            + len(world_str)
            + len(goals_str)
            + len(_on_demand_hint)
            + len(asst._ctx_cache_sysprompt)
            + len(health_str)
        )
        log.debug(
            f"[SysPrompt] lean budget={_SYSPROMPT_CHAR_BUDGET} fixed≈{_fixed_chars} "
            f"(memory/desktop moved to on-demand tools)"
        )

        parts = [time_ctx]
        if wm_str:
            parts.append(wm_str)
        if world_str:
            parts.append(world_str)
        if goals_str:
            parts.append(goals_str)
        if health_str:
            parts.append(health_str)
        parts.append(_on_demand_hint)
        parts.append(asst._ctx_cache_sysprompt)

        # Live user-tunable behavior directives — rebuilt every call so a
        # voice-issued settings change (user_settings tool) takes effect
        # on the very next turn, no restart needed.
        try:
            from state_engine import user_settings as _us
            from core import personality as _personality
            # Fixed floor (spec section 1: "dedicated personality layer
            # with persistent behavioral rules", never user-configurable)
            # first, then the user's adjustable humor/professionality/
            # honesty/talkativeness dials on top of it.
            _directives = [_personality.prompt_fragment(), _us.personality_prompt_fragment()]
            try:
                from memory.project_context import prompt_fragment as _proj_frag
                _pf = _proj_frag()
                if _pf:
                    _directives.append(_pf)
            except Exception:
                pass
            # Proactive suggestions feature removed — always answer-only.
            _directives.append(
                "ANSWER-ONLY POLICY: Answer exactly what Sir asked and stop. "
                "Do NOT add follow-up questions, unsolicited suggestions, "
                "'anything else?', or offers to do more, unless Sir explicitly "
                "asks for suggestions/options/what's next. "
                "Exception: brief project check-ins are allowed only when the "
                "system injects an activity check-in prompt."
            )
            # Fast mode: short pacing for quicker conversational turns.
            try:
                from utils.performance_mode import perf as _perf_mode
                _pace = _perf_mode.pacing_directive()
                if _pace:
                    _directives.append(_pace)
            except Exception:
                pass
            _directives.append(
                "PERSONAL COMPANION & LEARNING POLICY: You are Sir's dedicated, highly personal assistant. "
                "Continuously learn his working style, coding habits, preferred apps/tools, active projects, "
                "and daily routines. Whenever Sir shares personal preferences, opinions, project context, "
                "or constraints, silently call 'remember' or 'save_memory' in the background. "
                "Ground your answers in his remembered profile and keep responses personalized, concise, and natural."
            )

            _directives.append(
                "CONVERSATIONAL POLICY: Answer naturally and concisely. "
                "Never use chatbot filler ('Certainly!', 'I\\'d be happy to', 'How may I assist you?')."
            )
            try:
                from core.assistant_runtime import runtime as _rt_mode
                # Inject structured conversation state when available.
                _cs = _rt_mode.conversation.summary_block()
                if _cs:
                    _directives.append(_cs)
            except Exception:
                pass

            parts.append("\n".join(_directives))
        except Exception as exc:
            log.debug(f"user_settings prompt directives skipped (non-fatal): {exc}")

        # Gemini Live VAD — balanced 1-2s conversational turn-taking
        vad_config = None
        try:
            _td_kwargs = {
                "turn_detection_type": "SERVER_VAD",
                "silence_duration_ms": 250,
                "prefix_padding_ms": 20,
            }
            # Optional knobs — probe schema so unknown names never reach the wire.
            _td_fields = set()
            try:
                _mf = getattr(types.TurnDetectionConfig, "model_fields", None)
                if _mf is not None:
                    _td_fields = set(_mf.keys())
                else:
                    _td_fields = set(getattr(types.TurnDetectionConfig, "__annotations__", {}) or {})
            except Exception:
                pass
            for k, v in (
                ("eagerness", "HIGH"),
                ("start_of_speech_sensitivity", "HIGH"),
                ("end_of_speech_sensitivity", "HIGH"),
            ):
                if not _td_fields or k in _td_fields:
                    _td_kwargs[k] = v
            try:
                _td = types.TurnDetectionConfig(**_td_kwargs)
            except TypeError:
                # Strip unknown kwargs and retry with minimal set
                _td = types.TurnDetectionConfig(
                    turn_detection_type="SERVER_VAD",
                    silence_duration_ms=250,
                )
            _ri_kwargs = {"turn_detection": _td}
            # activity_handling only if RealtimeInputConfig accepts it
            try:
                _ri_fields = set()
                _mf = getattr(types.RealtimeInputConfig, "model_fields", None)
                if _mf is not None:
                    _ri_fields = set(_mf.keys())
                if not _ri_fields or "activity_handling" in _ri_fields:
                    _ri_kwargs["activity_handling"] = "START_OF_ACTIVITY_INTERRUPTS"
            except Exception:
                pass
            vad_config = types.RealtimeInputConfig(**_ri_kwargs)
        except Exception as _vad_exc:
            log.debug(f"RealtimeInputConfig VAD setup skipped: {_vad_exc}")

        cfg_kwargs = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[
                {"google_search": {}},
                {"function_declarations": asst._select_tool_declarations()},
            ],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=asst._voice_name,
                    )
                )
            ),
        )
        if vad_config is not None:
            cfg_kwargs["realtime_input_config"] = vad_config

        # Proactive Audio: allows the model to reject responding to out-of-context speech
        # or stay silent when the user is speaking to someone else / not directing requests to the assistant.
        try:
            cfg_kwargs["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        except Exception:
            cfg_kwargs["proactivity"] = {"proactive_audio": True}
        _advanced_keys: list[str] = []

        # Context window compression
        try:
            cfg_kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(target_tokens=10240),
            )
        except Exception:
            try:
                cfg_kwargs["context_window_compression"] = {
                    "sliding_window": {"target_tokens": 10240}
                }
            except Exception:
                log.debug("ContextWindowCompressionConfig unsupported — skipping.")

        # Session Resumption: restores prior session handle on reconnect
        try:
            _res_handle = getattr(asst, "_live_resumption_handle", None)
            if _res_handle:
                cfg_kwargs["session_resumption"] = types.SessionResumptionConfig(
                    handle=_res_handle
                )
        except Exception as _sr_exc:
            log.debug(f"SessionResumptionConfig setup skipped: {_sr_exc}")

        # Gemini 2.5 Flash Live uses thinking_budget (not thinking_level).
        # Zero disables thinking and minimizes latency for a voice assistant.
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            log.debug("ThinkingConfig(thinking_budget=0) unsupported — skipping.")

        # Build LiveConnectConfig.
        #
        # IMPORTANT: Proactive Audio and Affective Dialog are valid Live API
        # fields for Gemini 2.5 Flash Live when using v1beta. Do not disable
        # them merely because a different/older local google-genai schema does
        # not expose the fields: detect that SDK mismatch explicitly.
        _lc_fields = set()
        try:
            _mf = getattr(types.LiveConnectConfig, "model_fields", None)
            if _mf is not None:
                _lc_fields = set(_mf.keys())
            else:
                _lc_fields = set(
                    getattr(types.LiveConnectConfig, "__annotations__", {}) or {}
                )
        except Exception:
            pass

        if _lc_fields:
            _missing_advanced = [k for k in _advanced_keys if k not in _lc_fields]
            if _missing_advanced:
                raise RuntimeError(
                    "Installed google-genai SDK does not expose the Live API "
                    f"advanced-audio field(s): {', '.join(_missing_advanced)}. "
                    "Upgrade with: python -m pip install -U google-genai"
                )

        try:
            return types.LiveConnectConfig(**cfg_kwargs)
        except (TypeError, ValueError) as _cfg_exc:
            # If the SDK rejects the fields locally, this is an SDK/schema
            # mismatch, not evidence that Google's server feature is absent.
            # Retry only after removing fields that the local schema explicitly
            # does not know about. Never silently mark a supported feature as
            # permanently disabled.
            _unknown = []
            if _lc_fields:
                _unknown = [k for k in cfg_kwargs if k not in _lc_fields]
            if _unknown:
                for k in _unknown:
                    cfg_kwargs.pop(k, None)
                log.warning(
                    f"LiveConnectConfig removed locally-unknown field(s): {_unknown}. "
                    f"Original error: {_cfg_exc}"
                )
                return types.LiveConnectConfig(**cfg_kwargs)
            raise RuntimeError(f"LiveConnectConfig construction failed: {_cfg_exc}") from _cfg_exc


    def _select_tool_declarations(self) -> list[dict]:
        """Decide which tool schemas to send for this connect/reconnect.

        First connection: send only the compact everyday core set (~20
        tools). This avoids carrying the full schema payload through every
        initial voice turn.

        Reconnects: if the session has stayed within a small, focused set
        of activity categories so far (<=2), expand the schema list to
        those categories plus the core set. Otherwise stay on the core set
        (lean by default). Categories are expanded only when the user has
        actually used a capability — never send the full tool list by default.
        """
        asst = self._asst
        asst._connect_count += 1
        if asst._connect_count <= 1:
            initial = get_filtered_declarations()
            log.info(
                f"[ToolFilter] Initial connection: sending "
                f"{len(initial)}/{len(TOOL_DECLARATIONS)} core tool schemas."
            )
            return initial
        if 1 <= len(asst._recent_tool_categories) <= 2:
            filtered = get_filtered_declarations(asst._recent_tool_categories)
            log.info(
                f"[ToolFilter] Reconnect #{asst._connect_count}: sending "
                f"{len(filtered)}/{len(TOOL_DECLARATIONS)} tool schemas "
                f"(active categories: {sorted(asst._recent_tool_categories)})"
            )
            return filtered
        # Keep reconnects lean even before a category has been observed.
        return get_filtered_declarations()

    async def run(self):
        """Main loop — auto-reconnects on Gemini failure.

        Backend selection:
          Internet available + Gemini key  → Gemini Live
          Internet unavailable             → offline mode (fast-intent PC commands only)
          Gemini key missing entirely      → offline mode
          Gemini failure                   → reconnect with backoff
        """
        asst = self._asst
        try:
            import sounddevice as sd
        except ImportError as exc:
            log.error(f"Missing sounddevice: {exc}")
            asst.ui.write_log(f'<span style="color:#ff3355">ERROR: {exc}</span>')
            return

        asst._loop = asyncio.get_event_loop()
        asst._running = True
        asst._voice_name = asst._load_voice_preference()

        # ── Validate online config (independent of offline validation) ──────
        from core.config_manager import config as _cfg
        api_key = _cfg.gemini_key()
        gemini_available = bool(api_key)
        if not gemini_available:
            log.warning(
                "Gemini API key not found in config/api_keys.json — "
                "running in offline-only mode. PC commands and local AI still work."
            )
            asst.ui.write_log(
                '<span style="color:#FF9900">⚠️ Gemini API key not configured — '
                "offline mode only. Set 'gemini_api_key' in config/api_keys.json "
                "to enable Gemini Live.</span>"
            )

        # If offline-only (no Gemini key), keep the app alive so the
        # user can access the tray/window and configure their key.
        # The mic + local pipeline now run regardless of Gemini session
        # state (see _mic_task below), so wake word / local Whisper /
        # offline LLM / Piper TTS all still work here.
        if not gemini_available:
            asst._mic_task = asyncio.ensure_future(asst._listen_audio())
            asst.ui.set_state("OFFLINE")
            # Simple keep-alive — no tight loop, no repeated session calls.
            while asst._running:
                await asyncio.sleep(5.0)
            return

        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            log.error(f"google-genai not installed: {exc}")
            asst.ui.write_log(f'<span style="color:#ff3355">ERROR: {exc}</span>')
            return

        # CRITICAL: Gemini 2.5 Flash Live (proactivity + affective dialog)
        # requires api_version=v1beta. Using v1alpha caused intermittent
        # WebSocket 1011 Internal Error closes under tool-heavy turns.
        client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1alpha"},
        )
        # Expose client for Flash-Lite routing (_route_with_flash_lite).
        # The same client handles both the Live session (LIVE_MODEL) and
        # standard generate_content calls (ROUTING_MODEL).
        asst._genai_client = client

        # Start the mic capture task independently of the Gemini session
        # lifecycle. It used to be spawned only inside the `async with
        # client.aio.live.connect(...)` TaskGroup below, which meant the
        # mic never opened at all while offline (no internet at startup),
        # so wake-word detection / local Whisper / greetings never fired
        # until a Gemini connection succeeded. It now runs continuously;
        # the audio-forward-to-Gemini gate inside the callback checks
        # `asst.session is not None` so it safely no-ops while offline.
        asst._mic_task = asyncio.ensure_future(asst._listen_audio())

        # Startup voice greeting — fires once so the user knows Gama is alive.
        asyncio.ensure_future(asst._speak_startup_greeting())

        # Auto-reconnect loop — always connects to Gemini directly
        while asst._running:
            try:
                log.info("Connecting to Gemini Live...")
                asst.ui.set_state("THINKING")
                config = asst._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    asst.session = session
                    asst._loop = asyncio.get_event_loop()
                    # Wire continuous Live vision (desktop/camera → video frames)
                    try:
                        asst._setup_live_vision_sender()
                    except Exception as _lv_exc:
                        log.debug(f"live vision sender setup: {_lv_exc}")
                    asst.audio_in_queue = asyncio.Queue()
                    asst.out_queue = asyncio.Queue(maxsize=200)  # XLVII uses 200
                    asst._turn_done_event = asyncio.Event()
                    # Connection succeeded — reset exponential backoff counter.
                    try:
                        from core.session_manager import session_manager as _smgr
                        _smgr.reset()
                    except Exception:
                        pass
                    asst._stable_session_ts = time.monotonic()
                    # Brief quiet window so we do not immediately inject
                    # system text / wake-acks into a brand-new Live socket
                    # (a common 1011 pattern right after reconnect).
                    asst._session_quiet_until = time.monotonic() + 1.25

                    log.info("GAMA online.")
                    # Post-reconnect mic recovery: clear speaking/post-speech
                    # gates so the independent mic task immediately resumes
                    # forwarding PCM into the new out_queue. Without this,
                    # a 1011 reconnect often left Gama "awake" for text but
                    # deaf to the microphone until a full restart.
                    try:
                        asst._set_speaking(False)
                        asst._last_speaking_end_ts = 0.0
                        asst._wake_verifying = False
                        asst._announcing_while_asleep = False
                        asst._seen_audio_chunks = set()
                        asst._last_audio_chunk = None
                    except Exception:
                        pass
                    # During BOOT greeting keep a ready/listening UI — never
                    # flash SLEEPING right after connect (that felt like
                    # "went to sleep 1s after welcome").
                    if asst._awake or getattr(asst, "_boot_greeting_pending", False):
                        asst.ui.set_state("LISTENING")
                        asst.ui.write_log(
                            f'<span style="color:#00d4ff">GAMA online — listening.</span>'
                        )
                        if getattr(asst, "_connect_count", 1) > 1:
                            log.info("[Session] Reconnected while ACTIVE — mic forward re-armed.")
                        else:
                            log.info("[Session] First connect while ACTIVE — mic forward armed.")
                    else:
                        try:
                            from core.assistant_runtime import RuntimeMode
                            mode = asst._runtime.mode
                        except Exception:
                            mode = None
                        if mode is not None and str(mode.value) == "DEEP_SLEEP":
                            asst.ui.set_state("SLEEPING")
                            asst.ui.write_log(
                                f'<span style="color:#5ab8cc">GAMA in deep sleep — say "{asst._wake_cfg.wake_phrase}" to wake.</span>'
                            )
                        else:
                            asst.ui.set_state("IDLE")
                            asst.ui.write_log(
                                f'<span style="color:#5ab8cc">GAMA observing — say "{asst._wake_cfg.wake_phrase}" when you need me.</span>'
                            )

                    # Register callbacks for reminders/alarms/timers
                    from actions.reminder import set_speak_callback, set_wake_callback, set_ui_refresh_callback
                    set_speak_callback(asst._speak_via_session)
                    set_wake_callback(asst._wake_gama)
                    # Reminder set/cancel happens on whatever thread the
                    # action ran on (e.g. Gemini tool-call thread). Prefer
                    # hopping onto the Qt GUI thread via QTimer when a Qt
                    # UI is present; otherwise call the refresh (if any)
                    # directly so headless / web-only mode does not crash.
                    def _reminder_ui_refresh():
                        refresh = getattr(asst.ui, "_refresh_instant_dashboard", None)
                        if not callable(refresh):
                            return
                        try:
                            from PySide6.QtCore import QTimer as _QTimer_rem
                            _QTimer_rem.singleShot(0, refresh)
                        except Exception:
                            try:
                                refresh()
                            except Exception:
                                pass
                    set_ui_refresh_callback(_reminder_ui_refresh)

                    # Start the class-schedule reminder watcher (JEE prep
                    # timetable) — fires 10min/5min/start reminders on its
                    # own via the callbacks just registered above.
                    try:
                        # Phase 2 starts this watcher once the voice path is ready.
                        pass
                    except Exception as _cw_exc:
                        log.debug(f"class_schedule watcher skipped: {_cw_exc}")

                    # Long-horizon goal check-ins — idempotent, safe to
                    # call on every (re)connect. Generic watcher removed.
                    # Phase 2 starts the goal watcher once the voice path is ready.

                    # Daily briefing is disabled per speed-first spec: no
                    # unsolicited low-value status messages on startup.
                    # _send_briefing / _briefing_sent kept for compatibility
                    # but never triggered automatically.

                    # If this process just relaunched itself via
                    # restart_self, let Gama mention it's back (once).
                    if asst._just_restarted and not asst._restart_complete_announced:
                        tg.create_task(asst._send_restart_complete())
                        asst._restart_complete_announced = True

                    # Start system monitor
                    # Phase 2 starts system monitoring once the voice path is ready.

                    # Log audio device
                    try:
                        default_input = sd.default.device[0]
                        devices = sd.query_devices()
                        log.info(f"Audio input device #{default_input}: "
                                 f"{devices[default_input]['name'] if default_input is not None else 'None'}")
                    except Exception:
                        pass

                    # Launch the 4 audio tasks — exact same as Mark XXXIX-OR
                    tg.create_task(asst._send_realtime())
                    tg.create_task(asst._receive_audio())
                    tg.create_task(asst._play_audio())
                    tg.create_task(asst._system_stats_loop())

            except BaseException as exc:
                # Catch ExceptionGroup (TaskGroup) + regular exceptions.
                # ExceptionGroup inherits BaseException, not Exception, so a
                # plain `except Exception` silently let 1011 TaskGroup errors
                # escape and left the mic/session in a half-dead state.
                if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                    raise
                # ── GoAway (1008) — expected session-duration limit ────────
                # Gemini Live sessions have a maximum duration (~15 min).
                # When that limit is hit the server sends a GoAway signal and
                # closes with code 1008.  This is NOT an error — it is planned
                # behaviour.  Log at INFO, skip the error traceback, reset the
                # backoff counter so the next session starts fresh, and
                # reconnect after a minimal 0.5 s pause (no escalating delay).
                # Flatten ExceptionGroup messages so "1011" is visible.
                _parts = [str(exc)]
                if isinstance(exc, BaseExceptionGroup):
                    for _sub in exc.exceptions:
                        _parts.append(str(_sub))
                        if getattr(_sub, "__cause__", None):
                            _parts.append(str(_sub.__cause__))
                _exc_str = " | ".join(_parts).lower()
                _is_goaway = (
                    "1008" in _exc_str
                    or "goaway" in _exc_str
                    or "session durat" in _exc_str
                    or "policy violation" in _exc_str
                )
                # 1011 = server internal error / abrupt close — treat as
                # recoverable session death (same as GoAway): reconnect,
                # don't dump a full traceback every time.
                _is_1011 = (
                    "1011" in _exc_str
                    or "internal error" in _exc_str
                    or "connectionclosed" in _exc_str.replace(" ", "")
                )
                # 1007 during setup can mean the server rejected a setup field.
                # Proactive Audio + Affective Dialog are intentionally kept
                # enabled for Gemini 2.5 Flash Live. Do NOT permanently disable
                # them here; doing so masked the real configuration/API problem.
                _is_1007_setup = (
                    "1007" in _exc_str
                    and (
                        "proactivity" in _exc_str
                        or "affective" in _exc_str
                        or "unknown name" in _exc_str
                        or "cannot find field" in _exc_str
                    )
                )
                if _is_1007_setup:
                    log.error(
                        "[Session] Gemini rejected Live setup involving "
                        "Proactive Audio/Affective Dialog. Keeping the features "
                        "enabled for the next attempt. Full server error: %s",
                        exc,
                    )
                elif _is_goaway:
                    log.info(
                        f"[Session] Gemini GoAway received (session duration limit). "
                        "Reconnecting immediately…"
                    )
                elif _is_1011:
                    log.warning(
                        f"[Session] Live connection closed (1011/internal). "
                        f"Reconnecting… ({exc})"
                    )
                else:
                    log.error(f"Session error: {exc}")
                    log.error(traceback.format_exc())

            asst._set_speaking(False)
            asst._reflect_and_reset_session()
            asst.session = None
            # Drain audio queues so a dead session doesn't keep feeding
            # send_realtime / play loops with stale PCM (stops 1011 spam).
            try:
                if asst.out_queue is not None:
                    while not asst.out_queue.empty():
                        try:
                            asst.out_queue.get_nowait()
                        except Exception:
                            break
                if asst.audio_in_queue is not None:
                    while not asst.audio_in_queue.empty():
                        try:
                            asst.audio_in_queue.get_nowait()
                        except Exception:
                            break
            except Exception:
                pass
            asst.ui.set_state("THINKING")
            from core.session_manager import session_manager as _sess_mgr
            _sess_mgr.record_disconnect()
            # How long the previous session stayed up (monotonic).
            _up_for = 0.0
            try:
                _up_for = time.monotonic() - float(
                    getattr(asst, "_stable_session_ts", 0) or 0
                )
            except Exception:
                _up_for = 0.0
            if _is_1011:
                asst._last_1011_ts = time.monotonic()
            if _is_goaway:
                # GoAway = planned reconnect; reset backoff and use a short pause
                _sess_mgr.reset()
                await asyncio.sleep(0.5)
            elif _is_1011 and _up_for >= 20.0:
                # Session had been healthy for a while — treat 1011 like a
                # transient server blip: short pause, no backoff escalation.
                log.info(
                    f"[Session] 1011 after {_up_for:.0f}s uptime — "
                    "fast reconnect (no backoff escalation)."
                )
                _sess_mgr.reset()
                await asyncio.sleep(0.8)
            else:
                # Exponential backoff — Attempt 0→1.5s, 1→3s, 2→6s … 5+→30s cap.
                _should_reconnect = await _sess_mgr.wait_before_reconnect()
                if not _should_reconnect:
                    log.error("[SessionManager] Max reconnect attempts reached — stopping.")
                    asst._running = False
                    break

    # ============================================================
    # Mark XXXIX-OR audio pattern — 4 async tasks
    # ============================================================

    def _setup_live_vision_sender(self) -> None:
        """Register a thread-safe callback so LiveVisionEngine can push
        JPEG frames into the active Gemini Live session at ~1 FPS.

        Live API: session.send_realtime_input(video=Blob(jpeg, image/jpeg))
        See https://ai.google.dev/gemini-api/docs/live-api/capabilities
        """
        asst = self._asst
        from vision.live_vision import get_live_vision

        eng = get_live_vision()
        loop = asst._loop

        def _send(jpeg: bytes, mime: str = "image/jpeg") -> None:
            if not jpeg or asst.session is None or loop is None:
                return
            try:
                if not loop.is_running():
                    return

                async def _go():
                    if asst.session is None:
                        return
                    try:
                        from google.genai import types as _gtypes
                        # Gemini Live API expects image frames via media= (mapped to media_chunks in mldev protocol)
                        try:
                            await asst.session.send_realtime_input(
                                media=_gtypes.Blob(data=jpeg, mime_type=mime or "image/jpeg")
                            )
                        except TypeError:
                            await asst.session.send_realtime_input(
                                media={"data": jpeg, "mime_type": mime or "image/jpeg"}
                            )
                    except Exception as exc:
                        err = str(exc).lower()
                        if any(x in err for x in ("1011", "1008", "closed", "aborted")):
                            log.debug(f"[LiveVision] session dead on media send: {exc}")
                        else:
                            log.debug(f"[LiveVision] media send: {exc}")

                asyncio.run_coroutine_threadsafe(_go(), loop)
            except Exception as exc:
                log.debug(f"[LiveVision] schedule send failed: {exc}")

        eng.set_sender(_send)

    async def _send_system_text(self, text: str) -> None:
        """Send a system/text nudge to the live session mid-conversation.

        Gemini 3.1 Flash Live only accepts `send_client_content()` for
        seeding *initial* history (see `history_config` /
        `initial_history_in_client_content` in the migration guide) —
        calling it again after the session is already live closes the
        socket with a 1007 "Request contains an invalid argument.", which
        killed every wake-greeting/briefing/system-nudge call in this file
        under 3.1. Text sent mid-conversation has to go through
        `send_realtime_input(text=...)` instead — the same path real user
        text turns use — so every one of those call sites now routes
        through this helper.

        Also guards against sending during the post-reconnect quiet window
        or when the session is gone — mid-turn text is a common 1011 trigger.
        """
        asst = self._asst
        if asst.session is None:
            log.debug("[system-text] skipped — no live session")
            return
        now = time.monotonic()
        quiet_until = float(getattr(asst, "_session_quiet_until", 0) or 0)
        if now < quiet_until:
            log.debug(
                "[system-text] deferred/skipped — quiet window "
                f"({quiet_until - now:.1f}s left)"
            )
            return
        # Avoid poking the model while it is still producing audio; that
        # often ends the Live socket with 1011 Internal Error.
        with asst._speaking_lock:
            speaking = bool(asst._speaking)
        if speaking and not str(text).startswith("[WAKE]"):
            log.debug("[system-text] skipped — model/speakers still active")
            return
        await asst.session.send_realtime_input(text=text)

    async def _send_user_text(self, text: str) -> None:
        """User-originated text from the React HUD / typed UI.

        Unlike _send_system_text (system nudges), UI chat must not be
        dropped by the post-reconnect quiet window or a brief speaking
        gate — that is the common failure mode after a page refresh while
        the backend stays up: WS reconnects, typed commands appear in the
        log, but Gemini never receives them.
        """
        asst = self._asst
        text = (text or "").strip()
        if not text:
            return
        # Clear quiet window so a fresh UI reconnect can talk immediately
        try:
            asst._session_quiet_until = 0.0
        except Exception:
            pass
        # Wait briefly if the model is still speaking (up to ~3s)
        for _ in range(30):
            if asst.session is None:
                log.warning("[user-text] skipped — no live session")
                return
            with asst._speaking_lock:
                speaking = bool(asst._speaking)
            if not speaking:
                break
            await asyncio.sleep(0.1)
        if asst.session is None:
            log.warning("[user-text] skipped — no live session after wait")
            return
        try:
            await asst.session.send_realtime_input(text=text)
            log.info("[user-text] delivered to Live (%d chars)", len(text))
        except Exception as exc:
            log.warning("[user-text] send failed: %s", exc)
            raise

    async def _speak_startup_greeting(self) -> None:
        """Send the boot greeting without holding command readiness hostage."""
        asst = self._asst
        asst._awake = True
        asst._boot_greeting_pending = True

        _deadline = 20.0
        _waited = 0.0
        _poll = 0.25
        while _waited < _deadline:
            if asst.session is not None:
                break
            await asyncio.sleep(_poll)
            _waited += _poll

        if asst.session is None:
            log.debug("[startup-greeting] Session not ready; brief offline notice.")
            try:
                asst._speak_exact("Good morning, sir.", kind="result")
            except Exception:
                pass
            asst._finish_boot_to_observe()
            return

        await asyncio.sleep(0.4)
        try:
            await asst._send_system_text(
                "[STARTUP] You just came online as GAMA, a JARVIS-style personal "
                "assistant. Greet Sir in one short, confident line — e.g. "
                "'Systems online. I'm fully operational, Sir.' or "
                "'GAMA online. At your service, Sir.' "
                "Do not call tools. Do not ask questions. Say nothing else."
            )
            log.debug("[startup-greeting] Greeting sent via Gemini Live audio.")
        except Exception as exc:
            log.debug(f"[startup-greeting] Gemini greeting failed: {exc}")
            try:
                asst._speak_exact("Good morning, sir.", kind="result")
            except Exception:
                pass
            asst._finish_boot_to_observe()
            return

        # Voice and tools are ready as soon as the greeting request has been
        # accepted. Playback continues independently and remains interruptible.
        asst._finish_boot_to_observe()

    def _finish_boot_to_observe(self) -> None:
        """BOOT → ACTIVE after greeting (stay awake and listening).

        Previously this dropped straight into OBSERVE, which felt like the
        assistant went to standby immediately after saying hello. After the
        greeting we now remain ACTIVE with a normal Active Window so the
        user can speak without saying the wake word first.
        """
        asst = self._asst
        asst._boot_greeting_pending = False
        asst._awake = True
        asst._sync_clap_arm()  # clap only while asleep — disarm after boot ACTIVE
        # Disarm action tools briefly so residual audio / model drift
        # cannot open apps right after the greeting.
        # Fast mode: 0s (tools ready immediately after boot).
        _arm_delay = 6.0
        try:
            from utils.performance_mode import perf as _perf
            _arm_delay = float(_perf.tools_armed_delay_s)
        except Exception:
            pass
        asst._tools_armed_after = time.monotonic() + _arm_delay
        try:
            # Mark boot complete, then force ACTIVE so runtime may_speak /
            # may_stream match the UI.
            asst._runtime.on_boot_complete("startup greeting done")
            asst._runtime.on_wake("boot complete — stay active")
            asst._runtime.on_interaction("boot greeting finished")
        except Exception:
            pass
        try:
            asst._session_mgr.start_session(reason="boot complete")
        except Exception:
            pass
        try:
            asst.ui.set_state(asst._awake_state())
        except Exception:
            pass
        asst.ui.write_log(
            '<span style="color:#00ff88">⚡ Ready — listening. '
            f'Say "{asst._wake_cfg.wake_phrase}" if needed; say "go to sleep" to sleep.</span>'
        )
        log.info("BOOT complete → ACTIVE (listening; auto-standby disabled).")
        # Only now is the voice path ready.  Start optional monitoring,
        # learning, and model warmup after this point so they cannot delay
        # initial listening or contend with the first command.
        try:
            asst._start_phase2_services()
        except Exception as exc:
            log.debug(f"Phase 2 service startup skipped: {exc}")
        asst._schedule_auto_sleep()  # no-op stub
        if asst._loop is not None:
            try:
                if asst._runtime_tick_task is None or asst._runtime_tick_task.done():
                    asst._runtime_tick_task = asyncio.ensure_future(asst._runtime_tick_loop())
            except Exception as exc:
                log.debug(f"runtime tick start failed: {exc}")

    async def _runtime_tick_loop(self) -> None:
        """Drive OBSERVE→DEEP_SLEEP after long quiet Observe; never cut speech."""
        asst = self._asst
        while asst._running:
            try:
                await asyncio.sleep(1.0)
                if getattr(asst, "_boot_greeting_pending", False):
                    continue
                # Never transition modes while anyone is speaking.
                if asst._voice_activity():
                    continue
                rt = getattr(asst, "_runtime", None)
                if rt is None:
                    continue
                new_mode = rt.tick()
                if new_mode is None:
                    continue
                from core.assistant_runtime import RuntimeMode
                if new_mode == RuntimeMode.OBSERVE and asst._awake:
                    # Prefer the silence-based auto-sleep task; tick is backup only.
                    if asst._voice_activity():
                        continue
                    asst._enter_observe_mode("runtime tick active window")
                elif new_mode == RuntimeMode.DEEP_SLEEP:
                    if rt.seconds_in_observe() < 180:
                        continue
                    if asst._voice_activity():
                        continue
                    asst._enter_deep_sleep("runtime tick observe inactivity")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug(f"runtime tick error: {exc}")


    async def _system_stats_loop(self) -> None:
        """Feeds the HUD's live telemetry: CPU/RAM, battery, active app,
        and current task. Reads from the World Model (core.world_model)
        rather than polling psutil directly — the Context Awareness Engine
        already refreshes those fields every ~4 s, so this loop just reads
        the single source of truth instead of running a second independent
        poller. Falls back to a direct psutil read if the World Model
        isn't populated yet (e.g. JARVIS bootstrap failed), so the CPU/RAM
        readout never goes blank.
        5 s interval: this is an at-a-glance HUD readout, not a monitor."""
        asst = self._asst
        import psutil
        while asst._running:
            try:
                cpu = ram = None
                battery_text = app_text = task_text = ""
                try:
                    from core.world_model import world as _world
                    snap = _world.snapshot()
                    c = snap.computer
                    cpu, ram = c.cpu_percent, c.ram_percent
                    if c.battery_percent is not None:
                        plug = "charging" if c.battery_plugged else "on battery"
                        battery_text = f"BATT {c.battery_percent:.0f}% ({plug})"
                    if c.active_app:
                        app_text = f"APP: {c.active_app}"
                    active = [t for t in snap.tasks.values() if t.status == "running"]
                    if active:
                        t = active[0]
                        task_text = f"TASK: {t.description[:60]} ({t.progress:.0%})"
                except Exception:
                    pass

                if cpu is None or ram is None:
                    cpu = psutil.cpu_percent(interval=None)
                    ram = psutil.virtual_memory().percent

                asst.ui.set_system_stats(cpu, ram)
                asst.ui.set_world_stats(battery_text, app_text, task_text)
            except Exception:
                pass
            await asyncio.sleep(10.0)

    async def _receive_audio(self):
        """Receive from Gemini → audio_in_queue + handle transcriptions/tools.

        Features:
          - Wake word: when asleep, only responds to "wake up gama"
          - Barge-in: when user speaks during Gama's reply, immediately
            flush the audio queue and stop playback
          - Sleep word: "go to sleep" puts Gama in sleep mode
        """
        asst = self._asst
        out_buf, in_buf = [], []
        input_turn_parts: list[str] = []
        log.info("Recv started")

        try:
            while asst._running:
                async for response in asst.session.receive():

                    # --- BARGE-IN: Gemini marks the turn interrupted ---
                    # Only meaningful while we are actually in ACTIVE and
                    # playing audio. In OBSERVE the model may still emit
                    # interrupted=True (user spoke while a suppressed
                    # response was in flight) — ignore those so standby
                    # does not spam "[interrupted]" or flush state.
                    # When interruption/barge-in is OFF, ignore server
                    # interrupted entirely (echo on speakers often sets
                    # this flag even though the user did not barge in).
                    if response.server_content:
                        sc = response.server_content

                        # Capture session resumption handle for seamless reconnects
                        if hasattr(sc, "session_resumption_update") and sc.session_resumption_update:
                            _h = getattr(sc.session_resumption_update, "new_handle", None)
                            if _h:
                                asst._live_resumption_handle = _h
                                log.debug(f"[Live] Session resumption handle updated: {_h[:16]}...")

                        if hasattr(sc, "interrupted") and sc.interrupted:
                            _barge_on = bool(getattr(asst, "_barge_in_enabled", True))
                            if not _barge_on:
                                log.debug(
                                    "Gemini interrupted=True ignored "
                                    "(interruption/barge-in is off)."
                                )
                            else:
                                with asst._speaking_lock:
                                    _sp = asst._speaking
                                if asst._awake and (_sp or (
                                    asst.audio_in_queue is not None
                                    and not asst.audio_in_queue.empty()
                                )):
                                    asst._set_speaking(False)
                                    asst._hard_stop_speaker()
                                    asst._last_audio_chunk = None
                                    asst._seen_audio_chunks.clear()
                                    while not asst.audio_in_queue.empty():
                                        try:
                                            asst.audio_in_queue.get_nowait()
                                        except Exception:
                                            break
                                    asst.ui.write_log(
                                        '<span style="color:#007AFF">[interrupted]</span>'
                                    )
                                    log.info("Barge-in: user interrupted Gama's speech.")

                    # Audio data → play queue only when runtime allows speech
                    # (ACTIVE / BOOT greeting) or a reminder announcement.
                    # OBSERVE and DEEP_SLEEP drop all Gemini audio so the
                    # assistant never speaks while observing or sleeping.
                    _may_play = asst._awake or asst._announcing_while_asleep
                    try:
                        _rt = getattr(asst, "_runtime", None)
                        if _rt is not None:
                            _may_play = (_rt.may_speak and asst._awake) or asst._announcing_while_asleep
                    except Exception:
                        pass
                    if response.data and _may_play:
                        if asst._turn_done_event and asst._turn_done_event.is_set():
                            asst._turn_done_event.clear()
                        # Guard against Gemini Live re-delivering the same
                        # audio chunk(s) again after a tool-call round trip
                        # resumes the turn. The model frequently re-emits
                        # already-played audio from the start of the sentence
                        # when the turn resumes; the old back-to-back check
                        # only caught consecutive duplicates, so whole
                        # sentences were being played 2-4 times. A real new
                        # chunk of speech is never byte-identical to any
                        # chunk already heard in this turn, so dropping any
                        # repeat is safe.
                        chunk_hash = hash(response.data)
                        if chunk_hash not in asst._seen_audio_chunks:
                            try:
                                asst.audio_in_queue.put_nowait(response.data)
                            except Exception:
                                pass
                            asst._seen_audio_chunks.add(chunk_hash)
                        else:
                            log.debug(f"[audio] Dropped duplicate chunk (hash {chunk_hash & 0xFFFF:x}) already played this turn")
                        asst._last_audio_chunk = response.data

                    # Server content — transcriptions + turn events
                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                # Drop fragments that are pure reasoning echoes —
                                # the model occasionally speaks back the reason-tool
                                # response text ("Reasoning noted.", etc.) even though
                                # the tool now returns an empty string. Belt-and-
                                # suspenders guard so it never reaches the log or
                                # out_buf regardless of model behaviour.
                                if _is_reasoning_echo(txt):
                                    log.debug(f"[transcript] Suppressed reasoning echo: {txt!r}")
                                # Only record Gemini's output when intentionally awake
                                elif asst._awake or asst._announcing_while_asleep:
                                    # Gemini Live occasionally re-emits the exact
                                    # same transcription fragment it just sent
                                    # (e.g. right after a tool-call round trip
                                    # resumes the turn), which otherwise makes
                                    # GAMA say the same sentence twice. Drop an
                                    # exact repeat of the immediately-preceding
                                    # fragment before appending.
                                    # Check against every fragment already seen this
                                    # turn, not just the last couple — Gemini Live has
                                    # been observed re-emitting a whole sentence after
                                    # several intervening fragments (e.g. across a
                                    # multi-retry tool-call round trip, like a
                                    # malformed `reason` call being retried more than
                                    # once), which let repeats slip past a
                                    # last-N-fragment check. Compare case/whitespace
                                    # insensitively so trivial formatting differences
                                    # don't defeat the guard.
                                    # Exact-repeat check (case/whitespace-insensitive)
                                    # against every fragment already spoken this turn.
                                    norm_txt = txt.strip().lower()
                                    prior_norms = [f.strip().lower() for f in out_buf]
                                    is_exact_repeat = norm_txt in prior_norms
                                    # Partial-repeat check: Gemini Live sometimes
                                    # re-emits a fragment that's a substring of
                                    # something already spoken (or vice versa) rather
                                    # than a byte-identical repeat — e.g. resuming
                                    # mid-sentence after a tool-call round trip re-sends
                                    # the tail of what was already said. Only treat
                                    # short-in-long as a repeat (>=8 chars) to avoid
                                    # false positives on genuinely short new fragments.
                                    is_partial_repeat = False
                                    if not is_exact_repeat and len(norm_txt) >= 8:
                                        for p in prior_norms:
                                            if norm_txt in p or (len(p) >= 8 and p in norm_txt):
                                                is_partial_repeat = True
                                                break
                                    if is_exact_repeat or is_partial_repeat:
                                        log.debug(f"[transcript] Suppressed repeat fragment: {txt!r}")
                                    else:
                                        out_buf.append(txt)
                                        # Rolling spoken text for echo detection
                                        try:
                                            _prev = getattr(asst, "_last_spoken_text", "") or ""
                                            asst._last_spoken_text = (_prev + " " + txt).strip()[-800:]
                                        except Exception:
                                            asst._last_spoken_text = txt

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                # ── Echo / own-voice kill (stops double/triple speaking) ──
                                # Gama's TTS is often re-picked by the mic and appears
                                # as input_transcription. Treating that as a user turn
                                # causes a feedback loop (Welcome back → How may I help
                                # → How may I assist → …). DROP those transcripts fully.
                                def _norm(s: str) -> str:
                                    import re as _re
                                    s = (s or "").lower().strip()
                                    s = _re.sub(r"[^\w\s]", " ", s)
                                    return " ".join(s.split())

                                _in_n = _norm(txt)
                                _out_n = _norm(" ".join(out_buf[-16:]))
                                # Also track a rolling last-spoken string on the instance
                                _last_spoken = _norm(getattr(asst, "_last_spoken_text", "") or "")
                                _combined_out = (_out_n + " " + _last_spoken).strip()

                                def _is_echo(inp: str, out: str) -> bool:
                                    if not inp:
                                        return True
                                    if not out:
                                        return False
                                    if len(inp) < 3:
                                        return False
                                    # containment either way
                                    if inp in out or (len(out) >= 6 and out in inp):
                                        return True
                                    # high token overlap
                                    iw = set(w for w in inp.split() if len(w) > 2)
                                    ow = set(w for w in out.split() if len(w) > 2)
                                    if iw and ow:
                                        inter = len(iw & ow)
                                        if inter / max(len(iw), 1) >= 0.6:
                                            return True
                                        if inter / max(len(iw | ow), 1) >= 0.5:
                                            return True
                                    # classic assistant echo phrases
                                    _echo_phrases = (
                                        "welcome back", "how may i help", "how may i assist",
                                        "yes sir", "yes, sir", "what do you need",
                                        "i am listening", "affirmative", "at your service",
                                        "ready sir", "of course sir",
                                    )
                                    for ph in _echo_phrases:
                                        if ph in inp and (ph in out or not out):
                                            # if we're speaking / just spoke, treat as echo
                                            return True
                                    return False

                                with asst._speaking_lock:
                                    _still_speaking = asst._speaking
                                _recently_spoke = (
                                    time.monotonic() - float(getattr(asst, "_last_speaking_end_ts", 0) or 0)
                                ) < 3.5
                                _playing = _still_speaking or (
                                    asst.audio_in_queue is not None
                                    and not asst.audio_in_queue.empty()
                                )

                                _echo_hit = _is_echo(_in_n, _combined_out)
                                # While Gama is talking (or just finished), also drop
                                # short inputs and known assistant openers even if
                                # out_buf hasn't caught up yet (timing race).
                                _assistant_openers = (
                                    "welcome back", "how may i help", "how may i assist",
                                    "yes sir", "yes, sir", "what do you need",
                                    "i am listening", "affirmative", "at your service",
                                    "of course", "certainly", "right away",
                                    "what is your request", "using what",
                                )
                                if (_playing or _recently_spoke) and not _echo_hit:
                                    for _op in _assistant_openers:
                                        if _op in _in_n:
                                            _echo_hit = True
                                            break
                                    # Very short fragments while speaking are almost always bleed
                                    if len(_in_n) < 12 and _playing:
                                        _echo_hit = True

                                if _echo_hit:
                                    # Own-speech echo — drop silently (no INFO spam)
                                    continue  # next response in async for

                                # Half-duplex (interruption off): while Gama is
                                # playing, any input transcript is almost always
                                # speaker→mic bleed. Drop it so it never shows as
                                # USER or retriggers tools.
                                if _playing and not bool(
                                    getattr(asst, "_barge_in_enabled", True)
                                ):
                                    continue

                                # Real user speech while playing → barge-in
                                # Flag lives on the assistant, not this controller.
                                _suppress = time.monotonic() < float(
                                    getattr(asst, "_barge_in_suppress_until", 0.0) or 0.0
                                )
                                _substantial = len(txt.strip()) >= 4
                                if (
                                    bool(getattr(asst, "_barge_in_enabled", True))
                                    and asst._awake
                                    and not asst._announcing_while_asleep
                                    and not _suppress
                                    and _substantial
                                    and _playing
                                ):
                                    asst._immediate_barge_in()

                                in_buf.append(txt)
                                input_turn_parts.append(txt)
                                # Independently track what the user actually said,
                                # so DESTRUCTIVE verbal-confirmation checks can be
                                # matched against real transcript text instead of
                                # trusting a caller/LLM-supplied boolean (see
                                # _handle_tool_call / security/authentication.py
                                # check_verbal_confirmation).
                                asst._last_input_transcript = " ".join(input_turn_parts)[-2000:]
                                asst._last_input_transcript_ts = time.monotonic()
                                asst._current_turn_reasoning_allowed = _explicit_reasoning_requested(
                                    asst._last_input_transcript
                                )
                                # User is speaking / STT is producing text — show LISTENING
                                # instead of leaving READY/WAITING on the HUD.
                                if asst._awake:
                                    try:
                                        asst.ui.set_state("LISTENING")
                                    except Exception:
                                        pass
                                # Confirmation-intent classify ONLY when the transcript
                                # looks like a verbal yes/no — avoids the flash-lite
                                # generateContent storm on every partial transcript.
                                _tl_confirm = (txt or "").lower()
                                _looks_confirm = any(
                                    w in _tl_confirm
                                    for w in (
                                        "yes", "yeah", "yep", "confirm", "sure",
                                        "go ahead", "do it", "proceed", "ok ",
                                        "okay", "haan", "haa", "ji ", "no ",
                                        "cancel", "stop", "don't", "dont",
                                    )
                                )
                                _prestart_client = getattr(asst, "_genai_client", None)
                                if asst._awake and _prestart_client and _looks_confirm:
                                    try:
                                        from security.authentication import classify_confirmation_intent as _cci
                                        _prestart_transcript = asst._last_input_transcript
                                        _prev = asst._pending_verbal_intent_task
                                        if _prev is not None and not _prev.done():
                                            _prev.cancel()
                                        asst._pending_verbal_intent_task = asyncio.ensure_future(
                                            asyncio.get_event_loop().run_in_executor(
                                                None, _cci, _prestart_transcript, _prestart_client,
                                            )
                                        )
                                    except Exception:
                                        pass
                                # The user actually responded — any pending
                                # proactive suggestion is now either accepted
                                # or declined by whatever they said next;
                                # either way the tool-call gate should stop
                                # blocking and let Gemini act on the reply.
                                asst._proactive_awaiting_confirmation = False
                                # Cancel conversation timeout — user is speaking
                                asst._cancel_auto_sleep()
                                # --- WAKE WORD CHECK (legacy/fallback path) ---
                                # Only reachable at all when the local wake_word
                                # engine is unavailable (see __init__): audio no
                                # longer reaches the cloud while asleep once the
                                # local engine is active, so this cloud-transcript
                                # check naturally becomes dead code in that case.
                                # Kept so GAMA still wakes on spoken command if
                                # wake_word setup fails — see wake_word/README.md.
                                txt_lower = txt.lower().strip().strip(".!,;:")
                                if not asst._awake:
                                    # ── OBSERVE / DEEP_SLEEP cloud path ──
                                    # 1) Pure wake word → wake + short ack.
                                    # 2) Direct address ("what do you think, Gama?")
                                    #    → wake, inject observe context, answer.
                                    # 3) Everything else → buffer as silent
                                    #    context (observe only) and stay silent.
                                    _is_wake_only = (
                                        txt_lower in asst._wake_phrases
                                        or txt_lower in {
                                            p.replace(" ", "") for p in asst._wake_phrases
                                        }
                                    )
                                    _direct = asst._is_direct_address(txt)

                                    if _is_wake_only and not _direct:
                                        asst._flush_playback(reason="cloud wake")
                                        asst._wake_gama()
                                        try:
                                            asst._session_mgr.start_session(
                                                reason="wake word (cloud fallback)"
                                            )
                                        except Exception:
                                            pass
                                        asst.ui.set_state("LISTENING")
                                        asst.ui.write_log(
                                            '<span style="color:#00ff88">☀️ Gama is awake!</span>'
                                        )
                                        log.info("Wake word detected — Gama is now awake.")
                                        asst.ui.emit_event("SleepExited")
                                        pending = getattr(asst, "_observe_pending_request", None)
                                        if pending:
                                            await asst._answer_pending_observe_request(pending)
                                        else:
                                            # Local ack only — model [WAKE] text caused
                                            # delayed "Yes, Sir?" after real replies and
                                            # contributed to Live 1011 closes.
                                            try:
                                                await asst._send_wake_ack()
                                            except Exception:
                                                try:
                                                    asst._speak_exact(
                                                        _WAKE_ACK_LINE, kind="wake_ack"
                                                    )
                                                except Exception:
                                                    pass
                                        in_buf = []
                                        out_buf = []
                                        continue

                                    if _direct:
                                        await asst._wake_from_direct_address(txt)
                                        in_buf = []
                                        out_buf = []
                                        continue

                                    # Silent understanding — keep recent lines
                                    # so a later direct address has context.
                                    asst._record_observe_context(txt)
                                    continue
                                # --- SLEEP WORD CHECK ---
                                if asst._sleep_word_re.match(txt_lower):
                                    asst._enter_sleep_mode("sleep word detected")
                                    in_buf = []
                                    out_buf = []
                                    continue

                        if sc.turn_complete:
                            # Signal _play_audio that the turn is done
                            if asst._turn_done_event:
                                asst._turn_done_event.set()
                            # Reset the duplicate-chunk guard so the first
                            # audio/text fragment of the *next* turn is never
                            # wrongly suppressed just because it happens to
                            # match the tail of this turn.
                            asst._last_audio_chunk = None
                            asst._seen_audio_chunks.clear()

                            full_in = " ".join(in_buf).strip()
                            asst._last_input_transcript = full_in[-2000:] or asst._last_input_transcript
                            if full_in and asst._awake:
                                try:
                                    asst._last_user_voice_ts = time.monotonic()
                                    asst._runtime.on_interaction("user speech")
                                except Exception:
                                    pass
                                # First clear Latin user line after boot arms tools.
                                try:
                                    if time.monotonic() < float(getattr(asst, "_tools_armed_after", 0) or 0):
                                        _fi = full_in.strip()
                                        _latin = sum(
                                            1 for c in _fi
                                            if ("a" <= c.lower() <= "z") or c.isdigit()
                                        )
                                        if len(_fi) >= 4 and _latin >= 3:
                                            asst._tools_armed_after = time.monotonic()
                                except Exception:
                                    pass
                                asst._cancel_auto_sleep()
                                asst._schedule_auto_sleep()

                                # Gemini's input transcription is used directly.
                                # The old HUD-repair fallback made blocking Google
                                # SpeechRecognition/Sphinx calls from this receive
                                # task.  That display-only work could delay audio,
                                # tool calls, and the next user turn.
                                _display = full_in
                                asst.ui.write_log(f'<span style="color:#8ffcff">USER:</span> {_display}')
                                log.info(f"User said: {_display}")
                                if _display != full_in:
                                    log.debug(f"Live STT raw: {full_in}")
                                # Safety net: if local fast-intent ASR missed a
                                # deterministic settings toggle (e.g. "Turn
                                # interruption off"), run the regex matcher on
                                # Gemini's transcript so user_settings still
                                # executes without waiting on a tool call.
                                try:
                                    _fi_lower = full_in.lower()
                                    if any(
                                        k in _fi_lower
                                        for k in (
                                            "interruption", "barge-in", "barge in",
                                            "listening sensitivity", "voice verification",
                                            "wake greeting", "proactive suggestion",
                                        )
                                    ):
                                        asst._on_fast_intent_text(full_in, verified=True)
                                except Exception as _fi_exc:
                                    log.debug(f"Gemini-path fast-intent safety net: {_fi_exc}")
                            in_buf = []

                            full_out = _dedupe_repeated_sentences(" ".join(out_buf).strip())
                            if full_out and asst._awake:
                                asst.ui.write_log(f'<span style="color:#ffcc00">GAMA:</span> {full_out}')
                            out_buf = []
                            input_turn_parts = []
                            asst._current_turn_reasoning_allowed = False

                            # Save to conversation memory (only if awake)
                            if (full_in or full_out) and asst._awake:
                                asst._save_conversation(full_in, full_out)

                            # Mid-action barge-in follow-up: if speech
                            # earlier interrupted a running background task
                            # and this turn didn't already resolve it
                            # (resume/cancel/retry clear the state in
                            # dispatch_tool), gently offer to resume/abort
                            # now that the interrupting request is handled.
                            if asst._awake and asst._barge_in_paused_task_id:
                                # Wait 3 s before offering the followup — gives
                                # Gemini time to process the user's barge-in
                                # command (e.g. "cancel that") and clear the
                                # paused-task state before we'd ask again.
                                threading.Timer(3.0, asst._offer_paused_task_followup).start()

                            # If this turn was just GAMA announcing a
                            # reminder/timer/alarm/class-reminder that fired
                            # while asleep, drop straight back to sleep now
                            # that it's done speaking — don't linger awake
                            # and listening for further commands.
                            if asst._announcing_while_asleep:
                                asst._announcing_while_asleep = False
                                asst._awake = False
                                asst._wake_verifying = False
                                from security import trusted_session
                                trusted_session.invalidate("back to standby after announcement")
                                if asst._voice_pipeline is not None:
                                    asst._voice_pipeline.sleep()
                                asst.ui.set_state("SLEEPING")
                                asst.ui.write_log(
                                    '<span style="color:#5ab8cc">😴 Reminder announced — GAMA is back in standby. '
                                    f'Say "{asst._wake_cfg.wake_phrase}" to begin.</span>'
                                )
                                log.info("Sleep-mode reminder announcement done — GAMA is asleep again.")

                    # Tool calls (only if fully awake, not just announcing during sleep)
                    if response.tool_call and asst._awake and not asst._announcing_while_asleep:
                        await asst._handle_tool_call(response.tool_call)

        except Exception as exc:
            err_s = str(exc).lower()
            _recoverable = any(
                m in err_s
                for m in ("1011", "1008", "internal error", "connectionclosed",
                          "goaway", "aborted", "session durat")
            )
            if _recoverable:
                log.warning(f"[Recv] Live session closed ({exc}) — will reconnect.")
            else:
                log.error(f"❌ Recv: {exc}")
                log.error(traceback.format_exc())
            # Re-raise so TaskGroup / run() reconnect path is triggered
            raise

    def _speak_exact(self, text: str, priority=None, kind: str = "prompt",
                      blocking: bool = False) -> None:
        """Speak a short, fixed enrollment/verification/ack line verbatim
        (e.g. "Welcome back, Vineet.", "Please try again.", "One moment.").

        This deliberately does NOT go through the Gemini Live session
        (that's `_speak_via_session`, for normal conversational replies).
        Enrollment prompts and acks fire *while a tool call is still in
        flight*, and the Live session won't speak anything new sent to it
        until the pending function call's response has been sent back —
        so routing scripted speech through the session meant every
        enrollment instruction sat silent for the whole tool call and
        then played back all at once at the end.

        All such scripted speech is now arbitrated by
        voice/speech_manager.py (a priority queue sitting on top of the
        local offline engine) instead of being pushed straight into the
        raw TTS FIFO. That's what stops a stale "still working" ack from
        ever playing after the real result (a RESULT-priority line
        flushes any not-yet-started ACK), and — via blocking=True —
        lets enrollment keep its spoken instructions in lockstep with
        the on-screen step instead of racing ahead of the audio.

        Called from `ui.enrollment_speak` (enrollment worker threads, via
        `ui.speak_line(text)`) and from the tool dispatcher's "still
        working" acknowledgement. Safe to call from any thread.
        """
        asst = self._asst
        # Security: scrub any accidental confirmation-code echo before
        # the text reaches the TTS engine.
        text = _sanitize_spoken_text(text)

        # Speech Styler (spec section 16): the mandatory final rewrite
        # stage before ANY scripted line reaches TTS — removes robotic/
        # weak phrasing, adapts concision to detected mood, strips
        # emojis. Enrollment/prompt lines that need exact wording
        # (confirmation codes, security prompts) opt out via kind, since
        # styling must never alter something the user has to repeat back
        # verbatim.
        # speech_styler removed — use text as-is

        # Deduplication: skip if the identical line was already spoken
        # within the dedup window.  Exempts enrollment/prompt kinds because
        # enrollment legitimately repeats instructions step by step.
        if kind not in ("prompt", "enrollment", "wake_ack") and _spoken_dedup_check_and_mark(text):
            log.debug(f"[speak_exact] Duplicate suppressed ({kind}): {text!r}")
            return

        from voice.speech_manager import say as _sm_say, Priority as _Prio
        if priority is None:
            priority = _Prio.PROMPT
        # wake_ack must always be heard, even if the identical "Yes, Sir?"
        # line was just spoken moments ago (e.g. two quick wakes in a
        # row) — skip both the module-level and speech_manager-level
        # dedup windows for this kind only.
        _sm_say(text, priority=priority, kind=kind, blocking=blocking,
                dedup=(kind != "wake_ack"))

        # Conversation Session Manager: if Gama's line ends in a question,
        # extend the adaptive inactivity window so the user has a natural
        # amount of time to answer without needing the wake word again.
        try:
            asst._session_mgr.note_gama_asked_question(text.rstrip().endswith("?"))
        except Exception:
            pass

    def _enrollment_speak_sync(self, ui, text: str) -> None:
        """on_speak callback for voice enrollment. Runs on the
        enrollment worker thread (never the UI thread), so it's safe —
        and necessary — to block here until the line has actually
        finished playing before returning control to the enrollment
        flow. Without this, enrollment's timers/countdowns/steps (which
        advance on wall-clock time, independent of speech) would race
        ahead of the audio queue, and a whole session's worth of prompts
        could still be backlogged in the speech queue after the on-
        screen flow had already finished — which is exactly how
        "Voice enrollment is about to begin" ended up being heard after
        enrollment was already done.

        On-screen state updates for enrollment already go through their
        own dedicated signals (set_pose, set_guidance, etc.) elsewhere —
        this only owns the spoken side, kept in lockstep with it.
        """
        asst = self._asst
        asst._speak_exact(text, kind="prompt", blocking=True)

    # Microphone Calibration Wizard removed for cleanup/speed.

    # ── Barge-in on/off (live toggle, no restart) ───────────────────────────

    def _speak_via_session(self, text: str) -> None:
        """Send a text message to Gemini so Gama speaks it aloud.

        Used by the reminder/alarm/timer/class-reminder system. Always
        wakes GAMA first so it can actually respond with audio.

        Thread-safe: called from daemon timer threads (_fire_timer).
        Must never call asyncio APIs that require a loop in *this*
        thread (that was the OBSERVE/standby reminder failure:
        "There is no current event loop in thread 'Thread-N (_fire_timer)'").

        If GAMA was ASLEEP/OBSERVE when this fired, the wake is speak-only:
        mic audio still never reaches Gemini (see _announcing_while_asleep
        gating in _listen_audio), and GAMA drops straight back to sleep as
        soon as it's said its piece. If GAMA was already awake, this just
        behaves like a normal spoken message.

        Offline / no session: speaks directly via local Piper TTS so
        reminders/alarms/timers still fire audibly.
        """
        asst = self._asst
        was_asleep = not asst._awake
        try:
            asst._wake_gama()
        except Exception as exc:
            log.error(f"_wake_gama during speak failed: {exc}")
            # Fall through — still try local TTS so the reminder is heard.
            try:
                asst._speak_exact(text, kind="result")
            except Exception as exc2:
                log.error(f"Local TTS fallback also failed: {exc2}")
            return

        # ── Offline / no-loop fallback ───────────────────────────────────
        if not asst.session or not asst._loop or not asst._loop.is_running():
            log.info(f"[offline/no-loop] Speaking via local TTS: {text[:80]}")
            try:
                asst._speak_exact(text, kind="result")
            except Exception as exc:
                log.error(f"Local TTS speak failed: {exc}")
            if was_asleep:
                # Don't leave GAMA stuck awake after a local-only announce.
                try:
                    asst._announcing_while_asleep = False
                    asst._awake = False
                    asst.ui.set_state("SLEEPING")
                except Exception:
                    pass
            return
        # ─────────────────────────────────────────────────────────────────

        if was_asleep:
            # Mark announcing mode BEFORE speaking so that:
            #   1. _listen_audio's Gemini-forward gate stays closed during
            #      the announcement (mic audio never reaches Gemini).
            #   2. The turn_complete handler in _receive_audio puts GAMA
            #      straight back to sleep the moment Gemini finishes.
            asst._announcing_while_asleep = True

        try:
            fut = asyncio.run_coroutine_threadsafe(
                asst._send_system_text(text),
                asst._loop,
            )
            # Don't block the timer thread on Gemini; log failures async.
            def _done(f):
                try:
                    f.result()
                except Exception as exc:
                    log.error(f"Failed to send speak text: {exc}")
                    if was_asleep:
                        asst._announcing_while_asleep = False
                        asst._awake = False
                        try:
                            asst.ui.set_state("SLEEPING")
                        except Exception:
                            pass
                    # Ensure the user still hears the reminder.
                    try:
                        asst._speak_exact(text, kind="result")
                    except Exception:
                        pass
            fut.add_done_callback(_done)
            if was_asleep:
                log.info(f"[sleep-announce] Sent to Gemini for speaking: {text[:80]}")
            else:
                log.info(f"Sent to Gemini for speaking: {text[:80]}...")
        except Exception as exc:
            log.error(f"Failed to schedule speak text: {exc}")
            if was_asleep:
                asst._announcing_while_asleep = False
                asst._awake = False
                try:
                    asst.ui.set_state("SLEEPING")
                except Exception:
                    pass
            try:
                asst._speak_exact(text, kind="result")
            except Exception as exc2:
                log.error(f"Local TTS fallback failed: {exc2}")

    async def _send_briefing(self):
        """Send morning briefing prompt."""
        asst = self._asst
        await asyncio.sleep(1.5)
        if not asst.session:
            return
        try:
            from datetime import timezone, timedelta
            _ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(_ist).strftime("%I:%M %p IST").lstrip("0")
            await asst._send_system_text(
                f"[STARTUP_BRIEFING] It's {now}. Greet the user briefly and ask how you can help."
            )
        except Exception as exc:
            log.error(f"Briefing failed: {exc}")

    async def _send_restart_complete(self):
        """Let Gama mention it just finished restarting itself (the
        restart_self tool). Only fires once, right after a self-restart —
        see _just_restarted / restart marker.
        Phrasing is left to Gemini (like every other system prompt here)
        so it comes out sounding natural and varies each time, instead of
        a fixed scripted line."""
        asst = self._asst
        await asyncio.sleep(1.5)
        if not asst.session:
            return
        try:
            await asst._send_system_text(
                "[SYSTEM] You just finished restarting your own process "
                "(the user asked you to restart/reboot yourself a moment "
                "ago). Let the user know briefly, in one short natural "
                "sentence, that you're back online — vary the wording, "
                "don't use a fixed script."
            )
        except Exception as exc:
            log.error(f"Restart-complete announcement failed: {exc}")

    # See core/session_mixins.py (NotificationMixin) for:
    #   _on_download_complete, _on_habit_suggestion_silent,
    #   _on_jarvis_notification, _on_sys_alert

    def send_text(self, text: str):
        """Send text command to the session.

        Mirrors the voice sleep-mode gate exactly: while GAMA is asleep,
        typed commands are silently ignored too — the only thing that
        gets through is the wake phrase, which wakes GAMA up (greet, no
        answer to whatever was typed alongside it, same as voice). This
        closes the loophole where the UI text box bypassed `_awake`
        entirely and made GAMA reply "I'm in sleep mode..." instead of
        just staying silent like it does for voice.
        """
        asst = self._asst
        if text == "__voice__":
            return  # voice is always on

        # Real typed input from the user — same reasoning as the voice
        # transcript path above: this is the user's actual answer to any
        # pending proactive offer, so stop gating tool calls on it.
        asst._proactive_awaiting_confirmation = False
        asst._last_input_transcript = (text or "").strip()[-2000:]
        asst._last_input_transcript_ts = time.monotonic()
        asst._current_turn_reasoning_allowed = _explicit_reasoning_requested(
            asst._last_input_transcript
        )

        if not asst._awake:
            txt_lower = (text or "").lower().strip().strip(".!,;:")
            if txt_lower in asst._wake_phrases:
                asst._awake = True
                asst._session_mgr.start_session(reason="wake word (typed)")
                asst.ui.set_state(asst._awake_state())
                asst.ui.write_log(f'<span style="color:#00ff88">☀️ Gama is awake!</span>')
                log.info("Wake phrase typed — Gama is now awake.")
                # Always the fixed, deterministic acknowledgement — not
                # routed through Gemini, so it can't vary or turn into a
                # greeting/briefing.
                try:
                    asst._speak_exact(_WAKE_ACK_LINE, kind="wake_ack")
                except Exception as exc:
                    log.debug(f"Wake acknowledgement failed (non-fatal): {exc}")
            else:
                # Asleep — ignore, same as voice while asleep.
                log.info(f"Typed message ignored (GAMA asleep): {text[:80]!r}")
            return

        if asst._loop and asst.session:
            # User-originated typed commands use _send_user_text so they are
            # not dropped by quiet-window / speaking guards (page-refresh bug).
            asyncio.run_coroutine_threadsafe(
                asst._send_user_text(text),
                asst._loop,
            )
        elif asst._loop and not asst.session:
            log.warning(
                "[send_text] Live session not ready — message queued for retry: %r",
                (text or "")[:80],
            )
            # Best-effort: retry a few times while Live reconnects
            def _retry_user_text(msg=text, attempts=8):
                import time as _t
                for i in range(attempts):
                    _t.sleep(0.4)
                    if asst.session and asst._loop and asst._loop.is_running():
                        try:
                            asyncio.run_coroutine_threadsafe(
                                asst._send_user_text(msg), asst._loop
                            )
                            log.info("[send_text] delivered after retry #%d", i + 1)
                            return
                        except Exception as exc:
                            log.debug("[send_text] retry failed: %s", exc)
                log.warning("[send_text] gave up — Live session never came back")
            import threading as _thr
            _thr.Thread(target=_retry_user_text, daemon=True, name="ui-text-retry").start()
        elif asst._loop and asst._is_offline():
            # Offline: fast-intent PC commands still work; conversational
            # queries are not supported without Gemini (local LLM removed).
            log.debug("[send_text] Offline — text dropped (no Gemini session).")

    def schedule_shutdown(self) -> None:
        asst = self._asst
        asst._shutdown_pending = True

    def stop(self):
        asst = self._asst
        asst._running = False
        # Cancel conversation-timeout task if one is pending
        if asst._loop:
            asst._loop.call_soon_threadsafe(asst._cancel_auto_sleep)
        asst._sys_monitor.stop()
        asst._desktop_tracker.stop()
        if asst._wake_listener is not None:
            asst._wake_listener.close()
        try:
            asst._reflect_and_reset_session()
        except Exception as exc:
            log.debug(f"Shutdown memory reflection skipped: {exc}")
        try:
            asst._audio_out_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            from actions.goal_tracker import stop_goal_watcher
            stop_goal_watcher()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

    def _is_offline(self) -> bool:
        """True when internet is unavailable (no Gemini session possible).

        Single source of truth for offline/online routing decisions.
        """
        asst = self._asst
        return asst.session is None

    def _awake_state(self) -> str:
        asst = self._asst
        return "LISTENING"

    # _handle_offline_query removed — offline LLM (llama.cpp) support has been
    # disabled. PC commands via fast-intent still work offline; conversational
    # queries while Gemini is unreachable are silently dropped.

    def _save_conversation(self, user_text: str, gama_text: str) -> None:
        """Buffer a conversation exchange for crash recovery / session history.

        Exchanges are kept in memory (capped at 200) and mirrored to the rolling
        JSONL. Self-learning / auto profile extraction is disabled.
        """
        asst = self._asst
        if not user_text and not gama_text:
            return
        ts = time.strftime("%H:%M")
        line = f"[{ts}] User: {user_text} | Gama: {gama_text}"
        asst._session_exchanges.append(line)
        if len(asst._session_exchanges) > 200:
            asst._session_exchanges = asst._session_exchanges[-200:]

        # Persist to rolling JSONL so this exchange survives a crash
        try:
            import pathlib as _pl, json as _rj
            _pl.Path(_SESSION_ROLLING_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(_SESSION_ROLLING_PATH, "a", encoding="utf-8") as _rf:
                _rf.write(
                    _rj.dumps({
                        "text": line,
                        "session_start": asst._session_start_ts.isoformat(),
                    }) + "\n"
                )
        except Exception:
            pass  # never let file IO break the voice loop

        # World Model — keep conversation state current (last message on
        # each side + turn count). Topic/pending-questions/goal are left
        # for the LLM or planner to set explicitly via update_user /
        # update_conversation elsewhere; this call only records the raw
        # exchange, which is what everything else in the World Model that
        # depends on "what did the user just say" needs.
        try:
            from core.world_model import world as _world
            _world.update_conversation(
                last_user_message=user_text or None,
                last_assistant_message=gama_text or None,
            )
        except Exception:
            pass

        # Memory storage is model-driven via remember/save_memory tools —
        # no regex auto-extraction here.

    def _on_activity_checkin(self, message: str) -> None:
        """Gentle spoken check-in from activity_sentinel (idle / project)."""
        asst = self._asst
        try:
            text = (message or "").strip()
            if not text:
                return
            # Prefer session speak path; fall back to local TTS helper
            if hasattr(asst, "_speak_via_session"):
                asst._speak_via_session(text)
            elif hasattr(asst, "_speak_exact"):
                asst._speak_exact(text, kind="prompt")
        except Exception as exc:
            log.debug(f"activity check-in speak failed: {exc}")

    def _reflect_and_reset_session(self) -> None:
        """End-of-session hook: summarize buffered exchanges into
        long-term memory (background thread — never blocks reconnect),
        then reset the buffer and clear the rolling crash-recovery file
        so those exchanges are not replayed on the next startup."""
        asst = self._asst
        exchanges = asst._session_exchanges
        session_start = asst._session_start_ts
        asst._session_exchanges = []
        asst._session_start_ts = datetime.now()

        # Clear the crash-recovery rolling file — the session ended cleanly,
        # so the exchanges are about to be reflected; no need to replay them.
        try:
            import pathlib as _pl2
            _pl2.Path(_SESSION_ROLLING_PATH).write_text("", encoding="utf-8")
        except Exception:
            pass

        if not exchanges:
            return

        def _worker():
            try:
                # Late imports: memory modules pull in core at import time
                # (circular if done at module top).
                from memory.reflection import reflect_session, maybe_daily_rollup
                from memory.long_term import decay_sweep as memory_decay_sweep
                reflect_session(exchanges, session_start)
                memory_decay_sweep()
                maybe_daily_rollup()
            except Exception as exc:
                log.debug(f"Session reflection failed (non-fatal): {exc}")

        threading.Thread(target=_worker, name="gama-memory-reflect", daemon=True).start()


__all__ = ["LiveSessionController"]
