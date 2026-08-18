"""
security/trusted_session.py — Trusted Session System
=======================================================
Implements spec section 3: verify the owner once (at wake), then skip
re-verification for SENSITIVE-tier commands for a short window instead
of re-running biometric checks on every single command.

Mapping onto the existing 4-tier trust model (security/trust_levels.py)
-------------------------------------------------------------------------
    SAFE / NORMAL   — never needed verification anyway; untouched.
    SENSITIVE       — spec's "MEDIUM": a valid trusted session is enough,
                       no fresh voice check required.
    DESTRUCTIVE     — spec's "HIGH": ALWAYS requires a fresh, live
                       voice+face+verbal check regardless of session.
                       A trusted session never shortcuts this tier —
                       that's a deliberate, non-negotiable choice, not
                       an oversight. See verification_pipeline.py.

Session lifecycle
------------------
Created:    once speaker verification succeeds at wake
            (main.py: GamaAssistant._verify_owner_after_wake).
Duration:   10-15 seconds (SESSION_TTL_SECONDS), sliding-window style —
            each SENSITIVE-tier command that uses the session refreshes
            its expiry, so a rapid follow-up ("...and also delete that
            other one") doesn't re-trigger a fresh voice check, but the
            window is short enough that it can never be mistaken for a
            standing login.
Invalidated on:
    - expiry (checked lazily on every `is_valid()` call — no timer needed)
    - GAMA going back to sleep (main.py flips self._awake False)
    - an unverified speaker interrupts while GAMA is talking
      (main.py._maybe_owner_interrupt)
    - the Windows workstation locking (best-effort poller below; no-op
      on non-Windows platforms)
    - a failed voice/verbal/confirmation-code check for a
      SENSITIVE/DESTRUCTIVE action (security/security_manager.py)
    - explicit call to invalidate()/cancel_session(), e.g. a "log me
      out" / user-switch / manual-cancel hook

This module is intentionally storage-free (in-memory only, per process)
— a trusted session should never survive a restart or be persisted to
disk, since that would turn a short-lived convenience into a standing
credential.
"""

from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

# 5-minute sliding window: long enough to cover a full natural
# conversation without re-verifying on every SENSITIVE command,
# short enough that an unattended session doesn't stay open all day.
# Each SENSITIVE command that uses the session touches (extends) it,
# so rapid follow-ups never re-trigger a voice check mid-conversation.
# The window resets to zero on sleep, workstation-lock, or failed auth.
SESSION_TTL_SECONDS = 300.0

# Destructive session is permanently disabled (TTL = 0) — every DESTRUCTIVE
# action must pass the full voice+verbal+code pipeline every time.
# Do NOT raise this value; the shortcut has been intentionally removed.
DESTRUCTIVE_TTL_SECONDS = 0.0


@dataclass
class TrustedSession:
    speaker: str
    confidence: float
    created_at: float
    expires_at: float

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at

    def touch(self, ttl: float = SESSION_TTL_SECONDS) -> None:
        """Sliding-window refresh: extend expiry on active use."""
        self.expires_at = time.monotonic() + ttl


_lock = threading.Lock()
_session: Optional[TrustedSession] = None

# Separate lock + slot for the destructive-action trusted session.
_destructive_lock = threading.Lock()
_destructive_session: Optional[TrustedSession] = None


def create_session(speaker: str, confidence: float, ttl: float = SESSION_TTL_SECONDS) -> TrustedSession:
    global _session
    with _lock:
        now = time.monotonic()
        _session = TrustedSession(speaker=speaker, confidence=confidence,
                                   created_at=now, expires_at=now + ttl)
        log.info(f"Trusted session created for '{speaker}' (confidence {confidence:.2f}, "
                 f"expires in {ttl:.0f}s).")
        return _session


def get_session() -> Optional[TrustedSession]:
    """Returns the current session if it exists and hasn't expired, else
    None (and clears the stale reference so callers never see it again)."""
    with _lock:
        global _session
        if _session is None:
            return None
        if not _session.is_valid():
            log.info(f"Trusted session for '{_session.speaker}' expired.")
            _session = None
            return None
        return _session


def touch_session(ttl: float = SESSION_TTL_SECONDS) -> None:
    with _lock:
        if _session is not None and _session.is_valid():
            _session.touch(ttl)


def invalidate(reason: str = "") -> None:
    global _session
    with _lock:
        if _session is not None:
            log.info(f"Trusted session for '{_session.speaker}' invalidated"
                      + (f" ({reason})." if reason else "."))
            _session = None
    # Also invalidate destructive session on any global invalidation
    # (sleep, lock, failed auth) — if the base session is gone, the
    # elevated one certainly should be too.
    invalidate_destructive(reason)


def cancel_session(reason: str = "manual cancellation") -> None:
    """Explicit alias of invalidate() for call sites triggered by the
    user directly (e.g. 'log me out', switching users, or an explicit
    'cancel' during a pending sensitive action) — same behavior, clearer
    call-site intent."""
    invalidate(reason)


def create_destructive_session(speaker: str, confidence: float,
                               ttl: float = DESTRUCTIVE_TTL_SECONDS) -> "TrustedSession":
    """Create a short-lived session that skips re-verification for the
    DESTRUCTIVE tier. Called after a successful voice+verbal destructive
    check so rapid follow-up destructive commands (within `ttl` seconds)
    don't force the owner to re-speak the verbal confirmation each time.

    Completely separate from the normal SESSION_TTL_SECONDS session —
    a destructive session is only checked for DESTRUCTIVE-tier calls."""
    global _destructive_session
    if ttl <= 0:
        return None  # type: ignore[return-value]
    with _destructive_lock:
        now = time.monotonic()
        _destructive_session = TrustedSession(
            speaker=speaker, confidence=confidence,
            created_at=now, expires_at=now + ttl,
        )
        log.info(
            f"Destructive trusted session created for '{speaker}' "
            f"(conf {confidence:.2f}, expires in {ttl:.0f}s)."
        )
        return _destructive_session


def get_destructive_session() -> Optional[TrustedSession]:
    """Return the active destructive session or None if expired/absent."""
    global _destructive_session
    with _destructive_lock:
        if _destructive_session is None:
            return None
        if not _destructive_session.is_valid():
            log.info(f"Destructive trusted session for '{_destructive_session.speaker}' expired.")
            _destructive_session = None
            return None
        return _destructive_session


def touch_destructive_session(ttl: float = DESTRUCTIVE_TTL_SECONDS) -> None:
    """Sliding-window refresh for destructive session."""
    with _destructive_lock:
        if _destructive_session is not None and _destructive_session.is_valid():
            _destructive_session.touch(ttl)


def invalidate_destructive(reason: str = "") -> None:
    """Invalidate the destructive trusted session (e.g. on sleep or lock)."""
    global _destructive_session
    with _destructive_lock:
        if _destructive_session is not None:
            log.info(
                f"Destructive trusted session for '{_destructive_session.speaker}' invalidated"
                + (f" ({reason})." if reason else ".")
            )
            _destructive_session = None


def is_destructive_trusted(expected_name: Optional[str] = None) -> bool:
    """True if there's a valid destructive session for the owner.
    Called from security_manager.authorize() before running the full
    voice+verbal pipeline again for DESTRUCTIVE-tier actions."""
    session = get_destructive_session()
    if session is None:
        return False
    if expected_name is not None and session.speaker != expected_name:
        return False
    return True


def is_owner_trusted(expected_name: Optional[str] = None) -> bool:
    """True if there's a currently-valid session for (optionally) a
    specific expected speaker name. Does NOT refresh the session —
    callers that actually use this to authorize a command should call
    touch_session() themselves so only real usage extends the window."""
    session = get_session()
    if session is None:
        return False
    if expected_name is not None and session.speaker != expected_name:
        return False
    return True


# ---------------------------------------------------------------------
# Best-effort Windows workstation-lock watcher
# ---------------------------------------------------------------------
# Uses the classic OpenInputDesktop() trick: it fails/returns NULL while
# the workstation is locked, no message-loop/window-handle registration
# needed. Polling is cheap (a couple hundred microseconds) and runs at a
# low frequency, so this is safe to leave running for the process
# lifetime. No-op (never starts) on non-Windows platforms.

_watcher_started = False


def start_lock_watcher(poll_interval_s: float = 3.0) -> None:
    global _watcher_started
    if _watcher_started or platform.system() != "Windows":
        return
    _watcher_started = True

    def _poll_loop():
        import ctypes
        was_locked = False
        while True:
            try:
                desktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
                locked = (desktop == 0)
                if desktop:
                    ctypes.windll.user32.CloseDesktop(desktop)
                if locked and not was_locked:
                    invalidate("workstation locked")
                was_locked = locked
            except Exception as exc:
                log.debug(f"Lock watcher poll failed (non-fatal): {exc}")
            time.sleep(poll_interval_s)

    threading.Thread(target=_poll_loop, daemon=True, name="gama-lock-watcher").start()
    log.info("Trusted-session lock watcher started.")


__all__ = [
    "TrustedSession",
    "SESSION_TTL_SECONDS", "DESTRUCTIVE_TTL_SECONDS",
    "create_session", "get_session", "touch_session",
    "invalidate", "cancel_session", "is_owner_trusted",
    "create_destructive_session", "get_destructive_session",
    "touch_destructive_session", "invalidate_destructive", "is_destructive_trusted",
    "start_lock_watcher",
]
