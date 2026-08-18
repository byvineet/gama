"""
actions/notification_manager.py — Gama Unified Notification Manager
====================================================================
Central hub for ALL of Gama's background notifications.  A single
voice command — "turn notifications on" — arms every source at once:

  Instagram (FBNS push)
      Persistent FBNS (Facebook Notification Service) MQTT connection.
      Delivers likes, comments, story replies, follow requests, and other
      activity pushes the moment Instagram's servers queue them — the same
      stream the mobile app uses for its notification badge.

  Instagram (Realtime MQTT Direct)
      Persistent Realtime MQTToT connection subscribed to Direct.
      Delivers incoming DM sync events as they arrive.

  System alerts
      Battery low, CPU critical, RAM critical, internet lost/restored.
      Sourced from the SystemMonitor's event-bus events so no duplicate
      polling thread is needed.

All notifications are surfaced as desktop pop-ups (via desktop_notify)
and logged so Gama can also speak them.

Account safety (Instagram)
--------------------------
* FBNS and Realtime MQTT are official Instagram mobile-client protocols.
  Using them is indistinguishable from keeping the Instagram app open on
  a phone.
* Sessions are REUSED — connections borrow the already-logged-in
  instagrapi Client.  A fresh login is never triggered here.
* Each connection lives 20–30 minutes (randomised), then is gracefully
  closed and re-opened, mirroring what happens during a cell-network
  hand-off.
* Keep-alive pings are sent every 60–90 s (jittered), well inside
  Instagram's MQTT keep-alive window.
* Failed reconnects use exponential back-off (2 min → 4 → 8 → … capped
  at 30 min) with ±30 s jitter to prevent simultaneous retry storms.
* A hard rate-cap refuses more than 10 reconnects per connection type
  per 60-minute window.

Author: Gama / Vineet Machchal
"""
from __future__ import annotations

from utils.logger import get_logger

import logging
import random
import threading
import time
from typing import Callable, Dict, List, Optional

log = get_logger(__name__)
# ── Tuning constants ──────────────────────────────────────────────────────────

# Session lifetime: reconnect after a random duration in this range (seconds).
# 20–30 min mirrors typical mobile-client session lengths.
_FBNS_SESSION_MIN  = 20 * 60
_FBNS_SESSION_MAX  = 30 * 60
_RT_SESSION_MIN    = 20 * 60
_RT_SESSION_MAX    = 30 * 60

# Keep-alive ping cadence. Instagram's MQTT keep-alive window is ~90 s;
# pinging every 60–90 s (jittered) keeps the socket alive without hammering.
_PING_BASE         = 65.0     # seconds between pings
_PING_JITTER       = 12.0     # actual = base + uniform(0, jitter)

# read_once() poll cadence — kept as low as is safe: this only paces how
# often we check the *already-open* MQTT socket for buffered data, it does
# not add extra requests to Instagram, so tightening it just cuts the worst-
# case delivery latency for a push/DM event that's already in flight.
_READ_POLL         =  0.25    # seconds
_READ_JITTER       =  0.15    # actual = base + uniform(0, jitter)

# Safety: never reconnect faster than this after a clean session end
_MIN_RECONNECT_GAP = 300.0    # 5 minutes

# Exponential back-off on consecutive failures
_BACKOFF_BASE      = 120.0    # first retry: 2 min
_BACKOFF_FACTOR    =   2.0
_BACKOFF_MAX       = 1800.0   # cap: 30 min
_BACKOFF_JITTER    =  30.0    # ±jitter applied to each back-off value

# Hard rate cap: at most this many reconnects per hour per watcher type
_MAX_RECONNECTS_PER_HOUR = 10

# System alert cooldowns (seconds) — independent of SystemMonitor's own cooldown
_SYS_COOLDOWN: Dict[str, float] = {
    "battery": 300.0,
    "cpu":     600.0,
    "ram":     600.0,
    "network": 300.0,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

class _ReconnectGuard:
    """
    Refuses more than _MAX_RECONNECTS_PER_HOUR reconnects per 60-minute
    sliding window.  Thread-safe.
    """

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._times: List[float] = []

    def allowed(self) -> bool:
        """Check AND consume one slot.  Returns False if the cap is hit."""
        now = time.monotonic()
        with self._lock:
            self._times = [t for t in self._times if now - t < 3600.0]
            if len(self._times) >= _MAX_RECONNECTS_PER_HOUR:
                return False
            self._times.append(now)
            return True

    def wait_until_allowed(self, stop: threading.Event) -> bool:
        """
        Block (in 30-second increments, honouring *stop*) until a slot is
        available.  Returns True when a slot was acquired, False if *stop*
        was set before one became available.
        """
        while not stop.is_set():
            if self.allowed():
                return True
            stop.wait(30.0)
        return False


class _SysAlertTracker:
    """Per-category cooldown tracker for system desktop notifications."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: Dict[str, float] = {}

    def can_alert(self, key: str) -> bool:
        """Return True (and reset the timer) if *key*'s cooldown has elapsed."""
        now = time.monotonic()
        cooldown = _SYS_COOLDOWN.get(key, 300.0)
        with self._lock:
            if now - self._last.get(key, 0.0) >= cooldown:
                self._last[key] = now
                return True
            return False


# ── FBNS persistent watcher ───────────────────────────────────────────────────

class _FBNSWatcher:
    """
    Keeps a persistent FBNS (Facebook Notification Service) MQTT connection
    open in a daemon thread.  Calls *on_notification(payload)* for every
    push event received.

    The watcher waits silently if Instagram is not yet logged in and starts
    delivering events as soon as a session is available.
    """

    def __init__(self, on_notification: Callable[[dict], None]) -> None:
        self._cb    = on_notification
        self._stop  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._guard = _ReconnectGuard()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gama-fbns-watcher"
        )
        self._thread.start()
        log.info("[notification] FBNS watcher started.")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        log.info("[notification] FBNS watcher stopped.")

    @staticmethod
    def _get_client():
        try:
            from actions.instagram_service import instagram_service
            return instagram_service._client if instagram_service._logged_in else None
        except Exception:
            return None

    def _loop(self) -> None:
        consecutive_failures = 0

        while not self._stop.is_set():
            # Wait for a logged-in Instagram session
            cl = self._get_client()
            if cl is None or not hasattr(cl, "fbns_connect"):
                self._stop.wait(30.0)
                continue

            # Rate-cap check (blocks until a slot is free or stop is set)
            if not self._guard.wait_until_allowed(self._stop):
                break

            fbns = None
            session_start    = time.monotonic()
            session_duration = random.uniform(_FBNS_SESSION_MIN, _FBNS_SESSION_MAX)
            next_ping_at     = time.monotonic() + _PING_BASE + random.uniform(0, _PING_JITTER)

            try:
                log.info("[notification] Opening FBNS connection…")

                def _on_push(payload: dict) -> None:
                    try:
                        self._cb(payload)
                    except Exception as exc:
                        log.debug(f"[notification] FBNS callback error: {exc}")

                cl.fbns_on("push", _on_push)
                fbns = cl.fbns_connect()
                consecutive_failures = 0
                log.info("[notification] FBNS connected.")

                while not self._stop.is_set():
                    # Planned session end → graceful reconnect
                    if time.monotonic() - session_start >= session_duration:
                        log.debug("[notification] FBNS session age limit reached; reconnecting.")
                        break

                    # Keep-alive ping
                    now = time.monotonic()
                    if now >= next_ping_at:
                        try:
                            fbns.ping()
                        except Exception as exc:
                            log.warning(f"[notification] FBNS ping failed: {exc}")
                            break
                        next_ping_at = time.monotonic() + _PING_BASE + random.uniform(0, _PING_JITTER)

                    # Drain incoming events (non-blocking)
                    try:
                        cl.fbns_read_once()
                    except Exception as exc:
                        log.warning(f"[notification] FBNS read_once failed: {exc}")
                        break

                    self._stop.wait(_READ_POLL + random.uniform(0, _READ_JITTER))

            except Exception as exc:
                consecutive_failures += 1
                raw_delay = _BACKOFF_BASE * (_BACKOFF_FACTOR ** (consecutive_failures - 1))
                delay = min(_BACKOFF_MAX, raw_delay) + random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
                delay = max(30.0, delay)
                log.warning(
                    f"[notification] FBNS connection failed "
                    f"(attempt {consecutive_failures}): {exc}. "
                    f"Retrying in {delay:.0f}s."
                )
                self._stop.wait(delay)
                continue

            finally:
                if fbns is not None:
                    try:
                        cl.fbns_disconnect()
                    except Exception:
                        pass

            # Clean session end — brief mandatory gap before reconnect
            gap = 30.0 + random.uniform(0, 30.0)
            self._stop.wait(gap)


# ── Realtime MQTT persistent watcher ─────────────────────────────────────────

class _RealtimeWatcher:
    """
    Keeps a persistent Realtime MQTT (MQTToT) connection open in a daemon
    thread, subscribed to Direct messages.  Calls *on_dm(payload)* for each
    DM sync event received.
    """

    def __init__(self, on_dm: Callable[[dict], None]) -> None:
        self._cb    = on_dm
        self._stop  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._guard = _ReconnectGuard()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gama-realtime-watcher"
        )
        self._thread.start()
        log.info("[notification] Realtime MQTT watcher started.")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        log.info("[notification] Realtime MQTT watcher stopped.")

    @staticmethod
    def _get_client():
        try:
            from actions.instagram_service import instagram_service
            return instagram_service._client if instagram_service._logged_in else None
        except Exception:
            return None

    def _loop(self) -> None:
        consecutive_failures = 0

        while not self._stop.is_set():
            cl = self._get_client()
            if cl is None or not hasattr(cl, "realtime_connect"):
                self._stop.wait(30.0)
                continue

            if not self._guard.wait_until_allowed(self._stop):
                break

            rt = None
            session_start    = time.monotonic()
            session_duration = random.uniform(_RT_SESSION_MIN, _RT_SESSION_MAX)
            next_ping_at     = time.monotonic() + _PING_BASE + random.uniform(0, _PING_JITTER)

            try:
                log.info("[notification] Opening Realtime MQTT connection…")

                def _on_message(payload: dict) -> None:
                    try:
                        self._cb(payload)
                    except Exception as exc:
                        log.debug(f"[notification] Realtime callback error: {exc}")

                cl.realtime_on("message", _on_message)
                rt = cl.realtime_connect()
                rt.direct_subscribe()
                consecutive_failures = 0
                log.info("[notification] Realtime MQTT connected.")

                while not self._stop.is_set():
                    if time.monotonic() - session_start >= session_duration:
                        log.debug("[notification] Realtime session age limit reached; reconnecting.")
                        break

                    now = time.monotonic()
                    if now >= next_ping_at:
                        try:
                            rt.ping()
                        except Exception as exc:
                            log.warning(f"[notification] Realtime ping failed: {exc}")
                            break
                        next_ping_at = time.monotonic() + _PING_BASE + random.uniform(0, _PING_JITTER)

                    try:
                        rt.read_once()
                    except Exception as exc:
                        log.warning(f"[notification] Realtime read_once failed: {exc}")
                        break

                    self._stop.wait(_READ_POLL + random.uniform(0, _READ_JITTER))

            except Exception as exc:
                consecutive_failures += 1
                raw_delay = _BACKOFF_BASE * (_BACKOFF_FACTOR ** (consecutive_failures - 1))
                delay = min(_BACKOFF_MAX, raw_delay) + random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
                delay = max(30.0, delay)
                log.warning(
                    f"[notification] Realtime MQTT failed "
                    f"(attempt {consecutive_failures}): {exc}. "
                    f"Retrying in {delay:.0f}s."
                )
                self._stop.wait(delay)
                continue

            finally:
                if rt is not None:
                    try:
                        cl.realtime_disconnect()
                    except Exception:
                        pass

            gap = 30.0 + random.uniform(0, 30.0)
            self._stop.wait(gap)


# ── Notification Manager ──────────────────────────────────────────────────────

class NotificationManager:
    """
    Singleton notification hub wired to all alert sources.

    Voice entry point:
        "turn notifications on"   → notification_manager("on")
        "turn notifications off"  → notification_manager("off")
        "notification status"     → notification_manager("status")
    """

    def __init__(self) -> None:
        self._enabled        = False
        self._lock           = threading.Lock()
        self._sys_tracker    = _SysAlertTracker()
        self._fbns_watcher:  Optional[_FBNSWatcher]    = None
        self._rt_watcher:    Optional[_RealtimeWatcher] = None
        self._bus_subscribed = False
        self._initialized    = False  # lazy boot: avoids hitting disk on import

    # ── Lazy init ─────────────────────────────────────────────────────────────

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        try:
            from state_engine.user_settings import get_notifications_enabled
            if get_notifications_enabled():
                self._start(persist=False)
        except Exception as exc:
            log.debug(f"[notification] Could not restore saved state: {exc}")

    # ── Internal start / stop ─────────────────────────────────────────────────

    def _start(self, persist: bool = True) -> None:
        with self._lock:
            self._enabled = True

            # Instagram watchers — always create; they wait for a session
            if self._fbns_watcher is None:
                self._fbns_watcher = _FBNSWatcher(self._on_fbns_push)
            self._fbns_watcher.start()

            if self._rt_watcher is None:
                self._rt_watcher = _RealtimeWatcher(self._on_realtime_dm)
            self._rt_watcher.start()

            # System event subscriptions
            if not self._bus_subscribed:
                try:
                    from state_engine.event_bus import event_bus
                    event_bus.subscribe("BatteryLow",          self._on_battery_low)
                    event_bus.subscribe("CPUHigh",             self._on_cpu_high)
                    event_bus.subscribe("RAMHigh",             self._on_ram_high)
                    self._bus_subscribed = True
                    log.info("[notification] Subscribed to system events.")
                except Exception as exc:
                    log.warning(f"[notification] Could not subscribe to event bus: {exc}")

        if persist:
            try:
                from state_engine.user_settings import set_notifications_enabled
                set_notifications_enabled(True)
            except Exception as exc:
                log.warning(f"[notification] Could not persist setting: {exc}")

        log.info("[notification] Notifications enabled.")

    def _stop(self, persist: bool = True) -> None:
        with self._lock:
            self._enabled = False
            if self._fbns_watcher:
                self._fbns_watcher.stop()
                self._fbns_watcher = None
            if self._rt_watcher:
                self._rt_watcher.stop()
                self._rt_watcher = None
            # Note: event_bus subscriptions are intentionally kept so the
            # callbacks fire immediately if notifications are re-enabled later
            # without needing to re-subscribe.

        if persist:
            try:
                from state_engine.user_settings import set_notifications_enabled
                set_notifications_enabled(False)
            except Exception as exc:
                log.warning(f"[notification] Could not persist setting: {exc}")

        log.info("[notification] Notifications disabled.")

    # ── Instagram event handlers ──────────────────────────────────────────────

    def _on_fbns_push(self, payload: dict) -> None:
        if not self._enabled:
            return
        try:
            from actions.instagram_service import _parse_fbns_payload
            text = _parse_fbns_payload(payload)
        except Exception:
            text = str(payload)[:120]
        if not text:
            return
        log.info(f"[notification] Instagram push: {text}")
        self._desktop("Instagram", text, kind=f"ig_push_{text[:20]}", cooldown=5.0)

    def _on_realtime_dm(self, payload: dict) -> None:
        if not self._enabled:
            return
        try:
            from actions.instagram_service import _parse_realtime_dm
            sender_raw = _parse_realtime_dm(payload)
        except Exception:
            sender_raw = ""
        if not sender_raw:
            return

        # sender_raw is usually a numeric user_id — resolve it to a username
        # and pull the actual message text so the alert is instant *and*
        # useful, including for senders Gama has never seen/cached before
        # (new users, or first-time Message Requests). This is a quick
        # best-effort lookup; if it fails for any reason we still alert with
        # whatever we have rather than staying silent.
        username, text = self._resolve_dm_preview(sender_raw)

        display_name = username or sender_raw
        message = f"{text}" if text else "sent you a message"
        log.info(f"[notification] Instagram DM from {display_name}: {message}")
        self._desktop(
            f"Instagram DM from {display_name}",
            message,
            kind=f"ig_dm_{display_name}",
            cooldown=5.0,
        )

        # Feed this straight into the unread cache so "did X message me?"
        # answers instantly even if "check unread messages" was never
        # explicitly run — the live push already told us.
        if username:
            try:
                from actions.instagram_service import instagram_service
                key = username.strip().lower()
                instagram_service._unread_cache_names.add(key)
                if text:
                    instagram_service._unread_cache_texts[key] = text
                if instagram_service._unread_cache_at == 0.0:
                    instagram_service._unread_cache_at = time.monotonic()
            except Exception:
                pass

    @staticmethod
    def _resolve_dm_preview(sender_id: str) -> "tuple[str, str]":
        """
        Best-effort, fast lookup of (username, latest_message_text) for a
        Realtime DM sync event that only carries a raw user_id. Runs
        synchronously on the watcher thread (not the event loop) so it's
        safe to block briefly here. Returns ("", "") on any failure —
        callers must handle that gracefully rather than staying silent.
        """
        try:
            from actions.instagram_service import instagram_service
            cl = instagram_service._client
            if cl is None:
                return "", ""

            username = ""
            try:
                username = cl.username_from_user_id(sender_id) or ""
            except Exception:
                pass

            text = ""
            try:
                thread = cl.direct_thread_by_participants([int(sender_id)])
                messages = getattr(thread, "messages", None) or []
                if messages:
                    text = (getattr(messages[0], "text", None) or "").strip() or "(media or sticker)"
            except Exception:
                pass

            return username, text
        except Exception as exc:
            log.debug(f"[notification] DM preview resolution failed: {exc}")
            return "", ""

    # ── System event handlers ─────────────────────────────────────────────────

    def _on_battery_low(self, **kwargs) -> None:
        if not self._enabled or not self._sys_tracker.can_alert("battery"):
            return
        pct = kwargs.get("percent", "")
        pct_str = f" at {pct:.0f}%" if isinstance(pct, (int, float)) else ""
        msg = f"Battery is low{pct_str}. Consider plugging in."
        log.info(f"[notification] System: {msg}")
        self._desktop("⚠ Battery Low", msg, kind="sys_battery", cooldown=300.0)

    def _on_cpu_high(self, **kwargs) -> None:
        if not self._enabled or not self._sys_tracker.can_alert("cpu"):
            return
        pct = kwargs.get("percent", "")
        pct_str = f" at {pct:.0f}%" if isinstance(pct, (int, float)) else ""
        msg = f"CPU usage is critically high{pct_str}."
        log.info(f"[notification] System: {msg}")
        self._desktop("⚠ High CPU Usage", msg, kind="sys_cpu", cooldown=600.0)

    def _on_ram_high(self, **kwargs) -> None:
        if not self._enabled or not self._sys_tracker.can_alert("ram"):
            return
        pct = kwargs.get("percent", "")
        pct_str = f" at {pct:.0f}%" if isinstance(pct, (int, float)) else ""
        msg = f"RAM usage is critically high{pct_str}. You may want to close some apps."
        log.info(f"[notification] System: {msg}")
        self._desktop("⚠ High RAM Usage", msg, kind="sys_ram", cooldown=600.0)

    def _on_network_lost(self, **kwargs) -> None:
        if not self._enabled or not self._sys_tracker.can_alert("network"):
            return
        log.info("[notification] System: Internet connection lost.")
        self._desktop("⚠ No Internet", "Internet connection lost.", kind="sys_net", cooldown=300.0)

    def _on_network_restored(self, **kwargs) -> None:
        if not self._enabled:
            return
        log.info("[notification] System: Internet connection restored.")
        self._desktop("✓ Internet Restored", "You're back online.", kind="sys_net_ok", cooldown=300.0)

    # ── Desktop notification helper ───────────────────────────────────────────

    @staticmethod
    def _desktop(title: str, message: str, kind: str, cooldown: float = 10.0) -> None:
        try:
            from actions.desktop_notify import notify
            notify(title, message, kind=kind, cooldown=cooldown, force=False)
        except Exception as exc:
            log.debug(f"[notification] desktop notify failed: {exc}")

    # ── Voice tool entrypoint ─────────────────────────────────────────────────

    def __call__(self, action: str = "status", **kwargs) -> str:
        self._ensure_initialized()
        action = (action or "status").lower().strip().replace(" ", "_")

        if action in ("on", "enable", "start", "turn_on"):
            if self._enabled:
                return "Notifications are already on."
            self._start()
            return (
                "Notifications are now on. I'll alert you the moment something "
                "arrives — Instagram likes, comments, DMs, and system events like "
                "battery low, high CPU or RAM, and internet drops — as both a "
                "desktop pop-up and a voice alert."
            )

        if action in ("off", "disable", "stop", "turn_off"):
            if not self._enabled:
                return "Notifications are already off."
            self._stop()
            return "Notifications turned off. All background alert streams stopped."

        if action in ("status", "info"):
            state = "on" if self._enabled else "off"
            streams: list[str] = []
            if self._fbns_watcher and self._fbns_watcher._thread and self._fbns_watcher._thread.is_alive():
                streams.append("Instagram push (FBNS)")
            if self._rt_watcher and self._rt_watcher._thread and self._rt_watcher._thread.is_alive():
                streams.append("Instagram DMs (Realtime)")
            stream_status = (
                f"Active streams: {', '.join(streams)}."
                if streams else "No Instagram streams active."
            )
            sys_status = (
                "System alerts (battery, CPU, RAM, network) subscribed."
                if self._bus_subscribed
                else "System alerts not yet subscribed."
            )
            return f"Notifications are {state}. {stream_status} {sys_status}"

        return (
            f"Unknown notification action '{action}'. "
            "Say: 'turn notifications on', 'turn notifications off', or "
            "'notification status'."
        )


# ── Module-level singleton ────────────────────────────────────────────────────
notification_manager = NotificationManager()

__all__ = ["notification_manager"]
