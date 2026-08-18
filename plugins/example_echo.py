"""
Example plugin — echo tool.

Demonstrates the PLUGIN dict contract. Safe no-op for production demos.
Delete or keep as a template for new tools.
"""

from __future__ import annotations

from typing import Any, Dict


def _echo_handler(args: Dict[str, Any]) -> str:
    text = (args or {}).get("text") or (args or {}).get("message") or ""
    if not text:
        return "echo: (empty) — pass text= to echo something back."
    return f"echo: {text}"


PLUGIN = {
    "name": "plugin_echo",
    "description": (
        "Example plugin tool. Echoes the given text back. "
        "Use only for testing the plugin system."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {"type": "STRING", "description": "Text to echo back"},
        },
        "required": ["text"],
    },
    "risk": "SAFE",
    "category": "plugin",
    "behavior": "NON_BLOCKING",
    "handler": _echo_handler,
}
