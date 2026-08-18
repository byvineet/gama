"""
Gama - Performance Profiling
=============================
Lightweight, always-on stage timing for the voice pipeline.

Usage
-----
    from utils.perf import PerfTimer, timed, turn_report

    with PerfTimer("STT"):
        do_stt()

    @timed("Tool:web_search")
    def web_search(...): ...

    # At the end of a "turn" (wake -> response spoken), print a summary:
    turn_report()

Design notes
------------
- Near-zero overhead when idle: a single `time.perf_counter()` call per
  stage, no locks on the hot path except a short one when recording.
- Keeps a rolling per-stage history (deque, bounded) so we can compute
  p50/p95 instead of only "last value" - one slow tool call shouldn't
  hide behind an average.
- `PerfTimer` stages recorded during a single "turn" (see `turn()`
  context manager) are grouped and logged as one aligned block, matching
  the format asked for in the optimization spec:

      Wake Word.............32 ms
      Recording............420 ms
      STT..................175 ms
      ...
      Total...............1994 ms

  This makes bottlenecks visible in logs/gama.log without needing a
  separate profiler attached.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

log = logging.getLogger("gama.perf")

_HISTORY_LEN = 200          # samples kept per stage for p50/p95
_SLOW_STAGE_MS = {          # stage -> threshold; exceeding it logs a WARNING
    "WakeWord": 200,
    "SpeakerVerify": 400,
    "Intent": 20,
    "STT": 300,
    "LLM": 3000,
    "Tool": 1500,
    "BrowserLaunch": 500,
    "TTS": 300,
}

_lock = threading.Lock()
_history: Dict[str, Deque[float]] = {}

# Stages recorded for the *current* turn on the *current* thread, so a
# wake-word thread and the asyncio-loop thread don't interleave garbage
# into each other's turn report.
_local = threading.local()


def _record(stage: str, ms: float) -> None:
    with _lock:
        dq = _history.setdefault(stage, deque(maxlen=_HISTORY_LEN))
        dq.append(ms)
    threshold = _SLOW_STAGE_MS.get(stage)
    if threshold is not None and ms > threshold:
        log.warning(f"⚠️ SLOW STAGE: {stage} took {ms:.0f} ms (budget {threshold} ms)")
    turn = getattr(_local, "turn_stages", None)
    if turn is not None:
        turn.append((stage, ms))


@contextmanager
def PerfTimer(stage: str):
    """Context manager: time a block of code as `stage`."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _record(stage, (time.perf_counter() - t0) * 1000.0)


def timed(stage: str) -> Callable:
    """Decorator: time a sync function call as `stage`."""
    def deco(fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                _record(stage, (time.perf_counter() - t0) * 1000.0)
        wrapper.__name__ = getattr(fn, "__name__", stage)
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


def atimed(stage: str) -> Callable:
    """Decorator: time an async function call as `stage`."""
    def deco(fn: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                _record(stage, (time.perf_counter() - t0) * 1000.0)
        wrapper.__name__ = getattr(fn, "__name__", stage)
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


@contextmanager
def turn():
    """Group every PerfTimer/@timed call inside this block into one
    aligned "Total" report, logged on exit. Call once per logical
    interaction (wake -> final response), not per stage.
    """
    _local.turn_stages = []
    t0 = time.perf_counter()
    try:
        yield
    finally:
        stages: List[tuple] = getattr(_local, "turn_stages", [])
        total_ms = (time.perf_counter() - t0) * 1000.0
        _local.turn_stages = None
        if stages:
            _log_turn(stages, total_ms)


def _log_turn(stages: List[tuple], total_ms: float) -> None:
    name_w = max(len(s) for s, _ in stages) + 1
    lines = []
    for name, ms in stages:
        dots = "." * max(1, (name_w + 12) - len(name) - len(f"{ms:.0f} ms"))
        lines.append(f"{name}{dots}{ms:.0f} ms")
    dots = "." * max(1, (name_w + 12) - len("Total") - len(f"{total_ms:.0f} ms"))
    lines.append(f"Total{dots}{total_ms:.0f} ms")
    log.info("Turn timing:\n" + "\n".join(lines))


def stage_stats(stage: str) -> Optional[Dict[str, float]]:
    """p50 / p95 / last / count for a given stage, or None if no samples yet."""
    with _lock:
        dq = _history.get(stage)
        if not dq:
            return None
        samples = list(dq)
    samples_sorted = sorted(samples)
    return {
        "count": len(samples),
        "last_ms": samples[-1],
        "p50_ms": statistics.median(samples_sorted),
        "p95_ms": samples_sorted[int(len(samples_sorted) * 0.95) - 1] if len(samples_sorted) > 1 else samples_sorted[0],
        "p99_ms": samples_sorted[int(len(samples_sorted) * 0.99) - 1] if len(samples_sorted) > 1 else samples_sorted[0],
        "max_ms": samples_sorted[-1],
    }


def all_stats() -> Dict[str, Dict[str, float]]:
    with _lock:
        stages = list(_history.keys())
    return {s: stage_stats(s) for s in stages}


def report() -> str:
    """Human-readable bottleneck summary across all stages seen so far."""
    stats = all_stats()
    if not stats:
        return "No performance samples recorded yet."
    rows = sorted(stats.items(), key=lambda kv: kv[1]["p95_ms"], reverse=True)
    name_w = max(len(s) for s, _ in rows) + 1
    out = ["Performance summary (sorted by p95, worst first):",
           f"{'Stage':<{name_w}} {'count':>6} {'last':>8} {'p50':>8} {'p95':>8} {'max':>8}"]
    for name, st in rows:
        out.append(
            f"{name:<{name_w}} {st['count']:>6} "
            f"{st['last_ms']:>6.0f}ms {st['p50_ms']:>6.0f}ms "
            f"{st['p95_ms']:>6.0f}ms {st['max_ms']:>6.0f}ms"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Per-tool metrics — perf audit item.
# ---------------------------------------------------------------------------
# The generic PerfTimer/stage_stats machinery above already gives p50/p95
# per stage (and every tool call is already recorded as "Tool:<name>" by
# core/tool_dispatch.py), but it has no concept of *outcome* — success vs.
# failure vs. timeout, whether a retry happened, or how many nested Gemini
# calls a tool made internally. That's what's needed to actually confirm
# which of the other perf fixes are paying off in real usage, so it's
# tracked separately here in ToolMetrics, fed from core/execution_queue.py
# (the single choke point every tool call already passes through).

@dataclass
class ToolMetrics:
    """Aggregate outcome/latency stats for one tool name."""
    calls: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    retried: int = 0
    llm_calls_total: int = 0
    durations_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=_HISTORY_LEN))


_tool_metrics: Dict[str, ToolMetrics] = {}
_tool_metrics_lock = threading.Lock()

# Timeout-ish substrings checked against a failed tool's result text.
# Mirrors core.execution_queue._TRANSIENT_KEYWORDS' timeout subset — kept
# as a separate small list here so utils.perf has no import dependency on
# core.execution_queue (perf must stay import-cheap / dependency-light).
_TIMEOUT_HINTS = ("timeout", "timed out", "deadline exceeded")


def record_tool_outcome(
    name: str,
    duration_ms: float,
    success: bool,
    retried: bool = False,
    timed_out: bool = False,
    llm_calls: int = 0,
) -> None:
    """Record the outcome of one tool execution (called from
    core.execution_queue.ExecutionQueue.run after every attempt sequence
    completes — one call per user-facing tool invocation, not per retry)."""
    with _tool_metrics_lock:
        m = _tool_metrics.setdefault(name, ToolMetrics())
        m.calls += 1
        if success:
            m.successes += 1
        else:
            m.failures += 1
        if timed_out:
            m.timeouts += 1
        if retried:
            m.retried += 1
        m.llm_calls_total += max(0, llm_calls)
        m.durations_ms.append(duration_ms)


def tool_stats(name: str) -> Optional[Dict[str, float]]:
    """P50/P95/P99 latency + outcome counters for one tool, or None."""
    with _tool_metrics_lock:
        m = _tool_metrics.get(name)
        if m is None or not m.durations_ms:
            return None
        calls, successes, failures = m.calls, m.successes, m.failures
        timeouts, retried, llm_calls_total = m.timeouts, m.retried, m.llm_calls_total
        samples = sorted(m.durations_ms)

    n = len(samples)
    return {
        "calls": calls,
        "successes": successes,
        "failures": failures,
        "timeouts": timeouts,
        "retried": retried,
        "avg_llm_calls": round(llm_calls_total / calls, 2) if calls else 0.0,
        "p50_ms": statistics.median(samples),
        "p95_ms": samples[int(n * 0.95) - 1] if n > 1 else samples[0],
        "p99_ms": samples[int(n * 0.99) - 1] if n > 1 else samples[0],
        "max_ms": samples[-1],
    }


def all_tool_stats() -> Dict[str, Dict[str, float]]:
    with _tool_metrics_lock:
        names = list(_tool_metrics.keys())
    out = {}
    for n in names:
        st = tool_stats(n)
        if st is not None:
            out[n] = st
    return out


def tool_report(sort_by: str = "p95_ms") -> str:
    """Human-readable per-tool table: latency percentiles + failure/timeout
    counts + retry rate + average nested-LLM-calls-per-invocation."""
    stats = all_tool_stats()
    if not stats:
        return "No tool metrics recorded yet."
    rows = sorted(stats.items(), key=lambda kv: kv[1].get(sort_by, 0), reverse=True)
    name_w = max(len(s) for s, _ in rows) + 1
    out = [
        f"Tool metrics (sorted by {sort_by}, worst first):",
        f"{'Tool':<{name_w}} {'calls':>6} {'fail':>5} {'t/out':>6} {'retry':>6} "
        f"{'p50':>7} {'p95':>7} {'p99':>7} {'llm/call':>9}",
    ]
    for name, st in rows:
        out.append(
            f"{name:<{name_w}} {st['calls']:>6} {st['failures']:>5} "
            f"{st['timeouts']:>6} {st['retried']:>6} "
            f"{st['p50_ms']:>5.0f}ms {st['p95_ms']:>5.0f}ms {st['p99_ms']:>5.0f}ms "
            f"{st['avg_llm_calls']:>9}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Nested-LLM-call counting
# ---------------------------------------------------------------------------
# A thread-local counter, bumped by note_llm_call() every time a Gemini
# request actually goes out. core.execution_queue resets it right before
# calling a tool's executor function and reads it right after, so
# ToolMetrics.llm_calls_total reflects exactly how many Gemini calls that
# tool's own code triggered internally (e.g. tool chaining several
# sub-prompts) — the thing "nested LLM calls" in the audit refers to.
_llm_call_local = threading.local()


def note_llm_call() -> None:
    """Call this immediately before/after issuing a Gemini request. Cheap:
    just increments a thread-local int, no locking, no I/O."""
    count = getattr(_llm_call_local, "count", None)
    if count is None:
        _llm_call_local.count = 1
    else:
        _llm_call_local.count = count + 1


def reset_llm_call_count() -> None:
    _llm_call_local.count = 0


def get_llm_call_count() -> int:
    return getattr(_llm_call_local, "count", 0)


_llm_tracking_installed = False
_llm_tracking_lock = threading.Lock()


def install_llm_call_tracking() -> None:
    """Best-effort: monkey-patch google.genai's Models.generate_content so
    every Gemini call anywhere in the app — including the dozen-plus action
    modules that construct their own genai.Client and call it directly —
    is counted automatically, without having to hand-edit every call site.

    Idempotent and entirely defensive: if the installed google-genai SDK's
    internal shape doesn't match what's patched here (a version bump moved
    or renamed things), this silently no-ops and note_llm_call() simply
    never fires — tool latency/outcome metrics (the main payoff) are
    completely unaffected either way.
    """
    global _llm_tracking_installed
    if _llm_tracking_installed:
        return
    with _llm_tracking_lock:
        if _llm_tracking_installed:
            return
        try:
            from google.genai import models as _genai_models  # type: ignore

            _orig = _genai_models.Models.generate_content
            if getattr(_orig, "_gama_perf_wrapped", False):
                _llm_tracking_installed = True
                return

            def _wrapped(self, *args, **kwargs):
                note_llm_call()
                return _orig(self, *args, **kwargs)

            _wrapped._gama_perf_wrapped = True
            _genai_models.Models.generate_content = _wrapped
            _llm_tracking_installed = True
            log.info("[perf] Nested-LLM-call tracking installed (Models.generate_content patched)")
        except Exception as exc:
            log.debug(f"[perf] LLM call tracking not installed (non-fatal): {exc}")
            _llm_tracking_installed = True  # don't retry every import


# Installed at import time so it's active before any action module gets a
# chance to construct a genai.Client and start making calls. Safe: purely
# additive counting, never changes call behavior or return values.
install_llm_call_tracking()
