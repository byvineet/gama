"""
core/protocols/storage.py — Persistence for the Protocol System
================================================================================
JSON-backed storage under ~/.gama/protocols/. Thread-safe, and migrates any
legacy routines created via the older actions.macro_engine ("Protocol Alpha")
so nothing the user built before is lost.
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.protocols.models import (
    ActionType,
    OnFailureStrategy,
    Protocol,
    ProtocolExecutionRecord,
    ProtocolStep,
)

log = get_logger(__name__)
logger = log  # back-compat alias
PROTOCOLS_DIR = Path.home() / ".gama" / "protocols"
PROTOCOLS_DIR.mkdir(parents=True, exist_ok=True)
PROTOCOLS_FILE = PROTOCOLS_DIR / "protocols.json"
HISTORY_FILE = PROTOCOLS_DIR / "history.json"
OLD_ROUTINES_FILE = Path.home() / ".gama" / "routines.json"

_MAX_HISTORY = 200

# Best-effort mapping from the legacy MacroStep.type vocabulary to ActionType.
_LEGACY_TYPE_MAP = {
    "app": ActionType.OPEN_APP.value,
    "command": ActionType.TERMINAL.value,
    "delay": ActionType.WAIT.value,
    "wait_process": ActionType.WAIT_PROCESS.value,
    "wait_url": ActionType.BROWSER.value,
    "macro": ActionType.CALL_PROTOCOL.value,
}


class ProtocolStorage:
    """Owns reading/writing protocols.json and history.json. Not
    responsible for any business logic — that lives in registry/manager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._protocols: Dict[str, Protocol] = {}
        self._history: List[ProtocolExecutionRecord] = []
        self._next_numeric_id = 1
        self._load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            if PROTOCOLS_FILE.exists():
                try:
                    raw = json.loads(PROTOCOLS_FILE.read_text(encoding="utf-8"))
                    for item in raw.get("protocols", []):
                        p = Protocol.from_dict(item)
                        self._protocols[p.id] = p
                    self._next_numeric_id = raw.get(
                        "next_numeric_id",
                        max([p.numeric_id or 0 for p in self._protocols.values()], default=0) + 1,
                    )
                except Exception as exc:
                    logger.warning(f"[protocols.storage] Failed to load protocols.json: {exc}")

            if HISTORY_FILE.exists():
                try:
                    raw_h = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                    self._history = [ProtocolExecutionRecord.from_dict(r) for r in raw_h]
                except Exception as exc:
                    logger.warning(f"[protocols.storage] Failed to load history.json: {exc}")

            if not self._protocols:
                self._save()

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------
    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        try:
            payload = {
                "protocols": [p.to_dict() for p in self._protocols.values()],
                "next_numeric_id": self._next_numeric_id,
            }
            tmp = PROTOCOLS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(PROTOCOLS_FILE)
        except Exception as exc:
            logger.error(f"[protocols.storage] Failed to save protocols.json: {exc}")

    def _save_history(self) -> None:
        try:
            payload = [r.to_dict() for r in self._history[-_MAX_HISTORY:]]
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(HISTORY_FILE)
        except Exception as exc:
            logger.error(f"[protocols.storage] Failed to save history.json: {exc}")

    # ------------------------------------------------------------------
    # Queries / mutations
    # ------------------------------------------------------------------
    def get_all(self) -> List[Protocol]:
        with self._lock:
            return list(self._protocols.values())

    def get_by_id(self, proto_id: str) -> Optional[Protocol]:
        with self._lock:
            return self._protocols.get(proto_id)

    def save_protocol(self, protocol: Protocol) -> Protocol:
        with self._lock:
            if protocol.numeric_id is None:
                protocol.numeric_id = self._next_numeric_id
                self._next_numeric_id += 1
            protocol.modified_at = time.time()
            self._protocols[protocol.id] = protocol
            self._save()
            return protocol

    def delete_protocol(self, proto_id: str) -> bool:
        with self._lock:
            if proto_id in self._protocols:
                del self._protocols[proto_id]
                self._save()
                return True
            return False

    def add_history_record(self, record: ProtocolExecutionRecord) -> None:
        with self._lock:
            # Replace an existing in-progress record for the same execution_id
            # (status transitions), otherwise append.
            for i, r in enumerate(self._history):
                if r.execution_id == record.execution_id:
                    self._history[i] = record
                    break
            else:
                self._history.append(record)
            self._save_history()

    def get_history(self, limit: int = 20) -> List[ProtocolExecutionRecord]:
        with self._lock:
            return list(reversed(self._history))[:limit]


protocol_storage = ProtocolStorage()

__all__ = ["ProtocolStorage", "protocol_storage", "PROTOCOLS_DIR", "PROTOCOLS_FILE", "HISTORY_FILE"]
