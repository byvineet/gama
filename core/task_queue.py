"""
core/task_queue.py — Task Queue (Gama 2.0 Core Intelligence Layer, section 6)
==============================================================================
A lightweight, in-process task manager for work that runs *across* turns —
downloads, multi-step automation chains, monitors — as opposed to
core/planner.py's Plan/PlanStep, which executes a fixed step list
synchronously inside a single tool call and has no notion of "pause this
and check back later".

Supports (per spec section 6):
    - Sequential tasks      — default: one task at a time per worker.
    - Dependent tasks        — `depends_on=[task_id, ...]`; only becomes
                                ready once every dependency has COMPLETED.
    - Parallel tasks         — N worker threads (default 2 — single-user,
                                low-CPU/low-RAM philosophy, not a server).
    - Pause / Resume         — cooperative for a *running* task (the task's
                                own fn should poll `task_queue.is_pause_requested`)
                                and immediate for a *queued* task.
    - Cancel / Interrupt     — same cooperative model as pause; "interrupt"
                                is an alias for a request that also implies
                                "don't resume automatically".
    - Retry                  — automatic (retries_left) or manual (`retry()`
                                on a FAILED task).
    - Priority scheduling    — higher `priority` int runs first among ready
                                tasks; ties broken by insertion order (FIFO).

Continuously tracks current/remaining/completed/failed tasks so voice
queries like "What are you doing?" / "How much is left?" / "Stop." /
"Resume." (see core.task_queue) can be answered instantly —
no LLM call needed, per the spec's "Zero unnecessary AI calls" rule.

Event-driven: every state transition is published on the shared Event Bus
(state_engine.event_bus) using the exact event names the spec names
(TaskStarted, TaskPaused, TaskCompleted, TaskFailed, ...) so Voice/UI/Logger
can subscribe without coupling to this module. Mirrors into the existing
BackgroundTaskRegistry (state.tasks) too, so the debug panel and any code
that already reads `state.tasks.active()` keeps working without change.

Thread-safety: one RLock guards all mutable state; a Condition variable
(sharing that lock) wakes idle workers when a task becomes ready/resumed.
Workers are daemon threads started once, lazily, on first `add()` — an
idle Gama session never spins up worker threads it doesn't need.
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

TaskFn = Callable[..., Any]  # either fn() or fn(task_id) — see _call_fn


@dataclass
class Task:
    task_id: str
    name: str
    fn: TaskFn
    priority: int = 0
    depends_on: List[str] = field(default_factory=list)
    max_retries: int = 0
    retries_left: int = 0
    status: str = "QUEUED"  # QUEUED|RUNNING|PAUSED|COMPLETED|FAILED|CANCELLED
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    pause_requested: bool = False
    cancel_requested: bool = False

    # ── Live Task Awareness (Gama 2.0 voice spec) ──────────────────
    # A running task's fn is expected to call task_queue.report_step(...)
    # / set_waiting(...) as it progresses so voice narration and status
    # queries ("what are you doing?") can describe the *current* step,
    # not just "running" vs "queued".
    current_step: str = ""
    step_index: int = 0
    total_steps: int = 0
    completed_steps: int = 0
    progress_pct: Optional[float] = None
    eta_seconds: Optional[float] = None
    waiting: bool = False
    waiting_reason: str = ""
    retry_count: int = 0
    verifying: bool = False

    # Dynamic task modification (spec: "Skip duplicate files.", "Ignore
    # PDFs.", "Pause after this file.") — a running task's fn should
    # poll task_queue.get_modifiers(task_id) and honor whatever keys
    # it recognizes instead of the planner cancelling and restarting
    # the whole task.
    modifiers: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "name": self.name, "status": self.status,
            "priority": self.priority, "depends_on": list(self.depends_on),
            "error": self.error, "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "current_step": self.current_step, "step_index": self.step_index,
            "total_steps": self.total_steps, "completed_steps": self.completed_steps,
            "progress_pct": self.progress_pct, "eta_seconds": self.eta_seconds,
            "waiting": self.waiting, "waiting_reason": self.waiting_reason,
            "retry_count": self.retry_count, "verifying": self.verifying,
            "modifiers": dict(self.modifiers),
        }


class TaskQueue:
    """Process-wide task queue. See module docstring for semantics."""

    def __init__(self, max_workers: int = 2) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._tasks: Dict[str, Task] = {}
        self._ready_heap: List[tuple] = []  # (-priority, seq, task_id)
        self._seq = itertools.count()
        self._id_seq = itertools.count(1)
        self._max_workers = max(1, max_workers)
        self._workers_started = False
        self._shutdown = False

    # -- worker lifecycle -------------------------------------------------
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers_started:
                return
            self._workers_started = True
        for i in range(self._max_workers):
            t = threading.Thread(target=self._worker_loop, name=f"TaskQueueWorker-{i}", daemon=True)
            t.start()

    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while not self._shutdown:
                    task_id = self._pop_ready_nolock()
                    if task_id is not None:
                        break
                    self._cv.wait(timeout=1.0)
                if self._shutdown:
                    return
            self._run_task(task_id)

    def _pop_ready_nolock(self) -> Optional[str]:
        """Caller must hold self._lock. Pops the highest-priority ready,
        non-paused, non-cancelled task id, discarding stale heap entries."""
        while self._ready_heap:
            _, _, task_id = heapq.heappop(self._ready_heap)
            task = self._tasks.get(task_id)
            if task is None or task.status != "QUEUED":
                continue  # stale entry (cancelled/paused/already run)
            return task_id
        return None

    # -- public API ---------------------------------------------------------
    def add(self, name: str, fn: TaskFn, *, priority: int = 0,
            depends_on: Optional[List[str]] = None, max_retries: int = 0) -> str:
        """Queue a new task. Returns its task_id. Task becomes RUNNING as
        soon as a worker is free and its dependencies (if any) have all
        COMPLETED.

        `fn` may take zero arguments (`fn()`, existing behavior) or one
        (`fn(task_id)`) — the latter lets a long-running task call
        `task_queue.report_step(task_id, ...)` / `set_waiting(task_id, ...)`
        on itself for Live Task Awareness / voice narration, without a
        racy closure over a task_id that doesn't exist yet when the
        task starts. Detected automatically via the fn's signature."""
        task_id = f"task_{next(self._id_seq)}"
        task = Task(task_id=task_id, name=name, fn=fn, priority=priority,
                    depends_on=list(depends_on or []), max_retries=max_retries,
                    retries_left=max_retries)
        with self._cv:
            self._tasks[task_id] = task
            if self._dependencies_met_nolock(task):
                heapq.heappush(self._ready_heap, (-priority, next(self._seq), task_id))
                self._cv.notify()
        log.info(f"[TaskQueue] Queued '{name}' ({task_id}, priority={priority})")
        self._ensure_workers()
        return task_id

    def _call_fn(self, task: Task) -> Any:
        """Call task.fn(), passing task.task_id if the fn accepts a
        parameter — lets a task self-report progress via
        report_step(task_id, ...) / set_waiting(task_id, ...) without
        needing to know its own id ahead of time via a racy closure.
        Falls back to a plain fn() call for existing zero-arg tasks so
        every previously-written task keeps working unchanged.

        Worker threads here are plain threading.Thread, not asyncio tasks
        (see module docstring: "Workers are daemon threads"). task.fn may
        still be a coroutine function (core/task_scheduler.py always hands
        us one) — calling it returns an un-awaited coroutine object, not a
        result, which previously made _run_task record the coroutine
        itself as "result" and mark the task COMPLETED instantly without
        ever actually running it. Detect that case and drive it to
        completion on a fresh event loop local to this worker thread.
        """
        try:
            n_params = len(inspect.signature(task.fn).parameters)
        except (TypeError, ValueError):
            n_params = 0
        if n_params >= 1:
            call_result = task.fn(task.task_id)
        else:
            call_result = task.fn()
        if asyncio.iscoroutine(call_result):
            return asyncio.run(call_result)
        return call_result

    def _dependencies_met_nolock(self, task: Task) -> bool:
        return all(self._tasks.get(d) is not None and self._tasks[d].status == "COMPLETED"
                   for d in task.depends_on)

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "QUEUED":
                return
            task.status = "RUNNING"
            task.started_at = time.time()
            task.current_step = ""
            task.step_index = 0
            task.completed_steps = 0
            task.progress_pct = None
            task.eta_seconds = None
            task.waiting = False
            task.waiting_reason = ""
            task.verifying = False
        self._publish("TaskStarted", task_id=task_id, name=task.name)
        self._mirror_registry_start(task)

        try:
            result = self._call_fn(task)
            with self._lock:
                if task.cancel_requested:
                    task.status = "CANCELLED"
                else:
                    task.status = "COMPLETED"
                    task.result = result
                task.finished_at = time.time()
        except Exception as exc:
            log.warning(f"[TaskQueue] Task '{task.name}' ({task_id}) raised: {exc}")
            with self._lock:
                task.error = str(exc)
                if task.retries_left > 0:
                    task.retries_left -= 1
                    task.status = "QUEUED"
                    task.started_at = None
                    heapq.heappush(self._ready_heap, (-task.priority, next(self._seq), task_id))
                    self._cv.notify()
                    log.info(f"[TaskQueue] Retrying '{task.name}' ({task_id}), "
                            f"{task.retries_left} retr{'y' if task.retries_left == 1 else 'ies'} left")
                    return
                task.status = "FAILED"
                task.finished_at = time.time()

        if task.status == "COMPLETED":
            self._publish("TaskCompleted", task_id=task_id, name=task.name)
            self._mirror_registry_complete(task, ok=True)
            self._unblock_dependents(task_id)
            self._announce_status(task.name, "completed")
        elif task.status == "FAILED":
            self._publish("TaskFailed", task_id=task_id, name=task.name, error=task.error)
            self._mirror_registry_complete(task, ok=False)
            self._announce_status(task.name, "failed", task.error or "")
        elif task.status == "CANCELLED":
            self._publish("TaskCancelled", task_id=task_id, name=task.name)
            self._mirror_registry_complete(task, ok=False, detail="cancelled")
            self._announce_status(task.name, "cancelled")

    def _announce_status(self, name: str, status: str, error: str = "") -> None:
        """Speak task outcome via single-speaker SpeechAuthority (Gemini only).

        Respects channel-busy rules: if user/Gama is speaking, the line waits.
        Parallel tasks keep running; this is notification only.
        """
        try:
            from core.speech_authority import speech_authority
            if status == "completed":
                speech_authority.announce_task_completed(name)
            elif status == "failed":
                speech_authority.announce_task_failed(name, error)
            elif status == "cancelled":
                speech_authority.announce_task_cancelled(name)
        except Exception as exc:
            log.debug(f"[TaskQueue] announce skipped: {exc}")

    def _unblock_dependents(self, completed_id: str) -> None:
        with self._cv:
            for t in self._tasks.values():
                if t.status == "QUEUED" and completed_id in t.depends_on:
                    if self._dependencies_met_nolock(t):
                        heapq.heappush(self._ready_heap, (-t.priority, next(self._seq), t.task_id))
                        self._cv.notify()

    # -- control: pause / resume / cancel / interrupt / retry --------------
    def pause(self, task_id: str) -> bool:
        """Queued task: removed from scheduling immediately (status -> PAUSED).
        Running task: cooperative — sets pause_requested; the task's own fn
        must poll `task_queue.is_pause_requested(task_id)` to actually yield.
        This module cannot force-suspend an arbitrary native call."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status == "QUEUED":
                task.status = "PAUSED"
            elif task.status == "RUNNING":
                task.pause_requested = True
            else:
                return False
        self._publish("TaskPaused", task_id=task_id, name=task.name)
        return True

    def resume(self, task_id: str) -> bool:
        with self._cv:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status == "PAUSED":
                task.status = "QUEUED"
                heapq.heappush(self._ready_heap, (-task.priority, next(self._seq), task_id))
                self._cv.notify()
            elif task.status == "RUNNING" and task.pause_requested:
                task.pause_requested = False
            else:
                return False
        self._publish("TaskResumed", task_id=task_id, name=task.name)
        return True

    def cancel(self, task_id: str) -> bool:
        """Queued/paused task: cancelled immediately, never runs.
        Running task: cooperative — sets cancel_requested; the task's own fn
        should poll `task_queue.is_cancel_requested(task_id)` to stop early.
        If it doesn't, the task still runs to completion but its final
        status is forced to CANCELLED rather than COMPLETED."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in ("COMPLETED", "FAILED", "CANCELLED"):
                return False
            if task.status == "RUNNING":
                task.cancel_requested = True
                return True
            task.status = "CANCELLED"
            task.finished_at = time.time()
        self._publish("TaskCancelled", task_id=task_id, name=task.name)
        return True

    def interrupt(self, task_id: str) -> bool:
        """Alias for pause with intent 'don't auto-resume' — same
        cooperative mechanics as pause(); kept as a distinct name because
        the spec calls out Pause and Interrupt as separate user intents
        ('Stop.' vs 'Leave that for tomorrow.') even though the underlying
        primitive is identical here."""
        return self.pause(task_id)

    def retry(self, task_id: str) -> bool:
        """Manually re-queue a FAILED task with one fresh attempt."""
        with self._cv:
            task = self._tasks.get(task_id)
            if task is None or task.status != "FAILED":
                return False
            task.status = "QUEUED"
            task.error = ""
            task.started_at = None
            task.finished_at = None
            task.retries_left = max(task.retries_left, 1)
            heapq.heappush(self._ready_heap, (-task.priority, next(self._seq), task_id))
            self._cv.notify()
        log.info(f"[TaskQueue] Manually retrying '{task.name}' ({task_id})")
        self._ensure_workers()
        return True

    def is_pause_requested(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return bool(task and task.pause_requested)

    # -- Live Task Awareness (report from inside a running task's fn) -----
    def report_step(self, task_id: str, step_name: str, *,
                     step_index: Optional[int] = None, total_steps: Optional[int] = None,
                     completed_steps: Optional[int] = None,
                     progress_pct: Optional[float] = None,
                     eta_seconds: Optional[float] = None) -> None:
        """Call from inside a task's fn to report what it's doing right
        now. Publishes TaskProgressChanged (event-driven — call this on
        phase changes, not on a timer) so voice narration and status
        queries always reflect the current step, completed/remaining
        counts, and estimated time left."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.current_step = step_name
            task.waiting = False
            task.waiting_reason = ""
            if step_index is not None:
                task.step_index = step_index
            if total_steps is not None:
                task.total_steps = total_steps
            if completed_steps is not None:
                task.completed_steps = completed_steps
            if progress_pct is not None:
                task.progress_pct = progress_pct
            if eta_seconds is not None:
                task.eta_seconds = eta_seconds
            snapshot = task.as_dict()
        self._publish("TaskProgressChanged", task_id=task_id, name=task.name,
                       phase=step_name, **{k: v for k, v in snapshot.items()
                                            if k not in ("task_id", "name")})
        self._mirror_registry_progress(task)

    def set_waiting(self, task_id: str, reason: str = "") -> None:
        """Mark a running task as blocked on something slow (a download,
        an external process, a retry backoff) so narration can say
        "I'm waiting for..." instead of going silent."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.waiting = True
            task.waiting_reason = reason
        self._publish("TaskProgressChanged", task_id=task_id, name=task.name,
                       phase="waiting", waiting=True, waiting_reason=reason)
        self._mirror_registry_progress(task)

    def clear_waiting(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not task.waiting:
                return
            task.waiting = False
            task.waiting_reason = ""
        self._mirror_registry_progress(task)

    def set_verifying(self, task_id: str, verifying: bool = True) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.verifying = verifying
        self._publish("TaskProgressChanged", task_id=task_id, name=task.name,
                       phase="verifying" if verifying else task.current_step,
                       verifying=verifying)
        self._mirror_registry_progress(task)

    def mark_retry(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.retry_count += 1
        self._publish("TaskProgressChanged", task_id=task_id, name=task.name,
                       phase="retrying", retry_count=task.retry_count)
        self._mirror_registry_progress(task)

    # -- Dynamic task modification ("Skip duplicates.", "Ignore PDFs.") ---
    def modify(self, task_id: str, **modifiers: Any) -> bool:
        """Merge live modifiers into a running/queued task instead of
        cancelling and re-planning it. The task's own fn should poll
        `get_modifiers(task_id)` and honor whatever keys it recognizes
        (e.g. {"skip_duplicates": True}, {"ignore_extensions": [".pdf"]},
        {"pause_after_current_file": True})."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.modifiers.update(modifiers)
        self._publish("TaskModified", task_id=task_id, name=task.name, modifiers=modifiers)
        return True

    def get_modifiers(self, task_id: str) -> Dict[str, Any]:
        task = self._tasks.get(task_id)
        return dict(task.modifiers) if task else {}

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return bool(task and task.cancel_requested)

    # -- queries -------------------------------------------------------------
    def status(self, task_id: str) -> Optional[dict]:
        task = self._tasks.get(task_id)
        return task.as_dict() if task else None

    def list_tasks(self, limit: int = 20, active_only: bool = True) -> List[dict]:
        """Snapshot of tasks for display surfaces (HUD, debug panel).

        active_only=True (default) hides COMPLETED/CANCELLED tasks older
        than a minute so a long-running session's HUD list doesn't fill up
        with finished work — RUNNING/PAUSED/QUEUED/FAILED are always kept.
        Most-recently-created first.
        """
        with self._lock:
            tasks = list(self._tasks.values())
        if active_only:
            now = time.time()
            tasks = [
                t for t in tasks
                if t.status not in ("COMPLETED", "CANCELLED")
                or (t.finished_at and now - t.finished_at < 60)
            ]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.as_dict() for t in tasks[:limit]]

    def current_task_id(self) -> Optional[str]:
        """Most recently-started RUNNING task, else most recently-queued
        task — lets voice commands like 'Stop.' / 'Resume.' refer to
        'whatever's happening' without the user naming a task_id."""
        with self._lock:
            running = [t for t in self._tasks.values() if t.status == "RUNNING"]
            if running:
                return max(running, key=lambda t: t.started_at or 0).task_id
            queued = [t for t in self._tasks.values() if t.status in ("QUEUED", "PAUSED")]
            if queued:
                return max(queued, key=lambda t: t.created_at).task_id
            return None

    def list_active(self) -> List[dict]:
        with self._lock:
            return [t.as_dict() for t in self._tasks.values()
                    if t.status in ("QUEUED", "RUNNING", "PAUSED")]

    def list_all(self) -> List[dict]:
        with self._lock:
            return [t.as_dict() for t in self._tasks.values()]

    def describe_summary(self) -> str:
        """Human-readable answer for 'What are you doing?' / 'How much is
        left?' — zero LLM calls, near-instant."""
        with self._lock:
            tasks = list(self._tasks.values())
        running = [t for t in tasks if t.status == "RUNNING"]
        queued = [t for t in tasks if t.status == "QUEUED"]
        paused = [t for t in tasks if t.status == "PAUSED"]
        failed = [t for t in tasks if t.status == "FAILED"]
        completed = [t for t in tasks if t.status == "COMPLETED"]

        if not tasks:
            return "I'm not running any background tasks right now."

        bits = []
        if running:
            pieces = []
            for t in running:
                detail = t.name
                if t.waiting:
                    detail += f" (waiting: {t.waiting_reason})" if t.waiting_reason else " (waiting)"
                elif t.current_step:
                    detail += f" — {t.current_step}"
                    if t.progress_pct is not None:
                        detail += f", {t.progress_pct:.0f}%"
                pieces.append(detail)
            bits.append("Currently doing: " + ", ".join(pieces) + ".")
        if paused:
            bits.append("Paused: " + ", ".join(t.name for t in paused) + ".")
        if queued:
            bits.append(f"{len(queued)} more queued up.")
        if failed:
            bits.append(f"{len(failed)} failed.")
        if completed and not (running or queued or paused):
            bits.append(f"{len(completed)} completed.")
        return " ".join(bits) if bits else "Nothing outstanding right now."

    # -- event publishing / registry mirroring ------------------------------
    def _publish(self, event_name: str, **data) -> None:
        try:
            from state_engine import event_bus
            event_bus.publish(event_name, **data)
        except Exception:
            log.debug(f"[TaskQueue] publish('{event_name}') skipped", exc_info=True)

    def _mirror_registry_start(self, task: Task) -> None:
        try:
            from state_engine import state
            state.tasks.start(task.task_id, task.name)
        except Exception:
            pass

    def _mirror_registry_complete(self, task: Task, ok: bool, detail: str = "") -> None:
        try:
            from state_engine import state
            state.tasks.complete(task.task_id, ok=ok, detail=detail or task.error)
        except Exception:
            pass

    def _mirror_registry_progress(self, task: Task) -> None:
        try:
            from state_engine import state
            state.tasks.update_progress(
                task.task_id,
                current_step=task.current_step, step_index=task.step_index,
                total_steps=task.total_steps, completed_steps=task.completed_steps,
                progress_pct=task.progress_pct, eta_seconds=task.eta_seconds,
                waiting=task.waiting, waiting_reason=task.waiting_reason,
                retry_count=task.retry_count, verifying=task.verifying,
            )
        except Exception:
            pass


    # -- checkpoint persistence (Phase 3.1) ----------------------------------
    def _save_checkpoints(self) -> None:
        try:
            import json, pathlib
            path = pathlib.Path("memory/task_checkpoints.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            active = self.list_active()
            path.write_text(json.dumps(active, indent=2), encoding="utf-8")
        except Exception as exc:
            log.debug(f"[TaskQueue] _save_checkpoints failed: {exc}")

    def _load_checkpoints(self) -> List[dict]:
        try:
            import json, pathlib
            path = pathlib.Path("memory/task_checkpoints.json")
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.debug(f"[TaskQueue] _load_checkpoints failed: {exc}")
        return []


# Process-wide singleton — every module imports this same instance.
task_queue = TaskQueue()

__all__ = ["Task", "TaskQueue", "task_queue"]
