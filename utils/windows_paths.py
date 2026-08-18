"""
utils/windows_paths.py — Windows Known Folder path resolver
=============================================================
Resolves the real Windows user folder paths (Desktop, Downloads,
Documents, etc.) via the Windows Shell's SHGetKnownFolderPath API.
This is more reliable than Path.home() / "Desktop" because Windows
lets users relocate these folders (e.g. Desktop moved to D:\\).

Falls back gracefully on non-Windows or when the API is unavailable.

Author : Vineet Machchal
"""

from __future__ import annotations

import os
import platform
from functools import lru_cache
from pathlib import Path
from typing import Optional

_IS_WIN = platform.system() == "Windows"

# Windows Known Folder GUIDs — these never change across Windows versions.
_KNOWN_FOLDER_GUIDS: dict[str, str] = {
    "desktop":   "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures":  "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music":     "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos":    "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "appdata":   "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",  # Roaming AppData
    "localappdata": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "profile":   "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",  # User profile / home
    "temp":      None,  # Not a known-folder GUID; use env var
}


def _get_via_ctypes(name: str) -> Optional[Path]:
    """Use SHGetKnownFolderPath via ctypes to get the real folder path."""
    if not _IS_WIN:
        return None
    guid_str = _KNOWN_FOLDER_GUIDS.get(name)
    if not guid_str:
        return None
    try:
        import ctypes
        import ctypes.wintypes

        # Convert the GUID string to a GUID structure.
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        guid = GUID()
        ole32.CLSIDFromString(guid_str, ctypes.byref(guid))

        path_ptr = ctypes.c_wchar_p()
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(guid),
            0, None,
            ctypes.byref(path_ptr),
        )
        if result == 0 and path_ptr.value:
            resolved = Path(path_ptr.value)
            # Free the COM string.
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            return resolved
    except Exception:
        pass
    return None


@lru_cache(maxsize=None)
def get_user_folder(name: str) -> Path:
    """Return the real path for a Windows user folder by name.

    Names: desktop, documents, downloads, pictures, music, videos,
           appdata, localappdata, temp, home, profile.

    Resolution order:
      1. Windows SHGetKnownFolderPath (most accurate — follows user relocations)
      2. Environment variable fallback (USERPROFILE, LOCALAPPDATA, etc.)
      3. Path.home() / <name> last resort

    Always returns an absolute Path. Never returns a path inside the
    Gama installation directory.
    """
    key = name.strip().lower()

    # Special cases that don't map to a Known Folder GUID.
    if key == "home":
        key = "profile"  # Let Windows tell us the real user profile path.
    if key == "temp":
        t = os.environ.get("TEMP") or os.environ.get("TMP")
        if t:
            return Path(t)
        return Path.home() / "AppData" / "Local" / "Temp"

    # Try the authoritative Windows Shell API first.
    via_api = _get_via_ctypes(key)
    if via_api is not None:
        return via_api

    # Fallback: environment variables Windows always sets.
    _ENV_FALLBACKS: dict[str, str] = {
        "desktop":      str(Path.home() / "Desktop"),
        "documents":    str(Path.home() / "Documents"),
        "downloads":    str(Path.home() / "Downloads"),
        "pictures":     str(Path.home() / "Pictures"),
        "music":        str(Path.home() / "Music"),
        "videos":       str(Path.home() / "Videos"),
        "appdata":      os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")),
        "localappdata": os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")),
    }
    if key in _ENV_FALLBACKS:
        return Path(_ENV_FALLBACKS[key])

    # If we get here, the keyword was not a known folder. For "home" or
    # "profile", use the USERPROFILE environment variable before falling back.
    if key in ("home", "profile"):
        profile = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        if profile:
            return Path(profile)

    # Absolute last resort: home / name.
    return Path.home() / name


def resolve_user_path(raw: str) -> Path:
    """Resolve a raw path string that may be a shortcut keyword, noise-worded phrase
    (e.g., 'desktop folder', 'my downloads directory'), or an absolute/relative path.
    Expands ~ and environment variables, and performs intelligent user space search.
    """
    raw = (raw or "").strip().strip("'\"")
    if not raw:
        return Path.home()

    # Clean common noise words from natural language speech
    cleaned = raw.strip()
    import re
    cleaned_lower = re.sub(r"^(?:my|the)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned_lower = re.sub(r"\s+(?:folder|directory|dir|folders)$", "", cleaned_lower, flags=re.IGNORECASE).strip()
    lower = cleaned_lower.lower()

    # Pure keyword — return the known folder directly.
    if lower in _KNOWN_FOLDER_GUIDS or lower in ("home", "temp"):
        return get_user_folder(lower)

    # "desktop/foo" or "downloads/bar" — split off the keyword prefix.
    for keyword in list(_KNOWN_FOLDER_GUIDS.keys()) + ["home", "temp"]:
        prefix = keyword + "/"
        prefix_bs = keyword + "\\"
        if lower.startswith(prefix) or lower.startswith(prefix_bs):
            base = get_user_folder(keyword)
            remainder = raw[len(keyword) + 1:]
            return base / remainder

    # Regular path with possible ~ or %VAR%.
    expanded = os.path.expandvars(os.path.expanduser(raw))
    p = Path(expanded)
    if p.exists():
        return p

    # Also check if cleaned path exists directly
    if cleaned_lower != raw.lower():
        p_cleaned = Path(os.path.expandvars(os.path.expanduser(cleaned_lower)))
        if p_cleaned.exists():
            return p_cleaned

    # Search common user locations for a folder/file matching `lower` (case-insensitive)
    user_roots = [
        get_user_folder("desktop"),
        get_user_folder("documents"),
        get_user_folder("downloads"),
        Path.home(),
        get_user_folder("pictures"),
        get_user_folder("music"),
        get_user_folder("videos"),
    ]

    for root in user_roots:
        if not root.exists():
            continue
        # Direct child match
        direct = root / lower
        if direct.exists():
            return direct
        # Case-insensitive scan of immediate subdirectories/files in root
        try:
            for child in root.iterdir():
                if child.name.lower() == lower or child.name.lower() == raw.lower():
                    return child
        except Exception:
            pass

    return p


__all__ = ["get_user_folder", "resolve_user_path"]
