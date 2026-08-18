"""
utils/paths.py — Gama exe-safe resource paths
==============================================
When Gama is frozen with PyInstaller, files bundled via --add-data
are extracted to sys._MEIPASS (a temp folder). This helper resolves
the correct path whether running from source or from a frozen .exe.

Author : Vineet Machchal
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_base_dir() -> Path:
    """Return the base directory of the application.

    - When running from source: the folder containing main.py
    - When frozen (PyInstaller): the folder containing the .exe
    """
    if getattr(sys, "frozen", False):
        # PyInstaller — use the exe's directory
        return Path(sys.executable).resolve().parent
    # Running from source
    return Path(__file__).resolve().parent.parent


def resource_path(relative_path: str) -> Path:
    """Resolve a resource path that works both in source and frozen mode.

    For read-only bundled assets (face.png, prompt.txt), this returns
    the path inside sys._MEIPASS when frozen.

    For user-writable files (config, memory, logs), use get_base_dir()
    instead — those live next to the .exe, not in the temp bundle.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller bundle — read-only resources are in _MEIPASS
        return Path(sys._MEIPASS) / relative_path
    # Running from source
    return Path(__file__).resolve().parent.parent / relative_path


def user_data_path(relative_path: str) -> Path:
    """Resolve a user-writable path (config, memory, logs).

    When frozen, these go next to the .exe so the user can edit them.
    When running from source, they go in the project root.
    """
    return get_base_dir() / relative_path


__all__ = ["get_base_dir", "resource_path", "user_data_path"]
