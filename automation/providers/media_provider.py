"""
automation/providers/media_provider.py — Media Automation.

System master volume via pycaw/comtypes (native COM, fast). Per-app
volume (e.g. "mute Spotify") uses pycaw's per-session audio interface
when available; media key simulation (play/pause/next) falls back to
`pynput` virtual key events since Windows has no direct API for that.
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple

from utils.logger import get_logger
from automation.models import ActionResult, Capability
from automation.registry import registry

log = get_logger(__name__)
_IS_WINDOWS = sys.platform == "win32"


def _get_master_volume_iface():
    from utils.audio_endpoint import get_volume_endpoint
    return get_volume_endpoint()


def _set_volume(level: int, **_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Volume control is Windows-only")
    try:
        vol = _get_master_volume_iface()
        vol.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100.0, None)
        return ActionResult(ok=True, message=f"Volume set to {level}%")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Volume set failed: {exc}")


def _verify_volume(level: int, **_) -> Tuple[bool, str]:
    try:
        vol = _get_master_volume_iface()
        current = round(vol.GetMasterVolumeLevelScalar() * 100)
        return abs(current - level) <= 2, f"{current}%"
    except Exception as exc:
        return False, str(exc)


def _mute_system(mute: bool = True, **_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Mute is Windows-only")
    try:
        vol = _get_master_volume_iface()
        vol.SetMute(1 if mute else 0, None)
        return ActionResult(ok=True, message=("Muted" if mute else "Unmuted") + " system audio")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Mute failed: {exc}")


def _mute_app(name: str, mute: bool = True, **_) -> ActionResult:
    if not _IS_WINDOWS:
        return ActionResult(ok=False, message="Per-app mute is Windows-only")
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        from utils.audio_endpoint import ensure_com_initialized
        ensure_com_initialized()
        sessions = AudioUtilities.GetAllSessions()
        needle = name.lower()
        for session in sessions:
            proc = session.Process
            if proc and needle in proc.name().lower():
                session.SimpleAudioVolume.SetMute(1 if mute else 0, None)
                return ActionResult(ok=True, message=f"{'Muted' if mute else 'Unmuted'} {name}")
        return ActionResult(ok=False, message=f"No audio session found for '{name}'")
    except Exception as exc:
        return ActionResult(ok=False, message=f"Per-app mute failed: {exc}")


def _media_key(key: str, **_) -> ActionResult:
    """key in {play_pause, next, previous, volume_up, volume_down, mute}"""
    try:
        from pynput.keyboard import Controller, Key  # type: ignore
    except Exception:
        return ActionResult(ok=False, message="pynput not available")
    mapping = {
        "play_pause": Key.media_play_pause,
        "next": Key.media_next,
        "previous": Key.media_previous,
        "volume_up": Key.media_volume_up,
        "volume_down": Key.media_volume_down,
        "mute": Key.media_volume_mute,
    }
    k = mapping.get(key)
    if k is None:
        return ActionResult(ok=False, message=f"Unknown media key '{key}'")
    Controller().tap(k)
    return ActionResult(ok=True, message=f"Sent media key '{key}'")


def register() -> None:
    registry.register_many([
        Capability("media.set_volume", _set_volume, verify=_verify_volume, cost=0, speed_ms=10,
                   description="Set system master volume", keywords=("volume",)),
        Capability("media.mute_system", _mute_system, cost=0, speed_ms=10,
                   description="Mute/unmute system audio", keywords=("mute",)),
        Capability("media.mute_app", _mute_app, cost=1, speed_ms=30,
                   description="Mute/unmute a specific app's audio session",
                   keywords=("mute spotify", "mute app")),
        Capability("media.key", _media_key, cost=0, speed_ms=10,
                   description="Send a media key (play/pause/next/previous)",
                   keywords=("play", "pause", "next track", "skip")),
    ])


register()
