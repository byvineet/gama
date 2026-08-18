"""
storage/user_profiles.py — Trusted User Profiles
==================================================
Sits above storage/embeddings.py and answers the question "which trusted
users exist, and do they have a voice profile enrolled?" without callers
needing to know about the embeddings store directly.

This is what makes "support multiple trusted users in the future" (the
spec's phrase) actually work today rather than being a TODO: every
enrollment is already keyed by name.

Author: Gama Security Upgrade
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from storage.embeddings import get_store
from utils.logger import get_logger

log = get_logger(__name__)

PROFILES_META = Path.home() / ".gama" / "biometrics" / "users.json"
PROFILES_META.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_OWNER = "vineet"


@dataclass
class TrustedUser:
    name: str
    has_voice: bool
    is_owner: bool
    created_at: str
    voice_samples: int = 0


def _load_meta() -> Dict[str, dict]:
    if not PROFILES_META.exists():
        return {}
    try:
        return json.loads(PROFILES_META.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: Dict[str, dict]) -> None:
    try:
        PROFILES_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as exc:
        log.error(f"Could not write user profile metadata: {exc}")


def register_user(name: str, is_owner: bool = False) -> None:
    """Ensure a metadata entry exists for `name` (idempotent)."""
    meta = _load_meta()
    if name not in meta:
        meta[name] = {
            "is_owner": is_owner,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_meta(meta)
    elif is_owner and not meta[name].get("is_owner"):
        meta[name]["is_owner"] = True
        _save_meta(meta)


def list_users() -> List[TrustedUser]:
    meta = _load_meta()
    voice_store = get_store("speaker")
    names = set(meta.keys()) | set(voice_store.all().keys())

    users = []
    for name in sorted(names):
        v = voice_store.get(name)
        info = meta.get(name, {})
        users.append(TrustedUser(
            name=name,
            has_voice=v is not None,
            is_owner=bool(info.get("is_owner", name == DEFAULT_OWNER)),
            created_at=info.get("created_at", v.created_at if v else ""),
            voice_samples=len(v.embeddings) if v else 0,
        ))
    return users


def get_user(name: str) -> Optional[TrustedUser]:
    for u in list_users():
        if u.name == name:
            return u
    return None


def is_trusted(name: str) -> bool:
    voice_store = get_store("speaker")
    return voice_store.exists(name)


def delete_user(name: str) -> str:
    voice_store = get_store("speaker")
    removed = []
    if voice_store.delete(name):
        removed.append("voice")
    meta = _load_meta()
    if name in meta:
        del meta[name]
        _save_meta(meta)
        removed.append("metadata")
    if not removed:
        return f"No profile found for '{name}'."
    return f"Removed {', '.join(removed)} data for '{name}'."


__all__ = [
    "TrustedUser", "register_user", "list_users", "get_user",
    "is_trusted", "delete_user", "DEFAULT_OWNER",
]
