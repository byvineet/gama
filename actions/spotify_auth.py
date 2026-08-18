"""
actions/spotify_auth.py — Spotify Web API Authentication (OAuth PKCE)
=========================================================================
One-time login, then silent, indefinite reuse. Gama authenticates to
the Spotify Web API using the Authorization Code + PKCE flow — no
client secret to leak (Spotify's recommended flow for desktop/native
apps), and no browser-cookie or password handling on Gama's side at
all.

Flow
----
1. `login_interactive()` (run once, e.g. `python scripts/spotify_login.py`):
   - Generates a PKCE code_verifier/code_challenge pair.
   - Opens the user's browser to Spotify's /authorize page.
   - Runs a short-lived local HTTP server on 127.0.0.1 to catch the
     redirect and read the `code` query param — nothing is ever typed
     into Gama or stored in chat/voice history.
   - Exchanges the code for an access_token + refresh_token.
   - Stores ONLY the refresh_token, encrypted at rest via
     security/credential_store.py (Windows DPAPI, bound to this
     Windows user; Fernet fallback elsewhere). The access token is
     never persisted — it's short-lived and cheap to re-derive.

2. `get_access_token()` (called by spotify_web.py before every API
   call): returns a cached in-memory access token if it's still
   valid, otherwise silently exchanges the stored refresh_token for a
   new one. No browser, no prompt — "never require login again unless
   revoked", exactly as specced. If the refresh token itself has been
   revoked (Spotify returns invalid_grant), the stored token is
   cleared and this returns None so callers fall through to the next
   priority instead of failing loudly.

Nothing in this file ever logs a token value — only whether an
operation succeeded, matching the rest of Gama's credential handling.

Author : Gama Spotify Hybrid Integration
"""

from __future__ import annotations

from utils.logger import get_logger

import asyncio
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
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_PORT = 8899
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
# No scope requires any user-sensitive access — Search/Get Track are
# public-catalog endpoints. Kept minimal on purpose.
SCOPES = ""

_REFRESH_TOKEN_NAME = "spotify_refresh_token"

_token_lock = threading.RLock()
_access_token: Optional[str] = None
_access_token_expires: float = 0.0


# ---------------------------------------------------------------------------
# Client ID (public — not a secret, but still user-configurable)
# ---------------------------------------------------------------------------

def _config_path():
    from utils.paths import user_data_path
    return user_data_path("config/api_keys.json")


def client_id() -> str:
    env_val = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if env_val:
        return env_val
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("spotify_client_id", "")).strip()
    except Exception:
        return ""


def is_configured() -> bool:
    cid = client_id()
    return bool(cid) and "your" not in cid.lower()


# ---------------------------------------------------------------------------
# Refresh-token storage (encrypted — security/credential_store.py)
# ---------------------------------------------------------------------------

def _store_refresh_token(token: str) -> bool:
    try:
        from security.credential_store import set_secret, available
        if not available():
            logger.warning(
                "[Spotify] Secure credential storage unavailable on this system — "
                "refusing to persist the refresh token in plaintext. Login will be "
                "required again next run."
            )
            return False
        return set_secret(_REFRESH_TOKEN_NAME, token)
    except Exception:
        logger.debug("spotify_auth: failed to store refresh token", exc_info=True)
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
                body = "<html><body><h3>Gama is connected to Spotify.</h3>You can close this tab.</body></html>"
            else:
                body = "<html><body><h3>Spotify login failed or was cancelled.</h3>You can close this tab.</body></html>"
            self.wfile.write(body.encode("utf-8"))
            result.done.set()

        def log_message(self, fmt, *args):  # silence default stderr logging
            logger.debug("spotify_auth: callback %s", fmt % args)

    return Handler


def login_interactive(timeout: float = 180.0, open_browser: bool = True) -> Tuple[bool, str]:
    """Blocking, synchronous, one-time login. Intended to be run
    explicitly (a setup script or an explicit 'connect Spotify' voice
    command) — never called automatically from the play path, so a
    normal 'play <song>' request never pops a browser window."""
    if not is_configured():
        return False, ("No Spotify Client ID configured. Set 'spotify_client_id' in "
                        "config/api_keys.json (create a free app at "
                        "https://developer.spotify.com/dashboard, add redirect URI "
                        f"{REDIRECT_URI}).")

    verifier, challenge = _new_pkce_pair()
    state = _b64url(secrets.token_bytes(16))

    params = {
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }
    if SCOPES:
        params["scope"] = SCOPES
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
    logger.info("[Spotify] Opened browser for one-time Spotify login")

    got = result.done.wait(timeout=timeout)
    server.shutdown()
    server.server_close()

    if not got:
        return False, "Timed out waiting for Spotify login in the browser."
    if result.error:
        return False, f"Spotify login was denied: {result.error}"
    if not result.code or result.state != state:
        return False, "Spotify login callback was invalid (missing code or state mismatch)."

    ok, msg = _exchange_code(result.code, verifier)
    return ok, msg


def _exchange_code(code: str, verifier: str) -> Tuple[bool, str]:
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }, timeout=10)
    except Exception as exc:
        return False, f"Network error exchanging Spotify login code: {exc}"

    if resp.status_code != 200:
        return False, f"Spotify rejected the login code (HTTP {resp.status_code})."

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))
    if not refresh_token:
        return False, "Spotify did not return a refresh token."

    stored = _store_refresh_token(refresh_token)
    with _token_lock:
        global _access_token, _access_token_expires
        _access_token = access_token
        _access_token_expires = time.time() + expires_in - 60

    if not stored:
        return True, ("Logged in for this session, but the refresh token could not be "
                       "stored securely — you'll need to log in again next run.")
    return True, "Spotify account connected. Gama will not need to log in again unless you revoke access."


# ---------------------------------------------------------------------------
# Silent token refresh — the path every play request actually takes
# ---------------------------------------------------------------------------

def _refresh_sync() -> Optional[str]:
    refresh_token = _load_refresh_token()
    if not refresh_token:
        return None
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=8)
    except Exception:
        logger.debug("spotify_auth: token refresh network error", exc_info=True)
        return None

    if resp.status_code == 400:
        # invalid_grant -> refresh token was revoked/expired at Spotify's end
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") == "invalid_grant":
            logger.warning("[Spotify] Refresh token was revoked — clearing stored credential; "
                            "re-run the Spotify login to reconnect.")
            _clear_refresh_token()
        return None

    if resp.status_code != 200:
        logger.debug(f"spotify_auth: token refresh failed (HTTP {resp.status_code})")
        return None

    payload = resp.json()
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))
    # Spotify sometimes rotates the refresh token on renewal — persist
    # the new one if provided, otherwise keep the existing one.
    new_refresh = payload.get("refresh_token")
    if new_refresh:
        _store_refresh_token(new_refresh)

    with _token_lock:
        global _access_token, _access_token_expires
        _access_token = access_token
        _access_token_expires = time.time() + expires_in - 60
    return access_token


async def get_access_token() -> Optional[str]:
    """Async, non-blocking (offloaded to a thread) — returns a valid
    access token or None if not configured / not authenticated / the
    refresh failed. Callers must treat None as 'skip to the next
    priority', never as an error to surface to the user."""
    if not is_configured():
        return None
    with _token_lock:
        if _access_token and time.time() < _access_token_expires:
            return _access_token
    return await asyncio.to_thread(_refresh_sync)


def is_authenticated() -> bool:
    return is_configured() and has_refresh_token()


def invalidate_access_token() -> None:
    """Drop the cached in-memory access token so the next call to
    get_access_token() forces a fresh refresh — used when the Web API
    itself reports 401 despite our cache believing the token is
    still valid (clock skew, server-side early revocation, etc.)."""
    with _token_lock:
        global _access_token, _access_token_expires
        _access_token = None
        _access_token_expires = 0.0


__all__ = [
    "is_configured", "is_authenticated", "has_refresh_token",
    "login_interactive", "get_access_token", "invalidate_access_token",
    "REDIRECT_URI",
]
