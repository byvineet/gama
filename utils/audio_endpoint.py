"""
utils/audio_endpoint.py — Version-resilient pycaw master-volume endpoint
=========================================================================
Newer pycaw releases have twice changed the surface this codebase relied
on:

  1. `AudioUtilities.GetSpeakers()` used to return a raw COM `IMMDevice`
     pointer. Some releases now wrap it in pycaw's own `AudioDevice`
     object for device-listing purposes, which has no `.Activate(...)`
     method — calling it raises
     `AttributeError: 'AudioDevice' object has no attribute 'Activate'`.

  2. Some releases also stopped re-exporting the
     `CLSID_MMDeviceEnumerator` constant from `pycaw.pycaw` entirely
     (or moved it), which breaks
     `from pycaw.pycaw import CLSID_MMDeviceEnumerator` with
     `ImportError: cannot import name 'CLSID_MMDeviceEnumerator' ...`
     even though the rest of pycaw still works fine.

Going straight to the COM device enumerator — instead of
`AudioUtilities.GetSpeakers()` — sidesteps problem #1 on every pycaw
version. Falling back to the well-known, never-changing Windows CLSID
for `MMDeviceEnumerator` (a COM constant, not a pycaw one) when pycaw
doesn't export it itself sidesteps problem #2.

Every volume/mute code path in the app (actions/computer_settings.py,
actions/media_controller.py, automation/providers/media_provider.py)
should go through `get_volume_endpoint()` here instead of rolling its
own pycaw bootstrapping, so a future pycaw API change only needs to be
fixed in one place.
"""

from __future__ import annotations

import threading

# The well-known Windows CLSID for MMDeviceEnumerator. This is a COM
# constant defined by Windows itself (mmdeviceapi.h), not by pycaw — it
# will never change, so hardcoding it here is a safe, permanent fallback
# for whenever a given pycaw version doesn't re-export it.
_CLSID_MMDEVICEENUMERATOR_STR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

# COM apartments are per-OS-thread, not per-process: CoInitialize() must
# be called on every distinct thread that will touch COM before it does,
# not just once anywhere. A single process-wide "already done" flag is
# wrong here — asyncio.to_thread() dispatches tool calls onto a pool of
# worker threads, and background listeners (wake word, hotkeys) run on
# their own threads too, so volume/mute calls can legitimately arrive
# from many different OS threads over the app's lifetime. Using
# threading.local() ensures each thread initializes COM for itself
# exactly once, instead of some threads silently skipping
# initialization because a *different* thread already did it — the
# latter leads to raw STA interface pointers being used from a thread
# that never joined that apartment, which is undefined behavior and can
# surface as a native access violation deep in ctypes' vtable-call code
# (exactly the crash signature this was causing).
_thread_local = threading.local()


def _ensure_com_initialized() -> None:
    if getattr(_thread_local, "com_ready", False):
        return
    try:
        import comtypes
        comtypes.CoInitialize()
    except OSError:
        pass  # already initialized on this thread by something else — fine
    _thread_local.com_ready = True


def get_volume_endpoint():
    """Return a live `IAudioEndpointVolume` COM pointer for the default
    render (speaker) device, working across pycaw versions old and new.

    Raises on failure (caller should catch and fall back to media keys,
    same as before).
    """
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL, CoCreateInstance, GUID

    try:
        from pycaw.pycaw import (
            IAudioEndpointVolume,
            IMMDeviceEnumerator,
            CLSID_MMDeviceEnumerator,
            EDataFlow,
            ERole,
        )  # type: ignore
    except ImportError:
        # This pycaw release doesn't export CLSID_MMDeviceEnumerator
        # (renamed/removed) — the interface/enum classes still import
        # fine, so just supply the constant ourselves.
        from pycaw.pycaw import (
            IAudioEndpointVolume,
            IMMDeviceEnumerator,
            EDataFlow,
            ERole,
        )  # type: ignore
        CLSID_MMDeviceEnumerator = GUID(_CLSID_MMDEVICEENUMERATOR_STR)

    _ensure_com_initialized()

    enumerator = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
    device = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value)
    interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


__all__ = ["get_volume_endpoint", "ensure_com_initialized"]

# Public alias — other modules that make their own direct pycaw calls
# (not through get_volume_endpoint) should call this first on whatever
# thread they're running on, for the same per-thread-initialization
# reason described above.
ensure_com_initialized = _ensure_com_initialized
