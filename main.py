"""
Gama - Main Entry Point (Mark XLVII style)
==========================================
Real-time voice AI using Gemini Live audio.
Cross-platform. Premium HUD interface.

Author : Vineet Machchal
"""

from __future__ import annotations

import os
import sys

# ── Native math-library thread limits (MUST be set before numpy/BLAS load) ──
# OpenBLAS / MKL / OpenMP default to one thread per core. On Windows that
# often collides with PortAudio + AEC + onnxruntime and ends in:
#   "BLAS : Bad memory allocation"
# or a silent process kill with no Python traceback. Cap them early.
for _k, _v in (
    ("OPENBLAS_NUM_THREADS", "1"),
    ("GOTO_NUM_THREADS", "1"),
    ("OMP_NUM_THREADS", "1"),
    ("MKL_NUM_THREADS", "1"),
    ("NUMEXPR_NUM_THREADS", "1"),
    ("VECLIB_MAXIMUM_THREADS", "1"),
):
    os.environ.setdefault(_k, _v)
# onnxruntime also respects this
os.environ.setdefault("ORT_NUM_THREADS", "1")

import asyncio
import hashlib
import json
import logging
import random
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# Setup paths
from utils.paths import get_base_dir as _get_base_dir


def _get_data_dir() -> Path:
    r"""Writable data (memory, logs, user config) — persists like the dev version.

    Priority:
      1. GAMA_DATA env
      2. %APPDATA%\GAMA on Windows / ~/.gama elsewhere
    """
    env = (os.environ.get("GAMA_DATA") or "").strip()
    if env:
        d = Path(env)
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA") or Path.home()) / "GAMA"
    else:
        d = Path.home() / ".gama"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory").mkdir(parents=True, exist_ok=True)
        (d / "config").mkdir(parents=True, exist_ok=True)
        (d / "logs").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


BASE_DIR = _get_base_dir()
DATA_DIR = _get_data_dir()
os.environ.setdefault("GAMA_DATA", str(DATA_DIR))
os.environ.setdefault("GAMA_HOME", str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))
# Frozen onefile: also search PyInstaller extract dir for bundled modules
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    sys.path.insert(0, str(Path(sys._MEIPASS)))

# Logging
from utils.logger import setup_logging, get_logger
setup_logging()
log = get_logger(__name__)

# Self-healing: capture any unhandled crash (traceback + context) to
# logs/crashes/, match it against known failure signatures, and draft a
# (human-reviewed-only) fix suggestion for anything novel. See
# actions/self_diagnostics.py. Installed as early as possible so it can
# catch crashes during the rest of startup too.
try:
    from actions.self_diagnostics import install_global_handler as _install_crash_handler
    _install_crash_handler()
except Exception as _diag_exc:
    log.warning(f"[startup] self_diagnostics handler not installed: {_diag_exc}")

# Surface crash reports visually via a holo panel (widgets/data_overlay.py)
# once the UI exists. _GAMA_UI_REF is set by GamaClient.__init__ below; the
# notify callback checks it lazily each time since self_diagnostics can fire
# before the UI is constructed (e.g. a startup-path crash).
_GAMA_UI_REF = {"ui": None}

def _on_crash_notify(report) -> None:
    ui = _GAMA_UI_REF.get("ui")
    if ui is None:
        return  # UI not up yet — the crash report file on disk is still there
    ui.show_holo_panel(
        title="CRASH DETECTED",
        rows=[("Module", report.module), ("Type", report.exc_type),
              ("Known cause", "yes" if report.matched_known_signature else "no")],
        body=(report.diagnosis or "No known signature — a fix suggestion was drafted "
              "to logs/crashes/review/ for human review.")[:220],
        accent="#ff4d4d",
        hold_ms=8000,
    )

try:
    from actions.self_diagnostics import set_notify_callback as _set_crash_notify
    _set_crash_notify(_on_crash_notify)
except Exception:
    pass

# PERF: stage timing (wake word / verify / tool / turn totals). See
# utils/perf.py — near-zero overhead, logs slow-stage warnings and can
# print a bottleneck report (`gama.report_perf()` at runtime).
from utils.perf import PerfTimer, turn as _perf_turn, report as _perf_report


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource path (exe-safe)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative
    return BASE_DIR / relative


# Config paths — config is user-writable, so it lives next to the .exe
# (or in the project root when running from source). The prompt is
# read-only, so it can be bundled inside the .exe.
# Prefer user data config; seed from install tree on first run
_API_USER = DATA_DIR / "config" / "api_keys.json"
_API_INSTALL = BASE_DIR / "config" / "api_keys.json"
if not _API_USER.exists():
    try:
        import shutil as _shutil
        if _API_INSTALL.exists():
            _shutil.copy2(_API_INSTALL, _API_USER)
    except Exception:
        pass
API_CONFIG_PATH = _API_USER if _API_USER.exists() else _API_INSTALL

# Audio config (Mark XLVII style)
# Model roles (JARVIS redesign):
#   LIVE_MODEL — Gemini 2.5 Flash Native Audio Preview is the ONLY conversational
#                and reasoning model. No secondary routing LLM (Flash-Lite,
#                Groq, Llama, etc.). Python owns assistant state; Gemini
#                never manages BOOT / OBSERVE / ACTIVE / DEEP_SLEEP.
LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
# Fast text router for deterministic tool selection (not conversation).
ROUTING_MODEL = "gemini-2.0-flash-lite"

# Rolling JSONL file for crash-safe session persistence.
# Exchanges are appended here after every turn so reflect_session() can
# be replayed on next startup if Gama crashed before the session ended.
_SESSION_ROLLING_PATH = "memory/session_rolling.jsonl"
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
# Smaller chunk = lower latency (but more API calls).
# 512 samples = 32ms at 16kHz — fast enough for real-time conversation.
CHUNK_SIZE = 512

# Legacy interrupt thresholds + calibration (isolated from main — Phase 1)
from core.interrupt_calibration import *  # noqa: F401, F403
from core.interrupt_calibration import (  # explicit for static checkers
    apply_interrupt_calibration,
    _load_calibration_profile_for_active_mic,
    _looks_like_echo,
)

def _get_api_key() -> str:
    """Return the Gemini API key from the config manager (validates online config only)."""
    from core.config_manager import config as _cfg
    return _cfg.gemini_key()


# Prompt loading lives in core/prompt_loader.py so controllers can use it
# without importing main (which re-executes this module's side effects —
# see that module's docstring). Kept as a local name for existing callers.
from core.prompt_loader import load_system_prompt as _load_system_prompt  # noqa: E402


# ---------------------------------------------------------------------------
# Tool dispatcher — routes Gemini tool calls to action modules
# ---------------------------------------------------------------------------
# PERF: Most action modules are only ever touched if the user actually
# invokes that specific command (e.g. whatsapp_sender, browser_control).
# Importing all of them eagerly at process start forces every transitive
# dependency (cv2, playwright/selenium, pyautogui, smtplib helpers, etc.)
# to load before
# the UI can even appear. `_lazy_import` defers the real `import` until
# the function is first called; after that, Python's own sys.modules
# cache makes every later call as cheap as the eager version, so behavior
# is unchanged — only *when* the cost is paid changes.
import importlib


from utils.lazy import lazy_import as _lazy_import


# Needed immediately (constructed/called during startup or every turn) —
# kept as normal eager imports.
from actions.class_schedule    import start_watcher as start_class_watcher
from actions.system_info import get_system_status

class SystemMonitor:
    """Stub — background system monitor removed; consumption is in system_info."""
    def __init__(self, *a, **k): pass
    def start(self): pass
    def stop(self): pass
    def check(self): return None
from actions.desktop_context   import (
    desktop_context, init_tracker, summarize_for_prompt,
)
from actions.desktop_notify import (
    configure as configure_desktop_notify, desktop_notify,
)
from actions.calendar_action import calendar_action
from security import security_manager as security_gate
from memory.memory_manager     import (
    update_memory, format_memory_for_prompt,
)
from memory.context_builder    import build_session_context, recall as memory_recall, remember_fact, forget_fact, recall_for_prompt
from memory.reflection         import reflect_session, maybe_daily_rollup
from memory.long_term          import decay_sweep as memory_decay_sweep
from wake_word import WakeWordListener, load_wake_word_config

# --- Gama 2.0 Full-Duplex Voice Engine ------------------------------------
# execution_narrator: event-driven task->speech narration ("I'm verifying
#   that everything copied correctly.", "Done, sir.") — subscribes itself
#   to the shared Event Bus once started, no further wiring needed.
# audio_coordinator: single place SpeechStarted/SpeechCompleted/
#   SpeechInterrupted get published from (see _set_speaking below).
# full_duplex_manager: zero-LLM-call shortcuts for direct status
#   questions ("what are you doing?") and task control ("Pause.",
#   "Resume.", "Stop.") — tried before falling through to Gemini.
# See INTEGRATION.md for the full rationale; all additive.
from voice import execution_narrator
from voice.audio_coordinator import get_coordinator as get_audio_coordinator
from voice.full_duplex_manager import get_manager as get_voice_manager

# ── Tool dispatch layer (extracted to core/tool_dispatch.py, C3 refactor) ──
# Owns every action-module binding + _execute_tool / _register_tools /
# _execute_tool_impl. Re-import the handful of names main.py's class body
# still calls directly (fast-intent shortcuts, enrollment gating, etc.)
# plus `set_active_assistant` so main() can register the live instance.
from core import tool_dispatch as _tool_dispatch
from core.tool_dispatch import (
    set_active_assistant,
    _execute_tool,
    open_app,
    system_info,
)

# ── Static tool data (extracted to core/tool_declarations.py) ──────────────
from core.tool_declarations import PROCESSING_ACK_LINES, TOOL_DECLARATIONS, get_filtered_declarations

# ── Spoken-text sanitization utilities (extracted to core/text_sanitize.py) ─
from core.text_sanitize import (
    _sanitize_spoken_text,
    _clean_transcript,
    _is_reasoning_echo,
    _explicit_reasoning_requested,
    _dedupe_repeated_sentences,
    _spoken_dedup_check_and_mark,
)


# HUD transcript correction via online Google SpeechRecognition / Sphinx was
# removed from the live receive path (perf audit). Gemini Live STT drives both
# tools and the conversation log; display-only repair must never block audio,
# tool calls, or the next user turn.


# ── GamaAssistant mixins (extracted to core/session_mixins.py, C3 refactor) ─
from core.session_mixins import VoicePreferenceMixin, NotificationMixin
from core.audio_controller import AudioController
from core.session_controller import SessionController
from core.ui_controller import UIController
from core.sleep_controller import SleepController
from core.barge_in_controller import BargeInController
from core.wake_controller import WakeController
from core.audio_stream import AudioStreamController
from core.live_session import LiveSessionController
from core.tool_controller import ToolController


class GamaAssistant(VoicePreferenceMixin, NotificationMixin):
    """Real-time voice assistant using Gemini Live API."""

    # Voice presets — male English/Hindi voices only
    VOICE_PRESETS = {
        "male":   "Charon",   # default — deep, clear, bilingual
        "charon": "Charon",
        "fenrir": "Fenrir",   # warmer
        "orus":   "Orus",     # firm
        "puck":   "Puck",     # younger
    }

    def __init__(self, ui):
        self.ui = ui
        # Controllers (module-level imports — do NOT re-import inside this
        # function or Python treats the names as locals and raises
        # UnboundLocalError on these early assignments).
        self.audio_ctrl = AudioController(self)
        self.session_ctrl = SessionController()
        self.ui_ctrl = UIController(ui)
        self.sleep_ctrl = SleepController(self)
        self.barge_in_ctrl = BargeInController(self)
        self.wake_ctrl = WakeController(self)
        self.audio_stream = AudioStreamController(self)
        self.live_session = LiveSessionController(self)
        self.tool_ctrl = ToolController(self)
        # Aliases used by the rest of the hardening path
        self._audio_ctl = self.audio_ctrl
        self._session_ctl = self.session_ctrl
        self._ui_ctl = self.ui_ctrl
        self._sleep_ctl = self.sleep_ctrl
        self._barge_in_ctl = self.barge_in_ctrl
        try:
            _GAMA_UI_REF["ui"] = ui  # let the module-level crash notifier reach it
        except Exception:
            pass
        try:
            self.ui.enrollment_speak.connect(self._speak_exact)
        except Exception:
            pass
        self._loop: asyncio.AbstractEventLoop | None = None
        self.session = None
        self.out_queue: "asyncio.Queue | None" = None
        self.audio_in_queue: "asyncio.Queue | None" = None
        # Tracks raw audio chunks already queued in the current turn so
        # Gemini Live re-emissions (common after a tool-call round trip) do
        # not cause the same sentence to be played multiple times.
        self._seen_audio_chunks: set[int] = set()
        # Kept for backwards compatibility; the turn-scoped set above is the
        # primary dedup guard. This catches the trivial back-to-back case
        # even if the set logic is somehow bypassed.
        self._last_audio_chunk: bytes | None = None
        self._speaking = False
        self._speaking_lock = threading.Lock()
        # Reference to the currently-open sd.RawOutputStream in _play_audio(),
        # so barge-in handlers can abort it immediately. sd.stop() (the
        # module-level helper) only affects the implicit default stream used
        # by sd.play()/sd.rec() — it has NO effect on a stream object created
        # explicitly via sd.RawOutputStream(), which is what Gemini Live
        # audio plays through. Without this reference, barge-in only cleared
        # the Python-side queue + flags while PortAudio kept playing whatever
        # was already handed to it — audio that "got interrupted" in the UI
        # but kept audibly playing to the end of the current chunk.
        self._live_out_stream = None
        self._live_out_stream_lock = threading.Lock()
        self._running = False
        # --- Tool-declaration filtering (perf audit item #2) ------------
        # Categories of tools actually called so far this session, used to
        # trim the schema list sent to Gemini Live on *reconnect* (never on
        # the first connect — see _build_config / core.tool_declarations
        # .get_filtered_declarations for the conservative fail-open rules).
        self._connect_count = 0
        self._recent_tool_categories: set[str] = set()
        # --- Local wake word (offline, low-CPU) -------------------------
        # Replaces the old "stream everything to Gemini and check the
        # cloud transcript" approach: a local model gates whether mic
        # audio ever leaves the machine at all. See wake_word/README.md.
        self._wake_cfg = load_wake_word_config()
        self._sys_monitor = SystemMonitor(on_alert=self._on_sys_alert, proactivity_level=self._wake_cfg.proactivity_level)
        # Phase 2 services are deliberately held until GAMA has completed
        # its voice boot.  Starting trackers, learning, health probes and
        # model warmup during __init__ caused the first usable voice session
        # to compete with them for CPU and disk.
        self._phase2_lock = threading.Lock()
        self._phase2_started = False
        self._phase2_device_monitor = None
        self._phase2_warm_light = False
        self._phase2_warm_heavy = False
        self._phase2_warm_light_models = None
        self._phase2_warm_heavy_models = None

        # Constructing the tracker is cheap and preserves existing prompt
        # integrations. Its polling thread starts in _start_phase2_services.
        self._desktop_tracker = init_tracker(on_download=self._on_download_complete)
        self._briefing_sent = False
        self._offline_greeting_sent = False
        # Was this process just relaunched via the restart_self tool?
        # Restart marker handling removed.
        # the session comes back up, instead of a normal cold-start.
        try:
            consume_restart_marker = lambda: None  # removed
            self._just_restarted = consume_restart_marker()
        except Exception:
            self._just_restarted = False
        self._restart_complete_announced = False
        self._shutdown_pending = False
        self._voice_name: str = "Charon"  # default male bilingual voice
        self._wake_listener = None  # loaded async below — model load can take 1-3s+
                                     # and used to block the whole UI thread here.
        self._wake_listener_ready = threading.Event()

        def _load_wake_listener():
            listener = WakeWordListener(self._wake_cfg)
            self._wake_listener = listener
            self._wake_listener_ready.set()
            if listener.available:
                log.info(
                    f"Local wake word engine active (backend={self._wake_cfg.backend}, "
                    f"phrase='{self._wake_cfg.wake_phrase}'). GAMA starts AWAKE."
                )
            else:
                log.warning("Local wake word engine unavailable — GAMA stays awake "
                            "(legacy always-listening behavior).")

        threading.Thread(target=_load_wake_listener, daemon=True,
                          name="gama-wakeword-load").start()

        # Internet monitor removed — Gama connects to Gemini directly on startup.

        # --- Trusted session: best-effort workstation-lock watcher -------
        # Invalidates the trusted session the moment Windows locks, per
        # spec section 3. No-op on non-Windows. See security/trusted_session.py.
        from security import trusted_session
        trusted_session.start_lock_watcher()

        # --- Gama 2.0 Full-Duplex Voice Engine ---------------------------
        # Turns on event-driven task narration (core.task_queue tasks that
        # call report_step()/set_waiting() get spoken automatically — see
        # voice/execution_narrator.py) and grabs the shared coordinator /
        # zero-LLM-call status+control shortcut manager used below in
        # _set_speaking / _on_fast_intent_text. Cheap — just subscribes to
        # the existing Event Bus, doesn't open any audio device itself.
        execution_narrator.start()
        self._audio_coordinator = get_audio_coordinator()
        self._voice_manager = get_voice_manager()

        # --- Performance preset (Fast / Balanced / Full) -------------------
        # GAMA_PERF_MODE=fast|balanced|full  or  config/performance.json
        try:
            from utils.performance_mode import perf as _perf
            self._perf = _perf
            log.info(
                f"[PerfMode] {_perf.name}: {_perf.description}"
            )
        except Exception as _perf_exc:
            log.warning(f"[PerfMode] unavailable ({_perf_exc}) — using balanced defaults.")
            self._perf = None

        # --- Initialize WebRTC AEC processor (sync, before mic opens) ------
        # Created here — before _listen_audio starts — so the mic callback
        # can reference self._aec without any import overhead on the
        # real-time audio thread.  Fails soft: if the package is absent,
        # self._aec.available is False and process() is a no-op passthrough.
        # Fast mode forces AEC/NS/AGC off (thin mic path, lower latency).
        # When all processing is off, skip the processor entirely — no init
        # log spam, no per-chunk process() call.
        try:
            from voice.aec import get_processor as _get_aec
            _aec_on = True
            _ns_on = True
            _agc_on = True
            if self._perf is not None:
                _aec_on = self._perf.aec_enabled
                _ns_on = self._perf.ns_enabled
                _agc_on = self._perf.agc_enabled
            if not _aec_on and not _ns_on and not _agc_on:
                self._aec = None
                log.info("AEC skipped (PerfMode=fast — passthrough).")
            else:
                log.info("Initializing AEC…")
                self._aec = _get_aec(
                    sample_rate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    enable_aec=_aec_on,
                    enable_ns=_ns_on,
                    enable_agc=_agc_on,
                )
                if self._aec.available:
                    log.info("AEC ready (processing active).")
                else:
                    log.info("AEC passthrough (audio-processing package not installed).")
        except Exception as _aec_init_exc:
            log.warning(f"AEC init failed: {_aec_init_exc} — mic will run without echo cancellation.")
            self._aec = None

        # --- Audio device hot-swap monitor --------------------------------
        # Detects default mic / speaker changes and triggers stream restarts
        # without requiring a full application restart. Uses efficient
        # polling (every 2s) — no aggressive busy-loop.
        self._mic_restart_event = threading.Event()  # set by device monitor when mic changes
        # Signals _play_audio to close and reopen the RawOutputStream on the
        # new device (e.g. after a Bluetooth headset connects and becomes default).
        self._output_device_changed = threading.Event()
        try:
            from voice.device_monitor import get_monitor as _get_dev_mon
            _dev_mon = _get_dev_mon()
            # Output device change: signal the Gemini audio stream to reopen.
            # audio stream to reopen on the new device.  Both paths are needed:
            # Gemini uses a long-lived sd.RawOutputStream (handled in _play_audio).
            def _on_output_dev_change(evt):
                log.info(
                    f"[device_monitor] Output device → {evt.new_name!r} "
                    "— signalling Gemini audio stream restart."
                )
                self._output_device_changed.set()
            _dev_mon.on_output_change(_on_output_dev_change)
            # Input device change: signal _listen_audio to reopen the stream.
            def _on_input_dev_change(evt):
                log.info(
                    f"[device_monitor] Mic device → {evt.new_name!r} "
                    "— signalling mic stream restart."
                )
                self._mic_restart_event.set()
                # Auto-load the new microphone's calibration profile, if
                # one exists; otherwise fall back to the generic defaults
                # and let the user know they can (re)calibrate.
                try:
                    from state_engine import mic_profiles as _mic_profiles
                    profile = _mic_profiles.get_profile(evt.new_name)
                    if profile and profile.get("params"):
                        apply_interrupt_calibration(profile["params"])
                        log.info(f"[device_monitor] Applied calibration profile for {evt.new_name!r}.")
                    else:
                        log.info(
                            f"[device_monitor] No calibration profile for {evt.new_name!r} — "
                            "using default interruption tuning until calibrated."
                        )
                except Exception as _cal_exc:
                    log.debug(f"[device_monitor] Calibration reload skipped: {_cal_exc}")
            _dev_mon.on_input_change(_on_input_dev_change)
            # The callbacks are registered now so no device event is missed
            # once Phase 2 begins; the polling thread itself is deferred.
            self._phase2_device_monitor = _dev_mon
        except Exception as _mon_exc:
            log.debug(f"[device_monitor] Init skipped (non-fatal): {_mon_exc}")

        log.info("Post-device-monitor init continuing…")

        # --- Model warmup — light ON by default, heavy OFF by default -------
        # Heavy tier previously warmed Whisper + speaker-ID (both removed).
        # Light tier still warms VAD + TTS.
        #
        # Warmup itself always runs on a background daemon thread, so it
        # never blocks startup or the UI — the old opt-in gate was trading
        # away latency for idle RAM without actually needing to. It's now
        # split into two tiers so low-RAM machines can still trim the
        # heavier tier without going back to fully-cold models:
        #   • "light" tier (VAD + Gemini TTS warmup): always on unless disabled.
        #   • "heavy" tier: removed (Whisper / speaker-ID no longer in tree).
        def _warm_light_models():
            try:
                from voice.vad import SileroVAD
                SileroVAD()  # loads the shared ONNX VAD session once
            except Exception as exc:
                log.debug(f"VAD warmup skipped: {exc}")
            try:
                from voice import tts_engine as _tts
                _tts.warmup()
            except Exception as exc:
                log.debug(f"Local TTS engine warmup skipped: {exc}")

        def _warm_heavy_models():
            # Heavy models (Whisper / speaker verification) removed.
            pass
        import os as _os

        def _env_flag(name: str, default: bool) -> bool:
            raw = _os.environ.get(name, "").strip().lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")

        # Back-compat: the old GAMA_WARMUP_MODELS=1 opt-in still works as an
        # explicit "warm everything" override if someone has it set.
        _legacy_opt_in = _os.environ.get("GAMA_WARMUP_MODELS", "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        _warm_light = _env_flag("GAMA_WARMUP_LIGHT", True)
        _warm_heavy = _env_flag("GAMA_WARMUP_HEAVY", False) or _legacy_opt_in

        self._phase2_warm_light = _warm_light
        self._phase2_warm_heavy = _warm_heavy
        self._phase2_warm_light_models = _warm_light_models
        self._phase2_warm_heavy_models = _warm_heavy_models
        if not _warm_light and not _warm_heavy:
            log.info(
                "Voice model warmup fully disabled (GAMA_WARMUP_LIGHT=0, "
                "GAMA_WARMUP_HEAVY=0) — first command each session will "
                "pay full model-load latency."
            )
        elif not _warm_heavy:
            log.info(
                "Heavy model warmup disabled (feature stack removed)."
            )

        # --- Optional periodic perf report (GAMA_DEBUG_PERF=1) ----------
        # Same opt-in pattern as state_engine/debug_panel.py's
        # GAMA_DEBUG_STATE. Logs a p50/p95 bottleneck table to
        # logs/gama.log every 5 minutes — off by default so normal runs
        # pay zero cost beyond the PerfTimer calls themselves.
        if _os.environ.get("GAMA_DEBUG_PERF", "").strip().lower() in ("1", "true", "yes", "on"):
            def _perf_report_loop():
                from utils.perf import tool_report as _tool_report
                while True:
                    time.sleep(300)
                    log.info("\n" + _perf_report())
                    log.info("\n" + _tool_report())
            threading.Thread(target=_perf_report_loop, daemon=True,
                              name="gama-perf-report").start()
        # Runtime state machine (BOOT → OBSERVE → ACTIVE → DEEP_SLEEP).
        # Python owns this; Gemini never does. See core/assistant_runtime.py.
        from core.assistant_runtime import runtime as _runtime
        self._runtime = _runtime
        # Compatibility: many existing code paths still read self._awake.
        # Map it onto runtime: ACTIVE (or BOOT greeting) counts as "awake"
        # for speaking/tool gates. OBSERVE streams but does not speak.
        self._awake = False
        self._wake_word = self._wake_cfg.wake_phrase  # for legacy cloud-side detection
        # Full accepted wake-phrase set (e.g. "gama" and "wake up gama")
        # used by the cloud-transcript/typed-input fallback paths below —
        # kept in sync with whatever the local Vosk engine accepts.
        self._wake_phrases = set(
            p.lower().strip() for p in (self._wake_cfg.wake_phrases or [self._wake_word])
        )
        self._sleep_word = "go to sleep"
        # Isolated-phrase match only — mirrors the wake-word engine's
        # exact-utterance philosophy. A naive substring ("x in txt_lower")
        # check would fire on any sentence that happens to *contain* one
        # of these phrases anywhere (e.g. "don't let it go to sleep on me"
        # or "set the system to sleep" if it ever grew a matching
        # substring) instead of only on the isolated command itself.
        self._sleep_word_re = re.compile(
            r"^(hey |okay |ok )?(gama[,]?\s+)?(go(ing)?\s+to\s+sleep|sleep\s+gama|gama\s+sleep|"
            r"goodnight\s+gama|goodbye\s+gama)[\s,.!]*(gama)?[\s,.!]*$",
            re.IGNORECASE,
        )
        # Set while GAMA briefly wakes ONLY to speak a reminder/timer/alarm/
        # class-reminder that fired while asleep. During this window mic
        # audio is still never forwarded to Gemini (no commands are heard,
        # only "wake up gama" can truly wake GAMA up) — once the
        # announcement finishes, GAMA drops straight back to sleep.
        self._announcing_while_asleep = False
        # Barge-in: when user interrupts, we need to flush the audio queue
        self._interrupt_event = asyncio.Event()
        # True between "wake word detected" and "speaker verification done".
        # Audio is NOT forwarded to Gemini during this window — this kills
        # false-wake responses (e.g. Hindi speech triggering Vosk's wake
        # detector while GAMA is asleep). _awake stays False until the
        # verification result decides whether to truly wake.
        self._wake_verifying: bool = False

        # ── Active Window (soft only) ─────────────────────────────────────
        # When-to-speak is handled by Live Proactive Audio + system prompt.
        # Local auto-standby on silence is disabled (_schedule_auto_sleep no-op).
        from core.assistant_runtime import ACTIVE_WINDOW_S as _AWS
        self._CONVERSATION_TIMEOUT_S: float = float(_AWS) if _AWS else 300.0
        self._auto_sleep_task: "asyncio.Task | None" = None
        self._runtime_tick_task: "asyncio.Task | None" = None
        self._boot_greeting_pending: bool = False
        # Tools stay disarmed until the first clear user utterance after boot
        # (or until this monotonic deadline). Stops Gemini inventing
        # computer_agent / open_app from noise right after the greeting.
        self._tools_armed_after: float = time.monotonic() + 8.0

        # ── Conversation Session Manager (JARVIS-style follow-ups) ────────
        # Tracks PASSIVE vs ACTIVE conversation state independently of the
        # blunt `self._awake` flag above, classifies every transcript as
        # directed-at-Gama / self-talk / human-to-human / unknown, and owns
        # the 10-15s adaptive inactivity window used for follow-up commands
        # that skip the wake word. See voice/session_manager.py.
        from voice.session_manager import get_session_manager
        self._session_mgr = get_session_manager()

        # Configure controllers now that wake phrases / locks exist.
        # (Instances were created at the top of __init__; do not import
        # AudioController/SessionController/UIController again here.)
        try:
            self.session_ctrl.wake_phrases = set(
                getattr(self, "_wake_phrases", set()) or set()
            ) | set(self.session_ctrl.wake_phrases)
        except Exception:
            pass
        try:
            self.audio_ctrl.attach(self)
        except Exception:
            pass
        from core.speech_authority import speech_authority
        # Single speaker = Gemini Live only. Acks/alerts/completions wait
        # until neither Gama nor the user is speaking.
        def _gama_speaking() -> bool:
            try:
                with self._speaking_lock:
                    return bool(self._speaking)
            except Exception:
                return False

        def _user_speaking() -> bool:
            # Recent user transcript activity or voice_activity probe
            try:
                if time.monotonic() - float(getattr(self, "_last_input_transcript_ts", 0) or 0) < 1.2:
                    return True
            except Exception:
                pass
            try:
                return bool(self._voice_activity())
            except Exception:
                return False

        def _is_listening() -> bool:
            return bool(getattr(self, "_awake", False)) and not _gama_speaking()

        def _speak_via_gemini(text: str) -> None:
            loop = getattr(self, "_loop", None)
            if loop is None or not loop.is_running():
                return
            try:
                asyncio.run_coroutine_threadsafe(self._send_system_text(text), loop)
            except Exception as exc:
                log.debug(f"speech_authority inject failed: {exc}")

        speech_authority.bind(
            is_gama_speaking=_gama_speaking,
            is_user_speaking=_user_speaking,
            is_listening=_is_listening,
            speak_via_gemini=_speak_via_gemini,
        )

        # Set while voice enrollment is actively recording from the
        # mic on its own worker thread. Mic->Gemini forwarding is gated off
        # during this window so the two don't fight over the audio device
        # and out_queue doesn't fill up with audio nobody is consuming.
        self._enrolling = False
        self._calibrating = False  # True while the Mic Calibration Wizard owns the mic
        self._enrollment_cancel_event = threading.Event()
        try:
            self.ui.voice_enroll_cancel_clicked.connect(self._enrollment_cancel_event.set)
        except Exception:
            pass
        # Processing-acknowledgement throttle (see _speak_exact call site
        # below): avoids GAMA repeating "one moment"/"processing" every
        # ~500ms during a long multi-tool-call task — only once per
        # 8-10s window, and never if a real result already landed.
        self._last_ack_ts = 0.0
        self._ack_min_interval_s = 9.0
        # Serializes the check-then-set of _last_ack_ts. Without this,
        # concurrent tool calls from the same batch (asyncio.gather in
        # _handle_tool_call) can each read the "interval elapsed" window
        # as true before either has written the new timestamp, so two
        # (sometimes identical) ack lines get spoken back to back.
        self._ack_lock = threading.Lock()

        # Duplicate function-call guard: Gemini Live occasionally re-sends
        # the same function_call (identical id, or identical name+args)
        # within one tool_call batch, or fires it again on the very next
        # turn after a brief reconnect replays a queued message. Neither
        # case was previously deduped, so the tool actually ran twice and
        # — since a real ("result") response speaks the outcome — GAMA
        # said the same completion sentence twice. Track recently-seen
        # calls (by id when present, else name+args) and skip repeats
        # inside this short window instead of re-executing/re-speaking.
        self._recent_tool_calls: dict[str, float] = {}
        # H3 fix: asyncio.Lock instead of threading.Lock.
        # _execute_single_tool_call is an async coroutine; holding a
        # threading.Lock across an await boundary blocks the event loop
        # thread and can deadlock if another coroutine tries to acquire it
        # while the first is suspended at the await. asyncio.Lock is
        # cooperative: it yields to the event loop while waiting, so no
        # deadlock and no thread blocking.
        self._recent_tool_calls_lock = asyncio.Lock()
        self._tool_call_dedup_window_s = 4.0

        # --- Rolling raw-audio buffer for voice verification -------------
        # A short (~4s) ring buffer of the most recent raw mic PCM, used
        # by actions/security.py to verify the speaker's identity before
        # running sensitive actions (shutdown, delete, terminal, etc.).
        # Populated in _listen_audio's callback; never written to disk
        # and never sent anywhere except the local voice_recognition
        # comparison, which itself never leaves the machine either.
        self._voice_buffer = bytearray()
        self._voice_buffer_lock = threading.Lock()
        self._voice_buffer_max_bytes = SEND_SAMPLE_RATE * 2 * 4  # ~4s of int16 mono

        # Local EmotionDetector DISABLED — tone/affect handled by Gemini
        # Live Affective Dialog (enable_affective_dialog=True).
        self._emotion_detector = None
        self._disable_live_advanced_audio = False  # set True after 1007 on proactivity/affective

        # Wake-ack / reconnect stability guards
        # Prevents "Yes, Sir?" firing again while a real reply is still
        # playing or immediately after, and rate-limits model nudges that
        # are a known trigger for Live WebSocket 1011 closes.
        self._last_wake_ack_ts: float = 0.0
        self._wake_ack_cooldown_s: float = 4.0
        self._session_quiet_until: float = 0.0  # monotonic; block system text until
        self._last_1011_ts: float = 0.0
        self._stable_session_ts: float = 0.0  # set on successful connect

        # Clean, VAD-bounded PCM of the most recent owner-verified utterance
        # (set in _on_local_transcript), plus when it was captured. Preferred
        # over the raw rolling `_voice_buffer` for security-gate re-checks —
        # see _handle_tool_call.
        self._last_verified_pcm: Optional[bytes] = None
        self._last_verified_pcm_ts: float = 0.0
        # Real transcript text of the most recent thing the user said,
        # used to independently verify DESTRUCTIVE verbal confirmations
        # rather than trusting a boolean the tool-calling model supplies.
        self._last_input_transcript: str = ""
        self._last_input_transcript_ts: float = 0.0
        # Pre-started confirmation-intent classification task (#1 perf improvement).
        # Launched the moment a user transcript arrives so the result is ready
        # (or nearly ready) by the time the security gate queries it, cutting
        # ~1,200–1,800ms off every SENSITIVE/DESTRUCTIVE verbal confirmation.
        self._pending_verbal_intent_task: "asyncio.Task | None" = None

        # Set whenever a [PROACTIVE_SUGGESTION] alert is delivered (see
        # _on_sys_alert), cleared the moment the user actually says/types
        # anything back. While True, launch-capable tools are blocked in
        # _execute_single_tool_call — proactive nudges must only ever be
        # *offered*, never auto-executed, even if the model tries to call
        # a tool straight off the back of its own suggestion text.
        self._proactive_awaiting_confirmation: bool = False

        # Dedicated single-worker thread pool for the blocking speaker
        # stream.write() call in _play_audio. Previously this used
        # asyncio.to_thread(), which borrows from the process-wide default
        # executor shared with every other to_thread call in the app —
        # if that pool is saturated by background work, audio playback
        # could stutter waiting for a free worker. A dedicated 1-thread
        # pool means playback writes are never queued behind unrelated work.
        from concurrent.futures import ThreadPoolExecutor as _TPE
        self._audio_out_executor = _TPE(max_workers=1, thread_name_prefix="audio-out")
        self._last_verified_pcm_freshness_s: float = 8.0

        # --- New parallel local voice pipeline (VAD -> Whisper + WeSpeaker) ---
        # Additive: runs alongside Gemini Live's own audio path (unchanged),
        # never blocks it. Feeds off the exact same mic callback below.
        # See voice/pipeline.py for the architecture; local transcripts are
        # currently only logged (self._on_local_transcript) — routing them
        # into the command path is a deliberate separate step so Gemini
        # Live's conversational behavior stays exactly as it was.
        # Local Whisper pipeline disabled — use Gemini Live cloud transcription only.
        # This removes CPU contention on the real-time mic path (major latency source).
        self._voice_pipeline = None

        # --- Owner-only smart interruption state ---
        self._interrupt_hot_frames = 0
        self._last_user_voice_ts: float = 0.0  # monotonic: last time mic heard human speech energy
        self._last_mic_rms: float = 0.0
        self._interrupt_fast_tier = False   # True while the current hot-frame run has been uniformly loud
        self._interrupt_check_inflight = False
        self._interrupt_env_mic: list = []   # recent mic amplitude samples (hot-frame window)
        self._interrupt_env_tts: list = []   # paired recent TTS/Gemini output envelope samples
        self._last_barge_in_ts: float = 0.0      # monotonic time of last barge-in
        self._barge_in_suppress_until: float = 0.0  # suppress ASR-triggered barge-in after mode changes
        # Rolling buffer of overheard lines while in OBSERVE (silent understanding).
        # Used when the user later addresses Gama so the answer has recent context.
        self._observe_context: list = []
        self._observe_context_max: int = 12
        self._observe_pending_request: Optional[str] = None  # last Q/command overheard in OBSERVE
        self._last_speaking_end_ts: float = 0.0  # monotonic time Gama last went silent
        self._gemini_speaker_rms: float = 0.0    # live RMS of Gemini audio being played

        # Whether the user can interrupt Gama mid-speech at all (Settings →
        # "turn barge-in on/off", persisted in state_engine/user_settings.py).
        # Read once here — cheap flag check in the audio callback below,
        # never a settings/file lookup on the real-time audio thread. Kept
        # in sync live via set_barge_in_enabled().
        try:
            from state_engine.user_settings import get_barge_in_enabled as _get_barge_in_enabled
            self._barge_in_enabled = _get_barge_in_enabled()
        except Exception:
            self._barge_in_enabled = True

        # --- Mid-action barge-in: track a task auto-paused by an
        # interruption so we can offer "resume or abort?" once the
        # interrupting turn is done, instead of leaving it silently
        # paused forever. See _immediate_barge_in / _offer_paused_task_followup.
        self._barge_in_paused_task_id: Optional[str] = None
        self._barge_in_paused_task_name: str = ""
        self._barge_in_followup_offered: bool = False

        # --- Long-term memory: per-session exchange buffer -------------
        # Exchanges accumulate here during a live session and are handed
        # to reflect_session() (summarize + auto-extract facts) once the
        # session ends — see memory/reflection.py. This replaces the old
        # approach of dumping raw conversation text into the prompt.
        self._session_exchanges: list[str] = []
        self._session_start_ts = datetime.now()
        try:
            memory_decay_sweep()
            maybe_daily_rollup()
        except Exception as exc:
            log.debug(f"Startup memory maintenance skipped: {exc}")

        # ── Crash-recovery: replay any session that didn't get reflected ──
        # If Gama crashed mid-session the rolling JSONL file will still
        # contain exchanges that were never passed to reflect_session().
        # Replay them now so nothing is lost.
        try:
            import pathlib as _pl, json as _js, datetime as _dtt
            _rolling = _pl.Path(_SESSION_ROLLING_PATH)
            if _rolling.exists() and _rolling.stat().st_size > 0:
                _age_h = (
                    _dtt.datetime.now().timestamp() - _rolling.stat().st_mtime
                ) / 3600.0
                if _age_h < 24.0:   # only replay if < 24 h old
                    _crash_exchanges = []
                    _crash_start = _dtt.datetime.now()
                    for _line in _rolling.read_text(encoding="utf-8").splitlines():
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _rec = _js.loads(_line)
                            _crash_exchanges.append(_rec.get("text", ""))
                            if _rec.get("session_start"):
                                try:
                                    _crash_start = _dtt.datetime.fromisoformat(
                                        _rec["session_start"]
                                    )
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    if _crash_exchanges:
                        log.info(
                            f"[crash-recovery] Replaying {len(_crash_exchanges)} "
                            "exchanges from previous session that ended unexpectedly."
                        )
                        def _replay_worker(_ex=_crash_exchanges, _st=_crash_start):
                            try:
                                reflect_session(_ex, _st)
                            except Exception as _e:
                                log.debug(f"Crash-recovery reflection failed: {_e}")
                        threading.Thread(
                            target=_replay_worker,
                            name="gama-crash-reflect",
                            daemon=True,
                        ).start()
                _rolling.write_text("", encoding="utf-8")  # clear after replay
        except Exception as _cr_exc:
            log.debug(f"Crash-recovery check skipped (non-fatal): {_cr_exc}")

        # --- Calendar: sync offline .ics with Google Calendar (if
        # connected) shortly after boot, then keep syncing periodically
        # in the background. No-ops silently if Google Calendar isn't
        # configured/connected — Gama just runs against the local .ics.
        try:
            # Deferred to Phase 2 so calendar/network setup cannot slow
            # the first microphone and Live-session connection.
            pass
        except Exception as exc:
            log.debug(f"Calendar sync startup skipped: {exc}")

    # ---------------------------------------------------------------
    # Voice preference persistence — see core/session_mixins.py (VoicePreferenceMixin)
    # ---------------------------------------------------------------
    # ---------------------------------------------------------------
    # Perf audit item #2 — filter tool declarations by activity.
    # ---------------------------------------------------------------
    def _start_phase2_services(self) -> None:
        """Start non-critical services after the voice path is usable.

        This method is intentionally idempotent.  It runs its work outside
        the asyncio event loop so importing optional subsystems or opening
        their databases can never delay Live audio, tool calls, or playback.
        """
        with self._phase2_lock:
            if self._phase2_started:
                return
            self._phase2_started = True

        def _run() -> None:
            # Leave a small quiet window after "Ready — listening" so a
            # command spoken immediately after startup gets first claim on
            # CPU, disk, and the default executor.
            time.sleep(2.0)
            log.info("Starting Phase 2 background services.")

            try:
                self._desktop_tracker.start()
            except Exception as exc:
                log.debug(f"desktop_context Phase 2 start skipped: {exc}")

            try:
                from actions.clipboard import start_monitor as _clip_hist_start
                _clip_hist_start()
            except Exception as exc:
                log.debug(f"clipboard Phase 2 start skipped: {exc}")

            try:
                from core import activity_sentinel as _act_sent
                _act_sent.configure(on_checkin=self._on_activity_checkin)
                _act_sent.start()
            except Exception as exc:
                log.debug(f"activity_sentinel Phase 2 start skipped: {exc}")

            try:
                configure_desktop_notify()
            except Exception as exc:
                log.debug(f"desktop_notify Phase 2 configure skipped: {exc}")

            try:
                start_class_watcher()
                from actions.goal_tracker import start_goal_watcher
                start_goal_watcher()
                self._sys_monitor.start()
            except Exception as exc:
                log.debug(f"scheduled monitoring Phase 2 start skipped: {exc}")

            try:
                from learning.habit_tracker import init as _init_habit_tracker
                _init_habit_tracker()
            except Exception as exc:
                log.debug(f"habit_tracker Phase 2 init skipped: {exc}")

            try:
                from core.jarvis_bootstrap import bootstrap_jarvis
                bootstrap_jarvis()
                from state_engine.event_bus import event_bus as _jarvis_bus
                _jarvis_bus.subscribe("GamaNotification", self._on_jarvis_notification)
                try:
                    from core.health_monitor import health_monitor as _hm_init
                    _hm_init.register(
                        name="gemini_live",
                        probe=lambda: self.session is not None,
                        poll_interval=15.0,
                    )
                    _hm_init.register(
                        name="workflow_learner",
                        probe=lambda: __import__(
                            "learning.workflow_learner", fromlist=["workflow_learner"]
                        ).workflow_learner is not None,
                        poll_interval=60.0,
                    )
                except Exception as exc:
                    log.debug(f"Health Monitor Phase 2 setup skipped: {exc}")
            except Exception as exc:
                log.debug(f"jarvis_bootstrap Phase 2 skipped: {exc}")

            try:
                if self._phase2_device_monitor is not None:
                    self._phase2_device_monitor.start()
            except Exception as exc:
                log.debug(f"device_monitor Phase 2 start skipped: {exc}")

            try:
                if self._phase2_warm_light and self._phase2_warm_light_models:
                    self._phase2_warm_light_models()
                if self._phase2_warm_heavy and self._phase2_warm_heavy_models:
                    self._phase2_warm_heavy_models()
            except Exception as exc:
                log.debug(f"voice model Phase 2 warmup skipped: {exc}")

            log.info("Phase 2 background services started.")

        threading.Thread(target=_run, daemon=True, name="gama-phase2").start()

    def _select_tool_declarations(self) -> list[dict]:
        return self.live_session._select_tool_declarations()

    def _build_config(self):
        return self.live_session._build_config()

    async def run(self):
        return await self.live_session.run()

    def _setup_live_vision_sender(self) -> None:
        return self.live_session._setup_live_vision_sender()

    async def _send_system_text(self, text: str) -> None:
        return await self.live_session._send_system_text(text)

    async def _send_user_text(self, text: str) -> None:
        return await self.live_session._send_user_text(text)

    async def _send_realtime(self):
        return await self.audio_stream._send_realtime()

    def _on_local_transcript(self, result) -> None:
        return self.wake_ctrl._on_local_transcript(result)

    def _on_local_unauthorized(self, result) -> None:
        return self.wake_ctrl._on_local_unauthorized(result)

    async def _listen_audio(self):
        return await self.audio_stream._listen_audio()

    def _sync_clap_arm(self) -> None:
        return self.wake_ctrl._sync_clap_arm()

    def _on_wake_engine_label(self, label: str) -> None:
        return self.wake_ctrl._on_wake_engine_label(label)

    async def _speak_startup_greeting(self) -> None:
        return await self.live_session._speak_startup_greeting()

    def _finish_boot_to_observe(self) -> None:
        return self.live_session._finish_boot_to_observe()

    async def _runtime_tick_loop(self) -> None:
        return await self.live_session._runtime_tick_loop()

    async def _send_wake_ack(self) -> None:
        return await self.wake_ctrl._send_wake_ack()

    def _on_fast_intent_text(self, text: str, verified: bool = True) -> None:
        return self.wake_ctrl._on_fast_intent_text(text, verified)

    async def _route_with_flash_lite(self, text: str) -> None:
        return await self.tool_ctrl._route_with_flash_lite(text)

    async def _send_offline_wake_greeting(self, verified_owner: bool = False) -> None:
        return await self.wake_ctrl._send_offline_wake_greeting(verified_owner)

    async def _send_wake_greeting(self, verified_owner: bool = False) -> None:
        return await self.wake_ctrl._send_wake_greeting(verified_owner)

    def _is_first_wake_today(self) -> bool:
        return self.wake_ctrl._is_first_wake_today()

    async def _send_daily_briefing(self) -> None:
        return  # daily_briefing removed

    def _hard_stop_speaker(self) -> None:
        return self.barge_in_ctrl._hard_stop_speaker()

    def _immediate_barge_in(self) -> None:
        return self.barge_in_ctrl._immediate_barge_in()

    def _offer_paused_task_followup(self) -> None:
        return self.barge_in_ctrl._offer_paused_task_followup()

    def _clear_barge_in_task_state(self) -> None:
        return self.barge_in_ctrl._clear_barge_in_task_state()

    def _is_offline(self) -> bool:
        return self.live_session._is_offline()

    def _awake_state(self) -> str:
        return self.live_session._awake_state()

    async def _system_stats_loop(self) -> None:
        return await self.live_session._system_stats_loop()

    async def _receive_audio(self):
        return await self.live_session._receive_audio()

    async def _play_audio(self):
        return await self.audio_stream._play_audio()

    async def _execute_single_tool_call(self, fc):
        return await self.tool_ctrl._execute_single_tool_call(fc)

    async def _ada_bg_tool(self, fc) -> None:
        return await self.tool_ctrl._ada_bg_tool(fc)

    async def _handle_tool_call(self, tool_call):
        return await self.tool_ctrl._handle_tool_call(tool_call)

    def _set_speaking(self, value: bool, interrupted: bool = False):
        return self.audio_stream._set_speaking(value, interrupted)

    def _schedule_auto_sleep(self) -> None:
        return self.sleep_ctrl._schedule_auto_sleep()

    def _cancel_auto_sleep(self) -> None:
        return self.sleep_ctrl._cancel_auto_sleep()

    def _flush_playback(self, reason: str = "") -> None:
        return self.barge_in_ctrl._flush_playback(reason)

    def _enter_observe_mode(self, reason: str) -> None:
        return self.sleep_ctrl._enter_observe_mode(reason)

    def _enter_deep_sleep(self, reason: str) -> None:
        return self.sleep_ctrl._enter_deep_sleep(reason)

    def _enter_sleep_mode(self, reason: str) -> None:
        return self.sleep_ctrl._enter_sleep_mode(reason)

    def _voice_activity(self) -> bool:
        return self.sleep_ctrl._voice_activity()

    async def _auto_sleep_after_timeout(self) -> None:
        return await self.sleep_ctrl._auto_sleep_after_timeout()

    def _save_conversation(self, user_text: str, gama_text: str) -> None:
        return self.live_session._save_conversation(user_text, gama_text)

    def _on_activity_checkin(self, message: str) -> None:
        return self.live_session._on_activity_checkin(message)

    def _reflect_and_reset_session(self) -> None:
        return self.live_session._reflect_and_reset_session()

    def _wake_gama(self) -> None:
        return self.sleep_ctrl._wake_gama()

    def _record_observe_context(self, text: str) -> None:
        return self.wake_ctrl._record_observe_context(text)

    def _looks_like_pending_request(self, text: str) -> bool:
        return self.wake_ctrl._looks_like_pending_request(text)

    def _is_direct_address(self, text: str) -> bool:
        return self.wake_ctrl._is_direct_address(text)

    async def _answer_pending_observe_request(self, pending: str) -> None:
        return await self.wake_ctrl._answer_pending_observe_request(pending)

    async def _wake_from_direct_address(self, user_text: str) -> None:
        return await self.wake_ctrl._wake_from_direct_address(user_text)

    def _speak_exact(self, text: str, priority=None, kind: str = "prompt",
                      blocking: bool = False) -> None:
        return self.live_session._speak_exact(text, priority=priority, kind=kind, blocking=blocking)

    def _enrollment_speak_sync(self, ui, text: str) -> None:
        return self.live_session._enrollment_speak_sync(ui, text)

    def set_barge_in_enabled(self, enabled: bool) -> None:
        return self.barge_in_ctrl.set_barge_in_enabled(enabled)

    def _speak_via_session(self, text: str) -> None:
        return self.live_session._speak_via_session(text)

    async def _send_briefing(self):
        return await self.live_session._send_briefing()

    async def _send_restart_complete(self):
        return await self.live_session._send_restart_complete()

    def send_text(self, text: str):
        return self.live_session.send_text(text)

    def schedule_shutdown(self) -> None:
        return self.live_session.schedule_shutdown()

    def stop(self):
        return self.live_session.stop()

def main() -> int:
    """Boot Gama — Web UI only (Qt desktop UI removed).

    Primary frontend is the React HUD (web_ui/). Python is backend only.
    """
    log.info("Booting Gama...")
    try:
        try:
            from utils.adaptive_perf import governor
            governor.start()
        except Exception as exc:
            log.warning(f"Adaptive performance governor failed to start: {exc}")

        return _main_web_only()
    except Exception as exc:
        log.exception(f"Fatal error: {exc}")
        return 1


def _main_web_only() -> int:
    """No Qt window — React HUD is the only UI (ws/http :8765)."""
    from ui_headless import HeadlessUI

    log.info("UI mode: WEB ONLY (Qt disabled). Open http://127.0.0.1:5173 or :8765")
    ui = HeadlessUI()
    ui.show()

    assistant = GamaAssistant(ui)
    set_active_assistant(assistant)
    ui.text_command.connect(assistant.send_text)

    try:
        from core.web_bridge import start_web_bridge
        start_web_bridge(assistant, host="127.0.0.1", port=8765)
        log.info("Web bridge started on http://127.0.0.1:8765 (React HUD)")
    except Exception as _wb_exc:
        log.warning(f"Web bridge not started (React HUD offline): {_wb_exc}")

    log.info("Gama ready (headless). Ctrl+C to exit.")
    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        log.info("Shutting down (KeyboardInterrupt).")
    except Exception as exc:
        log.error(f"Assistant crashed: {exc}")
        log.error(traceback.format_exc())
        return 1
    return 0





def _write_startup_crash(exc: BaseException) -> None:
    """Last-resort crash breadcrumb when logging may not flush."""
    import traceback as _tb
    text = f"{type(exc).__name__}: {exc}\n" + _tb.format_exc()
    candidates = []
    try:
        candidates.append(DATA_DIR / "logs" / "startup_crash.txt")
    except Exception:
        pass
    try:
        candidates.append(Path(__file__).resolve().parent / "logs" / "startup_crash.txt")
    except Exception:
        pass
    candidates.append(Path.cwd() / "startup_crash.txt")
    for p in candidates:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            print(f"[GAMA] Startup crash written to {p}", flush=True)
            break
        except Exception:
            continue
    try:
        print(text, flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as _boot_exc:
        try:
            log.exception("Startup crash: %s", _boot_exc)
        except Exception:
            pass
        _write_startup_crash(_boot_exc)
        raise
