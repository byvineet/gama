"""
actions/confirmation.py — Gama Confirmation Policy
====================================================
Confirm ONLY what can hurt.

Policy (three tiers):
  SAFE / LOW   — auto-run when intent confidence is high. Never ask.
  MEDIUM       — short one-line confirm for send-message / money / external post.
  DESTRUCTIVE  — voice verification + confirmation code (shutdown, delete permanent, format).

Safe reversible actions (sleep, lock, sign_out, volume, open app) never require
confirmation. The "are you sure?" loops that kill the JARVIS feel are banned
for routine work.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
from typing import Optional, Set

log = get_logger(__name__)
logger = log  # back-compat alias
# Permanent / irreversible — always require code (and voice when available).
DESTRUCTIVE_ACTIONS: Set[str] = {
    "shutdown", "power_off", "shut_down",
    "restart", "reboot",
    "delete_folder", "empty_recycle_bin",
    "format_drive", "format_disk",
    "registry_edit",
    "factory_reset", "wipe_memory",
    "uninstall_app",
}

# Outbound / costly — one short confirm, no code.
SENSITIVE_ACTIONS: Set[str] = {
    "send_email", "email_action", "compose_email",
    "send_whatsapp", "whatsapp_message", "whatsapp",
    "send_message", "message",
    "post_instagram", "instagram_post",
    "payment", "transfer_money", "purchase",
}

# Explicitly safe — never confirm even if a model hesitates.
SAFE_ACTIONS: Set[str] = {
    "sleep", "lock", "sign_out", "hibernate",
    "open_app", "set_volume", "set_brightness",
    "web_search", "edge_search", "weather_report",
    "play_music", "pause_media", "next_track",
    "read_screen", "screen_process", "clipboard",
    "get_time", "system_info", "desktop_context",
}


def set_confirmation_code(code: str, old_code: Optional[str] = None,
                           owner_authenticated: bool = False) -> str:
    """Set/change the shared confirmation (security) code. Minimum 4 characters.

    If a code is already set, the current code (`old_code`) is required
    unless `owner_authenticated` is True (e.g. the owner's voice was
    live-verified elsewhere).
    """
    from security.confirmation_store import has_confirmation_code, change_confirmation_code

    code = (code or "").strip()
    if not code:
        return "Please provide a code (at least 4 characters)."
    if len(code) < 4:
        return "Code must be at least 4 characters long."

    if not has_confirmation_code():
        change_confirmation_code(None, code, owner_authenticated=True)
        return "DONE: Confirmation code set."

    if change_confirmation_code(old_code, code, owner_authenticated=owner_authenticated):
        return "DONE: Confirmation code updated."

    return "ERROR: A code is already set. Provide the current code first, or verify by voice."


def verify_confirmation_code(code: str) -> str:
    """Verify the confirmation code. Returns success message or error."""
    from security.confirmation_store import has_confirmation_code, verify_confirmation_code as _verify

    if not has_confirmation_code():
        return "ERROR: No confirmation code set. Ask Vineet to set one first."
    provided = (code or "").strip()
    if provided and _verify(provided):
        return "VERIFIED"
    return "ERROR: Wrong code. Action cancelled."


def requires_confirmation(action: str) -> bool:
    """True only for permanent/destructive operations (code required)."""
    return action.lower().strip() in DESTRUCTIVE_ACTIONS


def requires_sensitive_confirm(action: str) -> bool:
    """True for outbound/costly actions — one short verbal confirm, no code."""
    a = action.lower().strip()
    if a in SAFE_ACTIONS:
        return False
    return a in SENSITIVE_ACTIONS


def is_safe_action(action: str) -> bool:
    return action.lower().strip() in SAFE_ACTIONS


def is_code_set() -> bool:
    from security.confirmation_store import has_confirmation_code
    return has_confirmation_code()


__all__ = [
    "set_confirmation_code", "verify_confirmation_code",
    "requires_confirmation", "requires_sensitive_confirm", "is_safe_action",
    "is_code_set", "DESTRUCTIVE_ACTIONS", "SENSITIVE_ACTIONS", "SAFE_ACTIONS",
]
