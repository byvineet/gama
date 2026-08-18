"""
storage/embeddings.py — Encrypted Embedding Store
====================================================
A single, generic, local-only encrypted key-value store for biometric
embeddings (currently voice only). `voice/` writes into
this store instead of rolling their own persistence, so there is exactly
one place that handles encryption-at-rest, atomic writes, and corruption
recovery.

Nothing here ever leaves the machine. Raw audio/images are never stored
by this module — only numeric embedding vectors and small metadata.

Author: Gama Security Upgrade
"""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)

STORAGE_DIR = Path.home() / ".gama" / "biometrics"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
KEY_FILE = STORAGE_DIR / "store.key"


def _get_fernet():
    from cryptography.fernet import Fernet
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
    else:
        key = Fernet.generate_key()
        KEY_FILE.write_bytes(key)
        try:
            KEY_FILE.chmod(0o600)
        except Exception:
            pass
    return Fernet(key)


@dataclass
class EmbeddingRecord:
    """One enrolled template: a named biometric embedding (or several,
    for multi-sample templates) plus metadata."""
    name: str
    kind: str                       # e.g. "speaker"
    embeddings: List[np.ndarray]    # one or more vectors (multi-sample)
    model_id: str                   # e.g. "ecapa-tdnn"
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    sample_count: int = 0
    trusted: bool = True
    threshold: Optional[float] = None  # adaptive per-profile verification threshold

    def centroid(self) -> np.ndarray:
        """Mean embedding across all enrolled samples — a robust single
        vector for fast comparison, while individual samples are kept
        for future re-scoring / diagnostics."""
        stacked = np.stack(self.embeddings, axis=0)
        c = stacked.mean(axis=0)
        norm = np.linalg.norm(c)
        return c / norm if norm > 1e-8 else c


class EmbeddingStore:
    """One encrypted file per `kind` (e.g. speaker), containing a dict
    of name -> EmbeddingRecord. Loaded lazily, cached in memory, and
    re-encrypted+written on every mutation (writes are infrequent —
    enrollment only — so this trades a little write cost for simplicity
    and crash-safety)."""

    def __init__(self, kind: str):
        self.kind = kind
        self._path = STORAGE_DIR / f"{kind}_profiles.enc"
        self._cache: Optional[Dict[str, EmbeddingRecord]] = None

    def _load(self) -> Dict[str, EmbeddingRecord]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            fernet = _get_fernet()
            raw = fernet.decrypt(self._path.read_bytes())
            self._cache = pickle.loads(raw)
        except Exception as exc:
            log.error(f"[{self.kind}] Could not load embedding store ({exc}); starting empty.")
            self._cache = {}
        return self._cache

    def _flush(self) -> None:
        assert self._cache is not None
        fernet = _get_fernet()
        raw = pickle.dumps(self._cache)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(fernet.encrypt(raw))
        tmp.replace(self._path)
        try:
            self._path.chmod(0o600)
        except Exception:
            pass

    # -- public API ----------------------------------------------------
    def get(self, name: str) -> Optional[EmbeddingRecord]:
        return self._load().get(name)

    def all(self) -> Dict[str, EmbeddingRecord]:
        return dict(self._load())

    def exists(self, name: str) -> bool:
        return name in self._load()

    def put(self, record: EmbeddingRecord) -> None:
        store = self._load()
        record.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        store[record.name] = record
        self._flush()
        log.info(f"[{self.kind}] Saved profile '{record.name}' "
                 f"({len(record.embeddings)} sample(s), model={record.model_id}).")

    def delete(self, name: str) -> bool:
        store = self._load()
        if name not in store:
            return False
        del store[name]
        self._flush()
        log.info(f"[{self.kind}] Deleted profile '{name}'.")
        return True

    def rename_or_merge(self, name: str, new_embeddings: List[np.ndarray], model_id: str) -> None:
        """Used by retrain/update flows — appends new samples to an
        existing profile instead of discarding history, capped so the
        record doesn't grow unbounded."""
        store = self._load()
        existing = store.get(name)
        if existing is None:
            self.put(EmbeddingRecord(name=name, kind=self.kind, embeddings=new_embeddings,
                                      model_id=model_id, sample_count=len(new_embeddings)))
            return
        MAX_SAMPLES = 40
        if existing.model_id != model_id:
            # Embeddings from different models live in different vector
            # spaces — cosine similarity between an old-model vector and
            # a new-model vector is meaningless noise, not just "less
            # accurate". Mixing them silently degrades verification in a
            # way that's very hard to diagnose later. Start the profile
            # fresh on the new model instead of merging.
            log.warning(
                f"[{self.kind}] Profile '{name}' was enrolled with model "
                f"'{existing.model_id}'; discarding those samples and "
                f"starting fresh with '{model_id}' instead of mixing "
                f"incompatible embedding spaces."
            )
            combined = new_embeddings[-MAX_SAMPLES:]
        else:
            combined = existing.embeddings + new_embeddings
            if len(combined) > MAX_SAMPLES:
                combined = combined[-MAX_SAMPLES:]
        existing.embeddings = combined
        existing.model_id = model_id
        existing.sample_count = len(combined)
        self.put(existing)


_stores: Dict[str, EmbeddingStore] = {}


def get_store(kind: str) -> EmbeddingStore:
    if kind not in _stores:
        _stores[kind] = EmbeddingStore(kind)
    return _stores[kind]


__all__ = ["EmbeddingRecord", "EmbeddingStore", "get_store", "STORAGE_DIR"]
