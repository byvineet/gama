"""
core/web_bridge.py — Real-time WebSocket bridge for the React HUD
=================================================================
Uses the pure ``websockets`` library (not FastAPI/Starlette WebSocket) so
browser handshakes are never rejected with HTTP 403.

  WS   ws://127.0.0.1:8765/ws
  HTTP http://127.0.0.1:8765/api/health
       http://127.0.0.1:8765/api/snapshot

Start from main.py::

    from core.web_bridge import start_web_bridge
    start_web_bridge(assistant)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("gama.web_bridge")

HOST = "127.0.0.1"
PORT = 8765

_clients: Set[Any] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_started = False
_assistant_ref: Any = None

_snapshot: Dict[str, Any] = {
    "primary": "IDLE",
    "activity": "NONE",
    "mood": "NORMAL",
    "status_text": "Standby",
    "speaking": False,
    "amplitude": 0.0,
    "awake": False,
    "owner_name": "Sir",
    "tasks": [],
    "reminders": [],
    "alarms": [],
    "timers": [],
    "goals": [],
    "alerts": [],
    "log": [],
    "gesture_enabled": False,
    "gesture_name": "",
    "gesture_frame": "",  # base64 JPEG (data URL payload without prefix)
    "camera_vision": False,
    "cpu": 0.0,
    "ram": 0.0,
    "disk": 0.0,
    "battery": None,  # percent or None
    "battery_charging": False,
    "wifi": True,
    "mic_muted": False,
    "ts": time.time(),
}
_log_buffer: List[Dict[str, Any]] = []
_LOG_MAX = 80
_amp_last_push = 0.0
_AMP_MIN_INTERVAL = 0.02  # ~50 fps — tighter speech sync
_gesture_last_push = 0.0
_GESTURE_MIN_INTERVAL = 0.033  # ~30 fps for smooth camera HUD


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_json(obj: Any) -> str:
    return json.dumps(obj, default=str)


async def _broadcast(envelope: Dict[str, Any]) -> None:
    if not _clients:
        return
    raw = _safe_json(envelope)
    dead = []
    for ws in list(_clients):
        try:
            await ws.send(raw)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def broadcast_sync(envelope: Dict[str, Any]) -> None:
    global _loop
    if _loop is None or not _loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(envelope), _loop)
    except Exception as exc:
        log.debug("broadcast_sync failed: %s", exc)


def push_state(
    primary: str = "",
    activity: str = "",
    mood: str = "",
    status_text: str = "",
    speaking: Optional[bool] = None,
    awake: Optional[bool] = None,
) -> None:
    if primary:
        _snapshot["primary"] = primary
    if activity:
        _snapshot["activity"] = activity
    if mood:
        _snapshot["mood"] = mood
    if status_text:
        _snapshot["status_text"] = status_text
    if speaking is not None:
        _snapshot["speaking"] = bool(speaking)
    if awake is not None:
        _snapshot["awake"] = bool(awake)
    _snapshot["ts"] = time.time()
    broadcast_sync({
        "type": "state",
        "data": {
            "primary": _snapshot["primary"],
            "activity": _snapshot["activity"],
            "mood": _snapshot["mood"],
            "status_text": _snapshot["status_text"],
            "speaking": _snapshot["speaking"],
            "awake": _snapshot["awake"],
            "ts": _snapshot["ts"],
        },
    })


def push_amplitude(level: float) -> None:
    """Push live mic/TTS level to the HUD (throttled for lightweight UI)."""
    global _amp_last_push
    level = max(0.0, min(1.0, float(level)))
    now = time.time()
    # Always store latest; only broadcast at ~25 Hz
    _snapshot["amplitude"] = level
    if now - _amp_last_push < _AMP_MIN_INTERVAL:
        return
    _amp_last_push = now
    broadcast_sync({"type": "amplitude", "data": {"level": level}})




def push_camera_vision(enabled: bool) -> None:
    """Tell the React HUD to open/close the browser webcam (getUserMedia).

    This is the fast, smooth camera preview path. Backend OpenCV frames are
    only sent to Gemini Live — never to the display canvas.
    """
    _snapshot["camera_vision"] = bool(enabled)
    broadcast_sync({
        "type": "camera_vision",
        "data": {"enabled": bool(enabled)},
    })
    # Also include in next full state push for reconnect clients
    try:
        push_state()  # no-op fields keep previous; we also send dedicated event
    except Exception:
        pass


def push_gesture_state(enabled: bool, gesture_name: str = "") -> None:
    """Notify HUD that gesture mode turned on/off."""
    _snapshot["gesture_enabled"] = bool(enabled)
    if not enabled:
        _snapshot["gesture_frame"] = ""
        _snapshot["gesture_name"] = ""
    elif gesture_name:
        _snapshot["gesture_name"] = gesture_name
    _snapshot["ts"] = time.time()
    broadcast_sync({
        "type": "gesture",
        "data": {
            "enabled": bool(enabled),
            "name": _snapshot.get("gesture_name", ""),
            "frame": _snapshot.get("gesture_frame", "") if enabled else "",
        },
    })


def push_gesture_frame(frame_bgr, gesture_name: str = "") -> None:
    """Push an annotated camera frame (OpenCV BGR ndarray) to the React HUD.

    Throttled to ~8 fps. Encodes JPEG quality 55 for small payloads.
    Safe to call from the GestureEngine worker thread.
    """
    global _gesture_last_push
    if frame_bgr is None:
        return
    now = time.time()
    if now - _gesture_last_push < _GESTURE_MIN_INTERVAL:
        # Still update name if present so label stays fresh
        if gesture_name:
            _snapshot["gesture_name"] = gesture_name
        return
    try:
        import cv2
        import base64
        import numpy as np

        if not isinstance(frame_bgr, np.ndarray) or frame_bgr.size == 0:
            return
        # Downscale for HUD bandwidth
        h, w = frame_bgr.shape[:2]
        max_w = 420
        if w > max_w:
            scale = max_w / float(w)
            frame_bgr = cv2.resize(
                frame_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
            )
        ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
        if not ok:
            return
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        _gesture_last_push = now
        _snapshot["gesture_enabled"] = True
        _snapshot["gesture_frame"] = b64
        if gesture_name:
            _snapshot["gesture_name"] = gesture_name
        broadcast_sync({
            "type": "gesture",
            "data": {
                "enabled": True,
                "name": _snapshot.get("gesture_name", ""),
                "frame": b64,
            },
        })
    except Exception as exc:
        log.debug("push_gesture_frame failed: %s", exc)


def push_log(role: str, text: str) -> None:
    entry = {"role": role, "text": text, "ts": time.time(), "time": _now_iso()}
    _log_buffer.append(entry)
    if len(_log_buffer) > _LOG_MAX:
        del _log_buffer[: len(_log_buffer) - _LOG_MAX]
    _snapshot["log"] = list(_log_buffer)
    broadcast_sync({"type": "log", "data": entry})


def push_tasks(tasks: Optional[List[Dict[str, Any]]] = None) -> None:
    if tasks is None:
        tasks = _collect_tasks()
    _snapshot["tasks"] = tasks
    broadcast_sync({"type": "tasks", "data": tasks})


def push_reminders() -> None:
    data = _collect_reminders()
    _snapshot.update(data)
    broadcast_sync({"type": "reminders", "data": data})


def _collect_tasks() -> List[Dict[str, Any]]:
    try:
        from core.task_queue import task_queue
        items = []
        for t in getattr(task_queue, "_tasks", {}).values():
            if hasattr(t, "as_dict"):
                items.append(t.as_dict())
            else:
                items.append({
                    "task_id": getattr(t, "task_id", ""),
                    "name": getattr(t, "name", ""),
                    "status": getattr(t, "status", ""),
                    "current_step": getattr(t, "current_step", ""),
                    "progress_pct": getattr(t, "progress_pct", None),
                })
        order = {
            "RUNNING": 0, "QUEUED": 1, "PAUSED": 2,
            "COMPLETED": 3, "FAILED": 4, "CANCELLED": 5,
        }
        items.sort(key=lambda x: order.get(str(x.get("status", "")), 9))
        return items[:40]
    except Exception as exc:
        log.debug("_collect_tasks: %s", exc)
        return list(_snapshot.get("tasks") or [])


def _fmt_when(val) -> str:
    """Serialize datetime / string reminder times for the HUD."""
    if val is None:
        return ""
    try:
        from datetime import datetime
        if isinstance(val, datetime):
            return val.strftime("%I:%M %p")
    except Exception:
        pass
    return str(val)


def _collect_reminders() -> Dict[str, Any]:
    # Real fields in actions/reminder.py: remind_at / alarm_at / ends_at
    out: Dict[str, Any] = {"reminders": [], "alarms": [], "timers": []}
    try:
        from actions import reminder as rem
        with rem._lock:
            out["reminders"] = [
                {
                    "id": r.get("id"),
                    "message": r.get("message") or r.get("text") or "",
                    "when": _fmt_when(r.get("remind_at") or r.get("when") or r.get("fire_at")),
                    "done": bool(r.get("done")),
                    "kind": "reminder",
                }
                for r in rem._reminders
                if not r.get("done")
            ]
            out["alarms"] = [
                {
                    "id": a.get("id"),
                    "message": a.get("message") or a.get("label") or "Alarm",
                    "when": _fmt_when(a.get("alarm_at") or a.get("when") or a.get("fire_at")),
                    "done": bool(a.get("done")),
                    "kind": "alarm",
                }
                for a in rem._alarms
                if not a.get("done")
            ]
            out["timers"] = [
                {
                    "id": t.get("id"),
                    "message": t.get("message") or t.get("label") or "Timer",
                    "when": _fmt_when(
                        t.get("ends_at") or t.get("when") or t.get("fire_at") or t.get("timer_at")
                    ),
                    "done": bool(t.get("done")),
                    "kind": "timer",
                }
                for t in rem._timers
                if not t.get("done")
            ]
    except Exception as exc:
        log.debug("_collect_reminders: %s", exc)
    return out


def _collect_goals() -> List[Dict[str, Any]]:
    try:
        import sqlite3
        from pathlib import Path as _P
        db = _P.home() / ".gama" / "goals.db"
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, description, status, progress_pct, deadline FROM goals "
            "WHERE status IN ('active','paused') ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()
        return [
            {
                "id": r["id"],
                "title": r["title"] or "",
                "description": r["description"] or "",
                "status": r["status"],
                "progress_pct": r["progress_pct"] or 0,
                "deadline": r["deadline"] or "",
            }
            for r in rows
        ]
    except Exception as exc:
        log.debug("_collect_goals: %s", exc)
        return list(_snapshot.get("goals") or [])



def _collect_system_stats() -> Dict[str, Any]:
    """Lightweight CPU/RAM/disk/battery/connectivity sample for the HUD."""
    out: Dict[str, Any] = {
        "cpu": 0.0, "ram": 0.0, "disk": 0.0,
        "battery": None, "battery_charging": False, "wifi": True,
    "mic_muted": False,
    }
    try:
        import psutil
        out["cpu"] = float(psutil.cpu_percent(interval=None))
        out["ram"] = float(psutil.virtual_memory().percent)
        try:
            out["disk"] = float(psutil.disk_usage("/").percent)
        except Exception:
            try:
                out["disk"] = float(psutil.disk_usage("C:\\").percent)
            except Exception:
                pass
        bat = psutil.sensors_battery()
        if bat is not None:
            out["battery"] = float(bat.percent)
            out["battery_charging"] = bool(bat.power_plugged)
        # Rough online check — non-blocking
        try:
            import socket
            socket.create_connection(("1.1.1.1", 53), timeout=0.15).close()
            out["wifi"] = True
        except Exception:
            out["wifi"] = False
    except Exception as exc:
        log.debug("_collect_system_stats: %s", exc)
    return out


def push_system_stats() -> None:
    data = _collect_system_stats()
    _snapshot.update(data)
    _snapshot["ts"] = time.time()
    broadcast_sync({"type": "sysstats", "data": data})


def refresh_all() -> None:
    _snapshot["tasks"] = _collect_tasks()
    rem = _collect_reminders()
    _snapshot["reminders"] = rem.get("reminders", [])
    _snapshot["alarms"] = rem.get("alarms", [])
    _snapshot["timers"] = rem.get("timers", [])
    _snapshot["goals"] = _collect_goals()
    _snapshot["log"] = list(_log_buffer)
    _snapshot.update(_collect_system_stats())
    _snapshot["ts"] = time.time()
    broadcast_sync({"type": "snapshot", "data": dict(_snapshot)})


def push_goals() -> None:
    goals = _collect_goals()
    _snapshot["goals"] = goals
    broadcast_sync({"type": "goals", "data": goals})


def push_alert(title: str, message: str, level: str = "warning") -> None:
    entry = {
        "id": f"a-{int(time.time()*1000)}",
        "title": title,
        "message": message,
        "level": level,
        "ts": time.time(),
        "time": _now_iso(),
    }
    alerts = list(_snapshot.get("alerts") or [])
    alerts.insert(0, entry)
    _snapshot["alerts"] = alerts[:20]
    broadcast_sync({"type": "alert", "data": entry})
    broadcast_sync({"type": "alerts", "data": _snapshot["alerts"]})


def push_display(payload: Dict[str, Any]) -> None:
    """
    Drive the central presence stage (voice-orb area) on the React HUD.

    Examples
    --------
    push_display({"mode": "hologram", "hologram": {"subject": "mars"}, "save": True})
    push_display({"mode": "reminders"})
    push_display({"mode": "timer", "timer": {"label": "Focus", "remainingSec": 300, "running": True}})
    push_display({"mode": "info", "info": {"title": "Status", "body": "All systems nominal"}})
    push_display({"close": True})  # or {"mode": "orb"}
    push_display({"load": "mars"})  # reload saved blueprint from browser localStorage
    """
    if not isinstance(payload, dict):
        return
    _snapshot["display"] = payload
    broadcast_sync({"type": "display", "data": payload})

    # Dual-write: also emit Gama Canvas display protocol so the new
    # visual workspace can render without depending on legacy mode.
    try:
        mode = str(payload.get("mode") or "")
        if payload.get("close") or mode == "orb":
            broadcast_sync({"channel": "display", "action": "clear"})
        elif mode:
            type_map = {
                "weather": "weather", "reminders": "reminders", "alerts": "alerts",
                "goals": "goals", "tasks": "tasks", "timer": "timer",
                "clock": "clock", "time": "clock",
                "confirm": "confirm", "info": "information", "enrollment": "information",
                "canvas": "dsl",
            }
            st = type_map.get(mode)
            if st:
                data = {}
                for k in ("weather", "timer", "clock", "confirm", "info", "enrollment", "canvas"):
                    if payload.get(k):
                        data = payload[k] if isinstance(payload[k], dict) else {"value": payload[k]}
                        break
                if mode == "enrollment" and isinstance(payload.get("enrollment"), dict):
                    e = payload["enrollment"]
                    data = {
                        "title": e.get("title") or "Enrollment",
                        "content": e.get("instruction") or e.get("status") or "",
                        "metadata": [
                            x for x in [
                                f"Step {e['step']}/{e['total']}" if e.get("step") is not None and e.get("total") is not None else None,
                                "Recording…" if e.get("recording") else None,
                            ] if x
                        ],
                    }
                broadcast_sync({
                    "channel": "display",
                    "action": "show",
                    "scene": {
                        "id": f"legacy-{mode}",
                        "type": st,
                        "layer": 3 if mode in ("confirm", "alerts") else 1,
                        "title": payload.get("title") or mode,
                        "data": data,
                        "transition": {"enter": "fade", "exit": "dissolve", "duration": 280},
                    },
                })
    except Exception as exc:
        log.debug("push_display canvas dual-write failed: %s", exc)



def show_hologram(subject: str, *, save: bool = False, title: str | None = None,
                  shape: str | None = None, notes: str | None = None) -> None:
    """Convenience: project a lightweight holographic blueprint of *subject*."""
    holo: Dict[str, Any] = {"subject": subject}
    if title:
        holo["title"] = title
    if shape:
        holo["shape"] = shape
    if notes:
        holo["notes"] = notes
    push_display({"mode": "hologram", "hologram": holo, "save": bool(save)})


def close_display() -> None:
    """Return the presence stage to the default voice orb."""
    push_display({"close": True})


def _wire_event_bus() -> None:
    try:
        from state_engine.event_bus import event_bus

        def _on_any(evt) -> None:
            name = getattr(evt, "name", "") or ""
            if name in (
                "TaskSubmitted", "TaskStarted", "TaskCompleted", "TaskFailed",
                "TaskCancelled", "TaskPaused", "TaskResumed", "TaskProgressChanged",
            ):
                push_tasks()
            elif name in ("SpeechStarted", "SpeechCompleted", "SpeechInterrupted"):
                push_state(speaking=(name == "SpeechStarted"))
            elif name in ("WakeWordDetected", "SleepEntered", "SleepExited"):
                push_state(awake=(name != "SleepEntered"))

        event_bus.subscribe("*", _on_any)
        log.info("web_bridge: subscribed to event_bus")
    except Exception as exc:
        log.debug("event_bus wire failed: %s", exc)

    try:
        from state_engine import state

        def _on_state(snap) -> None:
            push_state(
                primary=getattr(snap.primary, "value", str(snap.primary)),
                activity=getattr(snap.activity, "value", str(snap.activity)),
                mood=getattr(snap.mood, "value", str(snap.mood)),
                status_text=getattr(snap, "status_text", "") or "",
            )

        state.subscribe(_on_state)
        log.info("web_bridge: subscribed to StateManager")
    except Exception as exc:
        log.debug("state subscribe failed: %s", exc)

    try:
        from actions import reminder as rem
        rem.set_ui_refresh_callback(lambda: (push_reminders(), push_tasks()))
        log.info("web_bridge: reminder UI refresh callback set")
    except Exception as exc:
        log.debug("reminder callback failed: %s", exc)



def _handle_ui_chat(text: str) -> None:
    """Receive a typed message from the React HUD and inject it into Gama."""
    text = (text or "").strip()
    if not text:
        return
    # Echo to conversation log immediately
    push_log("user", text)
    log.info("[ui-chat] %s", text[:200])

    asst = _assistant_ref
    if asst is None:
        push_log("system", "Gama is not connected yet — is the backend running?")
        return

    # UI typing always implies the user is present: force awake so sleep-gate
    # in send_text does not silently drop the message. Also clear the
    # post-reconnect quiet window — that gate is for system nudges, not
    # intentional typed commands (page-refresh regression).
    try:
        asst._awake = True
        try:
            asst._session_quiet_until = 0.0
        except Exception:
            pass
        if hasattr(asst, "_wake_gama"):
            asst._wake_gama()
        if hasattr(asst, "_session_mgr"):
            try:
                asst._session_mgr.start_session(reason="ui chat")
            except Exception:
                pass
        try:
            asst.ui.set_state(asst._awake_state() if hasattr(asst, "_awake_state") else "ACTIVE")
        except Exception:
            pass
    except Exception as exc:
        log.debug("ui chat wake failed: %s", exc)

    # Preferred path: full send_text (uses _send_user_text + retry)
    try:
        if hasattr(asst, "send_text"):
            asst.send_text(text)
            return
    except Exception as exc:
        log.warning("ui chat send_text failed: %s", exc)

    # Fallback: direct Live session inject via user-text path when available
    loop = getattr(asst, "_loop", None)
    if loop is None or not getattr(loop, "is_running", lambda: False)():
        push_log("system", "Gama session is not ready yet — wait for Gemini Live.")
        return

    async def _inject() -> None:
        try:
            if hasattr(asst, "_send_user_text"):
                await asst._send_user_text(text)
                return
            session = getattr(asst, "session", None)
            if session is not None:
                await session.send_realtime_input(text=text)
            else:
                push_log("gama", "Live session offline — reconnecting…")
        except Exception as exc:
            log.warning("ui chat inject failed: %s", exc)
            push_log("system", f"Could not deliver message: {exc}")

    try:
        asyncio.run_coroutine_threadsafe(_inject(), loop)
    except Exception as exc:
        log.warning("ui chat schedule failed: %s", exc)


async def _ws_handler(websocket) -> None:
    try:
        req_path = websocket.request.path  # websockets >= 12
    except Exception:
        req_path = getattr(websocket, "path", "/ws")

    if req_path not in ("/ws", "/"):
        await websocket.close(code=1008, reason="use path /ws")
        return

    _clients.add(websocket)
    log.info("web_bridge: client connected (%s total) path=%s", len(_clients), req_path)
    try:
        refresh_all()
        await websocket.send(_safe_json({"type": "snapshot", "data": dict(_snapshot)}))
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            cmd = (payload.get("type") or payload.get("cmd") or "").lower()
            if cmd in ("ping", "heartbeat"):
                await websocket.send(_safe_json({"type": "pong", "ts": time.time()}))
            elif cmd in ("refresh", "snapshot"):
                refresh_all()
                await websocket.send(
                    _safe_json({"type": "snapshot", "data": dict(_snapshot)})
                )

            elif cmd in ("mute", "set_mute", "mic_mute"):
                muted = payload.get("muted")
                if muted is None:
                    muted = payload.get("value")
                if muted is None:
                    muted = payload.get("mute")
                muted = bool(muted)
                _snapshot["mic_muted"] = muted
                _snapshot["ts"] = time.time()
                # Best-effort: notify assistant if it exposes a mute hook
                try:
                    asst = _assistant_ref
                    if asst is not None:
                        ui = getattr(asst, "ui", None) or getattr(asst, "_ui", None)
                        if ui is not None and hasattr(ui, "set_muted"):
                            ui.set_muted(muted)
                        elif hasattr(asst, "set_mic_muted"):
                            asst.set_mic_muted(muted)
                except Exception as exc:
                    log.debug("mute hook failed: %s", exc)
                await websocket.send(
                    _safe_json({"type": "state", "data": {"mic_muted": muted}})
                )
                log.info("web_bridge: mic_muted=%s", muted)

            elif cmd in ("chat", "user_text", "message", "send"):
                text = (
                    payload.get("text")
                    or payload.get("message")
                    or payload.get("content")
                    or ""
                )
                text = str(text).strip()
                if text:
                    _handle_ui_chat(text)

            elif cmd in ("gesture_action", "gesture"):
                # Browser MediaPipe (same path as D2) → OS action
                action = (
                    payload.get("action")
                    or payload.get("name")
                    or (payload.get("data") or {}).get("action")
                    or ""
                )
                action = str(action).strip().lower()
                if action:
                    try:
                        result = apply_nexus_action(action)
                        log.debug("gesture_action %s → %s", action, result)
                    except Exception as exc:
                        log.debug("gesture_action failed: %s", exc)
    except Exception as exc:
        log.debug("ws client error: %s", exc)
    finally:
        _clients.discard(websocket)
        log.info("web_bridge: client disconnected (%s total)", len(_clients))


def _process_request(connection, request):
    """HTTP side-channel on the same port. Return None to continue WS upgrade."""
    path = getattr(request, "path", "/") or "/"
    method = getattr(request, "method", "GET") or "GET"

    headers = getattr(request, "headers", None)
    upgrade = ""
    if headers is not None:
        try:
            upgrade = headers.get("Upgrade") or headers.get("upgrade") or ""
        except Exception:
            upgrade = ""
    if str(upgrade).lower() == "websocket":
        return None  # complete WebSocket handshake — never 403

    if method == "OPTIONS":
        return connection.respond(200, b"")

    if path in ("/", "/api/health"):
        body = _safe_json({
            "ok": True,
            "clients": len(_clients),
            "ts": time.time(),
            "ws": "ws://%s:%s/ws" % (HOST, PORT),
            "service": "Gama Web Bridge",
        }).encode("utf-8")
        return connection.respond(200, body)

    if path == "/api/snapshot":
        refresh_all()
        return connection.respond(200, _safe_json(dict(_snapshot)).encode("utf-8"))

    if path == "/api/tasks":
        return connection.respond(200, _safe_json(_collect_tasks()).encode("utf-8"))

    if path == "/api/reminders":
        return connection.respond(200, _safe_json(_collect_reminders()).encode("utf-8"))

    return connection.respond(
        404, _safe_json({"error": "not found", "path": path}).encode("utf-8")
    )


async def _run_server(host: str, port: int) -> None:
    try:
        from websockets.asyncio.server import serve
    except ImportError:
        from websockets.server import serve  # type: ignore

    # Port probes (TCP connect with 0 bytes) and browsers hitting the port
    # without a WS upgrade produce InvalidMessage / EOFError noise.
    # Downgrade websockets' connection logger so the console stays clean.
    try:
        logging.getLogger("websockets").setLevel(logging.CRITICAL)
        logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
        logging.getLogger("websockets.asyncio.server").setLevel(logging.CRITICAL)
    except Exception:
        pass

    log.info(
        "web_bridge listening on http://%s:%s  ws://%s:%s/ws",
        host, port, host, port,
    )

    # origins=None => allow every Origin (required for Vite on :5173)
    kwargs = dict(process_request=_process_request)
    try:
        async with serve(
            _ws_handler,
            host,
            port,
            origins=None,
            ping_interval=20,
            ping_timeout=20,
            **kwargs,
        ):
            await asyncio.Future()
    except TypeError:
        try:
            async with serve(_ws_handler, host, port, origins=None, **kwargs):
                await asyncio.Future()
        except TypeError:
            async with serve(_ws_handler, host, port, **kwargs):
                await asyncio.Future()


def start_web_bridge(assistant: Any = None, host: str = HOST, port: int = PORT) -> None:
    """Start the bridge in a daemon thread (idempotent)."""
    global _started, _thread, _assistant_ref, _loop
    if _started:
        return
    _assistant_ref = assistant
    try:
        from pathlib import Path as _P
        import json as _json
        _keys = _P(__file__).resolve().parents[1] / "config" / "api_keys.json"
        if _keys.exists():
            _name = (_json.loads(_keys.read_text(encoding="utf-8")).get("owner_name") or "").strip()
            if _name:
                _snapshot["owner_name"] = _name
    except Exception:
        pass
    _wire_event_bus()

    def _run() -> None:
        global _loop
        try:
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop.run_until_complete(_run_server(host, port))
        except OSError as exc:
            log.error("web_bridge port bind failed (%s:%s): %s", host, port, exc)
        except Exception as exc:
            log.error("web_bridge failed: %s", exc, exc_info=True)

    def _sysstats_loop() -> None:
        # Seed non-blocking cpu_percent baseline
        try:
            import psutil
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        while _started:
            try:
                push_system_stats()
            except Exception:
                pass
            time.sleep(2.0)

    _thread = threading.Thread(target=_run, name="gama-web-bridge", daemon=True)
    _thread.start()
    threading.Thread(target=_sysstats_loop, name="sysstats-ticker", daemon=True).start()
    _started = True
    log.info("web_bridge thread started (target %s:%s)", host, port)


def stop_web_bridge() -> None:
    global _started
    _started = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_web_bridge()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
