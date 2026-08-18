"""
actions/self_diagnostics.py — Gama Self-Healing / Crash Forensics
=======================================================================
Extends actions/reliability.py and removed with a
closed diagnostic loop: when Gama's own process crashes (or logs a
soft/caught error worth remembering), this module

  1. CAPTURES  — writes a structured crash report (traceback, recent
     log tail, active module, timestamp) to logs/crashes/.
  2. MATCHES   — checks the traceback against a small local library of
     known failure signatures (missing dependency, locked audio
     device, stale API key, network drop, etc.) for an instant,
     free, offline diagnosis before ever calling out to Gemini.
  3. SUGGESTS  — for *unmatched* signatures, optionally asks
     removed to draft a plain-English root-cause guess
     and a patch suggestion. This is written to a review file — it is
     NEVER applied automatically. Gama should only mention the
     suggestion exists; a human reviews and applies any code change.
  4. TRACKS    — keeps a rolling crash-frequency counter so main.py /
     restart handling can distinguish "one-off hiccup" (safe to
     self-restart) from "crash-looping" (should stop retrying and
     surface the error clearly instead of restarting forever).

Nothing here auto-edits source files and nothing here auto-executes
suggested patches — this is a diagnostics/reporting layer, not an
autonomous code-modification system, deliberately, given DESTRUCTIVE-
tier actions elsewhere in this project require explicit human
confirmation.

Usage
-----
    # once, near the top of main.py, right after setup_logging():
    from actions.self_diagnostics import install_global_handler
    install_global_handler()

    # anywhere reliability.retry() or a caught Exception is worth
    # remembering (not necessarily fatal):
    from actions.self_diagnostics import record_soft_error
    record_soft_error("media", exc)

    # queried by main.py / restart handling before deciding to
    # auto-relaunch:
    from actions.self_diagnostics import is_crash_looping
    if is_crash_looping():
        ...  # don't keep auto-restarting; tell the user instead

Author : Vineet Machchal
"""

from __future__ import annotations

import json
import re
import sys
import asyncio
import threading
import traceback
from pathlib import Path
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_CRASH_DIR = _BASE_DIR / "logs" / "crashes"
_CRASH_INDEX = _CRASH_DIR / "index.jsonl"

# Crash-loop guard: if this many crashes happen within this window,
# is_crash_looping() flips True so callers stop auto-restarting.
_LOOP_WINDOW_SECONDS = 300.0
_LOOP_THRESHOLD = 3

_recent_crash_times: List[float] = []

# Optional notifier — set via set_notify_callback() so callers (e.g.
# main.py wiring up widgets/data_overlay.py) can react to a crash being
# recorded (e.g. show a holo panel) without this module needing to know
# anything about Qt/UI. Called with the CrashReport; must never raise.
_notify_callback = None


def set_notify_callback(fn) -> None:
    """Register a callable(report: CrashReport) -> None invoked after
    every record_crash()/record_soft_error() call. Exceptions inside
    `fn` are swallowed so a broken UI hook can never mask the original
    crash or crash the diagnostics path itself."""
    global _notify_callback
    _notify_callback = fn


def _notify(report: "CrashReport") -> None:
    if _notify_callback is None:
        return
    try:
        _notify_callback(report)
    except Exception:
        log.debug("[self_diagnostics] notify callback raised — ignored.")


# ---------------------------------------------------------------------------
# Known failure signatures — instant, free, offline diagnosis.
# Each entry: (compiled regex against "ExceptionType: message" + last
# traceback line, human explanation, suggested fix).
# ---------------------------------------------------------------------------
_KNOWN_SIGNATURES: List[tuple] = [
    (
        re.compile(r"ModuleNotFoundError|No module named", re.I),
        "A required Python package isn't installed in this environment.",
        "Run `pip install -r requirements.txt` (or reinstall the missing "
        "package specifically) and restart Gama.",
    ),
    (
        re.compile(r"PortAudioError|no default (input|output) device|sounddevice", re.I),
        "The microphone or speaker device Gama was using disappeared or changed "
        "(e.g. USB headset unplugged, Bluetooth device switched).",
        "Check Windows Sound settings for the intended default input/output "
        "device. voice/device_monitor.py should catch most swaps live; if this "
        "recurs, the device may be dropping out at the OS level.",
    ),
    (
        re.compile(r"invalid api key|401|permission_denied|unauthenticated", re.I),
        "The Gemini API key in config/api_keys.json is missing, invalid, or expired.",
        "Verify \"gemini_api_key\" in config/api_keys.json is current and has "
        "not been revoked in Google AI Studio.",
    ),
    (
        re.compile(r"ConnectionError|TimeoutError|WSServerHandshakeError|getaddrinfo failed", re.I),
        "Network connectivity to the Gemini Live API dropped mid-session.",
        "Usually transient (Wi-Fi drop / sleep-wake). If it repeats, check for "
        "captive portals or a firewall blocking outbound WebSocket traffic.",
    ),
    (
        re.compile(r"being used by another process|WinError 32|PermissionError", re.I),
        "A file Gama needed was locked by another process (often an AV scan or "
        "the app itself still shutting down).",
        "reliability.retry()/is_transient_error() should already retry this — "
        "if it still surfaces, the lock is held longer than the current retry "
        "budget allows.",
    ),
    (
        re.compile(r"sqlite3\.OperationalError.*locked", re.I),
        "The local memory/knowledge SQLite database was locked by a concurrent "
        "write from another Gama thread.",
        "Check for overlapping writers in memory/unified_memory.py — consider "
        "a short WAL-mode busy_timeout if this repeats.",
    ),
    (
        re.compile(r"RecursionError", re.I),
        "A tool-call or automation chain looped back into itself.",
        "Check automation/executor.py's step limit / automation's MAX_STEPS "
        "for the flow that was running when this happened.",
    ),
]


@dataclass
class CrashReport:
    timestamp: float
    module: str
    exc_type: str
    exc_message: str
    traceback_text: str
    diagnosis: Optional[str] = None
    suggested_fix: Optional[str] = None
    matched_known_signature: bool = False
    path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "module": self.module,
            "exc_type": self.exc_type,
            "exc_message": self.exc_message,
            "diagnosis": self.diagnosis,
            "suggested_fix": self.suggested_fix,
            "matched_known_signature": self.matched_known_signature,
            "traceback": self.traceback_text,
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# Core capture + diagnose
# ---------------------------------------------------------------------------
def _match_known_signature(signature_text: str) -> Optional[tuple]:
    for pattern, diagnosis, fix in _KNOWN_SIGNATURES:
        if pattern.search(signature_text):
            return diagnosis, fix
    return None


def _write_report(report: CrashReport) -> None:
    try:
        _CRASH_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"crash_{time.strftime('%Y%m%d_%H%M%S')}_{report.module}.json"
        out_path = _CRASH_DIR / fname
        report.path = str(out_path)
        out_path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        with open(_CRASH_INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": report.timestamp, "module": report.module,
                "exc_type": report.exc_type, "matched": report.matched_known_signature,
                "path": str(out_path),
            }) + "\n")
    except Exception as exc:
        log.warning(f"[self_diagnostics] Could not write crash report: {exc}")


def _maybe_draft_patch_suggestion(report: CrashReport) -> None:
    """For unmatched (novel) failures, ask automation for a best-effort
    root-cause guess. Written to a review file only — never applied."""
    try:
        import json as _json
        from google import genai
        api_path = _BASE_DIR / "config" / "api_keys.json"
        with open(api_path, "r", encoding="utf-8") as f:
            api_key = _json.load(f).get("gemini_api_key", "")
        if not api_key:
            return
        client = genai.Client(api_key=api_key)
        prompt = (
            "You are Gama's self-diagnostics module. A crash occurred in the "
            f"module '{report.module}'. Exception: {report.exc_type}: "
            f"{report.exc_message}\n\nTraceback:\n{report.traceback_text[-2500:]}\n\n"
            "In under 120 words: (1) most likely root cause, (2) a concrete "
            "suggested code-level fix or mitigation. This is advisory only — "
            "it will be shown to a human developer for review, not applied "
            "automatically. Be specific to this codebase where you can infer "
            "structure from the traceback (file/function names)."
        )
        response = client.models.generate_content(model="gemini-3.5-flash-lite", contents=prompt)
        suggestion = (response.text or "").strip()
        if suggestion:
            report.suggested_fix = suggestion
            review_dir = _CRASH_DIR / "review"
            review_dir.mkdir(parents=True, exist_ok=True)
            fname = f"suggestion_{time.strftime('%Y%m%d_%H%M%S')}_{report.module}.md"
            (review_dir / fname).write_text(
                f"# Crash suggestion — {report.module}\n\n"
                f"**When:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.timestamp))}\n\n"
                f"**Exception:** `{report.exc_type}: {report.exc_message}`\n\n"
                f"## Gemini's suggestion (unreviewed — do not apply blindly)\n\n{suggestion}\n\n"
                f"## Traceback\n```\n{report.traceback_text}\n```\n",
                encoding="utf-8",
            )
    except Exception as exc:
        log.debug(f"[self_diagnostics] Patch-suggestion draft skipped: {exc}")


def record_crash(
    exc: BaseException,
    module: str = "unknown",
    tb_text: Optional[str] = None,
    draft_suggestion: bool = True,
    **extra: Any,
) -> CrashReport:
    """Capture a hard crash. Safe to call from a top-level exception
    handler / sys.excepthook."""
    now = time.time()
    tb_text = tb_text or "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    signature_text = f"{type(exc).__name__}: {exc}\n{tb_text[-500:]}"

    match = _match_known_signature(signature_text)
    report = CrashReport(
        timestamp=now,
        module=module,
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        traceback_text=tb_text,
        diagnosis=match[0] if match else None,
        suggested_fix=match[1] if match else None,
        matched_known_signature=bool(match),
        extra=extra,
    )
    _write_report(report)

    _recent_crash_times.append(now)
    cutoff = now - _LOOP_WINDOW_SECONDS
    while _recent_crash_times and _recent_crash_times[0] < cutoff:
        _recent_crash_times.pop(0)

    if not match and draft_suggestion:
        _maybe_draft_patch_suggestion(report)

    log.error(
        f"[self_diagnostics] Crash captured in '{module}': {report.exc_type}: "
        f"{report.exc_message}"
        + (f" — known cause: {report.diagnosis}" if match else " — no known signature, drafting suggestion")
    )
    _notify(report)
    return report


def record_soft_error(module: str, exc: BaseException, **extra: Any) -> CrashReport:
    """Capture a caught-and-handled error that's still worth remembering
    (e.g. a retry() exhaustion in reliability.py) without treating it as
    a process-fatal crash for loop-detection purposes."""
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    signature_text = f"{type(exc).__name__}: {exc}\n{tb_text[-500:]}"
    match = _match_known_signature(signature_text)
    report = CrashReport(
        timestamp=time.time(),
        module=module,
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        traceback_text=tb_text,
        diagnosis=match[0] if match else None,
        suggested_fix=match[1] if match else None,
        matched_known_signature=bool(match),
        extra={**extra, "soft": True},
    )
    _write_report(report)
    _notify(report)
    return report


def is_crash_looping(threshold: int = _LOOP_THRESHOLD, window_seconds: float = _LOOP_WINDOW_SECONDS) -> bool:
    """True if Gama has hard-crashed `threshold`+ times within the last
    `window_seconds` — signal to stop auto-restarting and surface the
    problem to the user instead of looping forever."""
    now = time.time()
    cutoff = now - window_seconds
    recent = [t for t in _recent_crash_times if t >= cutoff]
    return len(recent) >= threshold


def recent_crash_summary(limit: int = 5) -> List[Dict[str, Any]]:
    """Read back the last few crash-index entries (cheap, for a status
    query like 'how have you been holding up' or a startup HUD note)."""
    if not _CRASH_INDEX.exists():
        return []
    try:
        lines = _CRASH_INDEX.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(l) for l in lines[-limit:]]
    except Exception:
        return []


_GLOBAL_HANDLER_INSTALLED = False


def install_global_handler() -> None:
    """Install a process-wide sys.excepthook that captures any truly
    unhandled exception before the process dies, so main.py's own crash
    (not caught anywhere else) still gets a forensic report and the
    crash-loop counter still increments. Call once, early in main.py.

    Idempotent: a second call (e.g. an accidental module double-import)
    is a no-op — otherwise every re-install would chain another hook onto
    the previous one and log a misleading duplicate line."""
    global _GLOBAL_HANDLER_INSTALLED
    if _GLOBAL_HANDLER_INSTALLED:
        return
    _GLOBAL_HANDLER_INSTALLED = True
    _prev_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            if exc_value is not None:
                record_crash(exc_value, module="main", draft_suggestion=True)
        except Exception:
            pass  # diagnostics must never mask or worsen the original crash
        _prev_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
    install_threading_exception_hook()
    log.info("[self_diagnostics] Global crash handler installed.")


__all__ = [
    "CrashReport", "record_crash", "record_soft_error",
    "is_crash_looping", "recent_crash_summary", "install_global_handler",
]


def _write_emergency_crash(kind: str, exc_type, exc_value, exc_tb) -> None:
    """Write a crash report directly to disk, bypassing async logging."""
    try:
        root = Path(__file__).resolve().parent.parent
        crash_dir = root / "logs" / "crashes"
        crash_dir.mkdir(parents=True, exist_ok=True)
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = crash_dir / f"crash_{stamp}_{kind}.log"
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        path.write_text(
            f"GAMA CRASH\n"
            f"Time: {__import__('datetime').datetime.now().isoformat()}\n"
            f"Kind: {kind}\n\n{text}",
            encoding="utf-8",
        )
    except Exception:
        pass


def _fatal_hook(exc_type, exc_value, exc_tb, kind="main"):
    _write_emergency_crash(kind, exc_type, exc_value, exc_tb)
    try:
        # Use the normal diagnostic logger too, if available.
        log = globals().get("logger") or globals().get("log")
        if log:
            log.critical("Unhandled %s exception:\n%s", kind,
                         "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    except Exception:
        pass
    try:
        from utils.logger import flush_and_stop_logging
        flush_and_stop_logging()
    except Exception:
        pass


def install_threading_exception_hook():
    """Capture uncaught exceptions from worker threads."""
    def hook(args):
        _fatal_hook(args.exc_type, args.exc_value, args.exc_traceback,
                    f"thread_{args.thread.name}")
    threading.excepthook = hook


def install_asyncio_exception_hook(loop=None):
    """Capture unhandled asyncio task exceptions."""
    loop = loop or asyncio.get_event_loop()
    previous = loop.get_exception_handler()

    def handler(loop, context):
        exc = context.get("exception")
        if exc is not None:
            _fatal_hook(type(exc), exc, exc.__traceback__, "asyncio")
        elif previous:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
