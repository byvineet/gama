"""
actions/google_calendar_auth.py — Google Calendar OAuth (PKCE)
==================================================================
One-time login, then silent, indefinite reuse — deliberately mirrors
OAuth PKCE's exact pattern so it fits Gama's existing
credential-handling conventions instead of introducing a new one.

Flow
----
1. `login_interactive()` (run once via scripts/google_calendar_login.py
   or the 'connect Google Calendar' voice command):
   - Generates a PKCE code_verifier/code_challenge pair.
   - Opens the user's browser to Google's OAuth consent page.
   - Runs a short-lived local HTTP server on 127.0.0.1 to catch the
     redirect and read the `code` query param.
   - Exchanges the code for an access_token + refresh_token.
   - Stores ONLY the refresh_token, encrypted at rest via
     security/credential_store.py (same as Spotify).

2. `get_access_token()` (called by calendar_action.py before every API
   call): returns a cached in-memory access token if still valid,
   otherwise silently exchanges the stored refresh_token for a new
   one. No browser, no prompt, unless access has been revoked.

Setup (one-time, by you)
-------------------------
Google's OAuth "Desktop app" client type issues a client_secret, but
(per Google's own docs) it is not treated as confidential for
installed/desktop apps since it can't truly be kept secret in a
distributed binary — PKCE is the actual security boundary here, same
as Spotify's flow. Still stored encrypted at rest like any other key.

1. https://console.cloud.google.com/apis/credentials -> Create
   Credentials -> OAuth client ID -> Application type: Desktop app.
2. Enable the "Google Calendar API" for that project.
3. Copy the Client ID + Client Secret into config/api_keys.json as
   "google_client_id" / "google_client_secret" (or set
   GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars).
4. Run: python scripts/google_calendar_login.py

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qs

import requests

log = get_logger(__name__)
logger = log  # back-compat alias
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_PORT = 8900  # one above Spotify's 8899, so both can be configured together
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
# Non-sensitive, event-CRUD-only scope — deliberately not the broader
# 'calendar' scope, which would also grant calendar *creation/deletion*
# and sharing-settings access Gama has no use for.
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"

_REFRESH_TOKEN_NAME = "google_calendar_refresh_token"

_token_lock = threading.RLock()
_access_token: Optional[str] = None
_access_token_expires: float = 0.0


# ---------------------------------------------------------------------------
# Client credentials
# ---------------------------------------------------------------------------
def _config_path():
    from utils.paths import user_data_path
    return user_data_path("config/api_keys.json")


def client_id() -> str:
    env_val = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    if env_val:
        return env_val
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("google_client_id", "")).strip()
    except Exception:
        return ""


def client_secret() -> str:
    env_val = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if env_val:
        return env_val
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("google_client_secret", "")).strip()
    except Exception:
        return ""


def is_configured() -> bool:
    cid, secret = client_id(), client_secret()
    return bool(cid) and bool(secret) and "your" not in cid.lower()


# ---------------------------------------------------------------------------
# Refresh-token storage (encrypted — security/credential_store.py)
# ---------------------------------------------------------------------------
def _store_refresh_token(token: str) -> bool:
    try:
        from security.credential_store import set_secret, available
        if not available():
            logger.warning(
                "[GoogleCalendar] Secure credential storage unavailable on this "
                "system — refusing to persist the refresh token in plaintext. "
                "Login will be required again next run."
            )
            return False
        return set_secret(_REFRESH_TOKEN_NAME, token)
    except Exception:
        logger.debug("google_calendar_auth: failed to store refresh token", exc_info=True)
        return False


def _load_refresh_token() -> Optional[str]:
    try:
        from security.credential_store import get_secret
        return get_secret(_REFRESH_TOKEN_NAME)
    except Exception:
        return None


def _clear_refresh_token() -> None:
    try:
        from security.credential_store import delete_secret
        delete_secret(_REFRESH_TOKEN_NAME)
    except Exception:
        pass


def has_refresh_token() -> bool:
    return bool(_load_refresh_token())


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_pkce_pair() -> Tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))[:128]
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# Local loopback redirect capture
# ---------------------------------------------------------------------------
class _CallbackResult:
    def __init__(self):
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        self.state: Optional[str] = None
        self.done = threading.Event()


def _make_handler(result: _CallbackResult, expected_state: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            result.state = (qs.get("state") or [""])[0]
            result.code = (qs.get("code") or [None])[0]
            result.error = (qs.get("error") or [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result.code and result.state == expected_state:
                body = "<html><body><h3>Gama is connected to Google Calendar.</h3>You can close this tab.</body></html>"
            else:
                body = "<html><body><h3>Google Calendar login failed or was cancelled.</h3>You can close this tab.</body></html>"
            self.wfile.write(body.encode("utf-8"))
            result.done.set()

        def log_message(self, fmt, *args):  # silence default stderr logging
            logger.debug("google_calendar_auth: callback %s", fmt % args)

    return Handler


def login_interactive(timeout: float = 180.0, open_browser: bool = True) -> Tuple[bool, str]:
    """Blocking, synchronous, one-time login. Intended to be run
    explicitly (a setup script or an explicit 'connect Google Calendar'
    voice command) — never called automatically, so a normal calendar
    query never pops a browser window."""
    if not is_configured():
        return False, (
            "No Google Client ID/Secret configured. Create an OAuth Desktop "
            "app client at https://console.cloud.google.com/apis/credentials, "
            "enable the Google Calendar API, add redirect URI "
            f"{REDIRECT_URI}, then put the values in config/api_keys.json as "
            "'google_client_id' and 'google_client_secret'."
        )

    verifier, challenge = _new_pkce_pair()
    state = _b64url(secrets.token_bytes(16))

    params = {
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": SCOPES,
        # Ensures a refresh_token is actually returned even on a
        # re-consent (Google only issues one on the *first* consent
        # per client/scope combo unless this is set).
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"

    result = _CallbackResult()
    try:
        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _make_handler(result, state))
    except OSError as exc:
        return False, f"Could not start local login server on port {REDIRECT_PORT}: {exc}"

    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    server_thread.start()

    if open_browser:
        webbrowser.open(auth_url)
    logger.info("[GoogleCalendar] Opened browser for one-time Google Calendar login")

    got = result.done.wait(timeout=timeout)
    server.shutdown()
    server.server_close()

    if not got:
        return False, "Timed out waiting for Google login in the browser."
    if result.error:
        return False, f"Google login was denied: {result.error}"
    if not result.code or result.state != state:
        return False, "Google login callback was invalid (missing code or state mismatch)."

    return _exchange_code(result.code, verifier)


def _exchange_code(code: str, verifier: str) -> Tuple[bool, str]:
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id(),
            "client_secret": client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }, timeout=10)
    except Exception as exc:
        return False, f"Network error exchanging Google login code: {exc}"

    if resp.status_code != 200:
        return False, f"Google rejected the login code (HTTP {resp.status_code}): {resp.text[:200]}"

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))
    if not refresh_token:
        return False, ("Google did not return a refresh token — this usually means Gama "
                        "was already authorized before without 'prompt=consent'. Revoke "
                        "access at https://myaccount.google.com/permissions and try again.")

    stored = _store_refresh_token(refresh_token)
    with _token_lock:
        global _access_token, _access_token_expires
        _access_token = access_token
        _access_token_expires = time.time() + expires_in - 60

    if not stored:
        return True, ("Logged in for this session, but the refresh token could not be "
                       "stored securely — you'll need to log in again next run.")
    return True, "Google Calendar connected. Gama will not need to log in again unless you revoke access."


# ---------------------------------------------------------------------------
# Silent token refresh — the path every calendar request actually takes
# ---------------------------------------------------------------------------
def _refresh_sync() -> Optional[str]:
    refresh_token = _load_refresh_token()
    if not refresh_token:
        return None
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id(),
            "client_secret": client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=8)
    except Exception:
        logger.debug("google_calendar_auth: token refresh network error", exc_info=True)
        return None

    if resp.status_code == 400:
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") in ("invalid_grant", "invalid_request"):
            logger.warning("[GoogleCalendar] Refresh token was revoked — clearing stored "
                            "credential; re-run the Google Calendar login to reconnect.")
            _clear_refresh_token()
        return None

    if resp.status_code != 200:
        logger.debug(f"google_calendar_auth: token refresh failed (HTTP {resp.status_code})")
        return None

    payload = resp.json()
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))

    with _token_lock:
        global _access_token, _access_token_expires
        _access_token = access_token
        _access_token_expires = time.time() + expires_in - 60
    return access_token


def get_access_token_sync() -> Optional[str]:
    """Sync version — returns a valid access token or None if not
    configured / not authenticated / the refresh failed. Callers must
    treat None as 'calendar unavailable', never as a hard error."""
    if not is_configured():
        return None
    with _token_lock:
        if _access_token and time.time() < _access_token_expires:
            return _access_token
    return _refresh_sync()


async def get_access_token() -> Optional[str]:
    """Async, non-blocking (offloaded to a thread) — mirrors
    OAuth PKCE's async wrapper for callers on the event loop."""
    import asyncio
    if not is_configured():
        return None
    with _token_lock:
        if _access_token and time.time() < _access_token_expires:
            return _access_token
    return await asyncio.to_thread(_refresh_sync)


__all__ = [
    "REDIRECT_URI", "is_configured", "has_refresh_token", "login_interactive",
    "get_access_token", "get_access_token_sync",
]
