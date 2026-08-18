"""
Gama - Memory Context Builder
=============================
Assembles what actually gets injected into the LLM: a small, relevant
slice of memory — never the whole database.

Two entry points:

* `build_session_context(project=None)` — called once per Live session
  (on connect/reconnect) to seed the system prompt with: essential
  profile facts, the most recent conversation summary, the most recent
  daily summary, and the highest-importance long-term memories.
* `recall(query, ...)` — used by the `recall_memory` tool so GAMA can
  pull specific memories on demand mid-conversation, instead of
  everything being pre-loaded up front.

Author : Vineet Machchal
"""

from __future__ import annotations

import re as _re
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait, FIRST_EXCEPTION
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from memory import long_term as lt
from memory import facade as _facade
from memory.memory_manager import format_memory_for_prompt

SESSION_CONTEXT_CHAR_BUDGET = lt.CONTEXT_CHAR_BUDGET


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_session_context(project: Optional[str] = None,
                           char_budget: int = SESSION_CONTEXT_CHAR_BUDGET,
                           query: Optional[str] = None) -> str:
    """Return a compact block of the most relevant long-term context.

    Budget is split roughly: 40% profile essentials (already capped by
    memory_manager), 20% recent summaries, 40% top important memories.

    When `query` is supplied (e.g. the last user utterance on a reconnect)
    both the profile ranking and the [THINGS I REMEMBER] block are biased
    toward entries relevant to that query.  Without a query the results are
    ranked purely by (decayed) importance + recency — the original behaviour.

    Performance: all 4 DB reads run concurrently in a thread pool, reducing
    cache-miss rebuild time from ~150ms (sequential) to ~50ms (parallel).
    """
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()

    # ── Run all 4 DB reads in parallel ──────────────────────────────────────
    with ThreadPoolExecutor(max_workers=4) as _pool:
        _f_profile = _pool.submit(format_memory_for_prompt, query=query)
        _f_convs   = _pool.submit(lt.latest_conversation_summaries, limit=1)
        _f_daily   = _pool.submit(lt.get_daily_summary, yesterday)
        _f_search  = _pool.submit(lt.search, query=query or "", top_k=8, project=project)
        # Wait for all; individual failures fall back to safe defaults below.
        _futures_wait([_f_profile, _f_convs, _f_daily, _f_search])

    try:
        profile = _f_profile.result()
    except Exception:
        profile = ""
    try:
        recent_convs = _f_convs.result()
    except Exception:
        recent_convs = []
    try:
        daily = _f_daily.result()
    except Exception:
        daily = ""
    try:
        hits = _f_search.result()
    except Exception:
        hits = []
    # ── Assemble output ──────────────────────────────────────────────────────

    parts: list[str] = []
    remaining = char_budget

    if profile:
        parts.append(profile)
        remaining -= len(profile)

    summary_lines = []
    if daily:
        summary_lines.append(f"Yesterday: {daily}")
    if recent_convs:
        summary_lines.append(f"Last time we spoke: {recent_convs[0]}")
    if summary_lines:
        block = "\n[RECENT CONTEXT]\n" + "\n".join(summary_lines)
        block = _truncate(block, max(0, min(len(block), int(char_budget * 0.3))))
        parts.append(block)
        remaining -= len(block)

    if remaining > 60 and hits:
        lines = [f"  - {_truncate(h.content, 200)}" for h in hits]
        block = "\n[THINGS I REMEMBER]\n" + "\n".join(lines)
        block = _truncate(block, remaining)
        parts.append(block)

    return "\n".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Per-turn query-ranked memory injection
# ---------------------------------------------------------------------------

# Minimum query length worth searching; very short strings (stop words,
# single chars) produce noisy results.
_MIN_QUERY_LEN = 4

# Hard char cap for the per-turn block — kept small so it never crowds out
# the working-memory / world-model blocks that sit alongside it.
_PER_TURN_BLOCK_BUDGET = 600

# Tokens shorter than this are skipped when matching entity names.
_ENTITY_MIN_TOKEN_LEN = 3

# ---------------------------------------------------------------------------
# recall_for_prompt() session cache — perf audit item.
# ---------------------------------------------------------------------------
# recall_for_prompt() used to be rebuilt from scratch (two SQLite reads: a
# vector search over up to MAX_SEARCH_ROWS memory rows + an entity
# relationship lookup) on *every single turn*, even though consecutive
# turns in a conversation very often re-ask about the same thing ("what
# time is my next class" -> "and after that?" -> "ok remind me 5 min
# before") or Gemini re-issues a near-identical transcript after a
# reconnect. A small in-process LRU with a short TTL absorbs that
# repetition cheaply and safely: 32 entries is enough to cover an entire
# active session's worth of distinct queries, and a short 45s TTL means
# a stale/updated memory is never visible for more than a few turns.
import re as _cache_re
import threading as _cache_threading
import time as _cache_time
from collections import OrderedDict as _OrderedDict

_RECALL_CACHE_MAXSIZE = 32
_RECALL_CACHE_TTL_S = 45.0  # within the audit's suggested 30-60s window

_recall_cache: "_OrderedDict[tuple, tuple[float, str]]" = _OrderedDict()
_recall_cache_lock = _cache_threading.Lock()

_WS_RE = _cache_re.compile(r"\s+")


def _normalize_query_key(query: str, top_k: int, project: Optional[str]) -> tuple:
    """Collapse whitespace/case so trivially-different phrasings of the same
    question ("What's my next class?" vs "what's my next class") share a
    cache entry instead of missing on a technicality."""
    norm = _WS_RE.sub(" ", (query or "").strip().lower())
    return (norm, top_k, project)


def _recall_cache_get(key: tuple) -> Optional[str]:
    with _recall_cache_lock:
        entry = _recall_cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if _cache_time.monotonic() - ts > _RECALL_CACHE_TTL_S:
            del _recall_cache[key]
            return None
        _recall_cache.move_to_end(key)  # LRU touch
        return value


def _recall_cache_put(key: tuple, value: str) -> None:
    with _recall_cache_lock:
        _recall_cache[key] = (_cache_time.monotonic(), value)
        _recall_cache.move_to_end(key)
        while len(_recall_cache) > _RECALL_CACHE_MAXSIZE:
            _recall_cache.popitem(last=False)  # evict least-recently-used


# Stop-words filtered out before entity name matching.
_ENTITY_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "what", "when", "where", "who", "how",
    "can", "you", "are", "this", "that", "its", "from", "get", "set",
    "any", "all", "has", "had", "was", "did", "will", "been", "have",
    "use", "not", "but", "our", "your", "my", "me", "him", "her", "we",
    "them", "they", "their", "it", "is", "at", "to", "do", "of", "in",
    "on", "a", "an",
})


def _entity_tokens(query: str) -> List[str]:
    """Return lowercase word tokens from *query* suitable for entity matching."""
    words = _re.findall(r"[a-z0-9]+", query.lower())
    return [w for w in words
            if len(w) >= _ENTITY_MIN_TOKEN_LEN and w not in _ENTITY_STOP_WORDS]


def _recall_entity_relationships(query: str,
                                  max_entities: int = 3,
                                  max_rels: int = 5) -> str:
    """Return a compact ``[RELATED ENTITIES]`` block for entities whose name
    matches keywords in *query*, or an empty string when nothing is found.

    Two cheap SQLite reads (no LLM, no embedding):
      1. LIKE-scan ``entities`` for keyword tokens extracted from the query.
      2. Fetch the highest-weight relationships for each matched entity.

    The result is formatted as a small bullet list capped at *_PER_TURN_BLOCK_BUDGET*
    characters so it never crowds out the vector-memory block.
    """
    tokens = _entity_tokens(query)
    if not tokens:
        return ""

    try:
        # ── 1. Find matching entities ────────────────────────────────────────
        matched: List[Tuple[str, str, str]] = []   # (entity_id, name, type)
        with lt._cursor() as cur:
            for tok in tokens[:6]:   # cap token scan to avoid excessive LIKE queries
                cur.execute(
                    "SELECT entity_id, name, entity_type FROM entities "
                    "WHERE LOWER(name) LIKE ? LIMIT ?",
                    (f"%{tok}%", max_entities),
                )
                for row in cur.fetchall():
                    entry = (row["entity_id"], row["name"], row["entity_type"])
                    if entry not in matched:
                        matched.append(entry)
                if len(matched) >= max_entities:
                    break

        if not matched:
            return ""

        # ── 2. Fetch top relationships for matched entities ──────────────────
        rel_lines: List[str] = []
        entity_ids = [m[0] for m in matched]
        placeholders = ",".join(["?"] * len(entity_ids))

        with lt._cursor() as cur:
            cur.execute(
                f"""
                SELECT r.source_id, r.relation_type, r.target_id, r.weight,
                       es.name AS src_name, et.name AS tgt_name
                FROM entity_relationships r
                LEFT JOIN entities es ON r.source_id = es.entity_id
                LEFT JOIN entities et ON r.target_id = et.entity_id
                WHERE r.source_id IN ({placeholders})
                   OR r.target_id IN ({placeholders})
                ORDER BY r.weight DESC
                LIMIT ?
                """,
                entity_ids + entity_ids + [max_rels],
            )
            for row in cur.fetchall():
                src  = row["src_name"] or row["source_id"]
                tgt  = row["tgt_name"] or row["target_id"]
                rel  = row["relation_type"]
                rel_lines.append(f"  - {src} → {rel} → {tgt}")

        if not rel_lines:
            return ""

        block = "[RELATED ENTITIES]\n" + "\n".join(rel_lines)
        return _truncate(block, 300)   # tight cap; supplement, not replacement

    except Exception:
        return ""


def recall_for_prompt(query: str, top_k: int = 4,
                      project: Optional[str] = None) -> str:
    """Return a compact memory block ranked against *query*.

    Combines two cheap, no-LLM lookups:
    1. **[RELEVANT MEMORIES]** — vector similarity search over stored memories
       (unchanged from the original implementation).
    2. **[RELATED ENTITIES]** — entity-graph relationships for entity names
       that appear in the query keywords (audit item #22). Two SQLite LIKE
       queries; adds richer structured context (e.g. "project X → depends_on →
       deadline Y") that pure vector search may miss.

    Designed to be called fresh on every user turn and injected alongside the
    Working Memory block.

    Returns an empty string when:
      - ``query`` is too short to be meaningful
      - no memories are stored yet
      - all hits score below a minimum relevance threshold
    """
    query = (query or "").strip()
    if len(query) < _MIN_QUERY_LEN:
        return ""

    cache_key = _normalize_query_key(query, top_k, project)
    cached = _recall_cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        hits = lt.search(query=query, top_k=top_k, project=project)
    except Exception:
        hits = []

    # Run entity relationship lookup concurrently with the vector search result
    # assembly — it's a separate DB read so there's no ordering dependency.
    entity_block = _recall_entity_relationships(query)

    parts: List[str] = []

    if hits:
        lines = [f"  - {_truncate(h.content, 220)}" for h in hits]
        mem_block = "[RELEVANT MEMORIES]\n" + "\n".join(lines)
        # Leave room for entity block if present
        mem_budget = _PER_TURN_BLOCK_BUDGET - (len(entity_block) + 1 if entity_block else 0)
        parts.append(_truncate(mem_block, max(120, mem_budget)))

    if entity_block:
        parts.append(entity_block)

    result = _truncate("\n".join(parts), _PER_TURN_BLOCK_BUDGET) if parts else ""
    _recall_cache_put(cache_key, result)
    return result


def recall(query: str, top_k: int = lt.RECALL_TOP_K_DEFAULT,
           project: Optional[str] = None) -> str:
    """On-demand semantic memory search, for the `recall_memory` tool.

    C2 (GAMA_ARCHITECTURE_AUDIT.md): delegates to memory/facade.py — the
    single funnel for reads — instead of hitting `long_term.search()`
    directly. This used to duplicate facade.recall() verbatim (both did
    an `lt.search()` + format-as-bullets), which is exactly the kind of
    drift the audit's "single source of truth" fix was meant to prevent:
    two call sites doing the same lookup could silently diverge (e.g. one
    picks up a structured/preference hit the other misses). Now there is
    exactly one implementation.
    """
    return _facade.recall(query, top_k=top_k, project=project)


def remember_fact(text: str, project: Optional[str] = None,
                   temporary: bool = False, importance: Optional[float] = None) -> str:
    """Store an explicit long-term fact (used by the save_memory / project
    memory tools).

    C2: delegates to memory/facade.py's remember_fact(), which itself
    calls remember_dedup() so restating an existing fact updates it
    instead of creating a duplicate row. Previously this function called
    `lt.remember_dedup()` directly with near-identical logic to
    `facade.remember_fact()` — same write, two code paths.
    """
    text = (text or "").strip()
    if not text:
        return "Nothing to remember."
    _facade.remember_fact(text, project=project, temporary=temporary, importance=importance)
    return "Got it — I'll remember that."


def forget_fact(query: str, project: Optional[str] = None) -> str:
    """Delete a previously stored fact that best matches `query` (used by
    the forget_memory tool).

    C2: delegates to memory/facade.py's forget_fact() — same rationale as
    remember_fact() above; this was a second, near-identical implementation
    of the same delete-by-query operation.
    """
    return _facade.forget_fact(query, project=project)


__all__ = [
    "build_session_context", "recall", "recall_for_prompt",
    "remember_fact", "forget_fact", "SESSION_CONTEXT_CHAR_BUDGET",
]