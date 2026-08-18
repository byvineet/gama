"""
state_engine/mic_profiles.py — Per-Microphone Calibration Profiles
=====================================================================
JSON-backed store for barge-in calibration profiles, keyed by input
device name (e.g. "Realtek Integrated Microphone", "USB Headset").

Each profile holds the raw measurements taken during calibration
(voice/mic_calibration.py) plus the derived INTERRUPT_* parameters
that voice/barge_in.py / main.py should apply while that device is
the active microphone.

Profiles are never overwritten implicitly — only an explicit
recalibration (or `save_profile(..., overwrite=True)`) replaces an
existing entry for a device.

Author: Gama / Vineet Machchal
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()
_PROFILES_PATH = Path.home() / ".gama" / "mic_profiles.json"

# Which device profile is "active" — normally just whatever sounddevice
# currently reports as the default input, but kept explicit so the UI
# can show/reason about it without re-querying audio devices.
_ACTIVE_KEY = "_active_device"


def _read() -> Dict[str, Any]:
    with _LOCK:
        try:
            if _PROFILES_PATH.exists():
                return json.loads(_PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning(f"mic_profiles: failed to read {_PROFILES_PATH}: {exc}")
        return {}


def _write(data: Dict[str, Any]) -> None:
    with _LOCK:
        try:
            _PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
            _PROFILES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning(f"mic_profiles: failed to write {_PROFILES_PATH}: {exc}")


def has_profile(device_name: str) -> bool:
    data = _read()
    return bool(device_name) and device_name in data


def get_profile(device_name: str) -> Optional[Dict[str, Any]]:
    """Return the stored profile dict for `device_name`, or None."""
    if not device_name:
        return None
    return _read().get(device_name)


def save_profile(device_name: str, profile: Dict[str, Any], overwrite: bool = True) -> None:
    """Persist `profile` under `device_name`.

    Calibration is always an explicit user action (wizard "Apply" /
    "Recalibrate"), so overwrite defaults to True here; callers that
    want the "don't clobber an existing profile unless the user
    explicitly recalibrates" behaviour should check `has_profile()`
    themselves before calling this with overwrite=True.
    """
    data = _read()
    if not overwrite and device_name in data:
        log.info(f"mic_profiles: profile for {device_name!r} exists, not overwriting.")
        return
    profile = dict(profile)
    profile["calibrated_at"] = time.time()
    data[device_name] = profile
    _write(data)
    log.info(f"mic_profiles: saved profile for {device_name!r}.")


def delete_profile(device_name: str) -> None:
    data = _read()
    if device_name in data:
        del data[device_name]
        _write(data)


def list_profiles() -> Dict[str, Any]:
    data = _read()
    return {k: v for k, v in data.items() if k != _ACTIVE_KEY}


def set_active_device(device_name: str) -> None:
    data = _read()
    data[_ACTIVE_KEY] = device_name
    _write(data)


def get_active_device() -> str:
    return _read().get(_ACTIVE_KEY, "")


__all__ = [
    "has_profile", "get_profile", "save_profile", "delete_profile",
    "list_profiles", "set_active_device", "get_active_device",
]
