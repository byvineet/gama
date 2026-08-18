"""
core/config_manager.py — Centralised configuration with per-backend validation.

Design
------
Online (Gemini) validation and offline (llama.cpp) validation are
**completely independent** — a missing Gemini key never blocks offline mode,
and a missing llama model never blocks online mode.

Usage
-----
    from core.config_manager import config

    key = config.gemini_key()          # "" if not set
    ok, err = config.validate_gemini() # (True, "") or (False, "human error")
    ok, err = config.validate_llama()  # (True, "") or (False, "human error")
    path = config.llama_model_path()   # Path or None
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("gama.config_manager")

# GGUF magic bytes (first 4 bytes of any valid GGUF model file)
_GGUF_MAGIC = b"GGUF"


class ConfigManager:
    """Thread-safe, cached configuration manager.

    Loads ``config/api_keys.json`` on first access and caches the result.
    Call :meth:`reload` to force a fresh load (e.g. after the user edits
    the file at runtime).
    """

    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._lock = threading.RLock()
        self._data: Optional[dict] = None

    # ── Load / reload ────────────────────────────────────────────────────────

    def load(self) -> dict:
        """Return the cached config dict, loading from disk if needed."""
        with self._lock:
            if self._data is None:
                self._data = self._read()
                self._migrate_secrets_once()
            return dict(self._data)

    def _migrate_secrets_once(self) -> None:
        """Move any plaintext *_api_key/*_token/*_secret fields in this
        config file into the encrypted credential store (security/
        credential_store.py), on first load only. Safe no-op if secure
        storage isn't available (e.g. running off Windows in dev) or if
        there's nothing left to migrate — see credential_store for why
        this exists (the file previously held live keys in plaintext)."""
        try:
            from security.credential_store import migrate_plaintext_config
            migrated = migrate_plaintext_config(self._path)
            if migrated:
                log.info(f"[config] Migrated {migrated} into encrypted credential storage; "
                         f"{self._path} no longer holds plaintext secrets for these fields.")
                self._data = self._read()  # reflect the now-scrubbed file
        except Exception as exc:
            log.debug(f"[config] Secret migration skipped: {exc}")

    def reload(self) -> dict:
        """Discard the cache and reload from disk immediately."""
        with self._lock:
            self._data = None
            return self.load()

    def _read(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log.warning(f"[config] {self._path} did not contain a JSON object — using empty config.")
                return {}
            return data
        except FileNotFoundError:
            log.debug(f"[config] Config file not found: {self._path} — using defaults.")
            return {}
        except json.JSONDecodeError as exc:
            log.error(f"[config] Config file is malformed JSON ({exc}): {self._path}")
            return {}
        except Exception as exc:
            log.error(f"[config] Failed to read config: {exc}")
            return {}

    def save(self, updates: dict) -> bool:
        """Merge *updates* into the current config and write to disk.

        Returns True on success, False on failure.
        """
        with self._lock:
            data = self.load()
            data.update(updates)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self._data = data
                return True
            except Exception as exc:
                log.error(f"[config] Failed to save config: {exc}")
                return False

    # ── Getters ──────────────────────────────────────────────────────────────

    def gemini_key(self) -> str:
        """Return the Gemini API key, or '' if not configured.

        Checks the encrypted credential store first (where
        `_migrate_secrets_once` moves it to on first run); falls back
        to the plaintext config field only if secure storage is
        unavailable or nothing's been migrated yet, so this never
        breaks an existing setup mid-upgrade.
        """
        raw = self.load().get("gemini_api_key", "").strip()  # also triggers one-time migration
        try:
            from security.credential_store import get_secret
            stored = get_secret("gemini_api_key")
            if stored:
                return stored.strip()
        except Exception:
            pass
        # Reject placeholder values
        if raw.lower() in ("", "your_gemini_api_key_here", "your-api-key"):
            return ""
        return raw

    def llama_model_path(self) -> Optional[Path]:
        """Return the resolved llama model path, or None if not configured."""
        raw = self.load().get("llama_model_path", "").strip()
        if not raw:
            return None
        # Normalize: expand ~, resolve relative to config dir
        p = Path(raw.replace("\\", "/"))
        if not p.is_absolute():
            p = self._path.parent.parent / p  # relative to project root
        return p.resolve()

    def user_name(self) -> str:
        return self.load().get("user_name", "Vineet").strip() or "Vineet"

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    # ── Validation — COMPLETELY INDEPENDENT per backend ──────────────────────

    def validate_gemini(self) -> Tuple[bool, str]:
        """Validate online (Gemini) configuration only.

        Returns (True, "") on success or (False, human_readable_error).
        Never touches llama settings.
        """
        key = self.gemini_key()
        if not key:
            return False, (
                "Gemini API key not found in configuration.\n"
                f"Set 'gemini_api_key' in {self._path}"
            )
        return True, ""

    def validate_llama(self) -> Tuple[bool, str]:
        """Validate offline (llama.cpp) configuration only.

        Returns (True, "") on success or (False, human_readable_error).
        Never touches Gemini settings.
        """
        # 1. Check llama-cpp-python is installed
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            return False, (
                "llama-cpp-python is not installed.\n"
                "Run: pip install llama-cpp-python"
            )

        # 2. Check model path is configured
        path = self.llama_model_path()
        if path is None:
            return False, (
                "No local AI model configured.\n"
                f"Set 'llama_model_path' in {self._path}\n"
                "Download a GGUF model from https://huggingface.co/bartowski"
            )

        # 3. Validate the model file
        ok, err = validate_gguf_path(path)
        if not ok:
            return False, err

        return True, ""

    def is_gemini_available(self) -> bool:
        ok, _ = self.validate_gemini()
        return ok

    def is_llama_available(self) -> bool:
        ok, _ = self.validate_llama()
        return ok


# ── GGUF path validation (standalone, importable independently) ───────────────

def validate_gguf_path(path: Path) -> Tuple[bool, str]:
    """Validate a GGUF model file path with specific error messages.

    Checks:
      • file exists
      • is a regular file (not a directory)
      • is readable
      • has correct GGUF magic bytes

    Returns (True, "") or (False, human_readable_error).
    """
    # Normalize path (handles spaces, mixed separators)
    try:
        path = path.resolve()
    except Exception as exc:
        return False, f"Cannot resolve model path: {exc}"

    if not path.exists():
        return False, (
            f"Model file does not exist: {path}\n"
            "Check 'llama_model_path' in config/api_keys.json"
        )

    if not path.is_file():
        return False, f"Model path is a directory, not a file: {path}"

    try:
        size = path.stat().st_size
    except PermissionError:
        return False, f"Permission denied — cannot read model file: {path}"
    except Exception as exc:
        return False, f"Cannot stat model file ({exc}): {path}"

    if size < 8:
        return False, f"Model file is too small to be valid ({size} bytes): {path}"

    # Check GGUF magic bytes
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except PermissionError:
        return False, f"Permission denied reading model file: {path}"
    except Exception as exc:
        return False, f"Cannot open model file ({exc}): {path}"

    if magic != _GGUF_MAGIC:
        if magic[:2] in (b"PK", b"\x1f\x8b"):
            return False, (
                f"Not a GGUF model file — looks like a zip/archive: {path}\n"
                "Download the .gguf file directly, not a zip."
            )
        return False, (
            f"Not a valid GGUF model file (bad magic bytes {magic!r}): {path}\n"
            "Re-download the model — the file may be corrupt."
        )

    if not path.suffix.lower() == ".gguf":
        log.warning(f"[config] Model file does not have .gguf extension: {path.name}")

    return True, ""


# ── Module-level singleton ────────────────────────────────────────────────────

def _find_config_path() -> Path:
    """Resolve config/api_keys.json relative to the project root."""
    import sys
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "config" / "api_keys.json"


config = ConfigManager(_find_config_path())

__all__ = ["config", "ConfigManager", "validate_gguf_path"]
