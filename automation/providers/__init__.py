"""
automation/providers/__init__.py

Importing this package registers every built-in provider's capabilities.
Each provider module is cheap to import (just function defs + a
registry.register_many call — no device probing), so doing this once at
engine start-up costs microseconds and keeps idle CPU/RAM at zero.
"""

from automation.providers import (
    windows_provider,
    application_provider,
    file_provider,
    power_provider,
    clipboard_provider,
    media_provider,
)

__all__ = [
    "windows_provider",
    "application_provider",
    "file_provider",
    "power_provider",
    "clipboard_provider",
    "media_provider",
]
