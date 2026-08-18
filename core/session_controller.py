"""
core/session_controller.py — ACTIVE / OBSERVE / wake session policy
==================================================================
Owns mode-facing policy that used to live inline in main.py:
  - OBSERVE = listen only, never act (no tools, no speech from model)
  - Structured context (pending request + short buffer), not raw dumps
  - Wake vs direct-address vs pending-request answers
"""

from __future__ import annotations

import re
from typing import List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

_WAKE_NAMES = {"gama", "jarvis", "assistant"}

_REQUEST_MARKERS = (
    "what", "why", "how", "when", "where", "who", "which",
    "is it", "are you", "can you", "could you", "would you", "will you",
    "tell me", "remind", "open", "close", "set", "start", "stop",
    "play", "search", "find", "time", "date", "weather", "status",
    "volume", "brightness", "mute", "help", "please", "should", "?",
)

_OBSERVE_SILENCE_NUDGE = (
    "[MODE=OBSERVE] Stay silent. Do not speak. Do not call tools. "
    "Do not acknowledge. Only listen and remember structured context "
    "until the user addresses you by name or says the wake word."
)


class SessionController:
    def __init__(self, wake_phrases: Optional[set] = None) -> None:
        self.wake_phrases = set(wake_phrases or set()) | set(_WAKE_NAMES)
        self.observe_context: List[str] = []
        self.observe_context_max = 12
        self.pending_request: Optional[str] = None

    def reset_observe_buffer(self) -> None:
        self.pending_request = None
        # Keep a short tail of context across standby periods if desired;
        # full clear is safer to avoid stale questions after long idle.
        self.observe_context.clear()

    def record_observe_utterance(self, text: str) -> None:
        text = (text or "").strip()
        if not text or len(text) < 3:
            return
        tl = text.lower().strip().strip(".!,;:?")
        if tl in self.wake_phrases:
            return
        if self.observe_context and self.observe_context[-1].lower() == text.lower():
            return
        self.observe_context.append(text)
        if len(self.observe_context) > self.observe_context_max:
            del self.observe_context[: len(self.observe_context) - self.observe_context_max]
        if self.looks_like_request(text):
            self.pending_request = text
            log.info(f"[session] pending observe request: {text[:100]!r}")

    def looks_like_request(self, text: str) -> bool:
        t = (text or "").lower().strip()
        if len(t) < 4:
            return False
        return any(m in t for m in _REQUEST_MARKERS)

    def is_wake_only(self, text: str) -> bool:
        t = (text or "").lower().strip().strip(".!,;:?")
        if not t:
            return False
        if t in self.wake_phrases:
            return True
        # single-token near-wake garbage
        if len(t.split()) == 1 and len(t) <= 6:
            for w in self.wake_phrases:
                if w and (w in t or t in w):
                    return True
        return False

    def is_direct_address(self, text: str) -> bool:
        t = (text or "").lower().strip().strip(".!,;:?")
        if not t:
            return False
        has_name = any(n and n in t for n in self.wake_phrases)
        if not has_name:
            return False
        residual = t
        for n in sorted(self.wake_phrases, key=len, reverse=True):
            residual = residual.replace(n, " ")
        residual = " ".join(residual.split()).strip()
        if len(residual) < 3:
            return False
        if any(m in t for m in _REQUEST_MARKERS):
            return True
        return len(residual.split()) >= 2

    def structured_context_block(self, max_lines: int = 8) -> str:
        """Compact structured context for Gemini — not a raw transcript dump."""
        lines = []
        if self.pending_request:
            lines.append(f"Pending request: {self.pending_request}")
        recent = self.observe_context[-max_lines:]
        if recent:
            # Cap each line; avoid replaying long speech
            short = [r[:120] for r in recent]
            lines.append("Recent context: " + " | ".join(short))
        return "\n".join(lines)

    def consume_pending(self) -> Optional[str]:
        p = self.pending_request
        self.pending_request = None
        return p

    def observe_nudge_text(self) -> str:
        return _OBSERVE_SILENCE_NUDGE

    def pending_answer_prompt(self, pending: str) -> str:
        ctx = self.structured_context_block()
        return (
            "[OBSERVE_WAKE] While observing, the user asked something, then said "
            "the wake word. Do NOT only say 'Yes, Sir?'. Answer the pending "
            "request briefly. No filler. No tool narration.\n"
            f"Pending request: {pending}\n"
            f"{ctx}"
        )

    def direct_address_prompt(self, user_text: str) -> str:
        self.pending_request = None
        ctx = self.structured_context_block()
        return (
            "[OBSERVE_WAKE] User addressed you while observing. Answer their "
            "request directly and concisely. No 'Yes, Sir?' only. No filler.\n"
            f"User said: {user_text}\n"
            f"{ctx}"
        )

    def confirm_needed(self, tool_name: str, level: str) -> bool:
        """Confirm only what matters — DESTRUCTIVE / SENSITIVE."""
        return (level or "").upper() in ("DESTRUCTIVE", "SENSITIVE", "CRITICAL")


__all__ = ["SessionController"]
