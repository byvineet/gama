"""
visual_schema.py — Pydantic Canvas DSL schema for Gama.

Gemini 3.1 Flash-Lite produces structured JSON.
This module validates it before anything reaches React.

Rules:
- Never execute model output
- Reject unknown primitives / dangerous attributes
- Enforce reasonable complexity limits
- Keep validation pure (no rendering)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── Limits ──────────────────────────────────────────────────────────────────
MAX_ELEMENTS = 80
MAX_CHILDREN_DEPTH = 8
MAX_TEXT_LEN = 2000
MAX_PATH_LEN = 8000
MAX_SCENE_CHILDREN = 12
MAX_ID_LEN = 64


class TransitionName(str, Enum):
    none = "none"
    fade = "fade"
    slide = "slide"
    scale = "scale"
    reveal = "reveal"
    scan = "scan"
    pulse = "pulse"
    glow = "glow"
    dissolve = "dissolve"
    rotate = "rotate"
    draw = "draw"


class SceneType(str, Enum):
    idle = "idle"
    weather = "weather"
    tasks = "tasks"
    goals = "goals"
    reminders = "reminders"
    alerts = "alerts"
    calendar = "calendar"
    timer = "timer"
    pomodoro = "pomodoro"
    music = "music"
    system = "system"
    status = "status"
    execution = "execution"
    search = "search"
    notes = "notes"
    information = "information"
    table = "table"
    list = "list"
    chart = "chart"
    progress = "progress"
    card = "card"
    gauge = "gauge"
    metric = "metric"
    timeline = "timeline"
    confirm = "confirm"
    notification = "notification"
    image = "image"
    scene = "scene"
    custom_svg = "custom_svg"
    dsl = "dsl"
    compose = "compose"
    model_3d = "model_3d"
    clock = "clock"
    time = "time"


ALLOWED_SVG_TYPES = frozenset({
    "g", "text", "line", "circle", "ellipse", "rect",
    "path", "polygon", "polyline", "image",
})


class TransitionSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enter: Optional[TransitionName] = None
    update: Optional[TransitionName] = None
    exit: Optional[TransitionName] = None
    duration: Optional[int] = Field(default=None, ge=0, le=5000)


class PositionSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    y: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class SizeSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    w: Optional[float] = Field(default=None, ge=0.05, le=1.0)
    h: Optional[float] = Field(default=None, ge=0.05, le=1.0)


class SceneStyle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    opacity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    accent: Optional[str] = Field(default=None, max_length=32)
    background: Optional[str] = Field(default=None, max_length=64)
    padding: Optional[Union[str, int, float]] = None
    align: Optional[Literal["start", "center", "end", "stretch"]] = None


class SvgElement(BaseModel):
    """Declarative SVG primitive — no event handlers, no scripts."""

    model_config = ConfigDict(extra="ignore")

    type: str
    id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)

    # geometry
    x: Optional[float] = None
    y: Optional[float] = None
    cx: Optional[float] = None
    cy: Optional[float] = None
    r: Optional[float] = None
    rx: Optional[float] = None
    ry: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    x1: Optional[float] = None
    y1: Optional[float] = None
    x2: Optional[float] = None
    y2: Optional[float] = None
    d: Optional[str] = Field(default=None, max_length=MAX_PATH_LEN)
    points: Optional[str] = Field(default=None, max_length=4000)

    # text
    text: Optional[str] = Field(default=None, max_length=MAX_TEXT_LEN)

    # presentation
    fill: Optional[str] = Field(default=None, max_length=200)
    stroke: Optional[str] = Field(default=None, max_length=200)
    strokeWidth: Optional[Union[float, str]] = None
    opacity: Optional[Union[float, str]] = None
    fontSize: Optional[Union[float, str]] = None
    fontFamily: Optional[str] = Field(default=None, max_length=120)
    fontWeight: Optional[Union[str, int, float]] = None
    textAnchor: Optional[Literal["start", "middle", "end"]] = None
    transform: Optional[str] = Field(default=None, max_length=200)
    className: Optional[str] = Field(default=None, max_length=64)

    # image only
    href: Optional[str] = Field(default=None, max_length=500_000)

    children: Optional[List["SvgElement"]] = None

    @field_validator("type")
    @classmethod
    def _type_allowed(cls, v: str) -> str:
        t = (v or "").strip().lower()
        if t not in ALLOWED_SVG_TYPES:
            raise ValueError(f"unsupported SVG primitive: {v}")
        return t

    @field_validator("href")
    @classmethod
    def _safe_href(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        low = s.lower()
        if low.startswith("javascript:") or "<script" in low:
            raise ValueError("unsafe image href")
        if not (
            low.startswith("data:image/")
            or low.startswith("https://")
            or low.startswith("http://")
            or low.startswith("/")
            or low.startswith("./")
            or low.startswith("blob:")
        ):
            raise ValueError("image href must be data:, http(s), or relative")
        return s

    @field_validator("text", "d", "points", "fill", "stroke", "transform", "className", "fontFamily")
    @classmethod
    def _no_script(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if "javascript:" in v.lower() or "<script" in v.lower():
            raise ValueError("forbidden content in string field")
        return v

    @model_validator(mode="after")
    def _limit_children(self) -> "SvgElement":
        if self.children and len(self.children) > MAX_ELEMENTS:
            self.children = self.children[:MAX_ELEMENTS]
        return self


SvgElement.model_rebuild()


class CustomSvgData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    viewBox: str = Field(default="0 0 1000 600", max_length=64)
    width: Optional[Union[str, float, int]] = "100%"
    height: Optional[Union[str, float, int]] = "100%"
    background: Optional[str] = Field(default=None, max_length=64)
    elements: List[SvgElement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cap_elements(self) -> "CustomSvgData":
        if len(self.elements) > MAX_ELEMENTS:
            self.elements = self.elements[:MAX_ELEMENTS]
        return self


class CanvasScene(BaseModel):
    """Top-level scene document produced by the visual model or tools."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    type: str
    layer: int = Field(default=1, ge=0, le=4)
    title: Optional[str] = Field(default=None, max_length=200)
    data: Dict[str, Any] = Field(default_factory=dict)
    children: Optional[List["CanvasScene"]] = None
    position: Optional[PositionSpec] = None
    size: Optional[SizeSpec] = None
    transition: Optional[TransitionSpec] = None
    animation: Optional[TransitionSpec] = None
    duration: Optional[int] = Field(default=None, ge=0, le=600_000)
    style: Optional[SceneStyle] = None
    interactive: Optional[bool] = None

    # Convenience fields the model sometimes puts at top level for custom_svg
    viewBox: Optional[str] = None
    elements: Optional[List[SvgElement]] = None

    @field_validator("type")
    @classmethod
    def _normalize_type(cls, v: str) -> str:
        t = (v or "information").strip().lower().replace("-", "_")
        # accept enum values + a few aliases
        aliases = {
            "task": "tasks",
            "goal": "goals",
            "reminder": "reminders",
            "alert": "alerts",
            "svg": "custom_svg",
            "hud": "custom_svg",
            "custom": "custom_svg",
            "sys": "system",
            "status_panel": "status",
            "model3d": "model_3d",
            "3d": "model_3d",
            "mesh": "model_3d",
            "solid": "model_3d",
        }
        t = aliases.get(t, t)
        try:
            return SceneType(t).value
        except ValueError:
            # unknown types fall back to information rather than crashing
            return "information"

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        s = "".join(c if c.isalnum() or c in "-_" else "-" for c in (v or "scene"))
        return (s or "scene")[:MAX_ID_LEN]

    @model_validator(mode="after")
    def _promote_svg_fields(self) -> "CanvasScene":
        # Model often returns type=custom_svg with elements at top level
        if self.type in ("custom_svg", "dsl") and self.elements:
            data = dict(self.data or {})
            if "elements" not in data:
                data["elements"] = [e.model_dump(exclude_none=True) for e in self.elements]
            if self.viewBox and "viewBox" not in data:
                data["viewBox"] = self.viewBox
            self.data = data
        if self.children and len(self.children) > MAX_SCENE_CHILDREN:
            self.children = self.children[:MAX_SCENE_CHILDREN]
        return self


CanvasScene.model_rebuild()


class ValidationResult(BaseModel):
    ok: bool
    scene: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)


def validate_scene(raw: Any) -> ValidationResult:
    """
    Validate model/tool output into a safe scene dict for the display protocol.
    Never raises — always returns ValidationResult.
    """
    if not isinstance(raw, dict):
        return ValidationResult(ok=False, errors=["scene must be a JSON object"])

    try:
        # Soft-normalize common shapes before strict parse
        payload = dict(raw)
        if "id" not in payload or not payload.get("id"):
            payload["id"] = str(payload.get("type") or "visual") + "-main"
        if "type" not in payload:
            if payload.get("elements"):
                payload["type"] = "custom_svg"
            elif payload.get("children"):
                payload["type"] = "compose"
            else:
                payload["type"] = "information"

        scene = CanvasScene.model_validate(payload)
        out = scene.model_dump(exclude_none=True)

        # Strip internal convenience fields that React doesn't need at top level
        out.pop("viewBox", None)
        out.pop("elements", None)

        # Ensure custom_svg has elements inside data
        if out.get("type") in ("custom_svg", "dsl"):
            data = out.setdefault("data", {})
            if not data.get("elements") and payload.get("elements"):
                # re-validate nested elements already done by model
                els = payload.get("elements") or []
                cleaned = []
                for e in els[:MAX_ELEMENTS]:
                    try:
                        cleaned.append(SvgElement.model_validate(e).model_dump(exclude_none=True))
                    except Exception:
                        continue
                data["elements"] = cleaned
            if not data.get("viewBox"):
                data["viewBox"] = payload.get("viewBox") or "0 0 1000 600"

        return ValidationResult(ok=True, scene=out)
    except Exception as exc:
        return ValidationResult(ok=False, errors=[str(exc)])


def validate_svg_elements(elements: Any) -> List[Dict[str, Any]]:
    """Validate a raw list of SVG elements; drop invalid ones."""
    if not isinstance(elements, list):
        return []
    out: List[Dict[str, Any]] = []
    for e in elements[:MAX_ELEMENTS]:
        try:
            out.append(SvgElement.model_validate(e).model_dump(exclude_none=True))
        except Exception:
            continue
    return out


__all__ = [
    "CanvasScene",
    "CustomSvgData",
    "SvgElement",
    "TransitionSpec",
    "ValidationResult",
    "validate_scene",
    "validate_svg_elements",
    "MAX_ELEMENTS",
]
