"""
security/trust_levels.py — Command Trust Levels
===================================================
Defines the four security levels Gama's commands are classified into,
and the classifier that maps a (tool_name, action, args) triple to one
of them. This is the single source of truth for "how sensitive is this
command" — security_manager.py and verification_pipeline.py both defer
to `classify()` rather than keeping their own copies of this table.

Levels (least to most trusted-required):
    SAFE        — no verification, not even logged specially.
    NORMAL      — no verification required, but logged.
    SENSITIVE   — kept for backward compatibility only; classify() no
                  longer returns this level. Per current spec, voice
                  verification is reserved exclusively for DESTRUCTIVE
                  actions (shutdown, restart, delete, format, etc.) —
                  everything that used to be SENSITIVE (terminal
                  commands, registry editor, etc.) is now NORMAL
                  (logged, but not blocked pending a voice check).
    DESTRUCTIVE — requires voice verification (or, if the user has
                  opted out of voice verification via user_settings,
                  the confirmation code) + verbal "yes" confirmation.

Author: Gama Security Upgrade
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional


class TrustLevel(IntEnum):
    SAFE = 0
    NORMAL = 1
    SENSITIVE = 2
    DESTRUCTIVE = 3


# ---------------------------------------------------------------------------
# Classification table
# ---------------------------------------------------------------------------
# Tools/actions that are always SENSITIVE (require voice verification).
# Matches the spec's examples: Command Prompt, PowerShell, Registry Editor,
# Services, Task Manager, Device Manager, Scheduled Tasks.
_SENSITIVE_TOOLS: dict[str, "bool | set[str]"] = {
    "terminal_command": True,               # Command Prompt / PowerShell
    "computer_agent": True,                 # arbitrary automation/commands
    "code_helper": {"run"},
    "process_manager": {"list", "info"},    # viewing (Task Manager-like); kill is DESTRUCTIVE
    "computer_settings": {
        "open_registry", "open_services", "open_task_manager",
        "open_device_manager", "open_scheduled_tasks", "device_manager",
        "services", "registry_editor", "scheduled_tasks", "task_manager",
    },
    "desktop_context": set(),  # NORMAL by default; placeholder for future sensitive sub-actions
}

# Tools/actions that are always DESTRUCTIVE (require voice + verbal
# confirmation). Matches the spec's examples: delete files, empty recycle
# bin, shutdown, restart, log off, kill processes, disk cleanup, format
# drives, automation scripts that modify files, install/uninstall
# software, system configuration changes.
_DESTRUCTIVE_TOOLS: dict[str, "bool | set[str]"] = {
    "computer_settings": {
        "shutdown", "restart", "reboot", "sleep", "hibernate",
        "lock", "sign_out", "log_off", "format", "disk_cleanup",
        "system_config", "change_settings",
    },
    "file_controller": {"delete", "empty_recycle_bin", "format"},
    "process_manager": {"kill", "kill_all", "terminate"},
    "game_updater": {"install", "uninstall"},
    "startup_manager": {"add", "remove", "enable", "disable"},
    "computer_agent": set(),  # covered by True above at SENSITIVE; escalated below by keyword
    "advanced_automation": True,   # automation scripts that modify files
}

# NORMAL tools — no verification, but every call is logged (audit trail).
_NORMAL_TOOLS = {
    "open_app", "browser_control", "web_search", "edge_search",
    "youtube_video", "computer_settings",  # (non-destructive actions fall through here)
    "mouse_actions", "keyboard_actions", "downloader", "screen_recorder",
    "screen_processor", "clipboard", "notes", "reminder", "class_schedule",
    "email_sender", "whatsapp_sender", "system_monitor", "system_info",
    "desktop_context", "meeting_watch",
}

# SAFE tools — pure chat/info, no logging beyond normal conversation logs.
_SAFE_TOOLS = {
    "weather_report", "daily_briefing", "music_player", "user_settings",
}

# Password-shaped args anywhere escalate to DESTRUCTIVE regardless of tool,
# since credentials are as sensitive as anything on this list.
_PASSWORD_ARG_KEYS = {"password", "passcode", "pin", "secret"}

# Free-text keywords that, if present in a generic automation tool's args,
# escalate an otherwise-SENSITIVE tool (e.g. computer_agent/terminal_command)
# up to DESTRUCTIVE — covers "run a script that deletes/formats/etc." even
# when the tool itself is generic.
_DESTRUCTIVE_KEYWORDS = (
    "format c:", "del /s", "del /f", "rmdir /s", "rd /s", "diskpart",
    "shutdown", "rm -rf", "reg delete", "uninstall", "erase-volume",
)


def _args_text(args: dict) -> str:
    return " ".join(str(v).lower() for v in args.values() if isinstance(v, (str, int, float)))


def classify(tool_name: str, args: Optional[dict] = None) -> TrustLevel:
    args = args or {}
    action = str(args.get("action", "")).lower().strip()

    if any(k in _PASSWORD_ARG_KEYS for k in args.keys()):
        return TrustLevel.SENSITIVE

    text = _args_text(args)
    if any(kw in text for kw in _DESTRUCTIVE_KEYWORDS):
        return TrustLevel.DESTRUCTIVE

    spec = _DESTRUCTIVE_TOOLS.get(tool_name)
    if spec is True:
        return TrustLevel.DESTRUCTIVE
    if isinstance(spec, set) and action in spec:
        return TrustLevel.DESTRUCTIVE

    spec = _SENSITIVE_TOOLS.get(tool_name)
    if spec is True:
        return TrustLevel.SENSITIVE
    if isinstance(spec, set) and action in spec:
        return TrustLevel.SENSITIVE

    if tool_name in _SAFE_TOOLS:
        return TrustLevel.SAFE

    if tool_name in _NORMAL_TOOLS:
        return TrustLevel.NORMAL

    # Unknown tool: default to NORMAL (logged, not blocked) rather than
    # SAFE (invisible) or DESTRUCTIVE (would break unrelated features
    # every time a new tool is added without updating this table).
    return TrustLevel.NORMAL


def describe(level: TrustLevel) -> str:
    return {
        TrustLevel.SAFE: "SAFE — no verification required",
        TrustLevel.NORMAL: "NORMAL — logged, no verification required",
        TrustLevel.SENSITIVE: "SENSITIVE — requires voice verification",
        TrustLevel.DESTRUCTIVE: "DESTRUCTIVE — requires voice + verbal confirmation",
    }[level]


__all__ = ["TrustLevel", "classify", "describe"]
