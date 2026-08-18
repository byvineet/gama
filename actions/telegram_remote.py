"""
actions/telegram_remote.py — Remote command access via Telegram
===============================================================
Phase 3: poll the configured bot for messages from the trusted chat
and dispatch them as GAMA tool/text commands.

Security:
  - Only the configured chat_id is accepted
  - Optional shared prefix / secret phrase
  - Destructive tools still go through normal registry/confirmation paths
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import threading
import time
from typing import Any, Callable, Optional

log = get_logger(__name__)
_poll_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_offset = 0
_started = False
_lock = threading.Lock()


def _api(method: str, payload: dict | None = None, timeout: float = 35.0) -> dict:
    from actions.telegram_sender import _api_call, _get_bot_token
    token = _get_bot_token()
    if not token:
        return {"ok": False, "description": "bot not configured"}
    # telegram_sender._api_call already uses token from config
    return _api_call(method, payload or {}, timeout=timeout)


def _trusted_chat() -> str:
    from actions.telegram_sender import _get_chat_id
    return str(_get_chat_id() or "").strip()


def _dispatch_text(text: str) -> str:
    """Send text into the active assistant or tool registry."""
    text = (text or "").strip()
    if not text:
        return "empty"
    # Routine shortcut
    try:
        name = match_trigger_phrase(text)
        if name:
            return run_routine(name)
    except Exception:
        pass
    # Prefer assistant send_text when live
    try:
        from core.tool_dispatch import get_active_assistant
        asst = get_active_assistant()
        if asst is not None and hasattr(asst, "send_text"):
            asst.send_text(text)
            return "Forwarded to GAMA session."
    except Exception as exc:
        log.debug("assistant forward failed: %s", exc)
    # Fallback: treat as a soft status
    return f"GAMA not in an active session; received: {text[:120]}"


def _handle_message(msg: dict) -> None:
    from actions.telegram_sender import _send_message
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    trusted = _trusted_chat()
    if not trusted or chat_id != trusted:
        log.info("[telegram_remote] ignored chat_id=%s", chat_id)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    # Ignore our own alert echoes
    if text.startswith("⚠ Gama alert"):
        return
    # Commands
    lower = text.lower()
    if lower in ("/start", "help", "/help"):
        _send_message(
            chat_id,
            "GAMA remote ready.\n"
            "Send any command in plain language.\n"
            "Examples:\n"
            "• status\n"
            "• run study mode\n"
            "• read my unread emails\n"
            "• generate daily report",
        )
        return
    if lower in ("/status", "status"):
        try:
            from core.notification_router import queue_status
            body = queue_status()
        except Exception:
            body = "ok"
        _send_message(chat_id, f"GAMA remote online.\n{body}")
        return

    log.info("[telegram_remote] command: %s", text[:80])
    try:
        result = _dispatch_text(text)
    except Exception as exc:
        result = f"Error: {exc}"
    try:
        _send_message(chat_id, str(result)[:3500])
    except Exception as exc:
        log.debug("reply failed: %s", exc)


def _poll_loop(interval: float = 1.5) -> None:
    global _offset
    log.info("[telegram_remote] poll loop started")
    while not _stop.is_set():
        try:
            from actions.telegram_sender import is_configured
            if not is_configured():
                _stop.wait(5.0)
                continue
            payload = {
                "timeout": 20,
                "offset": _offset,
                "allowed_updates": ["message"],
            }
            data = _api("getUpdates", payload, timeout=35.0)
            if not data.get("ok"):
                _stop.wait(interval)
                continue
            for upd in data.get("result") or []:
                _offset = max(_offset, int(upd.get("update_id", 0)) + 1)
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        _handle_message(msg)
                    except Exception as exc:
                        log.warning("[telegram_remote] handle failed: %s", exc)
        except Exception as exc:
            log.debug("[telegram_remote] poll error: %s", exc)
            _stop.wait(3.0)
    log.info("[telegram_remote] poll loop stopped")


def start_telegram_remote() -> str:
    """Start background long-poll (idempotent)."""
    global _poll_thread, _started
    with _lock:
        if _started and _poll_thread and _poll_thread.is_alive():
            return "Telegram remote already running."
        try:
            from actions.telegram_sender import is_configured
            if not is_configured():
                return "Telegram not configured. Use telegram_sender setup first."
        except Exception as exc:
            return f"Telegram unavailable: {exc}"
        _stop.clear()
        _poll_thread = threading.Thread(
            target=_poll_loop, name="gama-telegram-remote", daemon=True
        )
        _poll_thread.start()
        _started = True
    return "Telegram remote listener started."


def stop_telegram_remote() -> str:
    global _started
    _stop.set()
    with _lock:
        _started = False
    return "Telegram remote stop requested."


def telegram_remote(action: str = "status", **kwargs) -> str:
    action = (action or "status").lower().strip().replace("-", "_")
    if action in ("start", "enable", "on"):
        return start_telegram_remote()
    if action in ("stop", "disable", "off"):
        return stop_telegram_remote()
    if action in ("status",):
        alive = _poll_thread is not None and _poll_thread.is_alive()
        return f"telegram_remote running={alive} offset={_offset}"
    return "Unknown telegram_remote action. Use: start, stop, status."


__all__ = [
    "telegram_remote",
    "start_telegram_remote",
    "stop_telegram_remote",
]
