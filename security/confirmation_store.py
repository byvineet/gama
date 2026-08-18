"""
security/confirmation_store.py — Confirmation Code Storage
=============================================================
Lightweight, dependency-free storage for the single confirmation code
used to gate DESTRUCTIVE actions (see security/trust_levels.py).

The code is hashed at rest (PBKDF2-HMAC-SHA256) and never stored in the
clear. Previously this lived in guard/storage.py and was shared with the
now-removed Guard Mode feature; the storage format is unchanged so any
code set before this refactor keeps working.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

GAMA_DIR = Path.home() / ".gama"
CONFIG_FILE = GAMA_DIR / "confirmation_config.json"
GAMA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG: Dict[str, Any] = {
    "confirmation_code_hash": None,   # PBKDF2 hash, never the raw code
    "confirmation_code_salt": None,
}


def _load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def set_confirmation_code(code: str) -> None:
    cfg = _load_config()
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, 200_000)
    cfg["confirmation_code_hash"] = digest.hex()
    cfg["confirmation_code_salt"] = salt.hex()
    _save_config(cfg)


def verify_confirmation_code(code: str) -> bool:
    cfg = _load_config()
    h, s = cfg.get("confirmation_code_hash"), cfg.get("confirmation_code_salt")
    if not h or not s:
        return False
    salt = bytes.fromhex(s)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, 200_000)
    # Constant-time compare — a plain `==` on hex strings short-circuits on
    # the first mismatching character, which leaks timing information an
    # attacker could in principle use to narrow down the code byte-by-byte.
    # hmac.compare_digest runs in time independent of where the strings
    # first differ.
    return hmac.compare_digest(digest.hex(), h)


def has_confirmation_code() -> bool:
    cfg = _load_config()
    return bool(cfg.get("confirmation_code_hash"))


def change_confirmation_code(old_code: Optional[str], new_code: str,
                              owner_authenticated: bool = False) -> bool:
    """Change the confirmation code. Requires the *old* code to verify
    first, unless no code has been set yet (first-time setup), or
    `owner_authenticated` is True (caller already confirmed the current
    speaker via a live voiceprint match — never via a spoken claim of
    identity alone). Returns True if the code was changed."""
    if not new_code or len(new_code) < 4:
        return False
    if has_confirmation_code() and not owner_authenticated:
        if not old_code or not verify_confirmation_code(old_code):
            return False
    set_confirmation_code(new_code)
    return True


__all__ = [
    "set_confirmation_code", "verify_confirmation_code",
    "has_confirmation_code", "change_confirmation_code",
]
