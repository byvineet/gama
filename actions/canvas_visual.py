"""
canvas_visual.py — Local, Live-safe visual generator for Gama Canvas.

No nested Gemini / Flash-Lite calls. Custom visuals are built from local
declarative SVG templates so the Live WebSocket is never blocked and
cannot 1011 from a secondary model request.

Gemini Live only coordinates; this module pushes validated Canvas DSL
via the display protocol.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)


def _radar_elements(
    objects: Optional[List[Dict[str, Any]]] = None,
    *,
    title: str = "GAMA RADAR",
) -> List[Dict[str, Any]]:
    cx, cy = 500, 310
    els: List[Dict[str, Any]] = []

    els.append({
        "type": "rect", "x": 40, "y": 30, "width": 920, "height": 540,
        "rx": 12, "fill": "none", "stroke": "#0e7490", "strokeWidth": 1, "opacity": 0.35,
    })
    els.append({
        "type": "text", "x": 500, "y": 58, "text": title,
        "textAnchor": "middle", "fill": "#7dd3fc", "fontSize": 20, "fontWeight": 600,
    })
    els.append({
        "type": "text", "x": 500, "y": 82, "text": "SCAN ACTIVE",
        "textAnchor": "middle", "fill": "#38bdf8", "fontSize": 11, "opacity": 0.7,
    })

    for r, op in ((70, 0.25), (120, 0.35), (180, 0.45), (240, 0.55)):
        els.append({
            "type": "circle", "cx": cx, "cy": cy, "r": r,
            "fill": "none", "stroke": "#38bdf8", "strokeWidth": 1, "opacity": op,
        })

    els.append({
        "type": "line", "x1": cx - 250, "y1": cy, "x2": cx + 250, "y2": cy,
        "stroke": "#0e7490", "strokeWidth": 1, "opacity": 0.4,
    })
    els.append({
        "type": "line", "x1": cx, "y1": cy - 250, "x2": cx, "y2": cy + 250,
        "stroke": "#0e7490", "strokeWidth": 1, "opacity": 0.4,
    })

    a0, a1 = -20, 40
    r = 240
    x0 = cx + r * math.cos(math.radians(a0 - 90))
    y0 = cy + r * math.sin(math.radians(a0 - 90))
    x1 = cx + r * math.cos(math.radians(a1 - 90))
    y1 = cy + r * math.sin(math.radians(a1 - 90))
    els.append({
        "type": "path",
        "d": f"M {cx} {cy} L {x0:.1f} {y0:.1f} A {r} {r} 0 0 1 {x1:.1f} {y1:.1f} Z",
        "fill": "#38bdf8", "opacity": 0.08, "stroke": "#38bdf8", "strokeWidth": 1,
    })
    els.append({
        "type": "line", "x1": cx, "y1": cy, "x2": x1, "y2": y1,
        "stroke": "#7dd3fc", "strokeWidth": 1.5, "opacity": 0.7,
    })

    els.append({
        "type": "circle", "cx": cx, "cy": cy, "r": 18,
        "fill": "#0c4a6e", "stroke": "#38bdf8", "strokeWidth": 2,
    })
    els.append({
        "type": "circle", "cx": cx, "cy": cy, "r": 6,
        "fill": "#7dd3fc", "stroke": "none",
    })
    els.append({
        "type": "text", "x": cx, "y": cy + 36, "text": "GAMA",
        "textAnchor": "middle", "fill": "#e0f2fe", "fontSize": 11, "fontWeight": 600,
    })

    if not objects:
        objects = [
            {"label": "ALPHA", "angle": 35, "range": 0.55},
            {"label": "BRAVO", "angle": 110, "range": 0.72},
            {"label": "CHARLIE", "angle": 200, "range": 0.48},
            {"label": "DELTA", "angle": 280, "range": 0.85},
            {"label": "ECHO", "angle": 320, "range": 0.38},
        ]

    for obj in objects[:8]:
        try:
            ang = float(obj.get("angle", 0))
            rng = float(obj.get("range", 0.6))
            label = str(obj.get("label") or obj.get("name") or "OBJ")[:12]
        except Exception:
            continue
        dist = 70 + max(0.15, min(1.0, rng)) * 170
        bx = cx + dist * math.cos(math.radians(ang - 90))
        by = cy + dist * math.sin(math.radians(ang - 90))
        els.append({
            "type": "circle", "cx": bx, "cy": by, "r": 5,
            "fill": "#22d3ee", "stroke": "#e0f2fe", "strokeWidth": 1,
        })
        els.append({
            "type": "circle", "cx": bx, "cy": by, "r": 12,
            "fill": "none", "stroke": "#22d3ee", "strokeWidth": 1, "opacity": 0.35,
        })
        els.append({
            "type": "text", "x": bx, "y": by - 16, "text": label,
            "textAnchor": "middle", "fill": "#a5f3fc", "fontSize": 10,
        })

    els.append({
        "type": "text", "x": 60, "y": 550, "text": f"TRACKS  {len(objects[:8])}",
        "fill": "#67e8f9", "fontSize": 11, "opacity": 0.8,
    })
    els.append({
        "type": "text", "x": 940, "y": 550, "text": "RNG 240",
        "textAnchor": "end", "fill": "#67e8f9", "fontSize": 11, "opacity": 0.8,
    })
    return els


def _system_hud_elements(context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    ctx = context or {}

    def _f(key: str, default: float) -> float:
        try:
            v = ctx.get(key)
            return float(v) if v is not None else default
        except Exception:
            return default

    cpu_v, ram_v, disk_v = _f("cpu", 42.0), _f("ram", 58.0), _f("disk", 65.0)
    els: List[Dict[str, Any]] = []
    els.append({
        "type": "text", "x": 500, "y": 70, "text": "GAMA SYSTEM",
        "textAnchor": "middle", "fill": "#7dd3fc", "fontSize": 22, "fontWeight": 600,
    })
    els.append({
        "type": "circle", "cx": 500, "cy": 300, "r": 110,
        "fill": "none", "stroke": "#38bdf8", "strokeWidth": 2,
    })
    els.append({
        "type": "circle", "cx": 500, "cy": 300, "r": 90,
        "fill": "none", "stroke": "#0e7490", "strokeWidth": 1, "opacity": 0.6,
    })
    els.append({
        "type": "text", "x": 500, "y": 295, "text": "CORE",
        "textAnchor": "middle", "fill": "#e0f2fe", "fontSize": 14,
    })
    els.append({
        "type": "text", "x": 500, "y": 318, "text": "ONLINE",
        "textAnchor": "middle", "fill": "#22d3ee", "fontSize": 12,
    })

    def gauge(x: float, label: str, value: float) -> None:
        els.append({
            "type": "circle", "cx": x, "cy": 300, "r": 55,
            "fill": "none", "stroke": "#164e63", "strokeWidth": 8,
        })
        els.append({
            "type": "circle", "cx": x, "cy": 300, "r": 55,
            "fill": "none", "stroke": "#38bdf8", "strokeWidth": 8,
            "opacity": max(0.25, min(1.0, value / 100.0)),
        })
        els.append({
            "type": "text", "x": x, "y": 296, "text": f"{value:.0f}%",
            "textAnchor": "middle", "fill": "#e0f2fe", "fontSize": 16, "fontWeight": 600,
        })
        els.append({
            "type": "text", "x": x, "y": 380, "text": label,
            "textAnchor": "middle", "fill": "#7dd3fc", "fontSize": 12,
        })

    gauge(220, "CPU", cpu_v)
    gauge(780, "RAM", ram_v)
    els.append({
        "type": "text", "x": 500, "y": 520, "text": f"DISK  {disk_v:.0f}%",
        "textAnchor": "middle", "fill": "#67e8f9", "fontSize": 13,
    })
    return els


def _neural_elements() -> List[Dict[str, Any]]:
    els: List[Dict[str, Any]] = []
    els.append({
        "type": "text", "x": 500, "y": 50, "text": "PROCESS GRAPH",
        "textAnchor": "middle", "fill": "#7dd3fc", "fontSize": 18, "fontWeight": 600,
    })
    nodes = [
        (500, 160, "IN"),
        (320, 280, "PARSE"),
        (500, 280, "ROUTE"),
        (680, 280, "TOOL"),
        (500, 420, "OUT"),
    ]
    links = [(0, 1), (0, 2), (0, 3), (1, 4), (2, 4), (3, 4)]
    for a, b in links:
        x1, y1, _ = nodes[a]
        x2, y2, _ = nodes[b]
        els.append({
            "type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "stroke": "#0e7490", "strokeWidth": 1.5, "opacity": 0.7,
        })
    for x, y, label in nodes:
        els.append({
            "type": "circle", "cx": x, "cy": y, "r": 28,
            "fill": "#0c4a6e", "stroke": "#38bdf8", "strokeWidth": 2,
        })
        els.append({
            "type": "text", "x": x, "y": y + 4, "text": label,
            "textAnchor": "middle", "fill": "#e0f2fe", "fontSize": 11, "fontWeight": 600,
        })
    return els


def _generic_title_elements(prompt: str) -> List[Dict[str, Any]]:
    title = (prompt or "GAMA").strip()
    if len(title) > 48:
        title = title[:45] + "…"
    return [
        {
            "type": "rect", "x": 80, "y": 120, "width": 840, "height": 360,
            "rx": 16, "fill": "none", "stroke": "#0e7490", "strokeWidth": 1, "opacity": 0.5,
        },
        {
            "type": "circle", "cx": 500, "cy": 260, "r": 48,
            "fill": "none", "stroke": "#38bdf8", "strokeWidth": 2,
        },
        {
            "type": "circle", "cx": 500, "cy": 260, "r": 16,
            "fill": "#22d3ee", "stroke": "none",
        },
        {
            "type": "text", "x": 500, "y": 340, "text": "GAMA",
            "textAnchor": "middle", "fill": "#7dd3fc", "fontSize": 22, "fontWeight": 600,
        },
        {
            "type": "text", "x": 500, "y": 370, "text": title,
            "textAnchor": "middle", "fill": "#67e8f9", "fontSize": 13, "opacity": 0.85,
        },
    ]


def _match_local_template(prompt: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = (prompt or "").lower()
    if any(k in p for k in ("radar", "sonar", "scan beam", "scanning beam", "blip")):
        return {
            "type": "custom_svg",
            "id": "radar-main",
            "title": "Radar",
            "layer": 1,
            "viewBox": "0 0 1000 600",
            "elements": _radar_elements(title="GAMA RADAR"),
            "transition": {"enter": "scan", "exit": "dissolve", "duration": 400},
            "size": {"w": 0.72, "h": 0.72},
            "position": {"x": 0.5, "y": 0.48},
        }
    if any(k in p for k in ("system monitor", "system hud", "jarvis", "cpu ram", "hud showing", "system status visual")):
        return {
            "type": "custom_svg",
            "id": "sys-hud",
            "title": "System HUD",
            "layer": 1,
            "viewBox": "0 0 1000 600",
            "elements": _system_hud_elements(context),
            "transition": {"enter": "scan", "exit": "dissolve", "duration": 400},
            "size": {"w": 0.72, "h": 0.72},
            "position": {"x": 0.5, "y": 0.48},
        }
    if any(k in p for k in ("neural", "process graph", "how gama processes", "pipeline", "workflow")):
        return {
            "type": "custom_svg",
            "id": "process-graph",
            "title": "Process",
            "layer": 1,
            "viewBox": "0 0 1000 600",
            "elements": _neural_elements(),
            "transition": {"enter": "fade", "exit": "dissolve", "duration": 350},
            "size": {"w": 0.7, "h": 0.65},
            "position": {"x": 0.5, "y": 0.48},
        }
    return {
        "type": "custom_svg",
        "id": "visual-main",
        "title": "Visual",
        "layer": 1,
        "viewBox": "0 0 1000 600",
        "elements": _generic_title_elements(prompt),
        "transition": {"enter": "fade", "exit": "dissolve", "duration": 300},
        "size": {"w": 0.55, "h": 0.55},
        "position": {"x": 0.5, "y": 0.48},
    }


def _validate(scene: dict) -> dict:
    try:
        from actions.visual_schema import validate_scene
        result = validate_scene(scene)
        if result.ok and result.scene:
            return result.scene
    except Exception as exc:
        log.debug("canvas_visual validate soft-fail: %s", exc)
    return scene


def generate_visual(prompt: str, *, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Always local templates — never calls an external model."""
    scene = _match_local_template(prompt, context)
    return _validate(scene)


def generate_and_show(prompt: str, **kwargs) -> str:
    try:
        return _generate_and_show_impl(prompt, **kwargs)
    except Exception as exp:
        log.warning("generate_and_show failed: %s", exp)
        return "Visual generation failed."


def _generate_and_show_impl(prompt: str, **kwargs) -> str:
    from actions.display_stage import canvas_show, canvas_custom_svg

    context: Dict[str, Any] = {}
    try:
        from core import web_bridge as wb
        snap = getattr(wb, "_snapshot", {}) or {}
        context = {
            "cpu": snap.get("cpu"),
            "ram": snap.get("ram"),
            "disk": snap.get("disk"),
        }
    except Exception:
        pass

    scene = generate_visual(prompt, context=context)
    st = str(scene.get("type") or "custom_svg").lower()
    sid = str(scene.get("id") or "visual-main")
    data = scene.get("data") or {}
    elements = data.get("elements") or scene.get("elements") or []
    if not isinstance(elements, list):
        elements = []
    try:
        from actions.visual_schema import validate_svg_elements
        elements = validate_svg_elements(elements)
    except Exception:
        elements = [e for e in elements if isinstance(e, dict)][:80]

    if st in ("custom_svg", "dsl"):
        canvas_custom_svg(
            sid,
            elements,
            viewBox=str(data.get("viewBox") or scene.get("viewBox") or "0 0 1000 600"),
            title=str(scene.get("title") or "Visual"),
            layer=int(scene.get("layer") or 1),
        )
        pos = scene.get("position")
        size = scene.get("size")
        if pos or size:
            try:
                from actions.display_stage import canvas_command
                canvas_command("update", scene_id=sid, position=pos, size=size)
            except Exception:
                pass
        return f"Canvas visual '{sid}' shown."

    canvas_show({
        "id": sid,
        "type": st,
        "layer": int(scene.get("layer") or 1),
        "title": scene.get("title") or st,
        "data": data,
        "position": scene.get("position"),
        "size": scene.get("size"),
        "transition": scene.get("transition")
        or {"enter": "scan", "exit": "dissolve", "duration": 400},
    })
    return f"Canvas panel '{sid}' shown."


def canvas_visual(action: str = "generate", **kwargs) -> str:
    action = (action or "generate").lower().strip()
    prompt = str(
        kwargs.pop("prompt", None)
        or kwargs.pop("description", None)
        or kwargs.pop("text", None)
        or ""
    ).strip()
    kwargs.pop("action", None)
    if action in ("generate", "design", "create", "show", "visual"):
        if not prompt:
            return "Provide a visual description."
        return generate_and_show(prompt, **kwargs)
    return "Unknown canvas_visual action. Use generate with a prompt."


__all__ = ["canvas_visual", "generate_visual", "generate_and_show"]
