"""
core/assistant_runtime.py — Gama runtime state machine (JARVIS-style)
=====================================================================
Python owns assistant state. Gemini never owns it.

Four primary runtime states:

  BOOT        — startup only; services init + one natural greeting
  OBSERVE     — idle default. Gemini Live stays connected, audio may
                stream for silent understanding, but Gama NEVER speaks
                and NEVER interrupts.
  ACTIVE      — entered only after a successful wake. Short follow-up
                window (Active Window) where the wake word is not
                required. Every directed interaction resets the timer.
  DEEP_SLEEP  — long inactivity. Stop streaming audio to Gemini, drop
                live conversational context maintenance, keep only the
                local wake-word detector alive.

Engaged is an *internal* overlay (not a user-visible state): while a
long task / multi-turn exchange is under way the Active Window is
extended automatically.

Modules publish/subscribe via state_engine.event_bus. Callers should
not mutate state directly — use the transition methods on RuntimeState.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)


# ── Timing (seconds) ────────────────────────────────────────────────────────
# Standby / "when to speak" is handled by Gemini Live Proactive Audio
# (proactivity.proactive_audio=True) + system-prompt rules — not by a
# local silence timer. ACTIVE_WINDOW_S is only a soft reference for UI /
# engaged extensions; tick() does not auto-transition on timeout.
ACTIVE_WINDOW_S: float = 300.0
DEEP_SLEEP_AFTER_S: float = 999999.0       # no automatic deep-sleep
ENGAGED_ACTIVE_EXTENSION_S: float = 120.0  # while a long task / dialogue runs


class RuntimeMode(str, Enum):
    BOOT = "BOOT"
    OBSERVE = "OBSERVE"
    ACTIVE = "ACTIVE"
    DEEP_SLEEP = "DEEP_SLEEP"


@dataclass
class ConversationState:
    """Structured conversation understanding — never raw transcript dumps."""
    topic: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    referenced_objects: List[str] = field(default_factory=list)
    current_goal: Optional[str] = None
    recent_facts: List[str] = field(default_factory=list)
    pending_questions: List[str] = field(default_factory=list)
    confidence: float = 0.0
    active_task: Optional[str] = None
    last_user_intent: Optional[str] = None
    updated_at: float = field(default_factory=time.time)

    def summary_block(self, max_chars: int = 600) -> str:
        """Compact block suitable for injection into Gemini when ACTIVE."""
        lines: List[str] = ["[CONVERSATION STATE]"]
        if self.topic:
            lines.append(f"  Topic: {self.topic}")
        if self.current_goal:
            lines.append(f"  Goal: {self.current_goal}")
        if self.active_task:
            lines.append(f"  Active task: {self.active_task}")
        if self.participants:
            lines.append(f"  Participants: {', '.join(self.participants[:6])}")
        if self.referenced_objects:
            lines.append(f"  Refs: {', '.join(self.referenced_objects[:8])}")
        if self.pending_questions:
            lines.append(f"  Pending: {'; '.join(self.pending_questions[:3])}")
        if self.recent_facts:
            for f in self.recent_facts[-4:]:
                lines.append(f"  Fact: {f[:120]}")
        if self.last_user_intent:
            lines.append(f"  Last intent: {self.last_user_intent}")
        if self.confidence:
            lines.append(f"  Confidence: {self.confidence:.2f}")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def clear_ephemeral(self) -> None:
        """Drop session-ephemeral fields when leaving Active / entering Deep Sleep."""
        self.pending_questions.clear()
        self.last_user_intent = None
        self.updated_at = time.time()


class RuntimeState:
    """Process-wide assistant runtime. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode: RuntimeMode = RuntimeMode.BOOT
        self._engaged: bool = False
        self._last_activity_ts: float = time.monotonic()
        self._active_deadline_ts: float = 0.0
        self._observe_since_ts: float = time.monotonic()
        self.conversation = ConversationState()
        self._listeners: List[Callable[[RuntimeMode, RuntimeMode, str], None]] = []

    # ── reads ───────────────────────────────────────────────────────────────
    @property
    def mode(self) -> RuntimeMode:
        with self._lock:
            return self._mode

    @property
    def is_active(self) -> bool:
        return self.mode == RuntimeMode.ACTIVE

    @property
    def is_observe(self) -> bool:
        return self.mode == RuntimeMode.OBSERVE

    @property
    def is_deep_sleep(self) -> bool:
        return self.mode == RuntimeMode.DEEP_SLEEP

    @property
    def is_boot(self) -> bool:
        return self.mode == RuntimeMode.BOOT

    @property
    def engaged(self) -> bool:
        with self._lock:
            return self._engaged

    @property
    def may_speak(self) -> bool:
        """True only in ACTIVE (or BOOT greeting). Observe/DeepSleep never speak."""
        m = self.mode
        return m in (RuntimeMode.ACTIVE, RuntimeMode.BOOT)

    @property
    def may_stream_to_gemini(self) -> bool:
        """Deep Sleep stops mic → Gemini streaming. Observe + Active stream."""
        return self.mode in (RuntimeMode.OBSERVE, RuntimeMode.ACTIVE, RuntimeMode.BOOT)

    def seconds_until_active_timeout(self) -> float:
        with self._lock:
            if self._mode != RuntimeMode.ACTIVE:
                return 0.0
            return max(0.0, self._active_deadline_ts - time.monotonic())

    def seconds_in_observe(self) -> float:
        with self._lock:
            if self._mode != RuntimeMode.OBSERVE:
                return 0.0
            return max(0.0, time.monotonic() - self._observe_since_ts)

    # ── transitions ─────────────────────────────────────────────────────────
    def _transition(self, new_mode: RuntimeMode, reason: str) -> None:
        with self._lock:
            old = self._mode
            if old == new_mode:
                return
            self._mode = new_mode
            now = time.monotonic()
            self._last_activity_ts = now
            if new_mode == RuntimeMode.ACTIVE:
                extension = ENGAGED_ACTIVE_EXTENSION_S if self._engaged else ACTIVE_WINDOW_S
                self._active_deadline_ts = now + extension
            elif new_mode == RuntimeMode.OBSERVE:
                self._observe_since_ts = now
                self._active_deadline_ts = 0.0
            elif new_mode == RuntimeMode.DEEP_SLEEP:
                self._active_deadline_ts = 0.0
                self.conversation.clear_ephemeral()
            log.info(f"[Runtime] {old.value} → {new_mode.value} ({reason})")
        self._publish(old, new_mode, reason)
        for fn in list(self._listeners):
            try:
                fn(old, new_mode, reason)
            except Exception as exc:
                log.debug(f"[Runtime] listener error: {exc}")

    def on_boot_complete(self, reason: str = "startup complete") -> None:
        """BOOT → ACTIVE after greeting. No automatic standby/observe."""
        self._transition(RuntimeMode.ACTIVE, reason)

    def on_wake(self, reason: str = "wake word") -> None:
        """Any mode → ACTIVE on successful wake detection."""
        with self._lock:
            self._engaged = False
        self._transition(RuntimeMode.ACTIVE, reason)

    def on_interaction(self, reason: str = "user interaction") -> None:
        """Reset Active Window on every directed interaction."""
        with self._lock:
            if self._mode != RuntimeMode.ACTIVE:
                return
            now = time.monotonic()
            self._last_activity_ts = now
            extension = ENGAGED_ACTIVE_EXTENSION_S if self._engaged else ACTIVE_WINDOW_S
            self._active_deadline_ts = now + extension

    def pause_active_deadline(self) -> None:
        """While someone is speaking, freeze the Active Window countdown.

        Stores remaining time; call resume_active_deadline() when silence
        returns so the user gets a full quiet window, not a clipped one.
        """
        with self._lock:
            if self._mode != RuntimeMode.ACTIVE:
                return
            now = time.monotonic()
            remaining = max(0.0, self._active_deadline_ts - now)
            # Stash remaining on the instance; deadline pushed far away so tick() won't fire.
            self._paused_remaining_s = remaining
            self._active_deadline_ts = now + 10_000.0

    def resume_active_deadline(self) -> None:
        """Resume Active Window after a speaking pause (silence returns)."""
        with self._lock:
            if self._mode != RuntimeMode.ACTIVE:
                return
            remaining = getattr(self, "_paused_remaining_s", None)
            # After any speech, grant a full Active Window of silence (default 12s)
            # before standby — never a clipped half-window mid-conversation.
            full = ENGAGED_ACTIVE_EXTENSION_S if self._engaged else ACTIVE_WINDOW_S
            if remaining is None:
                remaining = full
            else:
                remaining = max(remaining, full)
            self._active_deadline_ts = time.monotonic() + remaining
            self._paused_remaining_s = None

    def set_engaged(self, engaged: bool, reason: str = "") -> None:
        with self._lock:
            if self._engaged == engaged:
                return
            self._engaged = engaged
            if self._mode == RuntimeMode.ACTIVE:
                now = time.monotonic()
                extension = ENGAGED_ACTIVE_EXTENSION_S if engaged else ACTIVE_WINDOW_S
                self._active_deadline_ts = now + extension
        log.debug(f"[Runtime] engaged={engaged} {reason}")

    def tick(self) -> Optional[RuntimeMode]:
        """Periodic check. Auto standby timeouts are disabled.

        When-to-speak is owned by Gemini Live Proactive Audio + prompt
        rules, not by local silence timers. Explicit 'go to sleep' still
        enters DEEP_SLEEP via force_deep_sleep().
        """
        return None

    def force_observe(self, reason: str = "forced") -> None:
        self._transition(RuntimeMode.OBSERVE, reason)

    def force_deep_sleep(self, reason: str = "forced") -> None:
        self._transition(RuntimeMode.DEEP_SLEEP, reason)

    def update_conversation(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                if hasattr(self.conversation, k) and v is not None:
                    setattr(self.conversation, k, v)
            self.conversation.updated_at = time.time()

    def on_mode_change(self, callback: Callable[[RuntimeMode, RuntimeMode, str], None]) -> None:
        self._listeners.append(callback)

    def _publish(self, old: RuntimeMode, new: RuntimeMode, reason: str) -> None:
        try:
            from state_engine.event_bus import event_bus
            event_bus.publish(
                "RuntimeModeChanged",
                old=old.value,
                new=new.value,
                reason=reason,
            )
            if new == RuntimeMode.OBSERVE:
                event_bus.publish("ObserveStarted", reason=reason)
            if old == RuntimeMode.OBSERVE and new != RuntimeMode.OBSERVE:
                event_bus.publish("ObserveStopped", reason=reason)
            if new == RuntimeMode.DEEP_SLEEP:
                event_bus.publish("DeepSleepEntered", reason=reason)
            if old == RuntimeMode.DEEP_SLEEP and new != RuntimeMode.DEEP_SLEEP:
                event_bus.publish("DeepSleepExited", reason=reason)
            if new == RuntimeMode.ACTIVE:
                event_bus.publish("WakeFinished", reason=reason)
        except Exception as exc:
            log.debug(f"[Runtime] event publish skipped: {exc}")

    def status(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode.value,
                "engaged": self._engaged,
                "may_speak": self.may_speak,
                "may_stream": self.may_stream_to_gemini,
                "active_remaining_s": max(0.0, self._active_deadline_ts - time.monotonic())
                if self._mode == RuntimeMode.ACTIVE else 0.0,
                "observe_s": max(0.0, time.monotonic() - self._observe_since_ts)
                if self._mode == RuntimeMode.OBSERVE else 0.0,
                "topic": self.conversation.topic,
            }


# Process-wide singleton
runtime = RuntimeState()

__all__ = [
    "RuntimeMode",
    "ConversationState",
    "RuntimeState",
    "runtime",
    "ACTIVE_WINDOW_S",
    "DEEP_SLEEP_AFTER_S",
]
