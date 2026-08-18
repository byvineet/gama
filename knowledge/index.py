"""
knowledge/index.py — Gama Knowledge Index
==========================================
The persistent, searchable memory of "what's on this computer": one
row per indexed file, with its content embedding, metadata, and a
content hash used to detect changes so re-embedding only ever touches
files that actually changed.

Performance contract (see Gama 2.0 spec — this module exists to hit
these numbers, not just to be "correct"):

  * Cached search            < 100ms
  * Semantic search          < 500ms
  * Open indexed file        instant (path lookup only, no re-read)
  * Never blocks the caller thread on a filesystem walk

How that's achieved:

  * SQLite + WAL (same as memory/long_term.py) — indexed lookups,
    safe concurrent reads while a background indexer writes.
  * Embeddings are float32 blobs decoded lazily and only for the rows
    a coarse pre-filter (FTS / mtime / category) already shortlisted —
    never "load every embedding into RAM and brute-force everything"
    on a big filesystem.
  * An in-memory LRU-ish cache of the last N queries' results, keyed
    on (query, scope), so a repeated or follow-up search
    ("open the newest one") is a dict lookup, not a re-embed.
  * `content_hash` (mtime+size, cheap) gates re-embedding: unchanged
    files are skipped entirely on every reindex pass — "zero duplicate
    indexing" from the spec.
  * All writes go through a single write lock; reads don't block on it
    (SQLite WAL allows concurrent readers during a writer transaction).

This module does NOT walk the filesystem or extract file content —
that's `knowledge/watcher.py` (incremental indexer) and
`knowledge/documents.py` (per-type extractors). This module is purely
the storage + query engine, so it can be unit-tested and reused
without touching disk I/O concerns.

Author : Gama Knowledge Layer
"""

from __future__ import annotations

from utils.paths import get_base_dir as _get_base_dir

import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from utils.logger import get_logger
from utils.text_embed import DEFAULT_DIM, blob_to_vec, cosine, embed_text, vec_to_blob

log = get_logger(__name__)




BASE_DIR = _get_base_dir()
DB_PATH = BASE_DIR / "knowledge" / "index.db"

DEFAULT_SEARCH_LIMIT = 20
QUERY_CACHE_MAX = 128           # small — this is a hot-path latency cache, not storage
QUERY_CACHE_TTL_SEC = 60        # short TTL: index changes underneath it constantly

_lock = threading.RLock()
_local = threading.local()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # readers don't block on the writer
    conn.execute("PRAGMA synchronous=NORMAL")   # crash-safe enough, much faster than FULL
    conn.execute("PRAGMA cache_size=-8000")     # ~8MB page cache, keeps hot rows in RAM
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass
    return conn


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def _cursor():
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL,
    ext           TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL,        -- cheap mtime+size fingerprint, gates re-embedding
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,                 -- filesystem created time (ISO)
    modified_at   TEXT,                 -- filesystem modified time (ISO)
    indexed_at    TEXT NOT NULL,        -- when WE last indexed it
    category      TEXT,                 -- Study/Invoices/Code/Images/... (smart categorization)
    project       TEXT,                 -- project root path, if inside one
    summary       TEXT,                 -- short cached summary (for instant preview, no re-LLM-call)
    text_excerpt  TEXT,                 -- first N chars of extracted text, for FTS-ish substring hits
    embedding     BLOB,                 -- float32 vector; NULL until (lazily) embedded
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT,
    deleted       INTEGER NOT NULL DEFAULT 0  -- soft-delete: file vanished, tombstoned not rescanned
);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_deleted ON files(deleted);
CREATE INDEX IF NOT EXISTS idx_files_modified ON files(modified_at);

CREATE TABLE IF NOT EXISTS relationships (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_a   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    file_b   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL DEFAULT 'related',  -- related|duplicate|same_project|derived_from
    score    REAL NOT NULL DEFAULT 0.0,
    UNIQUE(file_a, file_b, kind)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db() -> None:
    with _cursor() as cur:
        cur.executescript(_SCHEMA)


init_db()


# ---------------------------------------------------------------------------
# Query result cache — the thing that actually gets cached search < 100ms
# ---------------------------------------------------------------------------
@dataclass
class _CacheEntry:
    result: List["SearchResult"]
    ts: float = field(default_factory=time.time)


_query_cache: Dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> Optional[List["SearchResult"]]:
    with _cache_lock:
        entry = _query_cache.get(key)
        if entry is None:
            return None
        if time.time() - entry.ts > QUERY_CACHE_TTL_SEC:
            del _query_cache[key]
            return None
        return entry.result


def _cache_put(key: str, result: List["SearchResult"]) -> None:
    with _cache_lock:
        if len(_query_cache) >= QUERY_CACHE_MAX:
            # drop the oldest entry rather than maintaining a full LRU —
            # this cache is a latency smoother, not a correctness guarantee
            oldest_key = min(_query_cache, key=lambda k: _query_cache[k].ts)
            del _query_cache[oldest_key]
        _query_cache[key] = _CacheEntry(result=result)


def invalidate_cache() -> None:
    """Called by the indexer after any write — cheap correctness net
    since the query cache is small and short-TTL anyway."""
    with _cache_lock:
        _query_cache.clear()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class FileRecord:
    id: int
    path: str
    filename: str
    ext: str
    content_hash: str
    size_bytes: int
    created_at: Optional[str]
    modified_at: Optional[str]
    indexed_at: str
    category: Optional[str]
    project: Optional[str]
    summary: Optional[str]
    text_excerpt: Optional[str]
    access_count: int
    last_accessed: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileRecord":
        return cls(
            id=row["id"], path=row["path"], filename=row["filename"], ext=row["ext"],
            content_hash=row["content_hash"], size_bytes=row["size_bytes"],
            created_at=row["created_at"], modified_at=row["modified_at"],
            indexed_at=row["indexed_at"], category=row["category"], project=row["project"],
            summary=row["summary"], text_excerpt=row["text_excerpt"],
            access_count=row["access_count"], last_accessed=row["last_accessed"],
        )


@dataclass
class SearchResult:
    record: FileRecord
    score: float  # combined ranking score, not raw cosine — see knowledge/ranking.py


# ---------------------------------------------------------------------------
# Write path — used by knowledge/watcher.py (incremental indexer)
# ---------------------------------------------------------------------------
def content_hash(size_bytes: int, modified_at: str) -> str:
    """Cheap fingerprint: size+mtime. Good enough to detect "this file
    changed" without reading file bytes — reading every file's content
    just to hash it would defeat the point of incremental indexing."""
    return f"{size_bytes}:{modified_at}"


def upsert_file(
    path: str,
    *,
    size_bytes: int,
    created_at: Optional[str],
    modified_at: Optional[str],
    text_excerpt: str = "",
    category: Optional[str] = None,
    project: Optional[str] = None,
    summary: Optional[str] = None,
    embed: bool = True,
    force: bool = False,
) -> int:
    """Insert or update a file's index entry. Only re-embeds when the
    content hash actually changed (zero duplicate indexing) — unless
    `force=True`, which always overwrites the existing row (new
    excerpt/category/embedding replace whatever was there before),
    for an explicit re-index rather than a routine catch-up scan."""
    p = Path(path)
    new_hash = content_hash(size_bytes, modified_at or "")
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    with _cursor() as cur:
        cur.execute("SELECT id, content_hash FROM files WHERE path = ?", (path,))
        existing = cur.fetchone()

        if existing and existing["content_hash"] == new_hash and not force:
            # unchanged — just clear any stale deleted flag and move on
            cur.execute("UPDATE files SET deleted = 0 WHERE id = ?", (existing["id"],))
            invalidate_cache()
            return existing["id"]

        embedding_blob = None
        if embed:
            # embed filename + excerpt together — cheap (<1ms for this
            # vectorizer) so doing it inline is fine; a real heavy model
            # would instead be queued here for background processing.
            embedding_blob = vec_to_blob(embed_text(f"{p.name}\n{text_excerpt}"))

        if existing:
            # force=True replaces summary/embedding outright (no COALESCE
            # fallback to the old value) so a re-index actually supersedes
            # what was indexed before, not just refreshes the timestamp.
            cur.execute(
                """UPDATE files SET filename=?, ext=?, content_hash=?, size_bytes=?,
                       created_at=?, modified_at=?, indexed_at=?, category=?, project=?,
                       summary=?, text_excerpt=?, embedding=?,
                       deleted=0
                   WHERE id=?"""
                if force else
                """UPDATE files SET filename=?, ext=?, content_hash=?, size_bytes=?,
                       created_at=?, modified_at=?, indexed_at=?, category=?, project=?,
                       summary=COALESCE(?, summary), text_excerpt=?, embedding=COALESCE(?, embedding),
                       deleted=0
                   WHERE id=?""",
                (p.name, p.suffix.lower().lstrip("."), new_hash, size_bytes,
                 created_at, modified_at, now, category, project,
                 summary, text_excerpt[:2000], embedding_blob, existing["id"]),
            )
            invalidate_cache()
            return existing["id"]
        else:
            cur.execute(
                """INSERT INTO files (path, filename, ext, content_hash, size_bytes,
                       created_at, modified_at, indexed_at, category, project, summary,
                       text_excerpt, embedding, access_count, last_accessed, deleted)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,0)""",
                (path, p.name, p.suffix.lower().lstrip("."), new_hash, size_bytes,
                 created_at, modified_at, now, category, project, summary,
                 text_excerpt[:2000], embedding_blob),
            )
            invalidate_cache()
            return cur.lastrowid


def mark_deleted(path: str) -> None:
    """Soft-delete: tombstone rather than remove, so a file that
    reappears (e.g. undo-delete, branch switch) doesn't need a full
    re-embed — this is the 'remove deleted files' requirement without
    losing the incremental-indexing benefit if it comes back."""
    with _cursor() as cur:
        cur.execute("UPDATE files SET deleted = 1 WHERE path = ?", (path,))
    invalidate_cache()


def purge_deleted(older_than_days: int = 30) -> int:
    """Hard-delete tombstones older than N days — periodic cleanup,
    not run on the hot path."""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - older_than_days * 86400))
    with _cursor() as cur:
        cur.execute("DELETE FROM files WHERE deleted = 1 AND indexed_at < ?", (cutoff,))
        return cur.rowcount


def touch_access(path: str) -> None:
    """Bump usage stats — feeds knowledge/ranking.py's recency/popularity
    signal. Fire-and-forget from the caller's perspective; cheap single-row
    update, never blocks a search."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE files SET access_count = access_count + 1, last_accessed = ? WHERE path = ?",
            (time.strftime("%Y-%m-%d %H:%M:%S"), path),
        )


# ---------------------------------------------------------------------------
# Read path — search
# ---------------------------------------------------------------------------
def get_by_path(path: str) -> Optional[FileRecord]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM files WHERE path = ? AND deleted = 0", (path,))
        row = cur.fetchone()
        return FileRecord.from_row(row) if row else None


def _candidate_rows(
    cur: sqlite3.Cursor,
    *,
    ext: Optional[str] = None,
    project: Optional[str] = None,
    category: Optional[str] = None,
    limit_scan: int = 5000,
) -> List[sqlite3.Row]:
    """Coarse pre-filter before touching embeddings at all — narrows
    the candidate set with cheap indexed WHERE clauses first, so the
    (relatively) expensive cosine-similarity loop only ever runs over
    a bounded slice, not the whole index."""
    clauses = ["deleted = 0", "embedding IS NOT NULL"]
    params: List[Any] = []
    if ext:
        clauses.append("ext = ?")
        params.append(ext.lstrip("."))
    if project:
        clauses.append("project = ?")
        params.append(project)
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = " AND ".join(clauses)
    cur.execute(
        f"SELECT * FROM files WHERE {where} ORDER BY modified_at DESC LIMIT ?",
        (*params, limit_scan),
    )
    return cur.fetchall()


def semantic_search(
    query: str,
    *,
    ext: Optional[str] = None,
    project: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    use_cache: bool = True,
) -> List[SearchResult]:
    """Embedding-similarity search over the index. Ranking beyond raw
    cosine (recency/project/popularity boosts) is layered on top by
    knowledge/ranking.py — this function returns similarity-ordered
    candidates only, which is what makes it cacheable and fast."""
    cache_key = f"{query}|{ext}|{project}|{category}|{limit}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    qvec = embed_text(query)
    with _cursor() as cur:
        rows = _candidate_rows(cur, ext=ext, project=project, category=category)

    # Batch every candidate's embedding into one matrix and score with a
    # single matmul instead of a per-row Python loop + per-row cosine() call
    # (mirrors the same fix applied to memory/long_term.py search()).
    valid_rows = []
    vecs = []
    for row in rows:
        vec = blob_to_vec(row["embedding"])
        if vec.shape[0] != qvec.shape[0]:
            continue  # stale embedding from a different dim/model — skip, don't crash
        valid_rows.append(row)
        vecs.append(vec)

    scored: List[SearchResult] = []
    if valid_rows:
        mat = np.stack(vecs)
        qnorm = float(np.linalg.norm(qvec))
        if qnorm > 1e-8:
            mnorms = np.linalg.norm(mat, axis=1)
            mnorms[mnorms < 1e-8] = 1e-8
            sims = (mat @ qvec) / (mnorms * qnorm)
        else:
            sims = np.zeros(len(valid_rows))
        scored = [
            SearchResult(record=FileRecord.from_row(row), score=float(sims[i]))
            for i, row in enumerate(valid_rows)
        ]

    scored.sort(key=lambda r: r.score, reverse=True)
    result = scored[:limit]

    if use_cache:
        _cache_put(cache_key, result)
    return result


def filename_search(pattern: str, limit: int = DEFAULT_SEARCH_LIMIT) -> List[FileRecord]:
    """Fast path for literal/substring filename lookups — doesn't touch
    embeddings at all, so this comfortably beats the 100ms cached-search
    budget even on a cold cache."""
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM files WHERE deleted = 0 AND filename LIKE ? "
            "ORDER BY datetime(modified_at) DESC, datetime(created_at) DESC LIMIT ?",
            (f"%{pattern}%", limit),
        )
        return [FileRecord.from_row(r) for r in cur.fetchall()]


def get_latest_file(pattern: Optional[str] = None, ext: Optional[str] = None, category: Optional[str] = None) -> Optional[FileRecord]:
    """Retrieve the single most recent file by date and time (modified_at / created_at), not sequence ID."""
    clauses = ["deleted = 0"]
    params: List[Any] = []
    if pattern:
        clauses.append("(filename LIKE ? OR path LIKE ? OR text_excerpt LIKE ?)")
        p_str = f"%{pattern}%"
        params.extend([p_str, p_str, p_str])
    if ext:
        clauses.append("ext = ?")
        params.append(ext.lstrip("."))
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = " AND ".join(clauses)
    query = f"SELECT * FROM files WHERE {where} ORDER BY datetime(modified_at) DESC, datetime(created_at) DESC, id DESC LIMIT 1"
    with _cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        return FileRecord.from_row(row) if row else None



def list_paths_under(prefix: str) -> List[str]:
    """All currently-indexed (non-deleted) paths starting with `prefix`.
    Used by the incremental indexer to reconcile deletions scoped to
    just the folders it scanned, without reaching into internals."""
    with _cursor() as cur:
        cur.execute(
            "SELECT path FROM files WHERE deleted = 0 AND path LIKE ?",
            (f"{prefix}%",),
        )
        return [row["path"] for row in cur.fetchall()]


def stats() -> Dict[str, Any]:
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM files WHERE deleted = 0")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM files WHERE deleted = 0 AND embedding IS NOT NULL")
        embedded = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM files WHERE deleted = 1")
        tombstoned = cur.fetchone()["c"]
    return {"indexed_files": total, "embedded_files": embedded, "tombstoned": tombstoned}


def reindex_all() -> int:
    """Re-compute and store embeddings for every indexed file using the
    current embed_text() implementation.

    Called automatically by utils.text_embed.trigger_reindex() after the
    semantic encoder is loaded (C3 upgrade), so that files indexed under
    the old hash-based embedder get regenerated in the new vector space.

    Returns the number of files successfully re-embedded.
    """
    import logging as _logging
    _log = _get_logger(__name__)

    # One-time guard: this exists to migrate old hash-based embeddings
    # into the new semantic vector space (C3 upgrade). Without this flag,
    # utils.text_embed.trigger_reindex() calls this on *every* app
    # startup, unconditionally re-embedding the entire knowledge base
    # (thousands of files) through the ONNX model every single launch —
    # a large, avoidable, recurring CPU spike. Once it's completed
    # successfully, skip it on future runs.
    with _cursor() as cur:
        cur.execute("SELECT value FROM meta WHERE key = 'semantic_reindex_done'")
        row = cur.fetchone()
    if row is not None and row["value"] == "1":
        _log.debug("[knowledge.reindex_all] Already completed previously — skipping.")
        return 0

    _log.info("[knowledge.reindex_all] Starting full re-index …")

    try:
        # Read all candidate rows into memory first so the long-lived read
        # cursor does not compete with the per-row UPDATE cursors under the
        # global _lock.  This fixes the deadlock seen when a generator-held
        # read cursor overlaps with a write cursor on the same connection.
        with _cursor() as cur:
            cur.execute(
                "SELECT path, filename, text_excerpt FROM files "
                "WHERE deleted = 0 AND text_excerpt IS NOT NULL"
            )
            rows = cur.fetchall()
    except Exception as exc:
        _log.warning(f"[knowledge.reindex_all] Failed to read candidates: {exc}")
        return 0

    updated = 0
    for row in rows:
        try:
            new_vec = embed_text(f"{row['filename']}\n{row['text_excerpt']}")
            new_blob = vec_to_blob(new_vec)
            with _cursor() as cur:
                cur.execute(
                    "UPDATE files SET embedding = ? WHERE path = ?",
                    (new_blob, row["path"]),
                )
            updated += 1
        except Exception as exc:
            _log.debug(f"[knowledge.reindex_all] Skipped {row['path']}: {exc}")

    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES ('semantic_reindex_done', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
    except Exception as exc:
        _log.debug(f"[knowledge.reindex_all] Failed to persist completion flag: {exc}")

    try:
        invalidate_cache()
    except Exception as exc:
        _log.debug(f"[knowledge.reindex_all] invalidate_cache failed (non-fatal): {exc}")

    _log.info(f"[knowledge.reindex_all] Done — re-embedded {updated}/{len(rows)} files.")
    return updated


# Alias for trigger_reindex() — see utils/text_embed.py
knowledge_index = type("_KnowledgeIndex", (), {"reindex_all": staticmethod(reindex_all)})()


__all__ = [
    "DB_PATH", "FileRecord", "SearchResult",
    "init_db", "content_hash", "upsert_file", "mark_deleted", "purge_deleted",
    "touch_access", "get_by_path", "semantic_search", "filename_search", "get_latest_file",
    "list_paths_under", "invalidate_cache", "stats", "reindex_all", "knowledge_index",
]