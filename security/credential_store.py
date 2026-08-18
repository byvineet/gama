"""
security/credential_store.py — Secure Credential Storage
==========================================================
Gama previously kept live API keys in plain text in
`config/api_keys.json` — readable by any process or person with file
access, and an easy accidental leak (e.g. if that folder is ever
zipped up and shared). This module is the fix: secrets are encrypted
at rest, bound to *this Windows user account*, and every access is
audit-logged.

Encryption strategy
--------------------
* On Windows (the only platform Gama runs on): Windows DPAPI via
  `pywin32`'s `win32crypt.CryptProtectData` / `CryptUnprotectData`.
  DPAPI ties the ciphertext to the logged-in Windows user — there is
  no separate passphrase to remember or lose, and the encrypted blob
  is useless to anyone who copies the file without also being logged
  in as this Windows user on this machine. This is the same mechanism
  Chrome/Edge use for saved passwords.
* Anywhere DPAPI isn't available (non-Windows dev/test environment,
  or `pywin32` missing): falls back to a `cryptography.Fernet` key
  held in a separate file with owner-only permissions
  (`credentials.key`, chmod 600). Not as strong as DPAPI (the key
  lives next to the data), but still far better than plaintext, and
  keeps this module importable/testable off Windows.

Storage layout (all under the existing `~/.gama/security/` dir used
by the rest of security/):
    credentials.enc   — JSON: {"name": "<base64 ciphertext>", ...}
    credentials.key   — Fernet key, fallback path only, 0600 perms

Nothing here ever logs a secret's *value* — only its name and whether
the operation succeeded, via security/security_logging.py's existing
audit trail.

Author : Gama Security Upgrade
"""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

SECURITY_DIR = Path.home() / ".gama" / "security"
SECURITY_DIR.mkdir(parents=True, exist_ok=True)
CRED_PATH = SECURITY_DIR / "credentials.enc"
FALLBACK_KEY_PATH = SECURITY_DIR / "credentials.key"

_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
_backend = None  # "dpapi" | "fernet" | None (unavailable — store fails closed)


def _try_dpapi():
    try:
        import win32crypt  # type: ignore
        return win32crypt
    except Exception:
        return None


def _try_fernet():
    try:
        from cryptography.fernet import Fernet
        return Fernet
    except Exception:
        return None


_win32crypt = _try_dpapi()
_Fernet = _try_fernet()

if _win32crypt is not None:
    _backend = "dpapi"
elif _Fernet is not None:
    _backend = "fernet"
else:
    _backend = None
    log.warning(
        "credential_store: neither pywin32 (DPAPI) nor cryptography (Fernet) "
        "is available — secure credential storage is disabled."
    )


def _get_fernet():
    """Load (or create, on first use) the fallback Fernet key with
    owner-only file permissions."""
    if not FALLBACK_KEY_PATH.exists():
        key = _Fernet.generate_key()
        FALLBACK_KEY_PATH.write_bytes(key)
        try:
            os.chmod(FALLBACK_KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except Exception:
            log.debug("credential_store: could not chmod fallback key file", exc_info=True)
    else:
        key = FALLBACK_KEY_PATH.read_bytes()
    return _Fernet(key)


def _encrypt(plaintext: str) -> str:
    data = plaintext.encode("utf-8")
    if _backend == "dpapi":
        blob = _win32crypt.CryptProtectData(data, "gama-credential", None, None, None, 0)
        return base64.b64encode(blob).decode("ascii")
    elif _backend == "fernet":
        return _get_fernet().encrypt(data).decode("ascii")
    raise RuntimeError("No secure storage backend available.")


def _decrypt(ciphertext: str) -> str:
    if _backend == "dpapi":
        raw = base64.b64decode(ciphertext.encode("ascii"))
        _, decrypted = _win32crypt.CryptUnprotectData(raw, None, None, None, 0)
        return decrypted.decode("utf-8")
    elif _backend == "fernet":
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    raise RuntimeError("No secure storage backend available.")


# ---------------------------------------------------------------------------
# Store I/O
# ---------------------------------------------------------------------------
def _read_store() -> dict:
    if not CRED_PATH.exists():
        return {}
    try:
        return json.loads(CRED_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.error("credential_store: credentials.enc is unreadable/corrupt — treating as empty.")
        return {}


def _write_store(data: dict) -> None:
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CRED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, CRED_PATH)
    try:
        os.chmod(CRED_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
    except Exception:
        pass


def _audit(action: str, name: str, ok: bool) -> None:
    try:
        from security.security_logging import get_auditor
        get_auditor().log_system_event(
            event_type="credential_access",
            description=f"credential {action}: {name}",
            details={"action": action, "name": name, "ok": ok, "backend": _backend or "none"},
        )
    except Exception:
        log.debug("credential_store: audit log write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def available() -> bool:
    return _backend is not None


def set_secret(name: str, value: str) -> bool:
    """Encrypt and store `value` under `name`. Overwrites any existing
    secret with the same name."""
    name = (name or "").strip()
    if not name or not available():
        _audit("set", name, False)
        return False
    with _lock:
        try:
            store = _read_store()
            store[name] = _encrypt(value)
            _write_store(store)
            _audit("set", name, True)
            return True
        except Exception:
            log.error(f"credential_store: failed to store secret '{name}'", exc_info=True)
            _audit("set", name, False)
            return False


def get_secret(name: str) -> Optional[str]:
    """Return the decrypted secret, or None if it doesn't exist / can't
    be decrypted (e.g. the store was copied to a different machine or
    a different Windows user — DPAPI will correctly refuse to decrypt
    it, which is the intended behaviour, not a bug)."""
    name = (name or "").strip()
    if not name or not available():
        return None
    with _lock:
        store = _read_store()
        blob = store.get(name)
        if blob is None:
            return None
        try:
            value = _decrypt(blob)
            _audit("get", name, True)
            return value
        except Exception:
            log.error(f"credential_store: failed to decrypt secret '{name}'", exc_info=True)
            _audit("get", name, False)
            return None


def delete_secret(name: str) -> bool:
    name = (name or "").strip()
    with _lock:
        store = _read_store()
        if name not in store:
            return False
        del store[name]
        _write_store(store)
        _audit("delete", name, True)
        return True


def list_secret_names() -> list[str]:
    """Names only — never values."""
    with _lock:
        return sorted(_read_store().keys())


# ---------------------------------------------------------------------------
# One-time migration off the old plaintext config/api_keys.json
# ---------------------------------------------------------------------------
_MIGRATE_KEY_SUFFIXES = ()
_PLACEHOLDER_VALUES = {}


def migrate_plaintext_config(config_path: Path) -> list[str]:
    """Move anything that looks like a credential out of `config_path`
    (a plaintext JSON file) into the encrypted store, then rewrite that
    file with those fields blanked out. Idempotent — safe to call on
    every startup; only actually touches the file the first time real
    secret values are found there. Returns the list of field names
    migrated (never the values)."""
    if not available() or not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    migrated = []
    changed = False
    for field, value in list(data.items()):
        if not isinstance(value, str):
            continue
        if not field.lower().endswith(_MIGRATE_KEY_SUFFIXES):
            continue
        if value.strip().lower() in _PLACEHOLDER_VALUES:
            continue
        if set_secret(field, value):
            data[field] = ""  # scrub plaintext copy
            migrated.append(field)
            changed = True

    if changed:
        try:
            tmp = config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, config_path)
            log.info(f"credential_store: migrated {migrated} out of {config_path} into "
                      f"encrypted storage ({_backend}).")
        except Exception:
            log.error("credential_store: migrated to encrypted store but failed to "
                       "scrub the plaintext file — do it manually.", exc_info=True)
    return migrated


__all__ = [
    "available", "set_secret", "get_secret", "delete_secret",
    "list_secret_names", "migrate_plaintext_config",
]
