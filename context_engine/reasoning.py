"""
context_engine/reasoning.py — Reasoning Engine.

Per spec: "It should not collect data. Instead it receives information
from Desktop Context, Vision, Memory, Voice, State Manager, Automation,
Plugins... It then decides what GAMA should do."

In this codebase, the heavy lifting of turning fused context into an
actual decision is already Gemini's function-calling loop in main.py —
that IS the reasoning step; rewriting it as a separate rule engine would
throw away a strictly more capable reasoner for a weaker one. What this
module provides is the *fusion* (assembling Desktop + Memory + State +
optional Vision into one context blob, replacing main.py's ad-hoc
parts.append calls) and one genuinely separable decision — whether a
user's phrasing calls for on-demand vision — exposed as advisory
telemetry/logging, not a hard gate (Gemini's own tool-calling already
decides whether to call screen_process/webcam_process; this just lets
main.py log/inspect that decision consistently and is available for a
future local-only fast-path if ever wanted).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Phrasing that the spec's "Activation Rules" calls out explicitly for
# the Vision Engine — kept as a simple, fast, local heuristic. This is
# advisory only: it never blocks or forces a vision call, it just names
# the signal so it can be logged/inspected/tested independently of the
# LLM's own tool-choice behavior.
_VISION_TRIGGER_PATTERNS = [
    r"\bwhat'?s on (my|the) screen\b",
    r"\bwhat (is|error) (is )?(this|that)\b",
    r"\bdescribe (this|that) image\b",
    r"\bread (this|that) (dialog|dialogue|popup|message)\b",
    r"\blook at (my|the) screen\b",
    r"\bwhat do you see\b",
    r"\bcan you see\b",
]
_VISION_TRIGGER_RE = re.compile("|".join(_VISION_TRIGGER_PATTERNS), re.IGNORECASE)


@dataclass
class FusedContext:
    desktop: str
    memory: str
    state_summary: str
    vision: Optional[str] = None

    def as_prompt_block(self) -> str:
        """Same shape main.py already injects into the session prompt —
        centralizing it here means future callers (a plugin, a second
        session, a debug tool) get identical fusion without duplicating
        the assembly logic."""
        parts = [p for p in (self.desktop, self.memory, self.state_summary, self.vision) if p]
        return "\n\n".join(parts)


class ReasoningEngine:
    """Stateless-ish coordinator: reads from the other engines, never
    polls or captures anything itself."""

    def wants_vision(self, user_text: str) -> bool:
        return bool(_VISION_TRIGGER_RE.search(user_text or ""))

    def state_summary(self) -> str:
        try:
            from state_engine import state
            snap = state.snapshot()
            active_tasks = state.tasks.active()
            bits = [f"Assistant is currently {snap.primary.value.title()}"]
            if snap.activity.value != "NONE":
                bits.append(f"(activity: {snap.activity.value})")
            if active_tasks:
                names = ", ".join(t.name for t in active_tasks)
                bits.append(f"Background tasks running: {names}.")
            return " ".join(bits)
        except Exception:
            return ""

    def fuse(self, desktop: str = "", memory: str = "", vision: Optional[str] = None) -> FusedContext:
        """Assemble the four inputs the spec calls out (Desktop, Memory,
        State, optional Vision) into one FusedContext. Reports
        ActivityState.ANALYZING_CONTEXT for the (near-instant, purely
        local, no LLM call) duration of assembly."""
        try:
            from state_engine import state, ActivityState
            with state.activity(ActivityState.ANALYZING_CONTEXT):
                return FusedContext(
                    desktop=desktop, memory=memory,
                    state_summary=self.state_summary(), vision=vision,
                )
        except Exception:
            return FusedContext(desktop=desktop, memory=memory,
                                 state_summary=self.state_summary(), vision=vision)


_engine: Optional[ReasoningEngine] = None


def get_reasoning_engine() -> ReasoningEngine:
    global _engine
    if _engine is None:
        _engine = ReasoningEngine()
    return _engine


def fuse_context(desktop: str = "", memory: str = "", vision: Optional[str] = None) -> str:
    """Convenience one-liner for main.py: replaces manual
    `parts.append(desktop_str); parts.append(memory_str)` with a single
    fused, state-aware block.

    Also injects the live context snapshot (active app, session mode,
    clipboard, media, network — zero-LLM, instant local read) so Gemini
    can answer "what am I doing?" / "what app is open?" without a tool call.
    """
    # Fast context snapshot — reads from the desktop_context cache, no OS poll.
    snapshot_block = ""
    try:
        from context_engine.context_snapshot import get_snapshot
        snap = get_snapshot()
        snapshot_block = snap.as_prompt_block()
    except Exception:
        pass

    # Merge snapshot into the desktop block so it flows naturally into the
    # existing prompt structure without any caller changes.
    if snapshot_block:
        desktop = "\n\n".join(filter(None, [snapshot_block, desktop]))

    return get_reasoning_engine().fuse(desktop=desktop, memory=memory, vision=vision).as_prompt_block()
