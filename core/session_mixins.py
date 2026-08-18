"""
core/session_mixins.py — GamaAssistant mixins (extracted from main.py, C3 refactor)
======================================================================================
Small, self-contained method groups pulled out of the ~3,500-line
GamaAssistant class. Each mixin only touches instance attributes that are
set up in GamaAssistant.__init__ (self.ui, self.VOICE_PRESETS, self._loop,
etc.) — no changes to behavior, just relocated for readability.

VoicePreferenceMixin — load/save/switch the TTS voice preset.
NotificationMixin    — proactive/system-alert delivery callbacks
                       (download-complete, habit suggestions, Phase 7
                       proactive engine events, generic system alerts).
"""

from __future__ import annotations

import asyncio
import re as _re

from utils.logger import get_logger

log = get_logger(__name__)


class VoicePreferenceMixin:
    """Voice preference persistence — load/save/switch the TTS voice preset."""

    def _load_voice_preference(self) -> str:
        """Load saved voice from memory, default to Charon (male)."""
        try:
            from memory.memory_manager import get_memory
            voice = get_memory("preferences", "voice")
            if voice and voice in self.VOICE_PRESETS:
                return self.VOICE_PRESETS[voice]
            if voice and voice in self.VOICE_PRESETS.values():
                return voice
        except Exception:
            pass
        return "Charon"  # default male

    def _save_voice_preference(self, voice_key: str) -> None:
        """Save voice preference to memory."""
        try:
            from memory.memory_manager import set_memory
            set_memory("preferences", "voice", voice_key)
        except Exception as exc:
            log.error(f"Failed to save voice preference: {exc}")

    def set_voice(self, voice_key: str) -> str:
        """Switch voice. Accepts: male, charon, fenrir, orus, puck."""
        voice_key = (voice_key or "").strip().lower()
        if not voice_key:
            return "Available voices: male (Charon), charon, fenrir, orus, puck."
        if voice_key not in self.VOICE_PRESETS:
            return (f"Unknown voice '{voice_key}'. Available: "
                    "male, charon, fenrir, orus, puck.")
        new_voice = self.VOICE_PRESETS[voice_key]
        self._voice_name = new_voice
        self._save_voice_preference(voice_key)
        log.info(f"Voice switched to: {new_voice} (key={voice_key})")
        return (f"Voice set to {voice_key} ({new_voice}). "
                "Will take full effect on next session restart.")


class NotificationMixin:
    """Proactive/system-alert delivery callbacks."""

    def _on_download_complete(self, filename: str):
        """Proactive suggestion when a new file lands in Downloads."""
        self._on_sys_alert(
            f"[SYSTEM_ALERT] A download just finished: '{filename}'. "
            "Briefly and casually let the user know, without being asked."
        )

    def _on_habit_suggestion_silent(self, suggestion_text: str) -> None:
        """Delivery target for actions/proactive_suggestions.py and
        removed — habit predictions, repeated-workflow
        offers, downloaded-zip offers, VS Code / Downloads nudges, etc.

        Per JARVIS-style behavior: these are NEVER spoken or injected into
        the live session unprompted, no matter how confident the guess is.
        Tony doesn't get asked "want me to set up your usual session?" —
        JARVIS just quietly knows the pattern and acts only if asked. The
        underlying learning (routine_analyzer / workflow_learner / app-chain
        counters) is untouched and keeps building confidence in the
        background; this only stops the unsolicited voice delivery.

        Logged to the HUD (grey, low-key) so it's visible if you're
        watching the log, and still retrievable on request via
        proactive_suggestions(action='status'), but it will never wake
        GAMA or speak on its own.
        """
        try:
            log.info(f"[habit-suggestion, silent] {suggestion_text}")
            self.ui.write_log(f'<span style="color:#64748B">[NOTICED] {suggestion_text}</span>')
        except Exception:
            pass

    def _on_jarvis_notification(self, evt) -> None:
        """Handle a Phase 7 Proactive Engine suggestion (core/proactive_engine.py).

        Only "high"/"urgent" priority suggestions (network offline, meeting
        soon) are treated like a real system alert — waking GAMA and
        letting it speak/investigate, same as the existing SystemMonitor
        alert pathway. Everything else ("normal"/"low" — high CPU, high
        RAM, long idle, workflow automation offers) is genuinely
        informational and does NOT justify waking GAMA from standby or
        triggering a full reasoning + tool-call turn just to announce a
        CPU spike. Those are written to the HUD log only, silently.

        This was previously wired to route every suggestion through
        _on_sys_alert regardless of priority, which caused GAMA to wake
        up and speak unprompted for routine "normal" alerts like transient
        CPU spikes — see the "speaks random things when not asked" report.
        """
        text = (evt.data.get("text") or "").strip()
        if not text:
            return
        priority = (evt.data.get("priority") or "normal").lower()
        if priority in ("high", "urgent"):
            wrapped = (
                f"[SYSTEM_ALERT] {text} "
                "State this in ONE short sentence, Sir-natural tone. Do not "
                "acknowledge, do not explain that you're checking, do not "
                "offer further help or ask what to do next — just the fact, "
                "then stop."
            )
            self._on_sys_alert(wrapped)
        else:
            try:
                self.ui.write_log(f'<span style="color:#64748B">[SUGGESTION] {text}</span>')
            except Exception:
                pass

    def _on_sys_alert(self, alert_text: str):
        """Handle system monitor alert — inject into session.
        Note: this does NOT push a desktop toast. Only reminders,
        alarms, timers (actions/reminder.py) and goal check-ins
        (actions/goal_tracker.py) get native desktop notifications
        automatically; everything else here is voice-only unless the
        user explicitly asks to be notified (see actions/desktop_notify.py).

        Offline: speaks the alert directly via local TTS when no session.
        """
        if "[PROACTIVE_SUGGESTION]" in alert_text:
            self._proactive_awaiting_confirmation = True
        self._wake_gama()
        if self._loop and self.session:
            asyncio.run_coroutine_threadsafe(
                self._send_system_text(alert_text),
                self._loop,
            )
        elif self._is_offline():
            # Strip [SYSTEM_ALERT] / [SYSTEM] instruction wrappers and
            # speak just the natural-language portion via local TTS.
            msg = alert_text
            m = _re.search(
                r'\[SYSTEM(?:_ALERT)?\]\s*(.*?)(?:\.\s*Briefly.*)?$',
                alert_text, _re.DOTALL | _re.IGNORECASE,
            )
            if m:
                msg = m.group(1).strip().rstrip(".")
                if msg:
                    msg = msg[0].upper() + msg[1:]
            if msg:
                log.info(f"[offline] System alert via local TTS: {msg[:80]}")
                self._speak_exact(msg, kind="result")

    def _on_sentinel_alert(self, title: str, body: str, accent: str = "#00d4ff", hold_ms: int = 6000) -> None:
        """Callback from ProactiveSentinel (core/proactive_sentinel.py). Log & notify UI."""
        log.info(f"[Sentinel UI Callback] {title}: {body}")
        try:
            if hasattr(self, "ui") and self.ui:
                formatted = f'<span style="color:{accent}; font-weight:bold;">[{title}]</span> {body}'
                self.ui.write_log(formatted)
                if hasattr(self.ui, "do_show_holo_panel"):
                    self.ui.do_show_holo_panel({
                        "title": title,
                        "body": body,
                        "accent": accent,
                        "hold_ms": hold_ms,
                    })
        except Exception as exc:
            log.debug(f"_on_sentinel_alert UI update failed: {exc}")

