"""
core/wake_controller.py — Wake-word handling, greetings, observe context — extracted from GamaAssistant (Phase 1).
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


class WakeController:
    """Wake-word handling, greetings, observe context — extracted from GamaAssistant (Phase 1)."""

    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    def _on_wake_engine_label(self, label: str) -> None:
        """Runs on the asyncio loop (dispatched via call_soon_threadsafe
        from the mic audio thread). Handles a debounced detection from
        the local wake word engine — either the wake phrase itself, or
        one of the configured interrupt words.
        """
        asst = self._asst
        if label == "wake":
            if asst._awake:
                # Already ACTIVE — ignore clap/wake (do not re-ack).
                try:
                    asst._runtime.on_interaction("wake while active")
                except Exception:
                    pass
                asst._schedule_auto_sleep()
                asst._sync_clap_arm()
                return

            wake_detected_ts = time.monotonic()

            # Drop any lagging playback from the previous ACTIVE turn so
            # wake never continues an old answer mid-sentence.
            asst._flush_playback(reason="wake word")

            # Instant wake into ACTIVE. Speaker verification is reserved for
            # DESTRUCTIVE tools only (security/trust_levels.py).
            asst._wake_gama()
            asst._sync_clap_arm()
            try:
                asst._session_mgr.start_session(reason="wake word")
            except Exception:
                pass

            with asst._speaking_lock:
                asst._last_speaking_end_ts = 0.0

            try:
                asst.ui.emit_event(
                    "WakeWordDetected",
                    latency_sec=round(time.monotonic() - wake_detected_ts, 4),
                )
                asst.ui.emit_event("SleepExited")
            except Exception:
                pass
            asst.ui.write_log('<span style="color:#00ff88">Gama is awake!</span>')
            log.info("Local wake word detected — ACTIVE mode.")

            asst._schedule_auto_sleep()
            # If the user asked something while observing (e.g. "what time
            # is it") and then said only "gama?", answer that — don't make
            # them repeat the question. Otherwise the usual "Yes, Sir?".
            pending = getattr(asst, "_observe_pending_request", None)
            if pending:
                asyncio.ensure_future(asst._answer_pending_observe_request(pending))
            else:
                asyncio.ensure_future(asst._send_wake_ack())
            return

        # Anything else configured under interrupt_words: "stop", "cancel",
        # "listen", etc. Only meaningful while GAMA is talking or asleep-ish.
        # Same echo-correlation gate as the amplitude barge-in path — Vosk's
        # phoneme matcher can trigger on Gama's own TTS bleed just as easily
        # as on real speech, so we reuse whatever envelope history the
        # amplitude path has already collected this window to reject echo.
        # Fully synchronous — no executor hop, so this can never stall.

        # A repeating alert (voice/event_voice.py) runs on the local voice-
        # model engine, not through Gemini's audio session, so it isn't
        # covered by the `was_speaking`/asst._speaking gate below. Any
        # "stop"/"cancel" word should silence it immediately regardless.
        if label in ("stop", "cancel"):
            try:
                from voice.event_voice import stop_alert as _stop_alert
                _stop_alert()
            except Exception as exc:
                log.debug(f"stop_alert() on interrupt word failed (non-fatal): {exc}")

        # Energy / echo-correlation interrupt path DISABLED.
        # Barge-in is handled solely by transcription matching in _receive_audio.
        # (stop/cancel still silences local alerts above.)

        if label == "listen":
            # Guarantee GAMA is awake and ready for the next sentence,
            # even if it was mid-response or drifting toward auto-sleep.
            asst._awake = True
            asst.ui.set_state(asst._awake_state())

    async def _send_wake_ack(self) -> None:
        """Speak a short wake acknowledgement once.

        Prefer local TTS for the fixed "Yes, Sir?" line. Routing it through
        the Live model often causes a second, delayed utterance after the
        real answer is already playing (model continues / proactivity), and
        mid-turn system text is a known contributor to Live 1011 closes.

        Guards:
          - cooldown between acks
          - skip while Gama is already speaking or just finished a reply
          - skip during post-reconnect quiet window
        """
        asst = self._asst
        try:
            now = time.monotonic()
            if now - float(getattr(asst, "_last_wake_ack_ts", 0) or 0) < float(
                getattr(asst, "_wake_ack_cooldown_s", 4.0)
            ):
                log.debug("[wake-ack] suppressed — cooldown active")
                return
            with asst._speaking_lock:
                speaking = bool(asst._speaking)
                last_end = float(getattr(asst, "_last_speaking_end_ts", 0) or 0)
            if speaking:
                log.debug("[wake-ack] suppressed — already speaking")
                return
            if last_end and (now - last_end) < 1.5:
                log.debug("[wake-ack] suppressed — reply just finished")
                return
            if now < float(getattr(asst, "_session_quiet_until", 0) or 0):
                log.debug("[wake-ack] suppressed — post-reconnect quiet window")
                return

            asst._last_wake_ack_ts = now
            # Local TTS: deterministic, no extra model turn, no 1011 risk.
            asst._speak_exact(_WAKE_ACK_LINE, kind="wake_ack")
        except Exception as exc:
            log.debug(f"Wake acknowledgement failed: {exc}")

    async def _send_offline_wake_greeting(self, verified_owner: bool = False) -> None:
        """Speak a wake greeting via local TTS when Gemini is unavailable."""
        asst = self._asst
        try:
            if verified_owner:
                from datetime import timezone, timedelta
                IST = timezone(timedelta(hours=5, minutes=30))
                _h = datetime.now(IST).hour
                if 5 <= _h < 12:
                    _tod_offline = "Good morning, Sir"
                elif 12 <= _h < 17:
                    _tod_offline = "Good afternoon, Sir"
                elif 17 <= _h < 21:
                    _tod_offline = "Good evening, Sir"
                else:
                    _tod_offline = "Good night, Sir"
                text = random.choice([
                    f"{_tod_offline}. Running offline — PC commands and voice controls still work.",
                    f"{_tod_offline}. Offline mode for now. Everything else still works fine.",
                    f"{_tod_offline}. Gemini's unreachable, so I'm on local mode — commands and voice still work.",
                ])
            else:
                text = "Hello, I'm in offline mode. How can I help?"
            asst.ui.write_log(
                f'<span style="color:#5ab8cc">GAMA (offline):</span> {text}'
            )
            await asyncio.to_thread(asst._speak_exact, text, kind="result")
        except Exception as exc:
            log.debug(f"Offline wake greeting failed (non-fatal): {exc}")

    async def _send_wake_greeting(self, verified_owner: bool = False) -> None:
        asst = self._asst
        try:
            if verified_owner:
                # Inject the current IST time directly so Gemini doesn't need
                # to call system_info(action='time') before greeting — that
                # extra round-trip was the main cause of the "stuck on
                # listening with no response" latency after waking.
                # Always use IST (UTC+5:30) regardless of host system timezone.
                from datetime import timezone, timedelta
                IST = timezone(timedelta(hours=5, minutes=30))
                now = datetime.now(IST)
                h, m = now.hour, now.minute
                period = "AM" if h < 12 else "PM"
                h12 = h % 12 or 12
                time_str = f"{h12}:{m:02d} {period} IST"
                if 5 <= h < 12:
                    tod = "morning"
                elif 12 <= h < 17:
                    tod = "afternoon"
                elif 17 <= h < 21:
                    tod = "evening"
                else:
                    tod = "night"

                # Session-restore offers removed — not JARVIS-like behavior.
                # GAMA should greet and then simply listen/act on context,
                # never proactively announce what was open last session.
                prompt = (
                    f"[SYSTEM] You just woke up. Current time: {time_str} ({tod})."
                    " Respond with ONE brief, respectful question showing you're "
                    "attentively waiting for Sir's command — e.g. \"Yes, Sir?\" — calm, "
                    "confident, economical with words. Vary the wording every time. "
                    "Do NOT state that you're awake, do NOT give a status update or "
                    "briefing, do NOT say 'voice verified'. "
                    "Do NOT call system_info for the time — it is already provided above. "
                    "Do NOT mention or offer to restore any previous session, app, or "
                    "activity unless Sir brings it up first."
                )
            else:
                prompt = "[SYSTEM] You just woke up. Greet the user. Keep it polite and brief."
            await asst._send_system_text(prompt)
        except Exception as exc:
            log.debug(f"Wake greeting failed (non-fatal): {exc}")

    def _is_first_wake_today(self) -> bool:
        """True once per calendar day — marks itself done immediately so a
        second wake a moment later doesn't double-fire the briefing."""
        asst = self._asst
        try:
            from memory import long_term as lt
            today = datetime.now().date().isoformat()
            if lt.meta_get("last_daily_briefing_date") == today:
                return False
            lt.meta_set("last_daily_briefing_date", today)
            return True
        except Exception as exc:
            log.debug(f"Daily-briefing date check failed (non-fatal): {exc}")
            return False

    async def _send_daily_briefing(self) -> None:
        """First owner wake-up of the day: gather weather / pending
        reminders / yesterday's recap / news and have Gemini narrate it as
        one short, calm, proactive briefing. Falls back to the plain
        wake greeting if nothing could be gathered or anything goes wrong.
        """
        return  # daily_briefing removed
        asst = self._asst
        try:
            asst.ui.write_log('<span style="color:#00d4ff">Putting together your briefing...</span>')
# REMOVED:             # daily_briefing removed
            prompt = await asyncio.to_thread(build_briefing_prompt)
            if not prompt:
                await asst._send_wake_greeting(verified_owner=True)
                return
            await asst._send_system_text(prompt)
        except Exception as exc:
            log.debug(f"Daily briefing failed (non-fatal): {exc}")
            await asst._send_wake_greeting(verified_owner=True)

    def _on_local_transcript(self, result) -> None:
        """Called (from a worker thread) when the local Whisper+speaker pipeline
        finishes an authorized utterance (~200-400ms after speech ends).

        Three jobs:
          1. Log the timing breakdown for perf analysis.
          2. Echo guard check — final safety net to drop any transcript that
             looks like Gama's own TTS output (e.g. a fast acoustic echo that
             got through both the playback-state gate and the VAD gate).
          3. Feed the transcript into match_fast_intent — this is a
             speaker-VERIFIED fast path that beats Gemini's ~800ms round trip
             for every command in the rule table, not just the Vosk subset.
             The dedup cache in already_fast_routed() silently absorbs any
             duplicate that arrives from Vosk or Gemini moments later.
        """
        asst = self._asst
        try:
            log.info(
                f"[local-pipeline] '{result.text}' — speaker={result.speaker} "
                f"conf={result.confidence:.2f} stt={result.stt_latency_ms}ms "
                f"verify={result.verify_latency_ms}ms total={result.total_latency_ms}ms "
                f"source={getattr(result, 'source', 'USER')}"
            )
        except Exception:
            pass
        # --- Remember this clean, VAD-bounded, owner-verified utterance ---
        # so the security gate (_handle_tool_call) can re-verify DESTRUCTIVE/
        # SENSITIVE tool calls against the exact same clip the wake/ambient
        # pipeline just verified nicely, instead of an arbitrary rolling
        # buffer that may have decayed or filled with silence/TTS bleed by
        # the time a Gemini tool call actually arrives.
        try:
            audio = getattr(result, "audio", None)
            if audio is not None:
                asst._last_verified_pcm = audio.tobytes()
                asst._last_verified_pcm_ts = time.monotonic()
        except Exception:
            pass
        try:
            text = (getattr(result, "text", "") or "").strip()
            if text and asst._awake and not asst._enrolling and asst._loop:
                # Echo guard: final check — even though voice/pipeline.py already
                # ran should_block(), this catches any transcript that squeaked
                # through between the segment-start check and the result delivery.
                # echo_guard removed — no transcript block

                # ── Conversation Session Manager: intent gate ────────────
                # Classify whether this utterance is actually directed at
                # Gama (vs self-talk / a human-to-human aside / unknown
                # background chatter) before it reaches the tool-dispatch
                # path. Inside an ACTIVE session this uses a relaxed
                # threshold (no wake word needed); the session timer is
                # reset on every directed hit. See voice/session_manager.py.
                try:
                    speaker = getattr(result, "speaker", None)
                    verdict = asst._session_mgr.evaluate(
                        text,
                        speaker_verified=bool(getattr(result, "authorized", True)),
                        speaker=speaker,
                    )
                except Exception:
                    log.debug("[session] evaluate() failed — failing open (non-fatal)", exc_info=True)
                    verdict = None

                if verdict is not None and verdict.intent.value != "directed_to_gama":
                    log.info(
                        f"[session] Ignored — {verdict.intent.value} "
                        f"(confidence={verdict.confidence:.2f}): {text!r}"
                    )
                    return

                # Reaches here only for an owner-verified utterance —
                # voice/pipeline.py routes failed verification to
                # _on_local_unauthorized instead. Safe to allow launches.
                asst._loop.call_soon_threadsafe(asst._on_fast_intent_text, text, True)
        except Exception:
            pass

    def _on_local_unauthorized(self, result) -> None:
        """Called when the local pipeline rejects a command for failing
        owner voice verification. Evidence capture already happened
        inside voice/pipeline.py; this just logs it to the UI."""
        asst = self._asst
        try:
            asst.ui.write_log(
                f'<span style="color:#ff3355">🔒 Unauthorized voice command rejected '
                f'locally (sim {result.similarity:.2f}).</span>'
            )
        except Exception:
            pass

    def _sync_clap_arm(self) -> None:
        """Clap-wake only while asleep/observing — never while ACTIVE."""
        asst = self._asst
        try:
            listener = getattr(asst, "_wake_listener", None)
            if listener is not None and hasattr(listener, "set_clap_armed"):
                listener.set_clap_armed(not bool(asst._awake))
        except Exception:
            pass

    def _record_observe_context(self, text: str) -> None:
        """Store overheard lines during OBSERVE for later wake / direct-address."""
        asst = self._asst
        text = (text or "").strip()
        if not text or len(text) < 3:
            return
        # Ignore pure wake-word fragments so they never become "pending questions".
        tl = text.lower().strip().strip(".!,;:?")
        wake_set = set(getattr(asst, "_wake_phrases", set()) or set())
        wake_set.update({"gama", "jarvis"})
        if tl in wake_set:
            return

        buf = getattr(asst, "_observe_context", None)
        if buf is None:
            asst._observe_context = []
            buf = asst._observe_context
        if buf and buf[-1].lower() == text.lower():
            return
        buf.append(text)
        max_n = getattr(asst, "_observe_context_max", 12)
        if len(buf) > max_n:
            del buf[: len(buf) - max_n]

        # Track the latest line that looks like a question/command so a
        # bare "gama?" can answer it instead of only saying "Yes, Sir?".
        if asst._looks_like_pending_request(text):
            asst._observe_pending_request = text
            log.info(f"[observe] pending request buffered: {text[:100]!r}")
        try:
            asst._runtime.update_conversation(
                recent_facts=list(buf[-6:]),
                last_user_intent="observe_overhear",
                pending_questions=(
                    [asst._observe_pending_request]
                    if getattr(asst, "_observe_pending_request", None)
                    else []
                ),
            )
        except Exception:
            pass
        log.debug(f"[observe] buffered context: {text[:80]!r}")

    def _looks_like_pending_request(self, text: str) -> bool:
        """Heuristic: is this an answerable question/command overheard in OBSERVE?"""
        asst = self._asst
        t = (text or "").lower().strip()
        if len(t) < 4:
            return False
        markers = (
            "what", "why", "how", "when", "where", "who", "which", "whose",
            "is it", "are you", "can you", "could you", "would you", "will you",
            "tell me", "remind", "open", "close", "set", "start", "stop",
            "play", "search", "find", "time", "date", "weather", "status",
            "volume", "brightness", "mute", "help", "please", "do i", "did i",
            "should", "?",
        )
        return any(m in t for m in markers)

    def _is_direct_address(self, text: str) -> bool:
        """True when the user is clearly talking *to* Gama, not just nearby.

        Examples that should wake + answer from OBSERVE:
          "what do you think gama"
          "gama, what's the weather"
          "hey gama open chrome"
        Pure wake word alone ("gama") is handled separately as wake-ack.
        Uses text_wake aliases so Whisper mis-hears (gamma, gemma, …) count.
        """
        asst = self._asst
        t = (text or "").lower().strip().strip(".!,;:?")
        if not t:
            return False
        # Prefer flexible name detection (mid-sentence OK).
        has_name = False
        try:
            from voice.text_wake import contains_wake_name, strip_wake_name
            has_name = contains_wake_name(t)
            residual = strip_wake_name(t) if has_name else t
        except Exception:
            residual = t
            names = set(getattr(asst, "_wake_phrases", set()) or set())
            names.update({"gama", "jarvis", "assistant", "gamma", "gemma"})
            has_name = any(n and n in t for n in names)
            if has_name:
                for n in sorted(names, key=len, reverse=True):
                    residual = residual.replace(n, " ")
                residual = " ".join(residual.split()).strip()
        if not has_name:
            return False
        # Strip residual fluff — if anything substantial remains, it's a
        # real request, not just the wake word.
        residual = " ".join((residual or "").split()).strip()
        if len(residual) < 3:
            return False
        # Light intent signals (question / command verbs)
        intent_markers = (
            "what", "why", "how", "when", "where", "who", "which",
            "do you", "can you", "could you", "would you", "will you",
            "tell me", "remind", "open", "close", "set", "start", "stop",
            "play", "search", "find", "think", "explain", "help", "please",
            "status", "time", "weather", "volume", "mute", "brightness",
        )
        if any(m in t for m in intent_markers):
            return True
        # Name + at least a few content words counts as directed.
        return len(residual.split()) >= 2

    async def _answer_pending_observe_request(self, pending: str) -> None:
        """Answer a question overheard in OBSERVE after a bare wake word."""
        asst = self._asst
        asst._observe_pending_request = None
        ctx_lines = list(getattr(asst, "_observe_context", []) or [])
        ctx_block = ""
        if ctx_lines:
            ctx_block = "\nRecent overheard context: " + " | ".join(ctx_lines[-8:])
        summary = ""
        try:
            summary = asst._runtime.conversation.summary_block() or ""
        except Exception:
            pass
        if summary:
            ctx_block += f"\n{summary}"

        prompt = (
            "[OBSERVE_WAKE] While you were in OBSERVE the user asked something, "
            "then said your wake word. Do NOT only say 'Yes, Sir?'. Answer the "
            "pending request now, briefly and directly.\n"
            f"Pending request: {pending}"
            f"{ctx_block}"
        )
        log.info(f"Answering pending observe request: {pending[:120]!r}")
        asst.ui.write_log(
            '<span style="color:#00ff88">⚡ Answering what you asked while observing…</span>'
        )
        try:
            await asst._send_system_text(prompt)
        except Exception as exc:
            log.error(f"Pending-observe answer failed: {exc}")
            try:
                asst._speak_exact(_WAKE_ACK_LINE, kind="wake_ack")
            except Exception:
                pass
        if ctx_lines:
            asst._observe_context = ctx_lines[-3:]

    async def _wake_from_direct_address(self, user_text: str) -> None:
        """OBSERVE → ACTIVE because the user addressed Gama with a request.

        Injects buffered observe context so the answer can use what was
        overheard while silent, then asks the model to answer the request
        (not just say "Yes, Sir?").
        """
        asst = self._asst
        asst._flush_playback(reason="direct address wake")
        asst._wake_gama()
        try:
            asst._session_mgr.start_session(reason="direct address while observing")
        except Exception:
            pass
        try:
            asst._runtime.on_interaction("direct address")
        except Exception:
            pass
        asst.ui.set_state("LISTENING")
        asst.ui.write_log(
            '<span style="color:#00ff88">⚡ Addressed while observing — answering.</span>'
        )
        log.info(f"Direct address from OBSERVE: {user_text[:120]!r}")

        # Clear pending — this utterance is the request itasst.
        asst._observe_pending_request = None

        ctx_lines = list(getattr(asst, "_observe_context", []) or [])
        summary = ""
        try:
            summary = asst._runtime.conversation.summary_block() or ""
        except Exception:
            summary = ""

        ctx_block = ""
        if ctx_lines:
            joined = " | ".join(ctx_lines[-8:])
            ctx_block = f"\nRecent overheard context while observing: {joined}"
        if summary:
            ctx_block += f"\n{summary}"

        # Strip wake name so the model focuses on the real ask.
        try:
            from voice.text_wake import strip_wake_name
            request_body = strip_wake_name(user_text) or user_text
        except Exception:
            request_body = user_text

        prompt = (
            "[OBSERVE_WAKE] You were in OBSERVE (silent listening only). "
            "The user just addressed you by name in their sentence. "
            "Do NOT say only 'Yes, Sir?'. Answer their request directly and "
            "concisely. Use the overheard context below when it is relevant "
            "to what they asked or what was being discussed.\n"
            f"User request: {request_body}\n"
            f"Full utterance: {user_text}"
            f"{ctx_block}"
        )
        try:
            await asst._send_system_text(prompt)
        except Exception as exc:
            log.error(f"Direct-address prompt failed: {exc}")
            try:
                asst._speak_exact("Yes, Sir?", kind="wake_ack")
            except Exception:
                pass
        if ctx_lines:
            asst._observe_context = ctx_lines[-3:]


    def _on_fast_intent_text(self, text: str, verified: bool = True) -> None:
        """Runs on the asyncio loop. A final transcript came back from the
        local fast-intent recognizer while awake."""
        asst = self._asst
        try:
            voice_mgr = getattr(asst, "_voice_manager", None)
            if voice_mgr is not None:
                if voice_mgr.try_answer_status_question(text) is not None:
                    return
                if voice_mgr.try_handle_control_command(text) is not None:
                    return
        except Exception:
            log.debug("FullDuplexVoiceManager shortcut check failed (non-fatal)", exc_info=True)

        from core.fast_intent import match_fast_intent, mark_fast_routed


        matched = match_fast_intent(text)
        if not matched:
            # Tier-2: Flash-Lite routing — handles commands too varied for
            # static regex but simple enough to skip the Live round-trip.
            # Only on the speaker-verified Whisper path (verified=True) so
            # ambient audio cannot bypass the speaker-verification gate.
            if verified:
                asyncio.ensure_future(asst._route_with_flash_lite(text))
            return
        tool, tool_args, label = matched

        # ── Direct task cancel — no Gemini round-trip needed ─────────────
        # When the matched rule is _direct_task_cancel, cancel whatever task
        # is currently running or was paused by barge-in immediately, then
        # speak a brief confirmation via local TTS (skipping Gemini entirely
        # so the cancel feels instant even mid-stream).
        if tool == "_direct_task_cancel":
            try:
                from voice.event_voice import stop_alert as _stop_alert
                _stop_alert()
            except Exception as exc:
                log.debug(f"stop_alert() on direct cancel failed (non-fatal): {exc}")
            try:
                from core.task_queue import task_queue
                cancelled = False
                # Priority 1: cancel the task that barge-in auto-paused
                if asst._barge_in_paused_task_id:
                    if task_queue.cancel(asst._barge_in_paused_task_id):
                        task_name = asst._barge_in_paused_task_name or "task"
                        asst._clear_barge_in_task_state()
                        asst._speak_exact(f"Cancelled — {task_name} stopped.", kind="result")
                        asst.ui.write_log(
                            f'<span style="color:#ff3355">🛑 Task cancelled by voice command.</span>'
                        )
                        cancelled = True
                # Priority 2: cancel whatever is currently RUNNING
                if not cancelled:
                    running_id = task_queue.current_task_id()
                    if running_id:
                        task = task_queue._tasks.get(running_id)
                        task_name = task.name if task else "task"
                        if task_queue.cancel(running_id):
                            asst._speak_exact(f"Stopped, Sir.", kind="result")
                            asst.ui.write_log(
                                f'<span style="color:#ff3355">🛑 Running task cancelled.</span>'
                            )
                            cancelled = True
                if not cancelled:
                    # Nothing was running — let Gemini handle it naturally
                    log.debug("_direct_task_cancel: no active task to cancel.")
            except Exception as exc:
                log.debug(f"_direct_task_cancel failed (non-fatal): {exc}")
            return  # handled — don't fall through to normal tool dispatch

        # ── Guard: never auto-launch anything from unverified audio ──────
        # Fast-intent may receive transcripts from ambient/unverified
        # sources. Launch-capable tools require verified=True (owner-
        # verified path) so ambient chatter cannot open apps or restart
        # the assistant. The secondary unconstrained Vosk ASR was removed
        # (perf audit); this guard remains for any residual unverified path.
        _LAUNCH_TOOLS = {"open_app", "automation_engine", "computer_agent", "restart_self"}
        if tool in _LAUNCH_TOOLS and not verified:
            log.info(
                f"⚠️ Fast intent '{label}' -> {tool}({tool_args}) suppressed: "
                "unverified speaker (ambient audio), launch tools require "
                "an owner-verified voice."
            )
            return

        async def _run():
            t0 = time.monotonic()
            try:
                result = await asyncio.to_thread(_execute_tool, tool, tool_args)
            except Exception as exc:
                log.debug(f"Fast intent '{label}' execution failed (non-fatal): {exc}")
                return
            from core.fast_intent import is_failure_result
            if is_failure_result(result):
                # Don't dedup a failure — e.g. open_app couldn't find
                # "physics wallah" as an app. Gemini gets the same audio
                # moments later and needs to actually retry (open it as a
                # website, or fall back to a web search) instead of just
                # being handed back this exact same failure string again.
                log.info(
                    f"⚡ Fast intent '{label}' -> {tool}({tool_args}) failed "
                    f"({result!r}) — not caching, letting Gemini retry."
                )
            else:
                mark_fast_routed(tool, tool_args, result)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.info(f"⚡ Fast intent '{label}' -> {tool}({tool_args}) in {elapsed_ms:.0f} ms: {result}")
            asst.ui.emit_event("FastIntent", label=label, tool=tool, latency_ms=round(elapsed_ms, 1))

            # Let Gemini narrate a short confirmation without re-deciding
            # what to do — this is the only reason we still touch the
            # session for a fast-routed command.
            if asst.session:
                try:
                    await asst._send_system_text(
                        f"[SYSTEM] The command has already been executed directly "
                        f"(no need to call a tool). Result: {result}. Briefly "
                        f"confirm this to the user in one short sentence."
                    )
                except Exception as exc:
                    log.debug(f"Fast intent confirmation send failed (non-fatal): {exc}")
            elif asst._is_offline():
                # Offline: Gemini can't narrate — speak a brief confirmation
                # via local TTS so the user knows the command ran.
                try:
                    _conf = str(result or "Done, Sir.").strip()
                    # Keep confirmation short: prefer the label or a trimmed result
                    if len(_conf) > 120 or not _conf:
                        _conf = f"{label} — done, Sir."
                    asst._speak_exact(_conf, kind="result")
                except Exception as _exc:
                    log.debug(f"[offline] Fast intent TTS confirmation failed: {_exc}")

        asyncio.ensure_future(_run())

    # Flash-Lite routing prompt — kept module-level so it isn't rebuilt
    # on every call. Intentionally terse: fewer tokens = faster TTFT.
    _FLASH_LITE_PROMPT = (
        "Route voice commands to tools. Return JSON only — no markdown, no explanation.\n"
        'Format: {"tool":"<name>","args":{<args>}} or {"tool":null}\n\n'
        "Tools:\n"
        "  computer_settings  action=volume_up|volume_down|mute|brightness_up|brightness_down\n"
        "  open_app           app_name=STRING\n"
        # "  instagram          action=login|logout|notifications|force_check|unread|read|send|set_credentials|set_username|change_username|set_password|change_password|resolve_challenge|status  [username=STRING] [message=STRING] [password=STRING] [code=STRING]\n"
        "  system_info        action=time|battery|cpu|ram|network|weather\n"
        "  reminder           action=set, message=STRING, in_minutes=N\n"
        "                     action=list\n"
        "                     action=cancel_all\n"
        "  notes              action=list\n"
        "  web_search         query=STRING\n"
        "  calculator         expression=STRING\n"
        "  music              action=play|pause|stop|next|previous\n\n"
        # "INSTAGRAM RULE: ANY command mentioning instagram — connect, login, logout, DM,\n"
        # "message, notifications, check instagram — MUST use the instagram tool.\n"
        # "NEVER route instagram to open_app.\n\n"
        # ── H1: Gemini-First gate ─────────────────────────────────────────
        # The Gemini Live model answering this user already has strong
        # general knowledge (facts, definitions, history, science, math,
        # coding, world knowledge).  web_search / edge_search must ONLY be
        # used when the user explicitly asks to search the web, or when the
        # answer requires real-time / up-to-date data (live scores, today's
        # news, current prices, etc.).  For everything Gemini already knows,
        # return {"tool":null} so the Live session answers directly —
        # faster, cheaper, no unnecessary browser automation.
        'GEMINI-FIRST RULE: return {"tool":null} for any question Gemini\n'
        "can answer from its own knowledge — definitions, explanations,\n"
        "history, science, math, coding, general facts, world knowledge.\n"
        "Use web_search ONLY when the user explicitly says 'search', 'look\n"
        "up', 'find online', 'latest news', 'current price', or when the\n"
        "answer is clearly time-sensitive (live scores, today's weather\n"
        "forecast, stock prices, breaking news).\n\n"
        'Return {"tool":null} when:\n'
        "  - Command is complex, multi-step, or ambiguous\n"
        "  - You are not ≥90% confident about the exact tool + args\n"
        "  - Command is conversational, requires judgment, or needs reasoning\n"
        "  - The question is factual/general knowledge Gemini already knows\n\n"
        'Examples:\n'
        '  "volume up"                    → {"tool":"computer_settings","args":{"action":"volume_up"}}\n'
        '  "what time is it"              → {"tool":"system_info","args":{"action":"time"}}\n'
        '  "open chrome"                  → {"tool":"open_app","args":{"app_name":"chrome"}}\n'
        # '  "connect instagram"            → {"tool":"instagram","args":{"action":"login"}}\n'
        # '  "login instagram"              → {"tool":"instagram","args":{"action":"login"}}\n'
        # '  "check instagram"              → {"tool":"instagram","args":{"action":"notifications"}}\n'
        '  "search for python tutorials"  → {"tool":"web_search","args":{"query":"python tutorials"}}\n'
        '  "who is elon musk"             → {"tool":null}  (Gemini knows this)\n'
        '  "what is python"               → {"tool":null}  (Gemini knows this)\n'
        '  "explain recursion"            → {"tool":null}  (Gemini knows this)\n'
        '  "capital of japan"             → {"tool":null}  (Gemini knows this)\n'
        '  "latest iphone price"          → {"tool":"web_search","args":{"query":"latest iphone price"}}\n'
        '  "what can you do"              → {"tool":null}'
    )

    # Keywords that signal a request genuinely needs live internet data.
    # Used as a secondary guard in _route_with_flash_lite to prevent
    # Flash Lite from routing factual questions to web_search even when
    # the prompt instruction is ignored.  (H1 fix)
    _WEB_SEARCH_TRIGGERS = frozenset({
        "search", "look up", "look it up", "find online", "google",
        "bing", "browse", "latest", "current", "today", "right now",
        "live", "news", "score", "price", "stock", "weather forecast",
        "breaking", "recent", "update", "trending",
    })


__all__ = ["WakeController"]
