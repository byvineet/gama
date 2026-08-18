"""
core/tool_controller.py — Tool call handling + flash-lite routing — extracted from GamaAssistant (Phase 1).
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

import random

from utils.logger import get_logger
from core.tool_dispatch import _execute_tool
from core.tool_declarations import PROCESSING_ACK_LINES, _TOOL_ACK_MAP
from core.interrupt_calibration import _SYSTEM_SLEEP_RE

log = get_logger(__name__)

# Tools whose FunctionResponse must return the REAL result (the model waits):
# security-critical actions where authorize() + the outcome matter synchronously.
# Everything else runs via _ada_bg_tool and reports back with a SYSTEM_ALERT.
# Single source of truth — previously two drifted copies (3 vs 4 entries)
# silently sent process_manager down the ungated background path.
_BLOCKING_TOOLS = frozenset({
    "computer_settings",      # shutdown/restart/sleep/lock need confirm
    "set_confirmation_code",
    "process_manager",        # kill / kill_all
    "startup_manager",        # add/remove startup entries
})


class ToolController:
    """Tool call handling + flash-lite routing — extracted from GamaAssistant (Phase 1)."""

    def __init__(self, assistant: Any = None) -> None:
        self._asst = assistant

    def attach(self, assistant: Any) -> None:
        self._asst = assistant

    async def _route_with_flash_lite(self, text: str) -> None:
        """DISABLED — Gemini Live is the only conversational model.

        Secondary routing LLMs (Flash-Lite / Groq / Llama) have been removed
        per the JARVIS redesign. Local fast-intent still short-circuits pure
        PC commands; everything else is handled by the Live session.
        """
        asst = self._asst
        return
        # --- dead code below retained only for reference; never executed ---
        """Tier-2 router: Gemini 3.6 Flash classifies commands that the local
        fast-intent regex didn't match.

        Routing stack (fastest to most powerful):
          1. Local fast-intent regex (Vosk, <20 ms) — already ran, returned None
          2. Gemini 3.6 Flash text call (this method, ~200-400 ms) — simple commands
          3. Gemini Live (audio session, ~800-1500 ms) — everything else

        Gemini 3.6 Flash capabilities used here:
          • response_mime_type="application/json" — structured output, no JSON
            parsing guesswork; eliminates the ```json fencing hacks entirely.
          • 1M token context — inject full World Model + Context Awareness
            snapshot so routing is grounded in what is actually on screen.
          • Workflow Learner predictions — inject recently-learned action
            sequences so the router can anticipate follow-on commands.
          • Phase 3 Context Awareness — resolve_command_context() enriches
            vague references ("that file", "it") before routing.

        Returns a JSON tool call or null.  When it returns a tool:
          - The tool is executed via _execute_tool (same path as fast-intent)
          - mark_fast_routed() dedupes so Gemini Live doesn't re-run it
          - The Live session is notified "already done" so it just narrates

        Only runs on the speaker-verified Whisper path — enforced in the caller
        (_on_fast_intent_text, verified=True gate).
        """
        client = getattr(asst, "_genai_client", None)
        if client is None:
            return
        if asst._is_offline():
            return  # no API available; Gemini Live isn't running either

        import json as _json
        try:
            from google.genai import types as _gtypes
        except ImportError:
            return

        try:
            t0 = time.monotonic()

            # ── Phase 3: Context Awareness — resolve vague references ─────────
            # Calls context_awareness.get_context_for_command(text) to resolve
            # "that file", "it", "there", etc. and inject active desktop state.
            _ctx_block = ""
            try:
                from core.jarvis_bootstrap import resolve_command_context, get_world_context_block
                _cmd_ctx = resolve_command_context(text)
                _world_ctx = get_world_context_block()
                _ctx_parts = []
                if _world_ctx:
                    _ctx_parts.append(_world_ctx)
                if _cmd_ctx:
                    _ctx_lines = []
                    for _ckey in ("active_app", "current_folder", "clipboard", "session_mode"):
                        _cval = _cmd_ctx.get(_ckey)
                        if _cval:
                            _ctx_lines.append(f"  {_ckey}: {str(_cval)[:80]}")
                    if _ctx_lines:
                        _ctx_parts.append("[COMMAND CONTEXT]\n" + "\n".join(_ctx_lines))
                if _ctx_parts:
                    _ctx_block = "\n\n".join(_ctx_parts)
            except Exception:
                pass

            # ── Phase 6: Workflow Learner — predicted next-action hint ────────
            # Surfaces patterns learned from this user's command sequences so
            # the router can make smarter tool selections on follow-on commands.
            _workflow_hint = ""
            try:
                from learning.workflow_learner import workflow_learner as _wl
                _recent = _wl.tracker.recent(n=5)
                _preds = _wl.predict_next(_recent, top_k=2)
                if _preds:
                    _hint_lines = ["[LEARNED WORKFLOW PREDICTIONS]"]
                    for _act, _conf in _preds:
                        _hint_lines.append(f"  • {_act} ({_conf:.0%})")
                    _workflow_hint = "\n".join(_hint_lines)
            except Exception:
                pass

            # Build enriched routing prompt — base + context + workflow hints.
            # Extra context only added when non-empty; keeps prompt minimal for
            # unambiguous commands (fast path stays fast).
            _extra_ctx = "\n\n".join(p for p in (_ctx_block, _workflow_hint) if p)
            _enriched_prompt = (
                asst._FLASH_LITE_PROMPT
                + (
                    f"\n\nCurrent context (use to resolve references and "
                    f"select the most relevant tool):\n{_extra_ctx}"
                    if _extra_ctx else ""
                )
            )

            def _call() -> str:
                # Gemini 3.6 Flash: response_mime_type enforces valid JSON
                # output natively — no markdown fences, no parse guesswork.
                _base_kwargs: dict = dict(
                    system_instruction=_enriched_prompt,
                    temperature=0.0,
                    max_output_tokens=120,
                    response_mime_type="application/json",
                )
                # NOTE: ROUTING_MODEL ("gemini-3.5-flash-lite") rejects
                # ThinkingConfig(thinking_budget=0) with a hard 400 Bad
                # Request instead of ignoring it — this was silently
                # breaking every single routing call (visible in logs as
                # repeated "400 Bad Request" right after each tool call),
                # so this whole fast-path router was never actually firing
                # and everything fell through to the slower Live session.
                # Try with thinking disabled first (cheapest/fastest), but
                # fall back to no thinking_config at all if the model
                # rejects it, instead of failing the call outright.
                try:
                    resp = client.models.generate_content(
                        model=ROUTING_MODEL,
                        contents=f"Command: {text}",
                        config=_gtypes.GenerateContentConfig(
                            thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
                            **_base_kwargs,
                        ),
                    )
                except Exception as _thinking_exc:
                    log.debug(
                        f"[flash-lite] ThinkingConfig rejected by {ROUTING_MODEL} "
                        f"({_thinking_exc}); retrying without it."
                    )
                    resp = client.models.generate_content(
                        model=ROUTING_MODEL,
                        contents=f"Command: {text}",
                        config=_gtypes.GenerateContentConfig(**_base_kwargs),
                    )
                return (resp.text or "").strip()

            loop = asyncio.get_running_loop()
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, _call),
                timeout=2.5,  # never let the router delay the Live response
            )
            latency_ms = (time.monotonic() - t0) * 1000

            if not raw:
                return

            # Safety strip — response_mime_type makes fences impossible, but
            # guard for older SDK versions that may not honour the MIME type.
            raw = raw.strip()
            for fence in ("```json", "```"):
                raw = raw.lstrip(fence)
            raw = raw.rstrip("```").strip()

            parsed = _json.loads(raw)
            tool = parsed.get("tool")
            if not tool:
                log.debug(
                    f"[flash-lite] '{text}' → null in {latency_ms:.0f} ms "
                    "(passed to Live session)"
                )
                return

            # ── H1: Gemini-First secondary guard ─────────────────────────
            # Even if Flash Lite routed to web_search / edge_search, veto
            # it unless the original text contains an explicit search intent
            # or a time-sensitive keyword.  This prevents factual questions
            # from burning a browser round-trip when Gemini Live can answer
            # directly in <100 ms.
            if tool in ("web_search", "edge_search"):
                _txt_lower = text.lower()
                _has_trigger = any(kw in _txt_lower for kw in asst._WEB_SEARCH_TRIGGERS)
                if not _has_trigger:
                    log.debug(
                        f"[flash-lite] Vetoed {tool}('{text}') — "
                        "no explicit search intent; passing to Live session (Gemini-First)."
                    )
                    return

            args = parsed.get("args", {})

            log.info(f"[flash-lite] '{text}' → {tool}({args}) in {latency_ms:.0f} ms")
            asst.ui.write_log(
                f'<span style="color:#00d4ff">⚡ Flash-Lite [{ROUTING_MODEL}]: {tool}</span>'
            )
            asst.ui.emit_event(
                "FlashLiteRouted",
                text=text,
                tool=tool,
                latency_ms=round(latency_ms, 1),
            )

            # Execute via the same dispatch and dedup path as local fast-intent.
            from core.fast_intent import mark_fast_routed as _mark, is_failure_result as _is_failure
            try:
                result = await asyncio.to_thread(_execute_tool, tool, args)
            except Exception as exc:
                log.debug(f"[flash-lite] Tool execution failed (non-fatal): {exc}")
                return

            if not _is_failure(str(result)):
                _mark(tool, args, str(result))
            log.info(f"[flash-lite] {tool} result: {result}")

            # Tell Live session: command already executed — just narrate it.
            if asst.session:
                try:
                    await asst._send_system_text(
                        "[SYSTEM] Flash-Lite already executed this command — "
                        f"do NOT call any tool. Result: {result}. "
                        "Confirm in one short sentence."
                    )
                except Exception as exc:
                    log.debug(f"[flash-lite] Live-session notify failed (non-fatal): {exc}")

        except asyncio.TimeoutError:
            log.debug(f"[flash-lite] Timeout on '{text}' — Live session will handle it")
        except _json.JSONDecodeError as exc:
            log.debug(f"[flash-lite] JSON parse error on '{raw}': {exc}")
        except Exception as exc:
            log.debug(f"[flash-lite] Routing failed (non-fatal): {exc}")

    async def _security_gate_check(self, name: str, args: dict) -> tuple[bool, str]:
        """Shared destructive-action gate used by BOTH tool paths.

        `_execute_single_tool_call` (blocking set) and `_ada_bg_tool`
        (everything else) must both pass through here — previously only the
        blocking path called security_gate.authorize, so file_controller /
        process_manager / terminal_command / advanced_automation ran
        ungated whenever Gemini sent them down the background path.
        Returns (allowed, deny_reason).
        """
        asst = self._asst

        _pcm_age = time.monotonic() - asst._last_verified_pcm_ts
        if asst._last_verified_pcm and _pcm_age < asst._last_verified_pcm_freshness_s:
            _recent_pcm = asst._last_verified_pcm
        else:
            _recent_pcm = None
            _vbl = getattr(asst, '_voice_buffer_lock', None)
            if _vbl is not None:
                with _vbl:
                    _recent_pcm = bytes(asst._voice_buffer) if asst._voice_buffer else None
            else:
                _recent_pcm = bytes(asst._voice_buffer) if asst._voice_buffer else None

        _verbal_flag = bool(args.get("verbal_confirmed") or args.get("confirmed"))
        _verbal_intent: Optional[bool] = None
        if _verbal_flag and asst._last_input_transcript:
            # Natural-language confirmation check — lets the user confirm
            # in their own words/language (English, Hindi, Hinglish, ...)
            # instead of matching a fixed word list.
            # Perf #1: use the pre-started background task launched when the
            # transcript first arrived — it may already be done, so the gate
            # pays near-zero extra latency instead of ~1,200–1,800ms.
            try:
                from security.authentication import classify_confirmation_intent
                _pre_task = asst._pending_verbal_intent_task
                if _pre_task is not None and not _pre_task.cancelled():
                    try:
                        _verbal_intent = await asyncio.wait_for(
                            asyncio.shield(_pre_task), timeout=0.5
                        )
                    except (asyncio.TimeoutError, Exception):
                        _verbal_intent = None
                if _verbal_intent is None:
                    loop = asyncio.get_running_loop()
                    _verbal_intent = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, classify_confirmation_intent,
                            asst._last_input_transcript, getattr(asst, "_genai_client", None),
                        ),
                        timeout=1.5,
                    )
            except Exception as _intent_exc:
                log.debug(f"[security] Verbal intent classification skipped: {_intent_exc}")
                _verbal_intent = None

        # Security (confirmation code / verbal confirmation) ONLY for
        # DESTRUCTIVE actions. Every other tool runs with zero blockage.
        _DESTRUCTIVE_HINTS = {
            "computer_settings", "file_controller", "process_manager",
            "startup_manager", "advanced_automation",
            "terminal_command",
        }
        _action = str(args.get("action", "") or "").lower()
        _destructive_actions = {
            "shutdown", "restart", "reboot", "sleep", "hibernate", "lock",
            "sign_out", "log_off", "format", "delete", "empty_recycle_bin",
            "kill", "kill_all", "terminate", "install", "uninstall",
            "add", "remove", "enable", "disable",
        }
        _needs_security = (
            name in _DESTRUCTIVE_HINTS and _action in _destructive_actions
        ) or any(k in (args or {}) for k in ("password", "passcode", "pin", "secret"))
        # Also escalate if free-text args contain destructive keywords
        _args_blob = " ".join(str(v).lower() for v in (args or {}).values() if isinstance(v, (str, int, float)))
        if any(kw in _args_blob for kw in ("format c:", "del /s", "rm -rf", "diskpart", "reg delete")):
            _needs_security = True

        if not _needs_security:
            return True, ""

        # authorize() is synchronous; run it in a worker so the event loop
        # (audio in/out, UI, other pending tool calls) never stalls.
        allowed, deny_reason = await asyncio.to_thread(
            security_gate.authorize,
            name, args,
            recent_pcm=_recent_pcm,
            confirmation_code=args.get("confirmation_code"),
            verbal_confirmed=_verbal_flag,
            transcript=asst._last_input_transcript,
            transcript_age_s=(time.monotonic() - asst._last_input_transcript_ts)
                if asst._last_input_transcript_ts else None,
            verbal_intent=_verbal_intent,
        )
        return allowed, deny_reason

    async def _execute_single_tool_call(self, fc):
        """Execute a single function call safely in a worker thread."""
        asst = self._asst
        name = fc.name
        args = dict(fc.args) if fc.args else {}
        call_id = getattr(fc, "id", None)
        _REDACT_KEYS = frozenset({
            "password", "confirmation_code", "code", "token",
            "api_key", "secret", "key", "auth", "credential",
        })
        _logged_args = {
            k: ("***" if k.lower() in _REDACT_KEYS else v)
            for k, v in args.items()
        }
        log.info(f"Tool call: {name}({_logged_args}) id={call_id}")

        # Intent / transcript gates REMOVED — Gemini Live decides via
        # function calling + TOOL_DECLARATIONS only. No local intent block.
        # (Destructive tools still go through security_gate.authorize below.)

        # Duplicate function-call guard removed — every tool call runs.

        # ── System-sleep guard (narrow, NOT a general intent gate) ───
        # Gemini decides tool calls server-side from audio, so a bare
        # "sleep" / "go to sleep" can get routed to computer_settings
        # even though that phrase means "put GAMA's session to sleep,"
        # not "put the OS to sleep." This is the one place that
        # distinction is enforced: only requires that the user's actual
        # transcript explicitly names the machine ("set the system/
        # computer/pc/laptop to sleep") before the real OS-sleep action
        # is allowed to run. Anything else — bare "sleep", "go to
        # sleep", "gama sleep" — is treated as GAMA's own sleep-mode
        # command instead of executing computer_settings.
        if name == "computer_settings" and str(args.get("action", "")).strip().lower() == "sleep":
            _transcript = (asst._last_input_transcript or "").strip()
            if not _SYSTEM_SLEEP_RE.search(_transcript):
                asst.ui.write_log(
                    '<span style="color:#FF9900">⚠ SLEEP GUARD:</span> '
                    "computer_settings(sleep) suppressed — no explicit "
                    "\"system/computer/pc to sleep\" phrasing heard; "
                    "treating as GAMA session-sleep instead."
                )
                log.info(
                    f"[sleep-guard] Blocked computer_settings(action=sleep) — "
                    f"transcript {_transcript!r} doesn't explicitly name the "
                    f"machine. Routing to GAMA session-sleep instead."
                )
                if asst._awake:
                    asst._enter_sleep_mode("sleep word detected (via blocked system-sleep tool call)")
                return {
                    "id": call_id,
                    "name": name,
                    "response": {
                        "result": (
                            "NOT EXECUTED: the user did not explicitly ask to put the "
                            "system/computer/PC to sleep — this reads as GAMA's own "
                            "'go to sleep' session command instead, which has already "
                            "been handled directly. Do not call computer_settings for "
                            "this; just acknowledge briefly if anything."
                        )
                    },
                }

        asst.ui.write_log(f'<span style="color:#007AFF">TOOL:</span> {name}({_logged_args})')

        # World Model task tracking — every tool call becomes a tracked
        # task (pending → running → completed/failed) so the World Model
        # actually reflects what GAMA is doing, not just desktop state.
        # Fall back to a synthetic id if Gemini didn't supply a call id.
        _task_id = call_id or f"{name}_{time.monotonic()}"
        _world = None
        try:
            from core.world_model import world as _world
            _world.add_task(_task_id, f"{name}({_logged_args})")
            _world.update_task(_task_id, status="running")
        except Exception:
            _world = None

        # Capability / confidence gates REMOVED for non-destructive tools.
        # Only security_gate.authorize (confirmation code) may block, and
        # only for DESTRUCTIVE actions. All other tools run freely;
        # Gemini Live decides what to call via function declarations.

        allowed, deny_reason = await self._security_gate_check(name, args)
        if not allowed:
            asst.ui.set_state("ERROR")
            asst.ui.write_log(f'<span style="color:#ff3355">🔒 BLOCKED:</span> {name} — {deny_reason}')
            log.warning(f"Destructive tool call blocked: {name}({args}) — {deny_reason}")
            try:
                if _world is not None:
                    _world.update_task(_task_id, status="failed", progress=0.0)
            except Exception:
                pass
            return {
                "id": call_id,
                "name": name,
                "response": {"result": f"BLOCKED: {deny_reason}"},
            }

        # Long-running / vision / computer-control tools must NEVER block
        # the Live session or Gama's ability to speak.  Run them fully in
        # the background, return a short "started" result immediately so
        # Gemini Live can continue/acknowledge, then push the final report
        # via _on_sys_alert so Live can speak it when ready.
        # Truly long / multi-step tools — background + immediate "started".
        # Fast tools still run via gather below with WHEN_IDLE scheduling so
        # they never stall speech, but they return a real result (not
        # "Started…") so the model can speak the answer cleanly.
        _LONG_RUNNING_TOOLS = frozenset({
            "computer_agent", "edith_screen_vision", "screen_agent",
            "automation_engine", "advanced_automation",
            "knowledge_action",
            "file_controller",
            "terminal_command", "generate_image",
            "browser_control", "ui_automation", "protocol_engine",
            "keyboard_actions", "mouse_actions",
            "open_app", "file_processor", "web_reader",
            "webcam_process", "screen_process", "live_vision", "email_sender",
            "telegram_sender",
        })
        _is_long = name in _LONG_RUNNING_TOOLS

        if _is_long:
            def _bg_long_tool(_n=name, _a=dict(args), _tid=_task_id):
                try:
                    res = _execute_tool(_n, _a)
                    ok = not (isinstance(res, str) and res.startswith("Tool failed"))
                    summary = (str(res)[:350] + "…") if len(str(res)) > 350 else str(res)
                    alert = (
                        f"[SYSTEM_ALERT] Tool '{_n}' finished. "
                        f"Result: {summary}. "
                        "State the outcome in ONE short natural sentence, then stop."
                    )
                    try:
                        if _world is not None:
                            _world.update_task(_tid, status="completed" if ok else "failed", progress=1.0)
                    except Exception:
                        pass
                except Exception as exc:
                    log.error(f"[bg-tool] {_n} failed: {exc}")
                    alert = (
                        f"[SYSTEM_ALERT] Tool '{_n}' failed: {exc}. "
                        "Mention the failure briefly, then stop."
                    )
                    try:
                        if _world is not None:
                            _world.update_task(_tid, status="failed", progress=0.0)
                    except Exception:
                        pass
                # Thread-safe inject into the Live session so Gemini can speak the report
                try:
                    loop = getattr(asst, "_loop", None)
                    if loop is not None and loop.is_running():
                        asyncio.run_coroutine_threadsafe(asst._send_system_text(alert), loop)
                    else:
                        log.info(f"[bg-tool] No live loop; result was: {alert[:120]}")
                except Exception as inj_exc:
                    log.debug(f"[bg-tool] inject failed: {inj_exc}")
            import threading as _thr
            _thr.Thread(target=_bg_long_tool, daemon=True, name=f"BgTool-{name}").start()
            result = (
                f"Started '{name}' in the background (non-blocking). "
                "I'll report the result when it finishes so you can speak it. "
                "Continue the conversation normally."
            )
            # No ack needed — Live will handle speaking; we already returned.
            ack_sent = False
        else:
            task = asyncio.ensure_future(asyncio.to_thread(_execute_tool, name, args))
            ack_sent = False
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except asyncio.TimeoutError:
                now = time.monotonic()
                # Check-then-set under a lock: when several tool calls from
                # the same batch race here concurrently (asyncio.gather in
                # _handle_tool_call), an unlocked check let more than one of
                # them see "interval elapsed" before any had written the new
                # timestamp, so two ack lines fired almost simultaneously.
                with asst._ack_lock:
                    if now - asst._last_ack_ts >= asst._ack_min_interval_s:
                        asst._last_ack_ts = now
                        ack_sent = True
                if ack_sent:
                    from voice.speech_manager import Priority as _Prio
                    _action = (args.get("action") or "").lower().strip()
                    _ack_key = f"{name}_{_action}" if _action else name
                    _ack_lines = _TOOL_ACK_MAP.get(_ack_key) or _TOOL_ACK_MAP.get(name) or PROCESSING_ACK_LINES
                    asst._speak_exact(random.choice(_ack_lines),
                                       priority=_Prio.ACK, kind="ack")
                # Hard 8-second wall-clock ceiling — prevents a hung HTTP call
                # (network drop, DNS timeout, unresponsive server) from blocking
                # the tool slot indefinitely and forcing a full Gemini session
                # reset.  Returns a structured error Gemini can speak gracefully.
                try:
                    result = await asyncio.wait_for(task, timeout=8.0)
                except asyncio.TimeoutError:
                    result = (
                        f"Tool failed: '{name}' timed out after 8 seconds — "
                        "the service may be temporarily unavailable. Please try again."
                    )
                    log.warning(f"[ExecQueue] '{name}' hit 8s hard timeout; returning error to Gemini.")
                if ack_sent:
                    from voice.speech_manager import cancel as _sm_cancel
                    _sm_cancel(kind="ack")
        asst.ui.write_log(f'<span style="color:#00ff88">RESULT:</span> {str(result)[:200]}')

        _success = not (isinstance(result, str) and result.startswith("Tool failed"))
        try:
            if _world is not None:
                _world.update_task(
                    _task_id,
                    status=("completed" if _success else "failed"),
                    progress=1.0,
                )
        except Exception:
            pass
        try:
            from core.jarvis_bootstrap import record_action_outcome, record_workflow_action
            record_action_outcome(name, _success)
            if _success:
                record_workflow_action(name)
        except Exception:
            pass

        # Live API async function calling (NON_BLOCKING tools):
        # scheduling tells the model when to surface the result.
        # WHEN_IDLE = speak after current utterance; INTERRUPT = right away;
        # SILENT = absorb knowledge without speaking.
        # Default: every tool is NON_BLOCKING so the Live receive loop and
        # speech path stay free. Only high-security / confirmation tools
        # stay blocking so the model waits for authorize() + the real result
        # before continuing (shutdown, delete, kill, confirmation code, …).
        _resp = {"result": result}
        if name not in _BLOCKING_TOOLS:
            if isinstance(result, str) and result.startswith("Started "):
                _resp["scheduling"] = "SILENT"
            else:
                _resp["scheduling"] = "WHEN_IDLE"
        return {
            "id": call_id,
            "name": name,
            "response": _resp,
        }

    async def _ada_bg_tool(self, fc) -> None:
        """
        Canvas/display tools intentionally do NOT inject a SYSTEM_ALERT back
        into the Live session. The visual update is the user-facing result,
        and extra realtime text after a tool response is a known trigger for
        WebSocket 1011 closures on native-audio Live models.
        """
        asst = self._asst
        name = fc.name or ""
        args = dict(fc.args) if fc.args else {}
        # Security gate for background-path tools — destructive actions
        # (file_controller delete, process_manager kill, terminal, …) must
        # pass authorize() exactly like the blocking path. See
        # _security_gate_check: previously this path ran them ungated.
        try:
            allowed, deny_reason = await self._security_gate_check(name, args)
        except Exception as _gate_exc:
            log.warning(f"[security] gate check failed for {name}: {_gate_exc}")
            allowed, deny_reason = True, ""
        if not allowed:
            asst.ui.set_state("ERROR")
            asst.ui.write_log(f'<span style="color:#ff3355">🔒 BLOCKED:</span> {name} — {deny_reason}')
            log.warning(f"Destructive bg tool call blocked: {name}({args}) — {deny_reason}")
            return
        # Tools whose result is visual / side-effect only — never poke Live.
        _silent_tools = frozenset({
            "canvas_visual", "display_stage", "generate_image",
            "desktop_notify", "notification_manager",
        })
        try:
            result = await asyncio.to_thread(_execute_tool, name, args)
            log.info(f"{name} done: {str(result)[:120]}")
            if name in _silent_tools:
                return  # HUD/canvas already updated — do not poke Live session
            # If we are mid-utterance or in post-reconnect quiet, skip the
            # alert entirely rather than risk a 1011 on the Live socket.
            with asst._speaking_lock:
                speaking = bool(asst._speaking)
            if speaking or time.monotonic() < float(
                getattr(asst, "_session_quiet_until", 0) or 0
            ):
                log.debug(f"skip SYSTEM_ALERT for {name} (speaking/quiet)")
                return
            # Suppress repeated SYSTEM_ALERTs for the same tool+result so the
            # model cannot enter a call→alert→call loop.
            summary = (str(result)[:200] + "…") if len(str(result)) > 200 else str(result)
            _alert_key = f"alert:{name}:{summary}"
            now_mono = time.monotonic()
            _alert_window = float(getattr(asst, "_tool_call_dedup_window_s", 4.0)) * 2
            async with asst._recent_tool_calls_lock:
                _prev_alert = asst._recent_tool_calls.get(_alert_key)
                if _prev_alert is not None and (now_mono - _prev_alert) < _alert_window:
                    log.debug(f"skip duplicate SYSTEM_ALERT for {name}")
                    return
                asst._recent_tool_calls[_alert_key] = now_mono
            alert = (
                f"[SYSTEM_ALERT] Tool '{name}' finished. Result: {summary}. "
                "Tell the user this result in ONE short natural sentence. "
                "Do NOT call any tool again — just speak the answer and stop."
            )
            if asst.session and asst._loop and asst._loop.is_running():
                try:
                    await asst._send_system_text(alert)
                except Exception as e:
                    log.debug(f"notify failed: {e}")
        except Exception as exc:
            log.error(f"{name} failed: {exc}")
            if name in _silent_tools:
                return  # failures are logged; do not risk another 1011
            try:
                if asst.session and not asst._speaking:
                    err_s = str(exc).replace("\n", " ").strip()
                    err_s = (err_s[:200] + "…") if len(err_s) > 200 else err_s
                    await asst._send_system_text(
                        f"[SYSTEM_ALERT] Tool '{name}' failed: {err_s}. "
                        "Mention the failure briefly, then stop."
                    )
            except Exception:
                pass

    async def _handle_tool_call(self, tool_call):
        """
        - NON_BLOCKING / long tools: asyncio.create_task + immediate
          FunctionResponse (scheduling=SILENT). Conversation keeps flowing.
        - Short tools: await in parallel, then send responses.
        """
        asst = self._asst
        try:
            function_calls = tool_call.function_calls
            if not function_calls:
                return

            asst.ui.set_state("PROCESSING")
            asst._last_ack_ts = 0.0

            # Default: EVERY tool is non-blocking
            # so the Live receive/play loops never
            # stall. Only a tiny security-critical set stays synchronous so
            # authorize() can gate destructive OS actions before they run.
            _BLOCKING = _BLOCKING_TOOLS

            immediate: list = []
            short_fcs: list = []  # only _BLOCKING tools land here

            for fc in function_calls:
                name = fc.name or ""
                if name in _BLOCKING:
                    short_fcs.append(fc)
                    continue

                # Duplicate bg tool guard removed — every call runs.
                args = dict(fc.args) if fc.args else {}
                call_id = getattr(fc, "id", None)

                # fire-and-forget, never block the receive loop
                asyncio.create_task(asst._ada_bg_tool(fc), name=f"bg-{name}")
                if name == "display_stage":
                    _result = "Canvas updated."
                elif name == "canvas_visual":
                    _result = "Generating canvas visual."
                elif name in ("desktop_context", "recall_memory", "memory_search",
                              "get_world_context", "live_vision"):
                    _result = (
                        f"Fetching '{name}' in the background. "
                        "Use the result when it arrives; keep talking if needed."
                    )
                else:
                    _result = (
                        f"Started '{name}' in the background. "
                        "Continue the conversation; the result will arrive shortly."
                    )
                immediate.append({
                    "id": call_id,
                    "name": name,
                    "response": {
                        "result": _result,
                        "scheduling": "SILENT",
                    },
                })
                asst.ui.write_log(
                    f'<span style="color:#007AFF">TOOL (bg):</span> {name}'
                )
                log.info(f"Fired background tool: {name}")

            # Send immediate "started" responses so Live can keep speaking
            if immediate and asst.session:
                await asst.session.send_tool_response(function_responses=immediate)
                log.info(f"Sent {len(immediate)} NON_BLOCKING tool response(s).")

            # Short tools still run in parallel and report results normally
            if short_fcs:
                responses = await asyncio.gather(*[
                    asst._execute_single_tool_call(fc) for fc in short_fcs
                ])
                responses = [r for r in responses if r is not None]
                if responses and asst.session:
                    await asst.session.send_tool_response(
                        function_responses=responses
                    )
                    log.info(f"Sent {len(responses)} tool response(s) back to Gemini.")

            # Back to LISTENING (not READY) so the HUD matches the mic state.
            asst.ui.set_state("LISTENING" if asst._awake else "IDLE")
        except Exception as exc:
            log.error(f"Tool call failed: {exc}")
            log.error(traceback.format_exc())
            asst.ui.set_state("LISTENING" if asst._awake else "IDLE")


__all__ = ["ToolController"]
