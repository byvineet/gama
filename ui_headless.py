"""
ui_headless.py — Minimal UI surface for web-only mode (no Qt window).

Implements the same methods GamaAssistant expects from GamaUI, routing
visual updates to the React HUD via core.web_bridge.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

log = logging.getLogger("gama.ui.headless")


class Signal:
    """Thread-safe-ish plain signal (no Qt)."""

    def __init__(self) -> None:
        self._subs: list[Callable[..., Any]] = []

    def connect(self, fn: Callable[..., Any]) -> None:
        self._subs.append(fn)

    def emit(self, *args: Any) -> None:
        for fn in list(self._subs):
            try:
                fn(*args)
            except Exception:
                log.exception("Headless Signal subscriber raised")


class _CanvasShim:
    muted: bool = False


class HeadlessUI:
    """Drop-in stand-in for GamaUI when GAMA_WEB_UI_ONLY=1."""

    text_command: Signal
    mute_toggled: Signal
    enrollment_speak: Signal

    def __init__(self) -> None:
        self.text_command = Signal()
        self.mute_toggled = Signal()
        self.enrollment_speak = Signal()
        self._canvas = _CanvasShim()
        self.app = None
        self.window = None
        log.info("HeadlessUI active — Qt window disabled; use React HUD on :5173 or :8765")

    def show(self) -> None:
        pass

    def write_log(self, html: str) -> None:
        text = re.sub(r"<[^>]+>", "", html or "").strip()
        text = re.sub(
            r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0000200D]",
            "",
            text,
        ).strip()
        if text:
            log.info(f"[gama] {text}")
        try:
            from core.web_bridge import push_log
            low = text.lower()
            if "user:" in low or text.startswith("USER"):
                role, body = "user", text.split(":", 1)[-1].strip()
            elif "gama:" in low or text.startswith("GAMA"):
                role, body = "gama", text.split(":", 1)[-1].strip()
            else:
                role, body = "system", text
            if body:
                push_log(role, body)
        except Exception:
            pass

    def set_state(self, state: str) -> None:
        try:
            from core.web_bridge import push_state
            push_state(primary=state.upper(), status_text=state.upper())
        except Exception:
            pass
        try:
            from state_engine import state as _se, PrimaryState
            mapping = {
                "OFFLINE": PrimaryState.OFFLINE,
                "STARTING": PrimaryState.STARTING,
                "INITIALIZING": PrimaryState.INITIALIZING,
                "READY": PrimaryState.READY,
                "IDLE": PrimaryState.IDLE,
                "LISTENING": PrimaryState.LISTENING,
                "WAKE_WORD": PrimaryState.LISTENING,
                "PROCESSING": PrimaryState.PROCESSING,
                "THINKING": PrimaryState.THINKING,
                "SPEAKING": PrimaryState.SPEAKING,
                "WAITING": PrimaryState.WAITING,
                "EXECUTING": PrimaryState.EXECUTING,
                "SUCCESS": PrimaryState.READY,
                "SLEEPING": PrimaryState.SLEEPING,
                "ERROR": PrimaryState.ERROR,
                "SHUTTING_DOWN": PrimaryState.SHUTTING_DOWN,
            }
            mapped = mapping.get(state.upper())
            if mapped is not None:
                _se.set_primary(mapped, detail="headless.set_state")
        except Exception:
            pass

    def set_speaking(self, value: bool) -> None:
        # After speech ends, show LISTENING (not READY) so the HUD matches
        # the mic / Live input-transcription state.
        self.set_state("SPEAKING" if value else "LISTENING")
        try:
            from core.web_bridge import push_state
            push_state(speaking=bool(value))
        except Exception:
            pass

    def set_muted(self, value: bool) -> None:
        self._canvas.muted = bool(value)

    def set_activity(self, activity) -> None:
        try:
            from state_engine import state as _se, ActivityState
            if isinstance(activity, str):
                activity = ActivityState(activity.upper())
            _se.set_activity(activity)
        except Exception:
            pass

    def set_mood(self, mood) -> None:
        try:
            from state_engine import state as _se, MoodState
            if isinstance(mood, str):
                mood = MoodState(mood.upper())
            _se.set_mood(mood)
        except Exception:
            pass

    def emit_event(self, event_name: str, **data) -> None:
        try:
            from state_engine import state as _se
            _se.emit(event_name, **data)
        except Exception:
            pass

    def set_weather(self, temp: str, desc: str) -> None:
        pass

    def set_amplitude(self, amp: float) -> None:
        try:
            from core.web_bridge import push_amplitude
            push_amplitude(float(amp))
        except Exception:
            pass

    def set_system_stats(self, cpu: float, ram: float) -> None:
        pass

    def set_world_stats(self, battery_text: str, app_text: str, task_text: str) -> None:
        pass

    def show_holo_panel(self, *args, **kwargs) -> None:
        pass

    def stream_start(self, sid=None) -> str:
        import uuid
        return sid or uuid.uuid4().hex

    def stream_token(self, sid, tok) -> None:
        pass

    def speak_line(self, text: str) -> None:
        self.enrollment_speak.emit(text)

    # ── Voice enrollment → React HUD display stage ──────────────────────
    def voice_enroll_show(self) -> None:
        self.show_voice_enrollment()

    def show_voice_enrollment(self) -> None:
        try:
            from actions.display_stage import show_enrollment_on_display
            show_enrollment_on_display(
                kind="voice",
                title="Voice Enrollment",
                instruction="Get ready — speak the phrases shown on screen.",
                step=0,
                total=10,
                progress=0.0,
                status="Starting…",
                recording=False,
            )
        except Exception as exc:
            log.debug("voice_enroll_show failed: %s", exc)

    def voice_enroll_hide(self) -> None:
        self.hide_voice_enrollment()

    def hide_voice_enrollment(self) -> None:
        try:
            from core.web_bridge import close_display
            close_display()
        except Exception:
            try:
                from actions.display_stage import close_display_stage
                close_display_stage()
            except Exception as exc:
                log.debug("voice_enroll_hide failed: %s", exc)

    def voice_enroll_set_sentence(self, sentence: str, index: int, total: int) -> None:
        try:
            from actions.display_stage import show_enrollment_on_display
            show_enrollment_on_display(
                kind="voice",
                title="Voice Enrollment",
                instruction=str(sentence or ""),
                step=int(index),
                total=int(total),
                progress=max(0.0, (int(index) - 1) / max(1, int(total))),
                status=f"Say this phrase ({index}/{total})",
                recording=False,
            )
        except Exception as exc:
            log.debug("voice_enroll_set_sentence failed: %s", exc)

    def voice_enroll_set_recording(self, active: bool) -> None:
        try:
            from actions.display_stage import show_enrollment_on_display
            show_enrollment_on_display(
                kind="voice",
                title="Voice Enrollment",
                recording=bool(active),
                status="● Recording…" if active else "Processing…",
            )
        except Exception as exc:
            log.debug("voice_enroll_set_recording failed: %s", exc)

    def voice_enroll_set_bar(self, elapsed: float, duration: float) -> None:
        try:
            from actions.display_stage import show_enrollment_on_display
            dur = float(duration) or 1.0
            pct = max(0.0, min(1.0, float(elapsed) / dur))
            show_enrollment_on_display(
                kind="voice",
                title="Voice Enrollment",
                progress=pct,
                recording=True,
                status="● Recording…",
            )
        except Exception as exc:
            log.debug("voice_enroll_set_bar failed: %s", exc)

    def voice_enroll_set_status(self, text: str, ok: bool = True) -> None:
        try:
            from actions.display_stage import show_enrollment_on_display
            show_enrollment_on_display(
                kind="voice",
                title="Voice Enrollment",
                status=str(text or ("Good." if ok else "Try again.")),
                recording=False,
            )
        except Exception as exc:
            log.debug("voice_enroll_set_status failed: %s", exc)

    def voice_enroll_show_completion(self, message: str) -> None:
        try:
            from actions.display_stage import show_enrollment_on_display
            show_enrollment_on_display(
                kind="voice",
                title="Voice Enrollment Complete",
                instruction=str(message or "Enrollment complete."),
                progress=1.0,
                status="Done",
                recording=False,
            )
        except Exception as exc:
            log.debug("voice_enroll_show_completion failed: %s", exc)

    # Cancel signal used by tool_dispatch (Event-like interface optional)
    @property
    def voice_enroll_cancel_clicked(self):
        class _NoOp:
            def connect(self, *_a, **_k):
                return None
        return _NoOp()

    @property
    def scene(self):
        class _S:
            state = "LISTENING"

            def set_state(self, s): self.state = s
            def set_amplitude(self, *_): pass
            def set_system_stats(self, *_): pass

        return _S()


__all__ = ["HeadlessUI"]
