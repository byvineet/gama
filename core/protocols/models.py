"""
core/protocols/models.py — Data models for Gama's JARVIS-style Protocol System
================================================================================
Pure data classes: no side effects, no I/O. Everything else in core.protocols
builds on these shapes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class ActionType(str, Enum):
    """Every kind of step a Protocol can execute. New types can be appended
    here and handled in core.protocols.actions without touching the executor."""

    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    OPEN_FOLDER = "open_folder"
    OPEN_FILE = "open_file"
    TERMINAL = "terminal"
    KEYBOARD = "keyboard"
    TYPE_TEXT = "type_text"
    MOUSE = "mouse"
    BROWSER = "browser"
    WEB_SEARCH = "web_search"
    MEDIA_PLAY = "media_play"
    MEDIA_PAUSE = "media_pause"
    MEDIA_CONTROL = "media_control"
    VOLUME = "volume"
    BRIGHTNESS = "brightness"
    NOTIFICATION = "notification"
    CLIPBOARD = "clipboard"
    WAIT = "wait"
    WAIT_PROCESS = "wait_process"
    ASK_USER = "ask_user"
    SPEAK = "speak"
    AI_PROMPT = "ai_prompt"
    CALL_PROTOCOL = "call_protocol"
    PLUGIN = "plugin"
    TOOL = "tool"


class OnFailureStrategy(str, Enum):
    RETRY = "retry"
    FALLBACK = "fallback"
    SKIP = "skip"
    ASK_USER = "ask_user"
    ABORT = "abort"


class PermissionLevel(str, Enum):
    """Controls whether a protocol runs instantly or needs a spoken/typed
    confirmation first. Destructive protocols should be CONFIRM or higher."""

    INSTANT = "instant"
    CONFIRM = "confirm"
    RESTRICTED = "restricted"


@dataclass
class ProtocolStep:
    """A single action inside a Protocol's workflow."""

    action_type: str
    target: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    # Workflow control
    step_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parallel_group: Optional[str] = None
    condition: Optional[Dict[str, Any]] = None  # {"type": "...", "op": "...", "value": ...}
    on_failure: str = OnFailureStrategy.SKIP.value
    fallback_step: Optional["ProtocolStep"] = None
    retries: int = 0
    timeout_secs: Optional[float] = None
    order: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "step_id": self.step_id,
            "action_type": self.action_type,
            "target": self.target,
            "params": self.params,
            "parallel_group": self.parallel_group,
            "condition": self.condition,
            "on_failure": self.on_failure,
            "retries": self.retries,
            "timeout_secs": self.timeout_secs,
            "order": self.order,
        }
        if self.fallback_step is not None:
            d["fallback_step"] = self.fallback_step.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProtocolStep":
        fb = data.get("fallback_step")
        return cls(
            action_type=data.get("action_type", ActionType.WAIT.value),
            target=data.get("target", ""),
            params=data.get("params", {}) or {},
            step_id=data.get("step_id") or uuid.uuid4().hex[:8],
            parallel_group=data.get("parallel_group"),
            condition=data.get("condition"),
            on_failure=data.get("on_failure", OnFailureStrategy.SKIP.value),
            fallback_step=cls.from_dict(fb) if fb else None,
            retries=int(data.get("retries", 0)),
            timeout_secs=data.get("timeout_secs"),
            order=int(data.get("order", 0)),
        )


@dataclass
class ProtocolTrigger:
    """Future-ready trigger definition (scheduling architecture). Not
    actively evaluated yet, but persisted so it survives until a scheduler
    is added."""

    trigger_type: str  # e.g. "startup", "shutdown", "time", "device_event"
    value: Optional[str] = None  # e.g. "22:00", "headphones_connected"
    enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"trigger_type": self.trigger_type, "value": self.value, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProtocolTrigger":
        return cls(
            trigger_type=data.get("trigger_type", ""),
            value=data.get("value"),
            enabled=bool(data.get("enabled", False)),
        )


@dataclass
class ProtocolExecutionRecord:
    """One row of execution history / one live execution's status."""

    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    protocol_id: str = ""
    protocol_name: str = ""
    status: str = "running"  # running | paused | completed | failed | cancelled
    current_step_index: int = 0
    total_steps: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    logs: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "protocol_id": self.protocol_id,
            "protocol_name": self.protocol_name,
            "status": self.status,
            "current_step_index": self.current_step_index,
            "total_steps": self.total_steps,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": self.logs,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProtocolExecutionRecord":
        return cls(
            execution_id=data.get("execution_id") or uuid.uuid4().hex,
            protocol_id=data.get("protocol_id", ""),
            protocol_name=data.get("protocol_name", ""),
            status=data.get("status", "running"),
            current_step_index=int(data.get("current_step_index", 0)),
            total_steps=int(data.get("total_steps", 0)),
            started_at=data.get("started_at", time.time()),
            finished_at=data.get("finished_at"),
            logs=data.get("logs", []) or [],
            error=data.get("error"),
        )


@dataclass
class Protocol:
    """A complete, persisted Protocol definition."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    numeric_id: Optional[int] = None
    display_name: str = ""
    description: str = ""
    category: str = "General"
    tags: List[str] = field(default_factory=list)
    steps: List[ProtocolStep] = field(default_factory=list)
    triggers: List[ProtocolTrigger] = field(default_factory=list)
    permission_level: str = PermissionLevel.INSTANT.value
    enabled: bool = True
    version: int = 1
    created_at: float = field(default_factory=time.time)
    modified_at: float = field(default_factory=time.time)
    run_count: int = 0
    last_run_at: Optional[float] = None

    @property
    def display_identifier(self) -> str:
        if self.numeric_id is not None:
            return f"Protocol {self.numeric_id} ({self.display_name})"
        return self.display_name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "numeric_id": self.numeric_id,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "steps": [s.to_dict() for s in self.steps],
            "triggers": [t.to_dict() for t in self.triggers],
            "permission_level": self.permission_level,
            "enabled": self.enabled,
            "version": self.version,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "run_count": self.run_count,
            "last_run_at": self.last_run_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Protocol":
        return cls(
            id=data.get("id") or uuid.uuid4().hex,
            numeric_id=data.get("numeric_id"),
            display_name=data.get("display_name", ""),
            description=data.get("description", ""),
            category=data.get("category", "General"),
            tags=data.get("tags", []) or [],
            steps=[ProtocolStep.from_dict(s) for s in data.get("steps", [])],
            triggers=[ProtocolTrigger.from_dict(t) for t in data.get("triggers", [])],
            permission_level=data.get("permission_level", PermissionLevel.INSTANT.value),
            enabled=bool(data.get("enabled", True)),
            version=int(data.get("version", 1)),
            created_at=data.get("created_at", time.time()),
            modified_at=data.get("modified_at", time.time()),
            run_count=int(data.get("run_count", 0)),
            last_run_at=data.get("last_run_at"),
        )


__all__ = [
    "ActionType",
    "OnFailureStrategy",
    "PermissionLevel",
    "ProtocolStep",
    "ProtocolTrigger",
    "ProtocolExecutionRecord",
    "Protocol",
]
