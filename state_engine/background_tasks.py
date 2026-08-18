"""
state_engine/background_tasks.py — tracks long-lived background
services (reminders, alarms, meeting watcher, ...)
separately from the Primary State, so e.g. an active reminder timer
doesn't block GAMA from also being SLEEPING or LISTENING.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .enums import TaskStatus


@dataclass
class BackgroundTask:
    task_id: str
    name: str
    status: TaskStatus = TaskStatus.RUNNING
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Live Task Awareness — mirrored from core/task_queue.py's Task so
    # the debug panel / "what are you doing?" answers reflect the
    # current step even for callers that only read state.tasks and
    # never touch core.task_queue directly.
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

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "current_step": self.current_step,
            "step_index": self.step_index,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "progress_pct": self.progress_pct,
            "eta_seconds": self.eta_seconds,
            "waiting": self.waiting,
            "waiting_reason": self.waiting_reason,
            "retry_count": self.retry_count,
            "verifying": self.verifying,
        }


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, BackgroundTask] = {}

    def start(self, task_id: str, name: str, detail: str = "") -> BackgroundTask:
        with self._lock:
            task = BackgroundTask(task_id=task_id, name=name, detail=detail)
            self._tasks[task_id] = task
            return task

    def update(self, task_id: str, detail: str = "", status: Optional[TaskStatus] = None) -> Optional[BackgroundTask]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if detail:
                task.detail = detail
            if status is not None:
                task.status = status
            task.updated_at = time.time()
            return task

    def update_progress(self, task_id: str, **fields) -> Optional[BackgroundTask]:
        """Merge Live Task Awareness fields (current_step, progress_pct,
        eta_seconds, waiting, retry_count, ...) reported by
        core/task_queue.py's report_step()/set_waiting()/etc."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.updated_at = time.time()
            return task

    def complete(self, task_id: str, ok: bool = True, detail: str = "") -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = TaskStatus.COMPLETED if ok else TaskStatus.FAILED
            if detail:
                task.detail = detail
            task.updated_at = time.time()

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def active(self) -> list[BackgroundTask]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status == TaskStatus.RUNNING]

    def all(self) -> list[BackgroundTask]:
        with self._lock:
            return list(self._tasks.values())
