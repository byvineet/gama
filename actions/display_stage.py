"""
actions/display_stage.py — Central HUD / Gama Nexus controller
==============================================================
Panels: reminders · alerts · goals · tasks · weather (current/forecast) ·
        timer · clock/time · confirm · enrollment · info · canvas (universal freeform)

**Gama Nexus** is the user-facing name for the large visual panel (formerly
"Gama Canvas" / display stage). Phrases like "show that on Nexus",
"display it on the Nexus", "put it on Nexus" all route here.

The **canvas** / **nexus** action is the universal tool: Gama can write text,
images, links, badges, metrics, comparisons, and layout blocks onto the
voice-orb display without a dedicated panel type for each use case.

Author : Gama Nexus
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("gama.display_stage")

_last: Dict[str, Any] = {"mode": "orb"}
_revision = 0
_pending_confirm_id: Optional[str] = None


def _next_rev() -> int:
    global _revision
    _revision += 1
    return _revision


def _push(payload: Dict[str, Any]) -> bool:
    global _last
    payload = dict(payload)
    payload.setdefault("revision", _next_rev())
    try:
        from core.web_bridge import push_display
        push_display(payload)
        _last = payload
        return True
    except Exception as exc:
        log.debug("display_stage push failed: %s", exc)
        return False


def _resolve_image_src(src: str) -> str:
    """Normalize image source for the canvas.

    Accepts:
      - https:// or http:// URLs (passed through)
      - data:image/... URIs (passed through)
      - local filesystem paths → base64 data URI (so the WebView can render them)
    """
    s = (src or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(("https://", "http://", "data:image/")):
        return s
    # file:// URI
    if low.startswith("file://"):
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(s).path)
        # Windows file:///C:/...
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        s = path
    try:
        from pathlib import Path as _P
        path = _P(s).expanduser()
        if not path.is_file():
            # try relative to project / cwd
            alt = _P.cwd() / s
            if alt.is_file():
                path = alt
            else:
                return s  # let the UI try as-is
        raw = path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            log.warning("image too large for data URI (%d bytes): %s", len(raw), path)
            return s
        import base64
        mime = "image/png"
        suf = path.suffix.lower()
        if suf in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif suf == ".gif":
            mime = "image/gif"
        elif suf == ".webp":
            mime = "image/webp"
        elif suf == ".svg":
            mime = "image/svg+xml"
        elif suf == ".bmp":
            mime = "image/bmp"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        log.debug("_resolve_image_src failed for %r: %s", src, exc)
        return s



def display_stage(action: str = "reminders", **kwargs) -> str:
    action = (action or "reminders").lower().strip().replace("-", "_")

    # Nexus aliases — user may say "nexus", "on nexus", "gama_nexus", etc.
    if action in (
        "nexus", "gama_nexus", "on_nexus", "to_nexus", "show_on_nexus",
        "display_on_nexus", "put_on_nexus", "nexus_show", "nexus_display",
    ):
        # Prefer explicit scene_type / content; default to freeform show
        if kwargs.get("scene_type") or kwargs.get("type") or kwargs.get("scene"):
            action = "show"
        elif kwargs.get("cpu") is not None or kwargs.get("ram") is not None or kwargs.get("scene_type") == "system":
            action = "system"
        elif kwargs.get("content") or kwargs.get("body") or kwargs.get("text") or kwargs.get("message"):
            action = "information"
        else:
            action = "show"

    # ── Gama Nexus protocol (preferred visual output channel) ──────────
    if action in ("close", "hide", "dismiss", "clear", "orb", "reset", "clear_canvas", "clear_nexus"):
        layer = kwargs.get("layer")
        try:
            if layer is not None:
                return canvas_clear(int(layer))
        except Exception:
            pass
        # Clear canvas protocol + legacy
        try:
            canvas_clear()
        except Exception:
            pass
        return _close()

    if action in ("show", "canvas_show", "present"):
        scene = kwargs.get("scene")
        if isinstance(scene, dict) and scene.get("id") and scene.get("type"):
            return canvas_show(scene)
        # Build scene from flat args
        scene_type = str(kwargs.get("scene_type") or kwargs.get("type") or "information")
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or f"{scene_type}-main")
        data = kwargs.get("data") if isinstance(kwargs.get("data"), dict) else {}
        # Promote common flat fields into data
        for k in (
            "title", "content", "body", "location", "temperature", "condition",
            "cpu", "ram", "disk", "label", "remaining_sec", "minutes", "seconds",
            "text", "message", "items", "series", "value", "progress", "metadata",
            "image", "image_url", "caption", "src", "url", "path",
        ):
            if k in kwargs and kwargs[k] is not None and k not in data:
                data[k] = kwargs[k]
        layer = kwargs.get("layer")
        try:
            layer_i = int(layer) if layer is not None else 1
        except Exception:
            layer_i = 1

        # Enrich list-style scenes from live stores so the canvas is never empty
        # when the model only passes scene_type without a payload.
        scene_type_l = str(scene_type).lower().strip()
        try:
            if scene_type_l in ("tasks", "task") and not data.get("items") and not data.get("tasks"):
                raw = _lt() if callable(_lt) else []
                # list_tasks may return a string summary — try structured sources
                try:
                    from core import web_bridge as _wb
                    items = list(getattr(_wb, "_snapshot", {}).get("tasks") or [])
                except Exception:
                    items = []
                if not items:
                    try:
                        from state_engine.task_queue import get_queue
                        q = get_queue()
                        items = list(q.snapshot() if hasattr(q, "snapshot") else [])
                    except Exception:
                        items = []
                data["items"] = items
                data["tasks"] = items
            elif scene_type_l in ("goals", "goal") and not data.get("items") and not data.get("goals"):
                try:
                    from core import web_bridge as _wb
                    items = list(getattr(_wb, "_snapshot", {}).get("goals") or [])
                except Exception:
                    items = []
                if not items:
                    try:
                        from actions.goal_tracker import list_goals
                        # may return string — also try registry
                        from state_engine import goals as _goals_mod
                        items = list(getattr(_goals_mod, "all_goals", lambda: [])() or [])
                    except Exception:
                        items = []
                data["items"] = items
                data["goals"] = items
            elif scene_type_l in ("reminders", "reminder") and not data.get("items"):
                try:
                    from core import web_bridge as _wb
                    items = list(getattr(_wb, "_snapshot", {}).get("reminders") or [])
                except Exception:
                    items = []
                data["items"] = items
                data["reminders"] = items
            elif scene_type_l in ("alerts", "alert") and not data.get("items"):
                try:
                    from core import web_bridge as _wb
                    items = list(getattr(_wb, "_snapshot", {}).get("alerts") or [])
                except Exception:
                    items = []
                data["items"] = items
                data["alerts"] = items
            elif scene_type_l in ("system", "status"):
                try:
                    from core import web_bridge as _wb
                    snap = getattr(_wb, "_snapshot", {}) or {}
                    data.setdefault("cpu", snap.get("cpu"))
                    data.setdefault("ram", snap.get("ram"))
                    data.setdefault("disk", snap.get("disk"))
                except Exception:
                    pass
            elif scene_type_l in ("timer", "pomodoro"):
                # Ensure a live countdown: compute endsAt (ms) from remaining / minutes / seconds
                try:
                    rem = data.get("remaining_sec") or data.get("remainingSec")
                    if rem is None:
                        m = int(data.get("minutes") or kwargs.get("minutes") or 0)
                        s = int(data.get("seconds") or kwargs.get("seconds") or 0)
                        rem = m * 60 + s
                    rem = int(rem or 0)
                except Exception:
                    rem = 0
                if rem > 0:
                    data["remainingSec"] = rem
                    data["totalSec"] = rem
                    data.setdefault("running", True)
                    if data.get("endsAt") is None and data.get("ends_at") is None:
                        data["endsAt"] = int(time.time() * 1000) + rem * 1000
                    data.setdefault("label", data.get("label") or kwargs.get("label") or kwargs.get("message") or "Timer")
            elif scene_type_l in ("clock", "time"):
                data.setdefault("label", data.get("label") or kwargs.get("label") or kwargs.get("title") or "TIME")
                data.setdefault("hour12", True)
                data.setdefault("showSeconds", True)
                data.setdefault("showDate", True)
                scene_type = "clock"  # normalize
        except Exception as _enrich_exc:
            log.debug("scene enrich failed: %s", _enrich_exc)

        # scene_type may have been normalized (e.g. time → clock)
        final_type = scene_type if scene_type_l not in ("clock", "time") else "clock"
        if scene_type_l in ("clock", "time"):
            final_type = "clock"
        scene = {
            "id": scene_id,
            "type": final_type,
            "layer": max(0, min(4, layer_i)),
            "title": str(kwargs.get("title") or final_type),
            "data": data,
            "transition": kwargs.get("transition") or {"enter": "fade", "exit": "dissolve", "duration": 300},
        }
        if kwargs.get("duration") is not None:
            try:
                scene["duration"] = int(kwargs["duration"])
            except Exception:
                pass
        # Resolve local image paths when scene is type image or data has src/image
        if scene_type in ("image", "photo") or data.get("src") or data.get("image") or data.get("image_url"):
            raw_src = str(data.get("src") or data.get("image") or data.get("image_url") or data.get("url") or data.get("path") or "")
            if raw_src:
                data["src"] = _resolve_image_src(raw_src)
                scene["type"] = "image"
                scene["data"] = data
        return canvas_show(scene)

    if action in ("custom_svg", "svg", "hud", "jarvis", "custom"):
        elements = kwargs.get("elements") or kwargs.get("svg_elements") or []
        if isinstance(elements, str):
            try:
                import json as _json
                elements = _json.loads(elements)
            except Exception:
                elements = []
        if not isinstance(elements, list):
            elements = []
        if not elements and kwargs.get("elements_json"):
            try:
                import json as _json
                elements = _json.loads(str(kwargs.get("elements_json") or "[]"))
            except Exception:
                elements = []
        if not isinstance(elements, list):
            elements = []
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or "custom-hud")
        view_box = str(kwargs.get("viewBox") or kwargs.get("view_box") or "0 0 1000 600")
        title = str(kwargs.get("title") or "Custom")
        try:
            layer_i = int(kwargs.get("layer") or 1)
        except Exception:
            layer_i = 1
        return canvas_custom_svg(
            scene_id,
            elements,
            viewBox=view_box,
            title=title,
            layer=max(0, min(4, layer_i)),
        )

    if action in ("image", "show_image", "photo", "picture"):
        src = str(
            kwargs.get("image")
            or kwargs.get("image_url")
            or kwargs.get("url")
            or kwargs.get("path")
            or kwargs.get("src")
            or ""
        ).strip()
        if not src:
            return "Provide an image URL or local file path, sir."
        resolved = _resolve_image_src(src)
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or "image-main")
        caption = str(kwargs.get("caption") or kwargs.get("title") or kwargs.get("text") or "")
        return canvas_show({
            "id": scene_id,
            "type": "image",
            "layer": int(kwargs.get("layer") or 1),
            "title": caption or "Image",
            "data": {
                "src": resolved,
                "caption": caption,
                "alt": str(kwargs.get("alt") or caption or "Image"),
            },
            "transition": {"enter": "fade", "exit": "dissolve", "duration": 300},
        })

    if action in ("update", "patch"):
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or "")
        data = kwargs.get("data") if isinstance(kwargs.get("data"), dict) else {}
        for k in ("title", "content", "body", "value", "progress", "cpu", "ram", "disk", "status"):
            if k in kwargs and kwargs[k] is not None:
                data[k] = kwargs[k]
        if not scene_id:
            return "update requires scene_id."
        return canvas_command("update", scene_id=scene_id, data=data)


    if action in ("resize", "scale"):
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or "")
        if not scene_id:
            return "resize requires scene_id."
        size = kwargs.get("size") if isinstance(kwargs.get("size"), dict) else {}
        if kwargs.get("w") is not None:
            size["w"] = float(kwargs.get("w"))
        if kwargs.get("h") is not None:
            size["h"] = float(kwargs.get("h"))
        if kwargs.get("scale") is not None:
            try:
                sc = float(kwargs.get("scale"))
                size["w"] = sc
                size["h"] = sc
            except Exception:
                pass
        # named sizes
        named = str(kwargs.get("named") or kwargs.get("how") or "").lower()
        named_map = {
            "small": {"w": 0.28, "h": 0.28},
            "medium": {"w": 0.4, "h": 0.4},
            "large": {"w": 0.55, "h": 0.55},
            "full": {"w": 0.9, "h": 0.85},
        }
        if named in named_map:
            size = {**named_map[named], **size}
        if not size:
            return "Provide size w/h (0-1) or named: small, medium, large, full."
        return canvas_command("update", scene_id=scene_id, size=size)

    if action in ("save_layout", "save_display", "save"):
        name = str(kwargs.get("name") or kwargs.get("title") or "default").strip() or "default"
        # Frontend listens for this and writes localStorage; we just signal
        try:
            from core.web_bridge import broadcast_sync
            broadcast_sync({
                "channel": "display",
                "type": "display_cmd",
                "action": "save_layout",
                "name": name,
            })
            return f"Saving current canvas layout as '{name}'."
        except Exception as exc:
            return f"Could not save layout: {exc}"

    if action in ("load_layout", "load_display", "load", "import_layout"):
        name = str(kwargs.get("name") or kwargs.get("title") or "default").strip() or "default"
        try:
            from core.web_bridge import broadcast_sync
            broadcast_sync({
                "channel": "display",
                "type": "display_cmd",
                "action": "load_layout",
                "name": name,
            })
            return f"Loading canvas layout '{name}'."
        except Exception as exc:
            return f"Could not load layout: {exc}"

    if action in ("list_layouts",):
        try:
            from core.web_bridge import broadcast_sync
            broadcast_sync({
                "channel": "display",
                "type": "display_cmd",
                "action": "list_layouts",
            })
            return "Requested layout list from the display."
        except Exception as exc:
            return str(exc)

    if action in ("move", "reposition", "place"):
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or "")
        if not scene_id:
            return "move requires scene_id (e.g. tasks-main, goals-main)."
        pos = kwargs.get("position") if isinstance(kwargs.get("position"), dict) else {}
        if kwargs.get("x") is not None:
            pos["x"] = kwargs.get("x")
        if kwargs.get("y") is not None:
            pos["y"] = kwargs.get("y")
        # Named slots
        slot = str(kwargs.get("slot") or kwargs.get("where") or kwargs.get("corner") or "").lower()
        slots = {
            "center": {"x": 0.5, "y": 0.5},
            "left": {"x": 0.22, "y": 0.5},
            "right": {"x": 0.78, "y": 0.5},
            "top": {"x": 0.5, "y": 0.22},
            "bottom": {"x": 0.5, "y": 0.78},
            "top-left": {"x": 0.22, "y": 0.22},
            "top-right": {"x": 0.78, "y": 0.22},
            "bottom-left": {"x": 0.22, "y": 0.78},
            "bottom-right": {"x": 0.78, "y": 0.78},
        }
        if slot in slots:
            pos = {**slots[slot], **pos}
        if not pos:
            return "Provide position x/y (0-1) or a slot: center, left, right, top, bottom, top-left, …"
        return canvas_command(
            "update",
            scene_id=scene_id,
            data={"_position": pos},
            **{"position": pos},
        )

    if action in ("remove", "remove_scene"):
        scene_id = str(kwargs.get("scene_id") or kwargs.get("id") or "")
        if not scene_id:
            return "remove requires scene_id."
        return canvas_command("remove", scene_id=scene_id)

    if action in ("system", "system_status", "sys", "metrics", "live_metrics", "cpu_ram"):
        data = {
            "cpu": kwargs.get("cpu"),
            "ram": kwargs.get("ram"),
            "disk": kwargs.get("disk"),
            "battery": kwargs.get("battery"),
            "status": kwargs.get("status") or kwargs.get("message"),
        }
        # Auto-fill live metrics when not provided
        if data["cpu"] is None or data["ram"] is None:
            try:
                import psutil
                if data["cpu"] is None:
                    data["cpu"] = float(psutil.cpu_percent(interval=0.15))
                if data["ram"] is None:
                    data["ram"] = float(psutil.virtual_memory().percent)
                if data["disk"] is None:
                    try:
                        data["disk"] = float(psutil.disk_usage("/").percent)
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("live metrics fill failed: %s", exc)
        return canvas_show({
            "id": str(kwargs.get("scene_id") or "system-status"),
            "type": "system",
            "layer": 1,
            "title": str(kwargs.get("title") or "System — Gama Nexus"),
            "data": data,
            "transition": {"enter": "fade", "duration": 280},
        })

    if action in ("compare", "comparison", "table"):
        # Structured comparison / table for Nexus
        items = kwargs.get("items") or kwargs.get("rows") or kwargs.get("data")
        if isinstance(items, str):
            try:
                import json as _json
                items = _json.loads(items)
            except Exception:
                items = [{"label": "Note", "value": items}]
        if not isinstance(items, list):
            items = []
        columns = kwargs.get("columns") or kwargs.get("headers")
        if isinstance(columns, str):
            try:
                import json as _json
                columns = _json.loads(columns)
            except Exception:
                columns = [c.strip() for c in columns.split(",") if c.strip()]
        scene_type = "table" if action == "table" or columns else "list"
        payload_data: Dict[str, Any] = {
            "title": kwargs.get("title") or "Comparison",
            "items": items,
            "content": kwargs.get("content") or kwargs.get("body") or kwargs.get("text") or "",
        }
        if columns:
            payload_data["columns"] = columns
        if kwargs.get("series"):
            payload_data["series"] = kwargs.get("series")
            scene_type = "chart"
        return canvas_show({
            "id": str(kwargs.get("scene_id") or f"{scene_type}-main"),
            "type": scene_type,
            "layer": int(kwargs.get("layer") or 1),
            "title": str(kwargs.get("title") or "Comparison — Gama Nexus"),
            "data": payload_data,
            "transition": {"enter": "fade", "duration": 280},
        })

    if action in ("information", "info_card", "card"):
        return canvas_show({
            "id": str(kwargs.get("scene_id") or "info-card"),
            "type": "information",
            "layer": int(kwargs.get("layer") or 1),
            "title": str(kwargs.get("title") or "Information"),
            "data": {
                "title": kwargs.get("title") or "Information",
                "content": kwargs.get("content") or kwargs.get("body") or kwargs.get("text") or kwargs.get("message") or "",
                "metadata": kwargs.get("metadata") or kwargs.get("items") or [],
            },
            "transition": {"enter": "fade", "duration": 280},
        })

    if action in ("compose", "dashboard", "today"):
        # Live-safe: children may arrive as children_json string
        if not kwargs.get("children") and kwargs.get("children_json"):
            try:
                import json as _json
                kwargs = dict(kwargs)
                kwargs["children"] = _json.loads(str(kwargs.get("children_json") or "[]"))
            except Exception:
                pass
        # Multi-scene composition helper
        children = kwargs.get("children") or kwargs.get("scenes") or []
        if not isinstance(children, list):
            children = []
        if not children:
            # Build from flags: weather=true, tasks=true, ...
            for key, st in (
                ("weather", "weather"), ("tasks", "tasks"), ("goals", "goals"),
                ("reminders", "reminders"), ("alerts", "alerts"), ("system", "system"),
            ):
                if kwargs.get(key) in (True, "true", "1", "yes", 1):
                    children.append({"id": f"{st}-panel", "type": st, "data": {}})
        # Expand empty native children so SceneRenderer can pull snapshot extras
        return canvas_show({
            "id": str(kwargs.get("scene_id") or "compose-main"),
            "type": "scene",
            "layer": 1,
            "title": str(kwargs.get("title") or "Overview"),
            "children": children,
            "transition": {"enter": "fade", "duration": 320},
        })


    if action in ("reminders", "show_reminders", "list_reminders"):
        kwargs = dict(kwargs)
        kwargs["action"] = "show"
        kwargs["scene_type"] = "reminders"
        kwargs.setdefault("title", "Reminders")
        return display_stage("show", **kwargs)

    if action in ("alerts", "show_alerts", "warnings"):
        kwargs = dict(kwargs)
        kwargs["action"] = "show"
        kwargs["scene_type"] = "alerts"
        kwargs.setdefault("title", "Alerts")
        return display_stage("show", **kwargs)

    if action in ("goals", "show_goals"):
        kwargs = dict(kwargs)
        kwargs["action"] = "show"
        kwargs["scene_type"] = "goals"
        kwargs.setdefault("title", "Goals")
        return display_stage("show", **kwargs)

    if action in ("tasks", "show_tasks", "queue", "task_queue"):
        kwargs = dict(kwargs)
        kwargs["action"] = "show"
        kwargs["scene_type"] = "tasks"
        kwargs.setdefault("title", "Tasks")
        return display_stage("show", **kwargs)

    if action in ("weather", "show_weather"):
        forecast = _truthy(kwargs.get("forecast")) or str(kwargs.get("mode") or "").lower() == "forecast"
        return _show_weather(
            city=str(kwargs.get("city") or kwargs.get("location") or ""),
            forecast=forecast,
        )

    if action in ("forecast", "weather_forecast", "show_forecast"):
        return _show_weather(
            city=str(kwargs.get("city") or kwargs.get("location") or ""),
            forecast=True,
        )

    if action in ("timer", "show_timer"):
        return _show_timer(
            minutes=kwargs.get("minutes", 0),
            seconds=kwargs.get("seconds", 0),
            label=str(kwargs.get("label") or kwargs.get("message") or "Timer"),
            remaining_sec=kwargs.get("remaining_sec"),
            running=kwargs.get("running", True),
        )

    if action in ("clock", "time", "show_clock", "show_time", "current_time"):
        return _show_clock(
            label=str(kwargs.get("label") or kwargs.get("title") or kwargs.get("message") or "TIME"),
            timezone=kwargs.get("timezone") or kwargs.get("tz"),
            hour12=kwargs.get("hour12", True),
            show_seconds=kwargs.get("show_seconds", True),
            show_date=kwargs.get("show_date", True),
        )

    if action in ("confirm", "ask_confirm"):
        return _show_confirm(
            title=str(kwargs.get("title") or "Confirm"),
            body=str(kwargs.get("body") or kwargs.get("message") or kwargs.get("text") or ""),
            action_name=str(kwargs.get("action_name") or kwargs.get("tool") or ""),
            level=str(kwargs.get("level") or "destructive"),
        )

    if action in ("enrollment", "enroll", "voice_enroll", "face_enroll", "mic_calib"):
        return _show_enrollment(
            kind=str(kwargs.get("kind") or "voice"),
            title=str(kwargs.get("title") or "Enrollment"),
            instruction=kwargs.get("instruction") or kwargs.get("message"),
            step=kwargs.get("step"),
            total=kwargs.get("total"),
            progress=kwargs.get("progress"),
            status=kwargs.get("status"),
            recording=kwargs.get("recording"),
        )

    if action in ("info", "card", "message"):
        return _show_info(
            title=str(kwargs.get("title") or "Info"),
            body=str(kwargs.get("body") or kwargs.get("message") or kwargs.get("text") or ""),
            meta=kwargs.get("meta"),
        )

    # Universal freeform write / canvas
    if action in (
        "write", "show", "canvas", "display", "put", "render",
        "show_content", "write_on_display", "hud",
    ):
        return _show_canvas(**kwargs)

    if action == "status":
        mode = _last.get("mode", "orb")
        if mode == "orb" or _last.get("close"):
            return "The display is showing the voice orb (standby)."
        return f"The display is showing {mode}."

    return (
        "Unknown Gama Nexus action. Use: show, clear, weather, forecast, reminders, alerts, "
        "goals, tasks, timer, clock, time, system, metrics, information, compare, table, "
        "custom_svg, update, remove, compose, write, confirm, enrollment, close, status, nexus."
    )


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _show(mode: str, title: Optional[str] = None) -> str:
    ok = _push({"mode": mode, "title": title or mode.title()})
    try:
        from core import web_bridge as wb
        if mode == "reminders":
            wb.push_reminders()
        elif mode == "tasks":
            wb.push_tasks()
        elif mode == "goals":
            wb.push_goals()
    except Exception:
        pass
    if not ok:
        return f"Nexus bridge is offline — could not show {mode}."
    return f"Showing {title or mode} on Gama Nexus, sir."


def _close() -> str:
    global _pending_confirm_id
    _pending_confirm_id = None
    ok = _push({"close": True, "mode": "orb"})
    if not ok:
        return "Nexus bridge is offline — could not close the stage."
    return "Gama Nexus closed. Back to the voice orb, sir."


def _show_weather(city: str = "", forecast: bool = False) -> str:
    try:
        from actions.weather_report import weather_card
        card = weather_card(city, forecast=forecast)
    except Exception as exc:
        log.warning("weather_card error: %s", exc)
        card = {
            "error": str(exc),
            "location": city or "Unknown",
            "hours": [],
            "days": [],
            "mode": "forecast" if forecast else "current",
        }

    ok = _push({"mode": "weather", "weather": card, "title": card.get("location") or "Weather"})
    if not ok:
        return "Display bridge is offline — weather not shown."
    if card.get("error") and card.get("temp_c") is None:
        return f"Weather unavailable: {card.get('error')}"
    loc = card.get("location") or "your area"
    if forecast:
        return f"3-day forecast for {loc} is on Gama Nexus, sir."
    temp = card.get("temp_c")
    cond = card.get("condition") or ""
    if temp is not None:
        return f"Weather for {loc} is on Gama Nexus — {cond}, {temp}°C."
    return f"Weather for {loc} is on Gama Nexus."


def _normalize_blocks(kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build canvas blocks from flexible tool args."""
    blocks = kwargs.get("blocks")
    if isinstance(blocks, list) and blocks:
        out: List[Dict[str, Any]] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            btype = str(b.get("type") or "text").lower()
            if btype not in ("text", "image", "link", "badge", "divider"):
                btype = "text"
            block: Dict[str, Any] = {"type": btype}
            for k in ("content", "title", "align", "size", "weight", "color", "width", "id"):
                if b.get(k) is not None:
                    block[k] = b[k]
            out.append(block)
        if out:
            return out

    # Simple fields
    text = str(
        kwargs.get("text")
        or kwargs.get("body")
        or kwargs.get("message")
        or kwargs.get("content")
        or ""
    ).strip()
    image = str(kwargs.get("image") or kwargs.get("image_url") or "").strip()
    link = str(kwargs.get("link") or kwargs.get("url") or "").strip()
    link_title = str(kwargs.get("link_title") or "").strip()

    out = []
    if image:
        out.append({
            "type": "image",
            "content": image,
            "title": kwargs.get("image_title") or kwargs.get("title") or "",
            "size": kwargs.get("size") or "md",
            "align": kwargs.get("align") or "center",
        })
    if text:
        out.append({
            "type": "text",
            "content": text,
            "align": kwargs.get("align") or "center",
            "size": kwargs.get("size") or "md",
            "weight": kwargs.get("weight") or "normal",
            "color": kwargs.get("color"),
        })
    if link:
        out.append({
            "type": "link",
            "content": link,
            "title": link_title or link,
            "align": kwargs.get("align") or "center",
            "size": kwargs.get("size") or "md",
        })
    return out


def _show_canvas(**kwargs) -> str:
    blocks = _normalize_blocks(kwargs)
    if not blocks:
        return (
            "Nothing to show on Gama Nexus. Provide text, image, link, or blocks."
        )
    title = kwargs.get("title")
    align = str(kwargs.get("align") or "center").lower()
    if align not in ("left", "center", "right"):
        align = "center"
    canvas = {
        "title": title,
        "align": align,
        "blocks": blocks,
    }
    ok = _push({
        "mode": "canvas",
        "canvas": canvas,
        "title": title or "Display",
    })
    if not ok:
        return "Display bridge is offline — content not shown."
    return "Content is on Gama Nexus, sir."


def _show_timer(
    minutes: Any = 0,
    seconds: Any = 0,
    label: str = "Timer",
    remaining_sec: Any = None,
    running: Any = True,
) -> str:
    try:
        m = int(minutes or 0)
    except Exception:
        m = 0
    try:
        s = int(seconds or 0)
    except Exception:
        s = 0
    if remaining_sec is not None:
        try:
            total = int(remaining_sec)
        except Exception:
            total = m * 60 + s
    else:
        total = m * 60 + s
    if total <= 0:
        return "Please specify a duration for the timer, sir."

    is_running = _truthy(running) if not isinstance(running, bool) else running
    ends_at = int(time.time() * 1000) + total * 1000 if is_running else None
    ok = _push({
        "mode": "timer",
        "timer": {
            "id": f"tm_{int(time.time() * 1000)}",
            "label": label or "Timer",
            "remainingSec": total,
            "totalSec": total,
            "endsAt": ends_at,
            "running": is_running,
        },
        "title": label or "Timer",
    })
    if not ok:
        return "Display bridge is offline."
    return f"Timer of {total} seconds is on Gama Nexus, sir."


def _show_clock(
    label: str = "TIME",
    timezone: Any = None,
    hour12: Any = True,
    show_seconds: Any = True,
    show_date: Any = True,
) -> str:
    """Push a live clock scene that updates every second on the canvas."""
    is_hour12 = _truthy(hour12) if not isinstance(hour12, bool) else hour12
    show_sec = _truthy(show_seconds) if not isinstance(show_seconds, bool) else show_seconds
    show_dt = _truthy(show_date) if not isinstance(show_date, bool) else show_date
    tz = str(timezone).strip() if timezone else None
    ok = _push({
        "mode": "clock",
        "clock": {
            "id": f"clk_{int(time.time() * 1000)}",
            "label": label or "TIME",
            "hour12": is_hour12,
            "showSeconds": show_sec,
            "showDate": show_dt,
            "timezone": tz,
        },
        "title": label or "TIME",
    })
    if not ok:
        return "Display bridge is offline."
    return "Live clock is on Gama Nexus, sir."


def _show_confirm(
    title: str,
    body: str,
    action_name: str = "",
    level: str = "destructive",
) -> str:
    global _pending_confirm_id
    cid = f"cf_{int(time.time() * 1000)}"
    _pending_confirm_id = cid
    lvl = level if level in ("destructive", "sensitive", "info") else "destructive"
    ok = _push({
        "mode": "confirm",
        "confirm": {
            "id": cid,
            "title": title or "Confirm",
            "body": body or "Are you sure?",
            "action": action_name,
            "level": lvl,
        },
        "title": title or "Confirm",
    })
    if not ok:
        return "Display bridge is offline — please confirm verbally."
    return "Confirmation is on Gama Nexus, sir. Say yes or no."


def _show_enrollment(
    kind: str = "voice",
    title: str = "Enrollment",
    instruction: Any = None,
    step: Any = None,
    total: Any = None,
    progress: Any = None,
    status: Any = None,
    recording: Any = None,
) -> str:
    enroll: Dict[str, Any] = {
        "kind": kind if kind in ("voice", "face", "mic") else "voice",
        "title": title or "Enrollment",
    }
    if instruction is not None:
        enroll["instruction"] = str(instruction)
    if step is not None:
        try:
            enroll["step"] = int(step)
        except Exception:
            pass
    if total is not None:
        try:
            enroll["total"] = int(total)
        except Exception:
            pass
    if progress is not None:
        try:
            enroll["progress"] = float(progress)
        except Exception:
            pass
    if status is not None:
        enroll["status"] = str(status)
    if recording is not None:
        enroll["recording"] = _truthy(recording)
    ok = _push({"mode": "enrollment", "enrollment": enroll, "title": title})
    if not ok:
        return "Display bridge is offline."
    return f"{title} is on Gama Nexus, sir."


def _show_info(title: str, body: str, meta: Optional[str] = None) -> str:
    if not body:
        return "Nothing to show, sir."
    info: Dict[str, Any] = {"title": title or "Info", "body": body}
    if meta:
        info["meta"] = meta
    ok = _push({"mode": "info", "info": info, "title": title or "Info"})
    if not ok:
        return "Display bridge is offline."
    return f"Showing {title or 'info'} on Gama Nexus, sir."


def project_timer_on_display(
    total_seconds: int,
    label: str = "Timer",
    running: bool = True,
) -> None:
    if total_seconds <= 0:
        return
    try:
        _show_timer(remaining_sec=total_seconds, label=label, running=running)
    except Exception as exc:
        log.debug("project_timer_on_display: %s", exc)


def ask_confirm_on_display(
    title: str,
    body: str,
    *,
    action_name: str = "",
    level: str = "destructive",
) -> str:
    return _show_confirm(title, body, action_name=action_name, level=level)


def write_on_display(
    text: str = "",
    *,
    title: str = "",
    image: str = "",
    link: str = "",
    align: str = "center",
    size: str = "md",
    blocks: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Convenience for any code path to push freeform content."""
    return _show_canvas(
        text=text,
        title=title or None,
        image=image,
        link=link,
        align=align,
        size=size,
        blocks=blocks,
    )


def close_display_stage() -> str:
    return _close()


def show_enrollment_on_display(**kwargs) -> str:
    return _show_enrollment(**kwargs)




# ── Gama Canvas display protocol (structured, LLM-safe) ─────────────────

def canvas_command(action: str, scene: dict | None = None, **kwargs) -> str:
    """Push a structured display protocol command to the React Gama Nexus.

    Single broadcast only — do NOT also call push_display (that dual-wrote a
    second scene and produced duplicate cards on the HUD).
    """
    action_s = str(action or "show").lower().strip()
    payload: Dict[str, Any] = {
        "channel": "display",
        "action": action_s,
        "type": "display_cmd",
    }
    if scene is not None:
        payload["scene"] = scene
    for k in ("scene_id", "data", "animation", "stack", "layer", "position", "size"):
        if k in kwargs and kwargs[k] is not None:
            payload[k] = kwargs[k]
            if scene is not None and k in ("position", "size") and isinstance(scene, dict):
                scene[k] = kwargs[k]
    try:
        from core.web_bridge import broadcast_sync
        broadcast_sync(payload)
        log.info(
            "Nexus protocol: action=%s type=%s id=%s",
            action_s,
            (scene or {}).get("type") if isinstance(scene, dict) else None,
            (scene or {}).get("id") if isinstance(scene, dict) else None,
        )
        # Keep snapshot in sync for other consumers (no second WebSocket scene)
        try:
            from core import web_bridge as wb
            if action_s == "clear":
                wb._snapshot["display"] = {"close": True, "mode": "orb"}
            elif scene and isinstance(scene, dict):
                wb._snapshot["display"] = {
                    "mode": "canvas",
                    "title": scene.get("title"),
                    "canvas": scene,
                }
        except Exception:
            pass
        return f"Nexus {action_s} sent."
    except Exception as exc:
        log.debug("canvas_command failed: %s", exc)
        try:
            if action_s in ("clear", "remove") and not scene:
                return _close()
            if scene:
                return _push({"mode": "canvas", "canvas": scene, "title": scene.get("title")})
        except Exception:
            pass
        return "Display bridge is offline."


def canvas_show(scene: dict) -> str:
    """Validate scene with Pydantic when available, then push to React."""
    if not isinstance(scene, dict):
        return "Invalid scene payload."
    try:
        from actions.visual_schema import validate_scene
        result = validate_scene(scene)
        if result.ok and result.scene:
            scene = result.scene
        else:
            log.warning("canvas_show validation soft-fail: %s", result.errors)
            # Soft-fail: still attempt to show after light sanitization
            scene = dict(scene)
            scene.setdefault("id", "scene-main")
            scene.setdefault("type", "information")
            scene["layer"] = max(0, min(4, int(scene.get("layer") or 1)))
    except Exception as exc:
        log.debug("canvas_show pydantic unavailable: %s", exc)
    return canvas_command("show", scene)


def canvas_clear(layer: int | None = None) -> str:
    return canvas_command("clear", layer=layer)


def canvas_custom_svg(
    scene_id: str,
    elements: list,
    *,
    viewBox: str = "0 0 1000 600",
    title: str = "",
    layer: int = 1,
) -> str:
    """Show a sanitized custom SVG scene on the canvas (Pydantic-validated)."""
    safe_elements = elements if isinstance(elements, list) else []
    try:
        from actions.visual_schema import validate_svg_elements
        safe_elements = validate_svg_elements(safe_elements)
    except Exception as exc:
        log.debug("canvas_custom_svg validate: %s", exc)
        # Manual fallback: drop non-dict items
        safe_elements = [e for e in safe_elements if isinstance(e, dict)][:80]
    return canvas_show({
        "id": scene_id or "custom-svg",
        "type": "custom_svg",
        "layer": max(0, min(4, int(layer or 1))),
        "title": title,
        "data": {"viewBox": viewBox or "0 0 1000 600", "elements": safe_elements},
        "transition": {"enter": "scan", "exit": "dissolve", "duration": 400},
    })

__all__ = [
    "display_stage",
    "project_timer_on_display",
    "ask_confirm_on_display",
    "write_on_display",
    "close_display_stage",
    "show_enrollment_on_display",
    "canvas_command",
    "canvas_show",
    "canvas_clear",
    "canvas_custom_svg",
]
