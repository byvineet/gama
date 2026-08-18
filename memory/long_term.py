"""
Gama - Long-Term Memory Store
=============================
A real personal-memory system layered on top of the existing lightweight
JSON profile store (memory_manager.py), which is left untouched so
voice/confirmation-code preferences keep working exactly as before.

This module adds what a JARVIS-style assistant actually needs:

* Project-specific memory              (per-project facts, isolated)
* Conversation summaries                (one per session)
* Daily summaries                       (one rollup per calendar day)
* Semantic search over a vector store   (local, offline, zero-dependency)
* Memory importance scoring             (0..1, heuristic)
* Memory decay for temporary info       (recency-weighted, auto-pruned)

Design choices, explained:

* SQLite instead of a JSON blob — safe concurrent access, indexed
  lookups, and it scales to tens of thousands of memories without
  ever loading "everything" into RAM.
* Embeddings are computed locally with a deterministic hashing
  vectorizer (no model download, no network call, no GPU). This is
  intentionally "lightweight semantic search": good enough to match
  a personal assistant's memory by topic/keywords, at near-zero CPU
  cost and 100% offline. `embed_text()` is the single seam to swap
  in a real embedding model later without touching anything else.
* Nothing here ever hands the *entire* memory DB to the LLM. Callers
  always go through `search()` / `top_memories()` with a budget.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import hashlib
import math
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


BASE_DIR = _get_base_dir()
DB_PATH = BASE_DIR / "memory" / "long_term.db"

# ---------------------------------------------------------------------------
# Tunables (kept as simple module constants — see README for how to tune)
# ---------------------------------------------------------------------------
EMBED_DIM = 384                      # small = fast, low RAM
CONTEXT_CHAR_BUDGET = 1400           # hard cap injected into system prompt
RECALL_TOP_K_DEFAULT = 5
TEMPORARY_TTL_DAYS = 14              # temporary memories die after this...
TEMPORARY_REINFORCE_BONUS_DAYS = 10  # ...unless accessed, which extends life
IMPORTANCE_HALF_LIFE_DAYS = 21       # effective importance halves every N days
MIN_EFFECTIVE_IMPORTANCE = 0.04      # below this a temporary memory is pruned

_lock = threading.RLock()
_local = threading.local()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # fast + crash-safe
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass
    # Guard against a corrupted DB (e.g. WAL not checkpointed after hard kill).
    # If the image is malformed, recreate the DB from scratch — memories are
    # non-critical context; losing them is better than crashing on every start.
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"integrity_check: {result}")
    except sqlite3.DatabaseError as _ic_exc:
        import logging as _logging
        _get_logger(__name__).error(
            f"long_term.db failed integrity check ({_ic_exc}) — "
            "backing up corrupted file and creating a fresh database."
        )
        conn.close()
        import shutil as _shutil
        _shutil.copy2(str(DB_PATH), str(DB_PATH) + ".corrupt_backup")
        DB_PATH.unlink(missing_ok=True)
        for _ext in ("-shm", "-wal"):
            _p = DB_PATH.parent / (DB_PATH.name + _ext)
            if _p.exists():
                _p.unlink()
        conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _get_conn() -> sqlite3.Connection:
    """One connection per thread — SQLite objects aren't thread-safe to share."""
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
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL DEFAULT 'fact',   -- fact|preference|episodic|project
    project       TEXT,                            -- NULL = not project-scoped
    content       TEXT NOT NULL,
    embedding     BLOB NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.5,       -- 0..1, base importance
    temporary     INTEGER NOT NULL DEFAULT 0,      -- 1 = subject to decay/pruning
    created_at    TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    access_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_kind_project ON memories(kind, project);
CREATE INDEX IF NOT EXISTS idx_memories_temp ON memories(temporary);
-- Perf audit: search()'s candidate query below is ordered by
-- (importance DESC, last_accessed DESC) precisely so it can use this
-- composite index instead of a full table scan + sort as the memories
-- table grows into the tens of thousands of rows.
CREATE INDEX IF NOT EXISTS idx_memories_importance_last_accessed
    ON memories(importance DESC, last_accessed DESC);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_start TEXT NOT NULL,
    session_end   TEXT NOT NULL,
    summary       TEXT NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.4,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    date          TEXT PRIMARY KEY,   -- YYYY-MM-DD
    summary       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id     TEXT PRIMARY KEY,
    entity_type   TEXT NOT NULL,   -- person|project|topic|file|goal|deadline|device|app
    name          TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(entity_type, name);

CREATE TABLE IF NOT EXISTS entity_relationships (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL,
    relation_type TEXT NOT NULL,   -- relates_to|depends_on|mentioned_in|has_deadline|assigned_to|opened_file
    target_id     TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    metadata_json TEXT DEFAULT '{}',
    created_at    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON entity_relationships(relation_type);
"""


def init_db() -> None:
    with _cursor() as cur:
        cur.executescript(_SCHEMA)


init_db()


# ---------------------------------------------------------------------------
# Local embedding (deterministic hashing vectorizer — no network, no model)
# ---------------------------------------------------------------------------
# Shared with knowledge/ (see utils/text_embed.py) so memory and file/document
# search live in the same embedding space instead of two forked vectorizers.
from utils.text_embed import embed_text, tokenize as _tokenize  # noqa: E402
from utils.text_embed import vec_to_blob as _vec_to_blob  # noqa: E402
from utils.text_embed import blob_to_vec as _blob_to_vec  # noqa: E402


# ---------------------------------------------------------------------------
# Importance scoring
# ---------------------------------------------------------------------------
_IMPORTANT_HINTS = (
    "remember", "important", "always", "never forget", "my name is",
    "birthday", "anniversary", "deadline", "password", "allerg",
    "favorite", "favourite", "prefer", "goal", "project", "wife",
    "husband", "family", "phone number", "address",
    "device", "laptop", "phone", "person", "friend", "colleague",
)

# Content that should NOT be stored as long-term memories (Part 9).
# Greetings, acknowledgements, filler — these have zero recall value.
_NOISE_PATTERNS = (
    r"^\s*(?:hi|hello|hey|good morning|good evening|good afternoon|good night)\s*[\.\!]?\s*$",
    r"^\s*(?:ok|okay|sure|thanks|thank you|thx|np|no problem|got it|got that)\s*[\.\!]?\s*$",
    r"^\s*(?:yes|no|yep|nope|yeah|nah|alright|right|fine|great|nice|cool)\s*[\.\!]?\s*$",
    r"^\s*(?:done|finished|stop|cancel|never mind|nevermind|forget it)\s*[\.\!]?\s*$",
    r"^\s*gama\s*[\.\!]?\s*$",
    r"^\s*(?:wake up|go to sleep)\s*[\.\!]?\s*$",
)
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)


def is_noise(text: str) -> bool:
    """Return True if this text is filler/greeting that shouldn't be stored as memory."""
    return bool(_NOISE_RE.match((text or "").strip()))


def score_importance(text: str) -> float:
    """Cheap heuristic importance score in [0.15, 0.95].

    Weighs: explicit "remember this" style cues, presence of concrete
    facts (numbers/dates/proper nouns), and length as a weak signal of
    substance. No LLM call needed, so it costs ~0ms.
    """
    if not text:
        return 0.2
    if is_noise(text):
        return 0.05  # below any threshold — won't be stored
    t = text.lower()
    score = 0.3
    hint_hits = sum(1 for h in _IMPORTANT_HINTS if h in t)
    score += min(hint_hits, 3) * 0.15
    if re.search(r"\d", t):
        score += 0.05
    if 20 <= len(text) <= 300:
        score += 0.05
    return max(0.15, min(0.95, score))


def _effective_importance(base_importance: float, temporary: bool,
                           created_at: str, access_count: int) -> float:
    """Decay temporary memories over time; reinforcement (being recalled)
    slows the decay. Permanent (non-temporary) memories don't decay.
    """
    if not temporary:
        return base_importance
    try:
        age_days = (datetime.now() - datetime.fromisoformat(created_at)).total_seconds() / 86400.0
    except Exception:
        age_days = 0.0
    effective_half_life = IMPORTANCE_HALF_LIFE_DAYS + access_count * TEMPORARY_REINFORCE_BONUS_DAYS
    decay = math.exp(-age_days / max(effective_half_life, 1.0) * math.log(2))
    return base_importance * decay


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class MemoryHit:
    id: int
    kind: str
    project: Optional[str]
    content: str
    score: float
    importance: float
    created_at: str


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def remember(content: str, kind: str = "fact", project: Optional[str] = None,
             importance: Optional[float] = None, temporary: bool = False) -> int:
    """Store a new long-term memory. Returns its row id.

    Per spec Part 9: greetings, small talk, and acknowledgements are
    silently dropped — only meaningful facts, preferences, projects,
    people, devices, and deadlines are persisted.
    """
    content = (content or "").strip()
    if not content:
        return -1
    # Drop noise/filler — no reason to fill the DB with greetings
    if is_noise(content):
        return -1
    imp = score_importance(content) if importance is None else max(0.0, min(1.0, importance))
    now = datetime.now().isoformat()
    vec = embed_text(content)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO memories (kind, project, content, embedding, importance, "
            "temporary, created_at, last_accessed, access_count) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (kind, project, content, _vec_to_blob(vec), imp, int(temporary), now, now),
        )
        return cur.lastrowid


# Cosine-similarity threshold above which two facts are treated as "the same
# memory" rather than two separate ones. 0.92 is deliberately conservative —
# it merges near-identical restatements ("I like pizza" / "I really like
# pizza") without collapsing genuinely distinct facts that just share
# vocabulary ("I like pizza" / "I like Python").
DEDUP_SIMILARITY_THRESHOLD = 0.92


def remember_dedup(content: str, kind: str = "fact", project: Optional[str] = None,
                    importance: Optional[float] = None, temporary: bool = False) -> int:
    """Semantic-dedup-aware version of `remember()`.

    Added for C2 (memory consolidation, GAMA_ARCHITECTURE_AUDIT.md / F5).
    Before inserting, checks whether a near-identical fact already exists
    (cosine similarity >= DEDUP_SIMILARITY_THRESHOLD against the single
    closest match). If so, the existing row is "touched" (last_accessed
    bumped, importance raised to the max of the two) instead of creating a
    duplicate row — this is what `remember()` alone does NOT do, and is
    exactly the gap the audit flagged ("no deduplication or consistency
    guarantee" across writes).

    All new code should call this instead of `remember()` directly. The
    plain `remember()` is kept for internal/system writes (e.g. conversation
    summaries) where duplication isn't a realistic concern.
    """
    content = (content or "").strip()
    if not content:
        return -1
    if is_noise(content):
        return -1

    qvec = embed_text(content)
    if qvec is not None and float(np.linalg.norm(qvec)) > 1e-8:
        with _cursor() as cur:
            if project:
                cur.execute(
                    "SELECT * FROM memories WHERE project = ? OR project IS NULL "
                    "ORDER BY last_accessed DESC LIMIT ?",
                    (project, MAX_SEARCH_ROWS),
                )
            else:
                cur.execute(
                    "SELECT * FROM memories ORDER BY last_accessed DESC LIMIT ?",
                    (MAX_SEARCH_ROWS,),
                )
            rows = cur.fetchall()

        if rows:
            mvecs = np.stack([_blob_to_vec(r["embedding"]) for r in rows])
            sims = np.clip(mvecs @ qvec, 0.0, None)
            best_i = int(np.argmax(sims))
            if float(sims[best_i]) >= DEDUP_SIMILARITY_THRESHOLD:
                best = rows[best_i]
                new_imp = importance if importance is not None else score_importance(content)
                merged_imp = max(float(best["importance"]), max(0.0, min(1.0, new_imp)))
                now = datetime.now().isoformat()
                with _cursor() as cur:
                    cur.execute(
                        "UPDATE memories SET importance = ?, last_accessed = ?, "
                        "access_count = access_count + 1 WHERE id = ?",
                        (merged_imp, now, best["id"]),
                    )
                return int(best["id"])

    return remember(content, kind=kind, project=project, importance=importance, temporary=temporary)


def add_conversation_summary(summary: str, session_start: str, session_end: str,
                              importance: float = 0.4) -> int:
    summary = (summary or "").strip()
    if not summary:
        return -1
    now = datetime.now().isoformat()
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_summaries "
            "(session_start, session_end, summary, importance, created_at) VALUES (?,?,?,?,?)",
            (session_start, session_end, summary, importance, now),
        )
        return cur.lastrowid


def upsert_daily_summary(date_str: str, summary: str) -> None:
    now = datetime.now().isoformat()
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO daily_summaries (date, summary, created_at) VALUES (?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET summary=excluded.summary, created_at=excluded.created_at",
            (date_str, summary, now),
        )


def get_daily_summary(date_str: str) -> Optional[str]:
    with _cursor() as cur:
        cur.execute("SELECT summary FROM daily_summaries WHERE date = ?", (date_str,))
        row = cur.fetchone()
        return row["summary"] if row else None


def latest_conversation_summaries(limit: int = 3) -> List[str]:
    with _cursor() as cur:
        cur.execute(
            "SELECT summary FROM conversation_summaries ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [r["summary"] for r in cur.fetchall()]


def conversation_summaries_between(start_iso: str, end_iso: str) -> List[str]:
    with _cursor() as cur:
        cur.execute(
            "SELECT summary FROM conversation_summaries WHERE created_at >= ? AND created_at < ? "
            "ORDER BY id ASC",
            (start_iso, end_iso),
        )
        return [r["summary"] for r in cur.fetchall()]


def meta_get(key: str) -> Optional[str]:
    with _cursor() as cur:
        cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _touch(ids: Sequence[int]) -> None:
    if not ids:
        return
    now = datetime.now().isoformat()
    with _cursor() as cur:
        cur.executemany(
            "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
            [(now, i) for i in ids],
        )


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------
# Hard cap on rows pulled into Python/numpy per search() call. Prevents
# unbounded memory/CPU growth as the DB grows into the tens of thousands
# of rows — most-important/most-recently-accessed rows are preferred via
# the SQL ORDER BY below (served by idx_memories_importance_last_accessed),
# so the cap rarely drops anything relevant. Lowered from 4000 -> 1500
# per perf audit: 4000 rows * 384-dim float32 vectors is ~6MB of numpy
# stacking + a matmul on every single search() call, most of it wasted
# since top_k is almost always in the single digits.
MAX_SEARCH_ROWS = 1500


def search(query: str, top_k: int = RECALL_TOP_K_DEFAULT, project: Optional[str] = None,
           include_global: bool = True, kinds: Optional[Iterable[str]] = None) -> List[MemoryHit]:
    """Rank memories by (semantic similarity * 0.55 + effective importance * 0.45).

    `project` scopes to a specific project's memories; `include_global`
    additionally pulls in non-project (general) memories so project chats
    still benefit from user-profile-level facts.

    Similarity is computed as a single batched numpy matrix-vector product
    over all candidate rows rather than a per-row Python loop, and the
    number of candidate rows pulled from SQLite is capped (MAX_SEARCH_ROWS)
    so this stays fast as the memory DB grows.
    """
    with _cursor() as cur:
        if project and not include_global:
            cur.execute(
                "SELECT * FROM memories WHERE project = ? "
                "ORDER BY importance DESC, last_accessed DESC LIMIT ?",
                (project, MAX_SEARCH_ROWS),
            )
        elif project and include_global:
            cur.execute(
                "SELECT * FROM memories WHERE project = ? OR project IS NULL "
                "ORDER BY importance DESC, last_accessed DESC LIMIT ?",
                (project, MAX_SEARCH_ROWS),
            )
        else:
            cur.execute(
                "SELECT * FROM memories ORDER BY importance DESC, last_accessed DESC LIMIT ?",
                (MAX_SEARCH_ROWS,),
            )
        rows = cur.fetchall()

    if kinds:
        kinds = set(kinds)
        rows = [r for r in rows if r["kind"] in kinds]
    if not rows:
        return []

    eff_imps = [
        _effective_importance(r["importance"], bool(r["temporary"]),
                               r["created_at"], r["access_count"])
        for r in rows
    ]

    qvec = embed_text(query) if query else None
    use_sim = qvec is not None and float(np.linalg.norm(qvec)) > 1e-8

    if use_sim:
        # Batch every row's embedding into one (N, EMBED_DIM) matrix and do
        # a single matmul instead of N separate np.dot calls.
        mvecs = np.stack([_blob_to_vec(r["embedding"]) for r in rows])
        sims = mvecs @ qvec  # both sides L2-normalized -> cosine similarity
        sims = np.clip(sims, 0.0, None)
        scores = 0.55 * sims + 0.45 * np.asarray(eff_imps)
    else:
        scores = np.asarray(eff_imps)

    hits: List[MemoryHit] = [
        MemoryHit(id=r["id"], kind=r["kind"], project=r["project"],
                   content=r["content"], score=float(scores[i]),
                   importance=eff_imps[i], created_at=r["created_at"])
        for i, r in enumerate(rows)
    ]

    hits.sort(key=lambda h: h.score, reverse=True)
    top = hits[:max(1, top_k)]
    _touch([h.id for h in top])
    return top


def forget(query: str, project: Optional[str] = None,
           include_global: bool = True, min_score: float = 0.35) -> List[MemoryHit]:
    """Delete the best-matching memory (or memories) for `query`.

    Uses the same ranking as `search`, but only deletes hits whose
    combined score clears `min_score`, so a vague/empty query doesn't
    wipe out the highest-importance memory by accident. Returns the
    list of deleted MemoryHit rows (empty list if nothing matched).
    """
    query = (query or "").strip()
    if not query:
        return []
    hits = search(query, top_k=5, project=project, include_global=include_global)
    to_delete = [h for h in hits if h.score >= min_score]
    if not to_delete:
        return []
    with _cursor() as cur:
        cur.executemany("DELETE FROM memories WHERE id = ?", [(h.id,) for h in to_delete])
    return to_delete


# ---------------------------------------------------------------------------
# Decay / pruning — call periodically (e.g. once per session end)
# ---------------------------------------------------------------------------
def decay_sweep() -> int:
    """Delete temporary memories whose effective importance has decayed
    past the minimum threshold. Returns number of rows removed."""
    with _cursor() as cur:
        cur.execute("SELECT id, importance, created_at, access_count FROM memories WHERE temporary = 1")
        rows = cur.fetchall()
        dead = [
            r["id"] for r in rows
            if _effective_importance(r["importance"], True, r["created_at"], r["access_count"])
            < MIN_EFFECTIVE_IMPORTANCE
        ]
        if dead:
            cur.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in dead])
        # Hard cutoff safety net regardless of importance decay math above.
        cutoff = (datetime.now() - timedelta(days=TEMPORARY_TTL_DAYS * 3)).isoformat()
        cur.execute("DELETE FROM memories WHERE temporary = 1 AND created_at < ?", (cutoff,))
        return len(dead)


def stats() -> dict:
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM memories")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM memories WHERE temporary = 1")
        temp = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM conversation_summaries")
        convs = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM daily_summaries")
        days = cur.fetchone()["c"]
    return {"total_memories": total, "temporary": temp,
            "conversation_summaries": convs, "daily_summaries": days}


__all__ = [
    "remember", "search", "forget", "decay_sweep", "stats",
    "add_conversation_summary", "latest_conversation_summaries",
    "conversation_summaries_between", "upsert_daily_summary", "get_daily_summary",
    "meta_get", "meta_set", "embed_text", "score_importance", "MemoryHit",
    "CONTEXT_CHAR_BUDGET", "RECALL_TOP_K_DEFAULT",
]
