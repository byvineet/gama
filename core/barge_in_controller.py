"""
core/barge_in_controller.py — Barge-in / interruption logic — extracted from GamaAssistant (Phase 1).
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


class BargeInController:
    """Barge-in / interruption logic — extracted from GamaAssistant (Phase 1)."""

    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    def _hard_stop_speaker(self) -> None:
        """Immediately cut off whatever is currently coming out of the
        speaker, discarding any audio PortAudio already has buffered.

        This is the piece that was missing from barge-in: draining
        `audio_in_queue` only stops *future* chunks from being queued for
        playback — it does nothing about audio already handed to
        `stream.write()` or sitting in PortAudio's own internal buffer,
        which keeps playing out regardless. `stream.abort()` (as opposed
        to `stream.stop()`, which waits for buffered audio to finish)
        discards that buffered audio and returns immediately, which is
        what actually makes Gama stop mid-word instead of finishing the
        current chunk.

        Safe to call from any thread, and safe to call when nothing is
        playing (no-op).
        """
        asst = self._asst
        with asst._live_out_stream_lock:
            stream = asst._live_out_stream
        if stream is None:
            return
        try:
            stream.abort()
        except Exception as exc:
            log.debug(f"[barge-in] stream.abort() failed (non-fatal): {exc}")

    def _immediate_barge_in(self) -> None:
        """Siri/Google-style immediate barge-in — the actual stop-and-flush.

        Runs on the asyncio loop (dispatched via call_soon_threadsafe from
        the mic callback, once the amplitude + echo-correlation check in
        _listen_audio decides this is a real human voice, not Gama's own
        TTS bleed). Security still applies at the per-command level
        through security.py; this layer only decides whether to stop
        playback.

        Steps:
        1. Stop playback immediately.
        2. Flush the play queue so stale audio doesn't bleed into the
           next turn.
        3. Reset interrupt state so the next frame starts fresh.
        4. If a background task (core/task_queue.py) is actively RUNNING
           — e.g. a multi-step automation chain, download, or file
           operation — cooperatively pause it too, not just the voice.
           Interrupting Gama mid-sentence usually means "wait, hold on",
           and a task silently continuing to execute in the background
           while the user tries to redirect Gama is the opposite of
           that. The task is resumable — see _offer_paused_task_followup.
        Gemini receives the interrupting audio automatically once
        _set_speaking(False) clears the `gama_speaking` gate in
        _listen_audio.
        """
        asst = self._asst
        # Honor the user-facing interruption setting. When barge-in is off,
        # never stop playback or log an interrupt (speaker echo must not
        # count as a user barge-in).
        if not bool(getattr(asst, "_barge_in_enabled", True)):
            return
        try:
            with asst._speaking_lock:
                still_speaking = asst._speaking
            if not still_speaking:
                return  # already stopped on its own — nothing to do

            asst._set_speaking(False, interrupted=True)
            asst._hard_stop_speaker()
            asst._last_barge_in_ts = time.monotonic()
            try:
                from voice.event_voice import stop_alert as _stop_alert
                _stop_alert()
            except Exception as exc:
                log.debug(f"stop_alert() on barge-in failed (non-fatal): {exc}")
            while not asst.audio_in_queue.empty():
                try:
                    asst.audio_in_queue.get_nowait()
                except Exception:
                    break
            asst.ui.write_log('<span style="color:#007AFF">⚡ [interrupted]</span>')
            log.info("Barge-in: user spoke — playback stopped immediately.")

            # Mid-action interruption: pause whatever background task is
            # currently running, if any. Cooperative — the task's own fn
            # yields at its next task_queue.is_pause_requested() check,
            # so this never force-kills work mid-write.
            try:
                from core.task_queue import task_queue
                running_id = task_queue.current_task_id()
                if running_id and not asst._barge_in_paused_task_id:
                    task = task_queue._tasks.get(running_id)  # read-only peek
                    task_name = task.name if task else running_id
                    if task_queue.pause(running_id):
                        asst._barge_in_paused_task_id = running_id
                        asst._barge_in_paused_task_name = task_name
                        asst._barge_in_followup_offered = False
                        asst.ui.write_log(
                            f'<span style="color:#007AFF">⏸ [paused: {task_name}]</span>'
                        )
                        log.info(f"Barge-in: auto-paused running task '{task_name}'.")
            except Exception as exc:
                log.debug(f"Barge-in: task auto-pause skipped: {exc}")
        except Exception as exc:
            log.debug(f"Barge-in failed (non-fatal): {exc}")
        finally:
            asst._interrupt_hot_frames = 0
            asst._interrupt_check_inflight = False

    def _offer_paused_task_followup(self) -> None:
        """Called once the interrupting turn has finished responding.
        If barge-in auto-paused a task and the user's interrupting
        command didn't already address it (resume/cancel/retry go
        through task queue and clear _barge_in_paused_task_id
        themselves), gently ask whether to resume or abort it — once,
        not on every subsequent turn.
        """
        asst = self._asst
        if not asst._barge_in_paused_task_id or asst._barge_in_followup_offered:
            return
        try:
            from core.task_queue import task_queue
            task = task_queue._tasks.get(asst._barge_in_paused_task_id)
            if task is None or task.status not in ("PAUSED",):
                # Already resumed/cancelled/finished some other way —
                # nothing to offer, clear silently.
                asst._barge_in_paused_task_id = None
                return
            asst._barge_in_followup_offered = True
            name = asst._barge_in_paused_task_name or asst._barge_in_paused_task_id
            asst._on_sys_alert(
                f"By the way, I paused \"{name}\" when you interrupted me — "
                f"want me to resume it, or should I cancel it?"
            )
        except Exception as exc:
            log.debug(f"_offer_paused_task_followup failed (non-fatal): {exc}")

    def _clear_barge_in_task_state(self) -> None:
        """Call this whenever the paused task is explicitly resumed,
        cancelled, or retried through normal task queue handling,
        so the followup nudge doesn't fire for something already
        resolved."""
        asst = self._asst
        asst._barge_in_paused_task_id = None
        asst._barge_in_paused_task_name = ""
        asst._barge_in_followup_offered = False

    # ── Hybrid offline mode ──────────────────────────────────────────────────

    def _flush_playback(self, reason: str = "") -> None:
        """Hard-stop speaker + drain playback queue (standby / wake hygiene)."""
        asst = self._asst
        try:
            asst._set_speaking(False, interrupted=True)
        except Exception:
            try:
                asst._set_speaking(False)
            except Exception:
                pass
        try:
            asst._hard_stop_speaker()
        except Exception:
            pass
        q = getattr(self, "audio_in_queue", None)
        if q is not None:
            try:
                while not q.empty():
                    q.get_nowait()
            except Exception:
                pass
        # Suppress transcription-triggered barge-in briefly so residual
        # ASR fragments from the previous turn don't fire a false interrupt
        # right after we go idle or wake up.
        asst._last_barge_in_ts = time.monotonic()
        asst._barge_in_suppress_until = time.monotonic() + 1.2
        if reason:
            log.debug(f"Playback flushed ({reason})")

    def set_barge_in_enabled(self, enabled: bool) -> None:
        """Flip the live barge-in flag checked in the mic audio callback.
        Called from state_engine/user_settings.py right after the
        persisted setting is saved, so voice commands like
        "turn barge-in off" take effect on the very next audio frame."""
        asst = self._asst
        asst._barge_in_enabled = bool(enabled)
        if not enabled:
            # Drop any in-progress hot-frame run so a stale one can't fire
            # a barge-in right after re-enabling.
            asst._interrupt_hot_frames = 0
            asst._interrupt_fast_tier = False
            asst._interrupt_env_mic.clear()
            asst._interrupt_env_tts.clear()


__all__ = ["BargeInController"]
