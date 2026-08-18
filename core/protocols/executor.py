"""
core/protocols/executor.py — Workflow execution engine for Protocols
================================================================================
Runs a Protocol's steps as a workflow (sequential + parallel groups),
evaluating conditions, retrying/falling back/skipping on failure, honoring
pause/resume/cancel/skip, resolving {parameter} placeholders, and reporting
live progress to any registered listener (e.g. the Protocol Manager UI or
voice feedback layer).

Each execution runs on its own background thread so voice/text commands stay
responsive ("Coding Protocol initiated." returns immediately while the steps
run), while pause/resume/cancel act on that thread cooperatively via a
threading.Event + cooperative checkpoints between steps.
"""

from __future__ import annotations

from utils.logger import get_logger

import concurrent.futures
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.protocols.models import (
    Protocol,
    ProtocolExecutionRecord,
    ProtocolStep,
    ActionType,
    OnFailureStrategy,
    PermissionLevel,
)
from core.protocols.storage import protocol_storage
from core.protocols.registry import protocol_registry
from core.protocols.actions import action_handler_registry

import logging

log = get_logger(__name__)
logger = log  # back-compat alias
_MAX_CALL_DEPTH = 8
_DEFAULT_STEP_TIMEOUT = 60.0
# Minimum seconds between two *starts* of the same protocol. Without this,
# a repeated "protocol 1" command (e.g. because the user didn't hear a
# spoken confirmation — TTS quota exhaustion, dropped audio, etc.) launches
# a brand new execution on top of one that may still be running or that
# just finished seconds ago, compounding load (extra tool calls, extra TTS
# requests) instead of just re-confirming. This is a dedup window, not a
# rate limit — legitimate re-runs after this window still work normally.
_RERUN_COOLDOWN_SECONDS = 8.0


class ExecutionCancelledException(Exception):
    pass


class ExecutionPausedException(Exception):
    pass


class ProtocolExecutionContext:
    """Mutable state for a single in-flight execution."""

    def __init__(self, record: ProtocolExecutionRecord, parameters: Optional[Dict[str, Any]] = None) -> None:
        self.record = record
        self.parameters: Dict[str, Any] = parameters or {}
        self.paused = threading.Event()
        self.cancelled = threading.Event()
        self.skip_requested = threading.Event()
        self.lock = threading.RLock()

    def resolve_params(self, text: str) -> str:
        """Replace {param} placeholders in `text` using self.parameters."""
        if not text or "{" not in text:
            return text

        def _sub(m: "re.Match[str]") -> str:
            key = m.group(1).strip()
            return str(self.parameters.get(key, m.group(0)))

        return re.sub(r"\{([^{}]+)\}", _sub, text)


class ProtocolExecutor:
    """Owns all in-flight and historical executions."""

    def __init__(self) -> None:
        self._active: Dict[str, ProtocolExecutionContext] = {}
        self._lock = threading.RLock()
        self._listeners: List[Callable[[ProtocolExecutionRecord], None]] = []
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="protocol-step")
        # protocol_id -> monotonic time of last execute_protocol() start,
        # used by the re-entrancy / rerun-cooldown guard below.
        self._last_started: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Listeners (for UI / voice feedback to subscribe to live progress)
    # ------------------------------------------------------------------
    def register_status_listener(self, listener: Callable[[ProtocolExecutionRecord], None]) -> None:
        self._listeners.append(listener)

    def _notify_listeners(self, record: ProtocolExecutionRecord) -> None:
        for listener in list(self._listeners):
            try:
                listener(record)
            except Exception as exc:
                logger.debug(f"[protocols.executor] listener error: {exc}")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def execute_protocol(
        self,
        identifier: str,
        parameters: Optional[Dict[str, Any]] = None,
        call_stack: Optional[Set[str]] = None,
        task_id: Optional[str] = None,
    ) -> Tuple[bool, str, str]:
        """Resolve + kick off a protocol run. Returns (ok, message, execution_id)."""
        protocol = protocol_registry.resolve(identifier)
        if protocol is None:
            return False, f"I couldn't find a protocol matching '{identifier}', Sir.", ""

        if not protocol.enabled:
            return False, f"{protocol.display_name} is currently disabled.", ""

        is_nested_call = bool(call_stack)  # non-empty means a parent protocol is invoking us
        call_stack = set(call_stack or set())
        if protocol.id in call_stack:
            return False, f"Blocked a recursive protocol call ({protocol.display_name} calls itself).", ""
        if len(call_stack) >= _MAX_CALL_DEPTH:
            return False, "Protocol call depth exceeded — too many nested protocols.", ""
        call_stack = call_stack | {protocol.id}

        # ── Re-entrancy / rerun-cooldown guard ──────────────────────────
        # Only applies to top-level (voice/text-triggered) runs — a
        # protocol invoking another protocol as a step is never blocked
        # by this, only genuine duplicate top-level triggers are.
        if not is_nested_call:
            with self._lock:
                already_running = any(
                    c.record.protocol_id == protocol.id and c.record.status == "running"
                    for c in self._active.values()
                )
                last_started = self._last_started.get(protocol.id, 0.0)
                too_soon = (time.monotonic() - last_started) < _RERUN_COOLDOWN_SECONDS
            if already_running:
                return False, f"{protocol.display_name} is already running, Sir.", ""
            if too_soon:
                return False, f"Already just started {protocol.display_name}, Sir — give it a moment.", ""
            with self._lock:
                self._last_started[protocol.id] = time.monotonic()

        record = ProtocolExecutionRecord(
            execution_id=task_id or uuid.uuid4().hex,
            protocol_id=protocol.id,
            protocol_name=protocol.display_name,
            status="running",
            total_steps=len(protocol.steps),
        )
        ctx = ProtocolExecutionContext(record, parameters)

        with self._lock:
            self._active[record.execution_id] = ctx

        self._log_event(ctx, "info", f"{protocol.display_name} initiated.")
        self._publish_event("TaskStarted", ctx, name=protocol.display_name)

        thread = threading.Thread(
            target=self._run_workflow_safe,
            args=(protocol, ctx, call_stack),
            daemon=True,
            name=f"protocol-{protocol.display_name}",
        )
        thread.start()

        return True, f"{protocol.display_name} initiated.", record.execution_id

    # ------------------------------------------------------------------
    # Pause / resume / cancel / skip
    # ------------------------------------------------------------------
    def pause_execution(self, execution_id: Optional[str] = None) -> bool:
        ctx = self._find_active(execution_id)
        if not ctx:
            return False
        ctx.paused.set()
        ctx.record.status = "paused"
        self._notify_listeners(ctx.record)
        return True

    def resume_execution(self, execution_id: Optional[str] = None) -> bool:
        ctx = self._find_active(execution_id)
        if not ctx:
            return False
        ctx.paused.clear()
        ctx.record.status = "running"
        self._notify_listeners(ctx.record)
        return True

    def cancel_execution(self, execution_id: Optional[str] = None) -> bool:
        ctx = self._find_active(execution_id)
        if not ctx:
            return False
        ctx.cancelled.set()
        ctx.paused.clear()  # unblock a paused thread so it can observe cancellation
        return True

    def skip_current_step(self, execution_id: Optional[str] = None) -> bool:
        ctx = self._find_active(execution_id)
        if not ctx:
            return False
        ctx.skip_requested.set()
        return True

    def get_active_executions(self) -> List[ProtocolExecutionRecord]:
        with self._lock:
            return [ctx.record for ctx in self._active.values()]

    def get_execution_history(self, limit: int = 20) -> List[ProtocolExecutionRecord]:
        return protocol_storage.get_history(limit=limit)

    def _find_active(self, key: Optional[str]) -> Optional[ProtocolExecutionContext]:
        with self._lock:
            if key:
                ctx = self._active.get(key)
                if ctx:
                    return ctx
                # allow matching by protocol name too
                for c in self._active.values():
                    if c.record.protocol_name.lower() == str(key).lower():
                        return c
                return None
            # No key: most recently started active execution
            if not self._active:
                return None
            return sorted(self._active.values(), key=lambda c: c.record.started_at)[-1]

    # ------------------------------------------------------------------
    # Workflow driver
    # ------------------------------------------------------------------
    def _run_workflow_safe(self, protocol: Protocol, ctx: ProtocolExecutionContext, call_stack: Set[str]) -> None:
        try:
            self._run_workflow(protocol, ctx, call_stack)
        except ExecutionCancelledException:
            ctx.record.status = "cancelled"
            self._log_event(ctx, "warning", f"{protocol.display_name} cancelled.")
            self._publish_event("TaskCancelled", ctx, name=protocol.display_name)
        except Exception as exc:
            ctx.record.status = "failed"
            ctx.record.error = str(exc)
            self._log_event(ctx, "error", f"{protocol.display_name} failed: {exc}")
            logger.exception(f"[protocols.executor] {protocol.display_name} failed")
            self._publish_event("TaskFailed", ctx, name=protocol.display_name, error=str(exc))
            self._speak_failure(ctx, protocol.display_name, str(exc))
        else:
            if ctx.record.status not in ("cancelled", "failed"):
                ctx.record.status = "completed"
                self._log_event(ctx, "info", self._format_completion_summary(ctx, protocol))
                self._publish_event("TaskCompleted", ctx, name=protocol.display_name)
                self._speak_completion(ctx, protocol.display_name)
        finally:
            ctx.record.finished_at = time.time()
            protocol.run_count += 1
            protocol.last_run_at = ctx.record.finished_at
            protocol_storage.save_protocol(protocol)
            protocol_storage.add_history_record(ctx.record)
            self._notify_listeners(ctx.record)
            with self._lock:
                self._active.pop(ctx.record.execution_id, None)

    def _run_workflow(self, protocol: Protocol, ctx: ProtocolExecutionContext, call_stack: Set[str]) -> None:
        # Group steps: consecutive steps sharing a parallel_group run together.
        ordered = sorted(protocol.steps, key=lambda s: s.order)
        total = len(ordered)
        i = 0
        while i < len(ordered):
            self._check_state(ctx, ctx.record.execution_id)
            step = ordered[i]
            if step.parallel_group:
                group_id = step.parallel_group
                group = [step]
                j = i + 1
                while j < len(ordered) and ordered[j].parallel_group == group_id:
                    group.append(ordered[j])
                    j += 1
                self._report_progress(ctx, f"Running {len(group)} steps in parallel...", i)
                self._speak_step_progress(ctx, protocol.display_name, step, i, total)
                self._execute_parallel_group(group, ctx, call_stack, ctx.record.execution_id)
                i = j
            else:
                self._report_progress(ctx, step.target or step.action_type, i)
                self._speak_step_progress(ctx, protocol.display_name, step, i, total)
                self._execute_step_with_resilience(step, ctx, call_stack, ctx.record.execution_id)
                i += 1
            ctx.record.current_step_index = i

    # ------------------------------------------------------------------
    # Single-step execution with condition + retry/fallback/skip/ask/abort
    # ------------------------------------------------------------------
    def _execute_step_with_resilience(
        self, step: ProtocolStep, ctx: ProtocolExecutionContext, call_stack: Set[str], task_id: str
    ) -> None:
        self._check_state(ctx, task_id)

        if ctx.skip_requested.is_set():
            ctx.skip_requested.clear()
            self._log_event(ctx, "info", f"Skipped step: {step.action_type}:{step.target}")
            return

        if step.condition and not self._evaluate_condition(step.condition, ctx):
            self._log_event(ctx, "info", f"Condition not met, skipping: {step.action_type}:{step.target}")
            return

        attempts = 0
        max_attempts = 1 + max(0, step.retries) if step.on_failure == OnFailureStrategy.RETRY.value else 1
        timeout = step.timeout_secs or _DEFAULT_STEP_TIMEOUT

        while True:
            attempts += 1
            try:
                if step.action_type == ActionType.CALL_PROTOCOL.value:
                    self._call_nested_protocol(step, ctx, call_stack)
                else:
                    result = self._run_step_with_timeout(step, ctx, timeout)
                    self._log_event(ctx, "info", f"✓ {step.target or step.action_type} -> {result}")
                return
            except (ExecutionCancelledException, ExecutionPausedException):
                raise
            except Exception as exc:
                if attempts < max_attempts:
                    self._log_event(ctx, "warning", f"Retrying step ({attempts}/{max_attempts - 1}) after error: {exc}")
                    continue

                strategy = step.on_failure
                if strategy == OnFailureStrategy.FALLBACK.value and step.fallback_step is not None:
                    self._log_event(ctx, "warning", f"Step failed, using fallback: {exc}")
                    return self._execute_step_with_resilience(step.fallback_step, ctx, call_stack, task_id)
                if strategy == OnFailureStrategy.SKIP.value:
                    self._log_event(ctx, "warning", f"Step failed, skipping: {exc}")
                    return
                if strategy == OnFailureStrategy.ASK_USER.value:
                    self._log_event(ctx, "warning", f"Step failed, needs user input: {exc}")
                    return
                # ABORT (or RETRY exhausted with no fallback): stop the whole protocol.
                raise RuntimeError(f"Step '{step.action_type}:{step.target}' failed: {exc}") from exc

    def _run_step_with_timeout(self, step: ProtocolStep, ctx: ProtocolExecutionContext, timeout_secs: float) -> str:
        resolved = ProtocolStep(
            action_type=step.action_type,
            target=ctx.resolve_params(step.target),
            params={k: (ctx.resolve_params(v) if isinstance(v, str) else v) for k, v in step.params.items()},
            step_id=step.step_id,
        )
        future = self._pool.submit(action_handler_registry.execute, resolved, {"parameters": ctx.parameters})
        try:
            return future.result(timeout=timeout_secs)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"Step timed out after {timeout_secs}s")

    def _execute_parallel_group(
        self, sub_steps: List[ProtocolStep], ctx: ProtocolExecutionContext, call_stack: Set[str], task_id: str
    ) -> None:
        futures = {
            self._pool.submit(self._execute_step_with_resilience, s, ctx, call_stack, task_id): s
            for s in sub_steps
        }
        errors = []
        for future in concurrent.futures.as_completed(futures):
            step = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((step, exc))
        if errors:
            # Aggregate — if every parallel branch already handled its own
            # on_failure policy, this only re-raises for ABORT-level steps.
            details = "; ".join(f"{s.action_type}:{s.target} -> {e}" for s, e in errors)
            raise RuntimeError(f"Parallel group had failures: {details}")

    def _call_nested_protocol(self, step: ProtocolStep, ctx: ProtocolExecutionContext, call_stack: Set[str]) -> None:
        target_id = step.params.get("identifier") or step.target
        ok, msg, _ = self.execute_protocol(target_id, parameters=ctx.parameters, call_stack=call_stack)
        if not ok:
            raise RuntimeError(msg)
        self._log_event(ctx, "info", f"✓ Nested protocol: {msg}")

    # ------------------------------------------------------------------
    # Conditions
    # ------------------------------------------------------------------
    def _evaluate_condition(self, condition: Dict[str, Any], ctx: ProtocolExecutionContext) -> bool:
        cond_type = condition.get("type", "")
        op = condition.get("op", "==")
        expected = condition.get("value")

        try:
            if cond_type == "process_running":
                actual = self._is_process_running(ctx.resolve_params(condition.get("target", "")))
            elif cond_type == "internet_available":
                actual = self._is_internet_available()
            elif cond_type == "battery_below":
                level = self._get_battery_level()
                return level is not None and level < float(expected)
            elif cond_type == "app_missing":
                actual = not self._is_process_running(ctx.resolve_params(condition.get("target", "")))
            elif cond_type == "param_equals":
                actual = ctx.parameters.get(condition.get("target", ""))
            else:
                return True  # unknown condition types don't block execution
            return self._compare_values(actual, op, expected)
        except Exception as exc:
            logger.warning(f"[protocols.executor] condition evaluation failed ({cond_type}): {exc}")
            return True  # fail open so one bad condition doesn't brick a protocol

    @staticmethod
    def _is_process_running(process_name: str) -> bool:
        if not process_name:
            return False
        try:
            import psutil
            target = process_name.lower()
            for p in psutil.process_iter(["name"]):
                try:
                    if target in (p.info.get("name") or "").lower():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _is_internet_available() -> bool:
        import socket
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=2).close()
            return True
        except Exception:
            return False

    @staticmethod
    def _get_battery_level() -> Optional[float]:
        try:
            import psutil
            battery = psutil.sensors_battery()
            return float(battery.percent) if battery else None
        except Exception:
            return None

    @staticmethod
    def _compare_values(actual: Any, op: str, expected: Any) -> bool:
        try:
            if op in ("==", "eq"):
                return actual == expected
            if op in ("!=", "ne"):
                return actual != expected
            if op in ("<", "lt"):
                return float(actual) < float(expected)
            if op in ("<=", "le"):
                return float(actual) <= float(expected)
            if op in (">", "gt"):
                return float(actual) > float(expected)
            if op in (">=", "ge"):
                return float(actual) >= float(expected)
            if op == "truthy":
                return bool(actual)
        except Exception:
            return False
        return bool(actual)

    # ------------------------------------------------------------------
    # Voice narration — reuses the existing event bus + speech pipeline
    # (state_engine.event_bus -> voice.execution_narrator) instead of a
    # parallel speech system. TaskStarted/TaskProgressChanged get the
    # normal narrator treatment ("I'm working on X, sir."); completion
    # is spoken directly here with the exact "Executed <protocol>"
    # phrasing so it's unambiguous a protocol actually finished, not
    # just a generic "Done, sir."
    # ------------------------------------------------------------------
    def _publish_event(self, event_name: str, ctx: ProtocolExecutionContext, **extra: Any) -> None:
        try:
            from state_engine.event_bus import event_bus
            event_bus.publish(event_name, task_id=ctx.record.execution_id, **extra)
        except Exception as exc:
            logger.debug(f"[protocols.executor] event bus publish failed (non-fatal): {exc}")

    def _speak_step_progress(self, ctx: ProtocolExecutionContext, protocol_name: str, step: ProtocolStep, index: int, total: int) -> None:
        label = step.target or step.action_type.replace("_", " ")
        self._publish_event(
            "TaskProgressChanged",
            ctx,
            name=protocol_name,
            phase=f"step {index + 1} of {total}: {label}",
        )

    def _speak_completion(self, ctx: ProtocolExecutionContext, protocol_name: str) -> None:
        try:
            from voice import speech_manager
            from voice.speech_manager import Priority
            speech_manager.say(f"Executed {protocol_name}, Sir.", priority=Priority.COMPLETION, kind="result")
        except Exception as exc:
            logger.debug(f"[protocols.executor] completion speech failed (non-fatal): {exc}")

    def _speak_failure(self, ctx: ProtocolExecutionContext, protocol_name: str, error: str) -> None:
        try:
            from voice import speech_manager
            from voice.speech_manager import Priority
            speech_manager.say(f"{protocol_name} ran into a problem, Sir: {error}", priority=Priority.ERROR, kind="result")
        except Exception as exc:
            logger.debug(f"[protocols.executor] failure speech failed (non-fatal): {exc}")


    def _check_state(self, ctx: ProtocolExecutionContext, task_id: str) -> None:
        if ctx.cancelled.is_set():
            raise ExecutionCancelledException()
        while ctx.paused.is_set():
            time.sleep(0.2)
            if ctx.cancelled.is_set():
                raise ExecutionCancelledException()

    # ------------------------------------------------------------------
    # Logging / progress
    # ------------------------------------------------------------------
    def _report_progress(self, ctx: ProtocolExecutionContext, message: str, step_index: int) -> None:
        ctx.record.current_step_index = step_index
        self._notify_listeners(ctx.record)

    def _log_event(self, ctx: ProtocolExecutionContext, level: str, message: str) -> None:
        ctx.record.logs.append(message)
        if level == "error":
            logger.error(f"[protocol:{ctx.record.protocol_name}] {message}")
        elif level == "warning":
            logger.warning(f"[protocol:{ctx.record.protocol_name}] {message}")
        else:
            logger.info(f"[protocol:{ctx.record.protocol_name}] {message}")
        self._notify_listeners(ctx.record)

    def _format_completion_summary(self, ctx: ProtocolExecutionContext, protocol: Protocol) -> str:
        done = sum(1 for l in ctx.record.logs if l.startswith("✓"))
        return f"{protocol.display_name} completed successfully. ({done}/{len(protocol.steps)} steps confirmed)"


protocol_executor = ProtocolExecutor()

__all__ = [
    "ProtocolExecutor",
    "protocol_executor",
    "ProtocolExecutionContext",
    "ExecutionCancelledException",
    "ExecutionPausedException",
]
