"""
core/sleep_controller.py — Sleep / observe / wake state machine — extracted from GamaAssistant (Phase 1).
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

log = get_logger(__name__)


class SleepController:
    """Sleep / observe / wake state machine — extracted from GamaAssistant (Phase 1)."""

    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    def _schedule_auto_sleep(self) -> None:
        """No-op: silence→standby is owned by Live Proactive Audio.

        The model decides when not to respond (proactivity.proactive_audio).
        Explicit 'go to sleep' still uses DEEP_SLEEP paths. Stub kept so
        existing call sites remain safe.
        """
        asst = self._asst
        asst._cancel_auto_sleep()
        return

    def _cancel_auto_sleep(self) -> None:
        """Cancel an in-flight silence countdown. Safe from any thread."""
        asst = self._asst
        task = asst._auto_sleep_task
        if task and not task.done():
            try:
                loop = asst._loop
                if loop is not None and loop.is_running():
                    try:
                        running = asyncio.get_running_loop()
                    except RuntimeError:
                        running = None
                    if running is loop:
                        task.cancel()
                    else:
                        loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            except Exception:
                try:
                    task.cancel()
                except Exception:
                    pass
        asst._auto_sleep_task = None

    def _enter_observe_mode(self, reason: str) -> None:
        """ACTIVE → OBSERVE. Gemini stays connected; Gama stops speaking.

        This is the normal idle state after the Active Window expires or the
        user says a soft standby phrase. Audio may still stream for silent
        understanding; playback is gated by runtime.may_speak.
        """
        asst = self._asst
        asst._cancel_auto_sleep()
        asst._awake = False
        asst._wake_verifying = False
        asst._sync_clap_arm()
        try:
            asst._session_mgr.end_session(reason)
        except Exception:
            pass
        # Kill any lagging Gemini audio so a delayed chunk doesn't play
        # after we leave ACTIVE (or get spoken the moment we wake again).
        asst._flush_playback(reason=f"enter observe: {reason}")
        # Fresh observe buffer for this standby period.
        asst._observe_pending_request = None
        try:
            asst._runtime.force_observe(reason)
        except Exception:
            pass
        # Nudge the live model so it does not speak/tool-call while we only
        # want silent understanding. Best-effort; failures are non-fatal.
        try:
            loop = asst._loop
            if loop is not None and loop.is_running() and asst.session is not None:
                async def _observe_nudge():
                    try:
                        await asst._send_system_text(
                            "[MODE=OBSERVE] Stay silent. Do not speak, do not call "
                            "tools, do not acknowledge. Only listen and remember "
                            "context until the user addresses you by name or says "
                            "the wake word."
                        )
                    except Exception:
                        pass
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    asyncio.ensure_future(_observe_nudge())
                else:
                    asyncio.run_coroutine_threadsafe(_observe_nudge(), loop)
        except Exception:
            pass
        asst.ui.set_state("IDLE")
        try:
            asst.ui.emit_event("ObserveStarted")
        except Exception:
            pass
        asst.ui.write_log(
            f'<span style="color:#5ab8cc">👁 Observing — listening quietly. '
            f'Say "{asst._wake_cfg.wake_phrase}" or address me by name.</span>'
        )
        log.info(f"GAMA entered OBSERVE — reason: {reason}")

    def _enter_deep_sleep(self, reason: str) -> None:
        """OBSERVE → DEEP_SLEEP after long inactivity.

        Stops mic→Gemini streaming, suspends heavy pipeline work, keeps only
        the local wake-word detector alive.
        """
        asst = self._asst
        asst._cancel_auto_sleep()
        asst._awake = False
        asst._wake_verifying = False
        try:
            asst._session_mgr.end_session(reason)
        except Exception:
            pass
        try:
            from security import trusted_session
            trusted_session.invalidate(reason)
        except Exception as exc:
            log.debug(f"trusted_session.invalidate skipped: {exc}")
        if asst._voice_pipeline is not None:
            try:
                asst._voice_pipeline.sleep()
            except Exception:
                pass
        asst._set_speaking(False)
        for attr in ("audio_in_queue", "out_queue"):
            q = getattr(self, attr, None)
            if q is None:
                continue
            try:
                while not q.empty():
                    q.get_nowait()
            except Exception:
                pass
        try:
            asst._runtime.force_deep_sleep(reason)
        except Exception:
            pass
        asst.ui.set_state("SLEEPING")
        try:
            asst.ui.emit_event("DeepSleepEntered")
            asst.ui.emit_event("SleepEntered")
        except Exception:
            pass
        asst.ui.write_log(
            f'<span style="color:#5ab8cc">😴 Deep sleep. '
            f'Say "{asst._wake_cfg.wake_phrase}" to wake.</span>'
        )
        log.info(f"GAMA entered DEEP_SLEEP — reason: {reason}")

    def _enter_sleep_mode(self, reason: str) -> None:
        """Explicit 'go to sleep' → DEEP_SLEEP only.

        Automatic standby/OBSERVE is removed. Sleep is the only idle state
        and is entered solely by user command (or equivalent tool path).
        """
        asst = self._asst
        # Snapshot open apps for optional restore (non-blocking).
        try:
            from actions.session_restore import save_session as _save_sess
            import threading as _thr
            _thr.Thread(target=_save_sess, name="gama-session-snapshot",
                        daemon=True).start()
        except Exception as _exc:
            log.debug(f"Session snapshot skipped: {_exc}")
        # Prefer the dedicated deep-sleep path.
        try:
            asst._enter_deep_sleep(reason)
        except Exception as _ds_exc:
            log.warning(f"_enter_deep_sleep failed ({_ds_exc}); forcing runtime DEEP_SLEEP")
            try:
                asst._runtime.force_deep_sleep(reason)
            except Exception:
                asst._enter_observe_mode(reason)  # last-resort fallback


    def _wake_gama(self) -> None:
        """Enter ACTIVE from OBSERVE / DEEP_SLEEP (wake word or reminder).

        Reconnects streaming if coming out of Deep Sleep. Does not speak
        here — callers that need an ack use _send_wake_ack / _speak_via_session.
        """
        asst = self._asst
        was_deep = False
        try:
            was_deep = asst._runtime.is_deep_sleep
            asst._runtime.on_wake("wake")
        except Exception:
            pass
        asst._awake = True
        asst._wake_verifying = False
        asst._sync_clap_arm()
        if asst._voice_pipeline is not None:
            try:
                if hasattr(asst._voice_pipeline, "wake"):
                    asst._voice_pipeline.wake()
                elif hasattr(asst._voice_pipeline, "resume"):
                    asst._voice_pipeline.resume()
            except Exception:
                pass
        asst.ui.set_state(asst._awake_state())
        try:
            asst.ui.emit_event("WakeDetected")
        except Exception:
            pass
        if was_deep:
            asst.ui.write_log(
                '<span style="color:#00ff88">⚡ Leaving deep sleep — listening.</span>'
            )
            log.info("Woke from DEEP_SLEEP into ACTIVE.")
        else:
            log.info("Entered ACTIVE (wake).")
        asst._schedule_auto_sleep()

    def _voice_activity(self) -> bool:
        """True if Gama is speaking OR the user appears to be speaking.

        Used so Active Window / Observe timers only count *true silence*.
        While the user is mid-sentence the assistant must NOT drop to standby.
        """
        asst = self._asst
        with asst._speaking_lock:
            if asst._speaking:
                return True
        # User mic energy — recent hot frames from barge-in / amplitude path
        if getattr(asst, "_interrupt_hot_frames", 0) > 0:
            return True
        # Recent speech-level mic energy (updated in the capture callback)
        try:
            if time.monotonic() - float(getattr(asst, "_last_user_voice_ts", 0) or 0) < 1.6:
                return True
        except Exception:
            pass
        # Very recent final/partial user transcript also counts as directed speech
        try:
            if time.monotonic() - float(getattr(asst, "_last_input_transcript_ts", 0) or 0) < 2.0:
                return True
        except Exception:
            pass
        # Audio still queued for playback counts as Gama speaking
        q = getattr(self, "audio_in_queue", None)
        if q is not None:
            try:
                if not q.empty():
                    return True
            except Exception:
                pass
        return False

    async def _auto_sleep_after_timeout(self) -> None:
        """Silence-only Active Window → OBSERVE (standby).

        Rules:
          • Timer only advances during *true silence* (no Gama speech and
            no directed human voice on the mic).
          • While the user is speaking, countdown is paused and the full
            12s window is refreshed — never drop to standby mid-utterance.
          • After speech stops, require 12 continuous seconds of silence
            with no command / human voice directed at Gama.
          • Engaged long tasks keep ACTIVE until engagement clears.
        """
        asst = self._asst
        try:
            from core.assistant_runtime import ACTIVE_WINDOW_S
            window = float(getattr(asst, "_CONVERSATION_TIMEOUT_S", ACTIVE_WINDOW_S) or ACTIVE_WINDOW_S)
        except Exception:
            window = 12.0

        silence_needed = window
        slice_s = 0.25
        accumulated_silence = 0.0

        while asst._running and asst._awake:
            try:
                await asyncio.sleep(slice_s)
            except asyncio.CancelledError:
                return

            if not asst._awake or asst._enrolling:
                return

            # Engaged long task — do not drop to Observe.
            try:
                if getattr(asst, "_runtime", None) is not None and asst._runtime.engaged:
                    accumulated_silence = 0.0
                    try:
                        asst._runtime.pause_active_deadline()
                    except Exception:
                        pass
                    continue
            except Exception:
                pass

            if asst._voice_activity():
                # Someone is speaking — pause countdown and fully refresh the
                # 12s standby budget so mid-utterance never drops to standby.
                accumulated_silence = 0.0
                try:
                    if getattr(asst, "_runtime", None) is not None:
                        asst._runtime.pause_active_deadline()
                        asst._runtime.on_interaction("voice activity")
                except Exception:
                    pass
                continue

            # Pure silence slice (no Gama speech, no directed human voice)
            accumulated_silence += slice_s
            try:
                if getattr(asst, "_runtime", None) is not None:
                    # After speech stops, grant a full quiet window (not a clipped half).
                    if accumulated_silence <= slice_s + 0.01:
                        asst._runtime.on_interaction("silence window start")
            except Exception:
                pass

            if accumulated_silence >= silence_needed:
                break
        else:
            # loop exited because not running / not awake
            asst._auto_sleep_task = None
            return

        # Final guards right before transition
        if not asst._awake or asst._voice_activity() or asst._enrolling:
            # Speech resumed at the last moment — reschedule
            asst._auto_sleep_task = asyncio.ensure_future(
                asst._auto_sleep_after_timeout()
            )
            return

        asst._enter_observe_mode(
            f"active window expired ({silence_needed:.0f}s silence)"
        )
        try:
            asst._session_mgr.end_session("active window expired")
        except Exception:
            pass
        asst._auto_sleep_task = None



__all__ = ["SleepController"]
