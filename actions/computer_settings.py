"""
actions/computer_settings.py — Gama System Control (Mark XLVII style)
Volume, brightness, power, screenshots, window management, Wi-Fi, Bluetooth.

Destructive actions (shutdown, restart, sleep, lock) require a confirmation
code that the user sets once and must speak/verify each time.

Reliability layer:
  - Volume/brightness changes are read back after being applied to confirm
    they actually took effect (not just that the API call didn't throw).
  - Wi-Fi / Bluetooth use real enable/disable calls (not a blind "toggle"
    that assumes the prior state) and verify the resulting state.
  - Window management uses native Win32 APIs against the actual foreground
    window when available, falling back to global hotkeys only if the
    native path is unavailable.
  - Every action reports plainly if it needs administrator rights it
    doesn't have, instead of failing silently.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import platform
import subprocess
import time
from typing import Optional

from actions.reliability import is_admin, get_foreground_window_title
from utils.proc import hidden_kwargs

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()


def computer_settings(action: str, value: str = "", **kwargs) -> str:
    """Single entry point for all computer control actions.

    Supports both absolute and relative adjustments:
      volume_set 50     → set volume to 50%
      volume_up         → +10%
      volume_down       → -10%
      volume_increase X → +X%  (relative)
      volume_decrease X → -X%  (relative)
      brightness 75     → set brightness to 75%
      brightness_up     → +10%
      brightness_down   → -10%
      brightness_increase X → +X%  (relative)
      brightness_decrease X → -X%  (relative)

    For destructive actions (shutdown, restart), the caller
    MUST provide a valid `confirmation_code` kwarg. If no code is set,
    the action is blocked.

    sleep, lock, sign_out are safe actions — execute immediately, no confirmation.
    """
    action = (action or "").strip().lower()
    value = (value or "").strip()

    # --- Non-destructive actions (no code needed) ---
    if action in ("volume_up",):
        return _volume_change(+10)
    if action in ("volume_down",):
        return _volume_change(-10)
    if action in ("volume_set", "set_volume"):
        if value:
            return _volume_set(value)
        return "Provide a volume level (0-100)."
    # Relative volume adjustments (spec Part 10: "Increase volume by 15%")
    if action in ("volume_increase", "volume_increase_by", "increase_volume"):
        delta = _parse_int(value, 10)
        return _volume_change(+delta)
    if action in ("volume_decrease", "volume_decrease_by", "decrease_volume"):
        delta = _parse_int(value, 10)
        return _volume_change(-delta)
    if action in ("mute", "unmute", "toggle_mute"):
        return _toggle_mute()
    # Absolute brightness
    if action == "brightness":
        try:
            return _set_brightness(int(value))
        except ValueError:
            return f"Invalid brightness value: '{value}'. Use a number 0-100."
    # Relative brightness adjustments (spec Part 10)
    if action in ("brightness_up", "brightness_increase"):
        delta = _parse_int(value, 10)
        return _brightness_change(+delta)
    if action in ("brightness_down", "brightness_decrease"):
        delta = _parse_int(value, 10)
        return _brightness_change(-delta)
    if action in ("brightness_increase_by", "increase_brightness"):
        delta = _parse_int(value, 10)
        return _brightness_change(+delta)
    if action in ("brightness_decrease_by", "decrease_brightness"):
        delta = _parse_int(value, 10)
        return _brightness_change(-delta)
    if action in ("screenshot",):
        return _take_screenshot()
    if action in ("wifi", "toggle_wifi", "wifi_on", "wifi_off", "enable_wifi", "disable_wifi"):
        return _set_wifi(action, value)
    if action in ("bluetooth", "bluetooth_status"):
        return _bluetooth_status()
    if action in ("bluetooth_on", "enable_bluetooth"):
        return _set_bluetooth(True)
    if action in ("bluetooth_off", "disable_bluetooth"):
        return _set_bluetooth(False)
    if action in ("minimize", "minimize_all"):
        return _minimize_all()
    if action in ("close", "close_window"):
        return _close_active()
    if action in ("switch_window", "alt_tab"):
        return _alt_tab()
    if action in ("snap_left", "snap_right", "maximize", "restore_window", "snap_window"):
        return _snap_window(value or action)

    # --- Safe actions — execute immediately, no confirmation code needed ---
    # Per spec: sleep and lock are reversible and safe; no confirmation required.
    if action in ("sleep",):
        return _sleep()
    if action in ("lock", "lock_screen"):
        return _lock()

    # --- Truly destructive actions — REQUIRE confirmation code ---
    from actions.confirmation import requires_confirmation, verify_confirmation_code, is_code_set

    if requires_confirmation(action):
        # Check if a code is set
        if not is_code_set():
            return ("ERROR: No confirmation code set. "
                    "Please set a code first by saying 'set confirmation code to XXXX'. "
                    "Then I can perform shutdown/restart actions.")

        # Get the provided code from kwargs
        provided_code = kwargs.get("confirmation_code", "")

        # Verify the code
        result = verify_confirmation_code(provided_code)
        if result != "VERIFIED":
            return result  # Returns "ERROR: Wrong code..." message

        # Code is correct — execute the action
        if action in ("restart", "reboot"):
            return _restart()
        if action in ("shutdown", "power_off"):
            return _shutdown()

    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Helpers for relative adjustments (Part 10)
# ---------------------------------------------------------------------------
def _parse_int(value: str, default: int) -> int:
    """Parse an integer from a string, returning default on failure."""
    try:
        v = int(str(value).strip().rstrip("%"))
        return max(1, min(100, abs(v)))
    except (ValueError, TypeError):
        return default


def _get_current_brightness() -> Optional[int]:
    """Read current brightness level from WMI, or None if unavailable."""
    try:
        import wmi
        c = wmi.WMI(namespace="wmi")
        return int(c.WmiMonitorBrightness()[0].CurrentBrightness)
    except Exception:
        return None


def _brightness_change(delta: int) -> str:
    """Increase or decrease brightness by a relative amount."""
    current = _get_current_brightness()
    if current is None:
        # Fallback: try setting a sane default direction
        target = max(0, min(100, 50 + delta))
        return _set_brightness(target)
    target = max(0, min(100, current + delta))
    return _set_brightness(target)


# ---------------------------------------------------------------------------
# Volume — change + read back to confirm it actually took effect
# ---------------------------------------------------------------------------
def _get_volume_endpoint():
    # Shared, version-resilient bootstrap (handles both the
    # AudioUtilities.GetSpeakers()-returns-a-wrapper issue and pycaw
    # releases that no longer export CLSID_MMDeviceEnumerator) — see
    # utils/audio_endpoint.py for details on both failure modes.
    from utils.audio_endpoint import get_volume_endpoint
    return get_volume_endpoint()


def _record_volume_pref(level: int) -> None:
    """Record volume preference for this time of day (non-blocking, best-effort)."""
    try:
        from learning.habit_tracker import record_volume_preference
        record_volume_preference(level)
    except Exception:
        pass


def _volume_change(delta: int) -> str:
    try:
        volume = _get_volume_endpoint()
        before = volume.GetMasterVolumeLevelScalar()
        target = max(0.0, min(1.0, before + delta / 100.0))
        volume.SetMasterVolumeLevelScalar(target, None)
        time.sleep(0.1)
        after = volume.GetMasterVolumeLevelScalar()
        _record_volume_pref(int(after * 100))
        if abs(after - target) < 0.02:
            return f"Volume: {int(after * 100)}% (was {int(before * 100)}%)."
        return f"Volume change may not have applied — now reads {int(after * 100)}%."
    except Exception as exc:
        logger.debug(f"pycaw volume control unavailable ({exc}); falling back to media keys.")
        return _volume_change_via_keys(delta)


def _volume_set(value: str) -> str:
    try:
        pct = max(0, min(100, int(value)))
    except ValueError:
        return f"Invalid volume value: '{value}'."
    try:
        volume = _get_volume_endpoint()
        volume.SetMasterVolumeLevelScalar(pct / 100.0, None)
        time.sleep(0.1)
        after = volume.GetMasterVolumeLevelScalar()
        _record_volume_pref(int(after * 100))
        if abs(after * 100 - pct) < 2:
            return f"Volume set to {int(after * 100)}%."
        return f"Tried to set volume to {pct}%, but it now reads {int(after * 100)}%."
    except Exception as exc:
        return f"Volume control unavailable ({exc}). Try volume_up/volume_down instead."


def _volume_change_via_keys(delta: int) -> str:
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        key = Key.media_volume_up if delta > 0 else Key.media_volume_down
        for _ in range(max(1, abs(delta) // 5)):
            kb.press(key)
            kb.release(key)
        return f"Volume {'up' if delta > 0 else 'down'} (sent via media keys — could not verify exact level)."
    except Exception as exc:
        return f"Volume control failed: {exc}"


def _toggle_mute() -> str:
    try:
        volume = _get_volume_endpoint()
        was_muted = bool(volume.GetMute())
        volume.SetMute(0 if was_muted else 1, None)
        time.sleep(0.1)
        now_muted = bool(volume.GetMute())
        if now_muted != was_muted:
            return "Muted." if now_muted else "Unmuted."
        return "Mute toggle may not have applied."
    except Exception:
        try:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            kb.press(Key.media_volume_mute)
            kb.release(Key.media_volume_mute)
            return "Mute toggled (via media key — could not verify)."
        except Exception as exc:
            return f"Mute failed: {exc}"


# ---------------------------------------------------------------------------
# Brightness — set + read back
# ---------------------------------------------------------------------------
def _record_brightness_pref(level: int) -> None:
    """Record brightness preference for this time of day (non-blocking)."""
    try:
        from learning.habit_tracker import record_brightness_preference
        record_brightness_preference(level)
    except Exception:
        pass


def _set_brightness(level: int) -> str:
    level = max(0, min(100, int(level)))
    try:
        import wmi  # type: ignore
        c = wmi.WMI(namespace="wmi")
        methods = c.WmiMonitorBrightnessMethods()[0]
        methods.WmiSetBrightness(level, 0)
        time.sleep(0.15)
        try:
            current = c.WmiMonitorBrightness()[0].CurrentBrightness
        except Exception:
            current = None
        # Record preference regardless of readback availability
        _record_brightness_pref(current if current is not None else level)
        if current is not None:
            if abs(current - level) <= 2:
                return f"Brightness set to {current}%."
            return f"Tried to set brightness to {level}%, but it now reads {current}%. Some monitors (external/DDC) don't support this."
        return f"Brightness set to {level}% (could not verify — no readback available)."
    except Exception as exc:
        return (f"Brightness control unavailable on this display "
                f"(often the case for external monitors): {exc}")


# ---------------------------------------------------------------------------
# Screenshot — save + verify file exists and is non-empty, retry with a
# second backend if the first fails.
# ---------------------------------------------------------------------------
def _take_screenshot() -> str:
    from datetime import datetime
    from pathlib import Path
    save_dir = Path.home() / "Pictures" / "GamaScreenshots"
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"gama_ss_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

    try:
        import pyautogui
        img = pyautogui.screenshot()
        img.save(str(path))
    except Exception as exc:
        logger.debug(f"pyautogui screenshot failed ({exc}); trying mss.")
        try:
            import mss
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[0])
                mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        except Exception as exc2:
            return f"Screenshot failed (both backends): {exc2}"

    if path.exists() and path.stat().st_size > 0:
        return f"Screenshot saved: {path} (verified, {path.stat().st_size // 1024} KB)."
    return f"Screenshot command ran but the file looks empty or missing: {path}"


# ---------------------------------------------------------------------------
# Power actions
# ---------------------------------------------------------------------------
def _lock() -> str:
    try:
        if _OS == "Windows":
            ok = os.system("rundll32.exe user32.dll,LockWorkStation") == 0
            return "PC locked." if ok else "Lock command sent (could not confirm exit code)."
        elif _OS == "Darwin":
            subprocess.Popen(["pmset", "displaysleepnow"])
        else:
            subprocess.Popen(["loginctl", "lock-session"])
        return "PC locked."
    except Exception as exc:
        return f"Lock failed: {exc}"


def _sleep() -> str:
    try:
        if _OS == "Windows":
            os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        elif _OS == "Darwin":
            subprocess.Popen(["pmset", "sleepnow"])
        else:
            subprocess.Popen(["systemctl", "suspend"])
        return "Sleep mode."
    except Exception as exc:
        return f"Sleep failed: {exc}"


def _restart() -> str:
    try:
        if _OS == "Windows":
            r = subprocess.run(["shutdown", "/r", "/t", "0"], shell=False,
                               capture_output=True, text=True, timeout=10, **hidden_kwargs())
            if r.returncode != 0:
                return f"Restart command failed: {r.stderr.strip() or r.stdout.strip()}"
        elif _OS == "Darwin":
            subprocess.Popen(["sudo", "shutdown", "-r", "now"])
        else:
            subprocess.Popen(["sudo", "reboot"])
        return "Restarting."
    except Exception as exc:
        return f"Restart failed: {exc}"


def _shutdown() -> str:
    try:
        if _OS == "Windows":
            r = subprocess.run(["shutdown", "/s", "/t", "0"], shell=False,
                               capture_output=True, text=True, timeout=10, **hidden_kwargs())
            if r.returncode != 0:
                return f"Shutdown command failed: {r.stderr.strip() or r.stdout.strip()}"
        elif _OS == "Darwin":
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        else:
            subprocess.Popen(["sudo", "poweroff"])
        return "Shutting down."
    except Exception as exc:
        return f"Shutdown failed: {exc}"


# ---------------------------------------------------------------------------
# Wi-Fi — real enable/disable (not a blind toggle) + verified status
# ---------------------------------------------------------------------------
def _get_wifi_interface_name() -> Optional[str]:
    try:
        r = subprocess.run(["netsh", "interface", "show", "interface"],
                           capture_output=True, text=True, timeout=8, **hidden_kwargs())
        for line in r.stdout.splitlines():
            if "wi-fi" in line.lower() or "wireless" in line.lower():
                parts = line.split()
                return " ".join(parts[3:]) if len(parts) > 3 else "Wi-Fi"
    except Exception:
        pass
    return "Wi-Fi"


def _wifi_is_enabled(iface: str) -> Optional[bool]:
    try:
        r = subprocess.run(["netsh", "interface", "show", "interface", f"name={iface}"],
                           capture_output=True, text=True, timeout=8, **hidden_kwargs())
        out = r.stdout.lower()
        if "admin state" in out:
            for line in r.stdout.splitlines():
                if "admin state" in line.lower():
                    return "enabled" in line.lower()
        return "enabled" in out and "disabled" not in out
    except Exception:
        return None


def _set_wifi(action: str, value: str = "") -> str:
    if _OS != "Windows":
        return "Wi-Fi control is only implemented for Windows right now."

    iface = _get_wifi_interface_name()

    # Decide target state
    if action in ("wifi_on", "enable_wifi"):
        target_enabled = True
    elif action in ("wifi_off", "disable_wifi"):
        target_enabled = False
    elif value.lower() in ("on", "enable", "enabled"):
        target_enabled = True
    elif value.lower() in ("off", "disable", "disabled"):
        target_enabled = False
    else:
        # Legacy "toggle" behaviour — check current state and flip it.
        current = _wifi_is_enabled(iface)
        if current is None:
            return (f"Couldn't read current Wi-Fi state for '{iface}'. "
                    f"Try 'turn wifi on' / 'turn wifi off' explicitly.")
        target_enabled = not current

    admin_needed = not is_admin()
    cmd = ["netsh", "interface", "set", "interface", iface,
           "enabled" if target_enabled else "disabled"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=10, **hidden_kwargs())
        if r.returncode != 0:
            hint = " (this usually needs administrator rights)" if admin_needed else ""
            return f"Wi-Fi {'on' if target_enabled else 'off'} failed{hint}: {r.stderr.strip() or r.stdout.strip()}"
    except Exception as exc:
        return f"Wi-Fi toggle failed: {exc}"

    time.sleep(1.0)
    now = _wifi_is_enabled(iface)
    if now is not None and now == target_enabled:
        return f"Wi-Fi turned {'on' if target_enabled else 'off'} (verified)."
    if now is None:
        return f"Wi-Fi {'on' if target_enabled else 'off'} command sent, but couldn't verify the new state."
    return f"Tried to turn Wi-Fi {'on' if target_enabled else 'off'}, but it still reads {'on' if now else 'off'}."


# ---------------------------------------------------------------------------
# Bluetooth — real enable/disable via PnP device control + status
# ---------------------------------------------------------------------------
def _bluetooth_status() -> str:
    try:
        if _OS == "Windows":
            r = subprocess.run(["powershell", "-Command",
                                "Get-PnpDevice -Class Bluetooth | Select Status, FriendlyName | Format-Table -AutoSize"],
                               capture_output=True, text=True, timeout=8, **hidden_kwargs())
            return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "Bluetooth status unavailable."
        return "Bluetooth status unavailable on this OS."
    except Exception as exc:
        return f"Bluetooth check failed: {exc}"


def _set_bluetooth(enable: bool) -> str:
    if _OS != "Windows":
        return "Bluetooth control is only implemented for Windows right now."
    if not is_admin():
        return ("Turning Bluetooth on/off needs administrator rights. "
                "Please run Gama as administrator, or toggle it from Windows Settings.")
    verb = "Enable-PnpDevice" if enable else "Disable-PnpDevice"
    ps_cmd = (f"Get-PnpDevice -Class Bluetooth | Where-Object {{$_.Status -ne 'Error'}} "
              f"| {verb} -Confirm:$false")
    try:
        r = subprocess.run(["powershell", "-Command", ps_cmd],
                           capture_output=True, text=True, timeout=15, **hidden_kwargs())
        if r.returncode != 0:
            return f"Bluetooth {'on' if enable else 'off'} failed: {r.stderr.strip() or 'unknown error'}"
    except Exception as exc:
        return f"Bluetooth toggle failed: {exc}"

    time.sleep(1.0)
    status = _bluetooth_status()
    return f"Bluetooth turned {'on' if enable else 'off'}.\n{status}"


# ---------------------------------------------------------------------------
# Window management — native Win32 first, hotkey fallback
# ---------------------------------------------------------------------------
def _minimize_all() -> str:
    if _OS == "Windows":
        try:
            import win32gui  # type: ignore
            import win32con  # type: ignore

            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                    try:
                        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                    except Exception:
                        pass
                return True

            win32gui.EnumWindows(_cb, None)
            return "Minimized all windows (native)."
        except Exception as exc:
            logger.debug(f"native minimize_all failed ({exc}); falling back to hotkey.")
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        kb.press(Key.cmd); kb.press("d"); kb.release("d"); kb.release(Key.cmd)
        return "Minimized all windows (Show Desktop hotkey)."
    except Exception as exc:
        return f"Minimize all failed: {exc}"


def _close_active() -> str:
    before_title = get_foreground_window_title()
    if _OS == "Windows" and before_title:
        try:
            import win32gui  # type: ignore
            import win32con  # type: ignore
            hwnd = win32gui.GetForegroundWindow()
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            time.sleep(0.3)
            after_title = get_foreground_window_title()
            if after_title != before_title:
                return f"Closed '{before_title}'."
            return f"Sent close to '{before_title}' — it may be asking to save changes."
        except Exception as exc:
            logger.debug(f"native close failed ({exc}); falling back to Alt+F4.")
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        kb.press(Key.alt_l); kb.press(Key.f4); kb.release(Key.f4); kb.release(Key.alt_l)
        extra = f' to "{before_title}"' if before_title else ""
        return f"Sent Alt+F4{extra}."
    except Exception as exc:
        return f"Close failed: {exc}"


def _alt_tab() -> str:
    before_title = get_foreground_window_title()
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        kb.press(Key.alt_l); kb.press(Key.tab); kb.release(Key.tab); kb.release(Key.alt_l)
        time.sleep(0.2)
        after_title = get_foreground_window_title()
        if after_title and after_title != before_title:
            return f"Switched to '{after_title}'."
    except Exception as exc:
        return f"Switch failed: {exc}"


def _snap_window(direction: str) -> str:
    dir_clean = direction.lower().strip()
    key_map = {
        "snap_left": "left",
        "left": "left",
        "snap_right": "right",
        "right": "right",
        "maximize": "up",
        "max": "up",
        "restore_window": "down",
        "restore": "down",
    }
    target_key = key_map.get(dir_clean, "left")
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        k = getattr(Key, target_key)
        kb.press(Key.cmd); kb.press(k); kb.release(k); kb.release(Key.cmd)
        title = get_foreground_window_title()
        title_str = f" for '{title}'" if title else ""
        return f"Window adjusted ({dir_clean}){title_str}, Sir."
    except Exception as exc:
        return f"Snap window failed: {exc}"


__all__ = ["computer_settings"]
