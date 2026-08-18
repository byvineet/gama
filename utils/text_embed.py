"""
utils/text_embed.py — Shared local text embedding
==================================================
Drop-in semantic replacement: same public API as before, now backed by
BAAI/bge-small-en-v1.5 via fastembed (ONNX, CPU-only) instead of
feature-hashed bag-of-tokens.

The upgrade keeps the exact same call signatures and 384-dim float32
contract so memory/ and knowledge/ callers require zero changes.

Semantic model details
  Model : BAAI/bge-small-en-v1.5  (fastembed default)
  Dim   : 384  (matches DEFAULT_DIM — no DB schema change needed)
  Size  : ~84 MB, cached in ~/.cache/fastembed/ on first embed call
  CPU   : ~5–20 ms per text on a modern CPU — fine for local search

Fallback
  If fastembed is not installed OR the model cache is missing (first run
  before the model downloads), embed_text() silently falls back to the
  original hash-based implementation so startup never fails.

Re-indexing
  Existing SQLite blobs were encoded in the old hash space. After the
  first run with this module, new writes use semantic vectors. Old rows
  will have low (not error-causing) similarity scores and fade out as
  they are replaced. To force a clean re-index of the knowledge base,
  call: utils.text_embed.trigger_reindex()

C3 fix — GAMA_ARCHITECTURE_AUDIT.md
Author : Gama Knowledge Layer
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import List, Optional

import numpy as np

DEFAULT_DIM = 384  # BGE-small output dim; keep in sync with DB schemas

_WORD_RE = re.compile(r"[a-zA-Z0-9\u0900-\u097F]+", re.UNICODE)  # incl. Devanagari
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model handle — loads once on first embed_text() call
# ---------------------------------------------------------------------------

_model_lock = threading.Lock()
_model: Optional[object] = None   # fastembed.TextEmbedding once loaded
_model_failed: bool = False        # True after any load error → use fallback


def _load_model() -> Optional[object]:
    """Lazily load the ONNX sentence encoder. Thread-safe, loads at most once."""
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _model_lock:
        if _model is not None or _model_failed:
            return _model
        try:
            from fastembed import TextEmbedding  # type: ignore
            _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            _log.info(
                "[text_embed] Semantic encoder loaded: "
                "BAAI/bge-small-en-v1.5 (384-dim, ONNX)"
            )
        except Exception as exc:
            _model_failed = True
            _log.warning(
                f"[text_embed] fastembed unavailable — "
                f"falling back to hash embeddings: {exc}"
            )
    return _model


# ---------------------------------------------------------------------------
# Hash-based fallback (original implementation, kept verbatim)
# ---------------------------------------------------------------------------

def tokenize(text: str) -> List[str]:
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    tokens = list(words)
    # character trigrams add robustness to typos / partial matches /
    # filename fragments ("refract" should still hit "refraction")
    for w in words:
        if len(w) >= 3:
            tokens.extend(w[i:i + 3] for i in range(len(w) - 2))
    return tokens


def _hash_embed(text: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Feature-hashed bag-of-tokens embedding. Used as fallback only."""
    vec = np.zeros(dim, dtype=np.float32)
    for tok in tokenize(text):
        digest = hashlib.md5(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 1e-8:
        vec /= norm
    return vec


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def embed_text(text: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Embed a string into a 384-dim L2-normalised float32 vector.

    Uses BAAI/bge-small-en-v1.5 (semantic) when fastembed is available,
    falls back to the original hash-based implementation otherwise.

    The `dim` parameter is accepted for API compatibility; it is ignored
    when the semantic model is active (model output is always 384-dim).
    """
    model = _load_model()
    if model is not None:
        try:
            vec = next(iter(model.embed([text or ""])))
            arr = np.array(vec, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm > 1e-8:
                arr /= norm
            return arr
        except Exception as exc:
            _log.debug(
                f"[text_embed] Semantic embed failed, using hash fallback: {exc}"
            )
    return _hash_embed(text, dim=dim)


def vec_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Both vectors are L2-normalised by embed_text()."""
    return float(np.dot(a, b))


def is_semantic_active() -> bool:
    """Return True if the ONNX sentence encoder is loaded and active."""
    return _load_model() is not None


def trigger_reindex() -> None:
    """Schedule a background re-index of the knowledge base.

    Call once after startup to regenerate stored vectors that were encoded
    with the old hash-based embedder. Non-fatal if the knowledge index does
    not expose a reindex_all() method.
    """
    if not is_semantic_active():
        return
    try:
        from knowledge.index import knowledge_index
        import threading as _t
        _t.Thread(
            target=knowledge_index.reindex_all,
            daemon=True,
            name="knowledge-reindex",
        ).start()
        _log.info("[text_embed] Background knowledge re-index triggered.")
    except Exception as exc:
        _log.debug(f"[text_embed] Re-index trigger skipped (non-fatal): {exc}")


__all__ = [
    "DEFAULT_DIM", "tokenize", "embed_text", "vec_to_blob", "blob_to_vec",
    "cosine", "is_semantic_active", "trigger_reindex",
]
