"""
memory/facade.py — Single Source of Truth Gateway (C2 memory consolidation)
=============================================================================
GAMA_ARCHITECTURE_AUDIT.md, Issue #3, "Four Overlapping Memory Backends":

    The same user preference can conceivably be stored in `memory_manager`
    (JSON), `long_term.py` (SQLite), and `layered_memory.py` (SQLite) with
    no deduplication or consistency guarantee.

Concretely: the `save_memory` tool wrote into memory_manager's JSON store
while the `remember` tool wrote into long_term.db — same kind of data
(arbitrary user facts/preferences), two different backends, decided purely
by which tool name the model happened to call, with zero cross-checking.

This module does NOT physically merge the databases (that would need a
one-time migration + is high-risk to do blind, per the audit's own
complexity/risk rating for C2). Instead it fixes the actual problem — split
brain on write — by giving every write path in the codebase ONE function to
call, which then routes to the correct backend deterministically:

    Data shape                          Canonical backend
    -----------------------------------------------------------------------
    Structured key/value                memory/memory_manager.py (JSON)
      (category + key, e.g.
      preferences.voice, identity.name)
    Freeform natural-language fact      memory/long_term.py (SQLite),
      ("the user's dog is named Rex")     written via remember_dedup() so
                                           near-duplicate facts merge
                                           instead of piling up
    Working/session/episodic state      memory/layered_memory.py
      (in-turn slots, session log,        (unchanged — this was never
      episodic timeline)                  actually overlapping the above;
                                           it's short-term/session state,
                                           not long-term fact storage)
    Entity/relationship graph           memory/unified_memory.py
      (cross-referencing goals,            (unchanged — a distinct graph
      reminders, notes, files)             layer, not a fact store)

Reads go through the same funnel: `recall()` merges results from both the
structured store and the long-term fact store so a caller never has to
know (or guess) which backend an answer landed in.

Everything in `memory/context_builder.py` and the `save_memory` / `remember`
/ `recall_memory` tool handlers should call this module rather than reaching
into memory_manager.py or long_term.py directly.
"""

from __future__ import annotations

from typing import Any, Optional

from memory import long_term as _lt
from memory import memory_manager as _mm

# ── Structured key/value (identity, preferences, settings) ─────────────────


def get_preference(category: str, key: str) -> Optional[str]:
    """Canonical read for structured preferences/identity fields."""
    return _mm.get_memory(category, key)


def set_preference(category: str, key: str, value: str) -> None:
    """Canonical write for structured preferences/identity fields.

    Use this (not a freeform `remember()` call) whenever the data has a
    clear category+key shape — voice preference, language preference, a
    named setting, etc. Keeps that class of data in exactly one place.
    """
    _mm.set_memory(category, key, value)


def update_structured(new_data: dict) -> None:
    """Bulk structured update — mirrors memory_manager.update_memory().

    Exists so the `save_memory` tool (which historically wrote arbitrary
    category/key/value blobs) has a facade-level entry point instead of
    calling memory_manager directly.
    """
    _mm.update_memory(new_data)


# ── Freeform natural-language facts ─────────────────────────────────────────


def remember_fact(text: str, project: Optional[str] = None,
                   temporary: bool = False, importance: Optional[float] = None) -> int:
    """Canonical write for freeform facts. Always dedup-aware.

    This is what BOTH the `remember` tool and the `save_memory` tool should
    call when the value being stored is really a natural-language fact
    rather than a structured category/key/value pair (the audit's exact
    complaint: these two tools used to diverge into different backends for
    what is conceptually the same kind of write).
    """
    return _lt.remember_dedup(
        text, kind="fact", project=project,
        importance=importance, temporary=temporary,
    )


def forget_fact(query: str, project: Optional[str] = None) -> str:
    """Delete a previously stored fact that best matches `query`, for the
    `forget_memory` tool. Returns a human-readable confirmation string —
    never raw ids."""
    query = (query or "").strip()
    if not query:
        return "Tell me what to forget."
    deleted = _lt.forget(query, project=project)
    if not deleted:
        return "I couldn't find anything matching that to forget."
    if len(deleted) == 1:
        return f"Okay, I've forgotten: {deleted[0].content[:120]}"
    lines = [f"  - {h.content[:120]}" for h in deleted]
    return "Okay, I've forgotten:\n" + "\n".join(lines)


def recall_structured(query: str, top_k: int = 5, project: Optional[str] = None) -> list[dict[str, Any]]:
    """Merged recall across the structured store and the fact store.

    Structured hits (exact category/key text match) are returned first —
    they're precise by construction — followed by ranked semantic/importance
    hits from the long-term fact store. Callers get one ranked list instead
    of having to query two backends and reconcile the results themselves.
    """
    results: list[dict[str, Any]] = []

    # Structured store: cheap substring scan over category/key/value —
    # memory_manager's JSON store is small (identity/preferences only) so
    # this is fast and doesn't need its own index.
    try:
        mem = _mm.load_memory()
        q = query.lower().strip()
        if q:
            for category, items in mem.items():
                if not isinstance(items, dict):
                    continue
                for key, entry in items.items():
                    value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
                    haystack = f"{category} {key} {value}".lower()
                    if q in haystack:
                        results.append({
                            "source": "structured",
                            "category": category,
                            "key": key,
                            "content": value,
                            "score": 1.0,
                        })
    except Exception:
        pass

    # Long-term fact store: semantic + importance ranked.
    try:
        for hit in _lt.search(query, top_k=top_k, project=project):
            results.append({
                "source": "fact",
                "id": hit.id,
                "content": hit.content,
                "score": hit.score,
            })
    except Exception:
        pass

    return results[:top_k]


def recall(query: str, top_k: int = 5, project: Optional[str] = None) -> str:
    """On-demand memory search, for the `recall_memory` tool.

    Returns a short, human-readable string GAMA can speak from — never
    raw JSON, never the whole store. Merges structured (preferences) and
    freeform-fact hits so the tool works regardless of which backend the
    original write landed in.
    """
    query = (query or "").strip()
    if not query:
        return "No search query given."
    hits = recall_structured(query, top_k=top_k, project=project)
    if not hits:
        return "I don't have any memory of that."
    lines = [f"- {h['content'][:240]}" for h in hits if h.get("content")]
    if not lines:
        return "I don't have any memory of that."
    return "Here's what I remember:\n" + "\n".join(lines)


# ── Re-exports for convenience (so callers only need `from memory import facade`) ─
decay_sweep = _lt.decay_sweep
stats = _lt.stats


def master_connection_check() -> bool:
    """Verify that memory connection backends are loaded and operational."""
    try:
        conn = _lt._get_conn()
        res = conn.execute("PRAGMA journal_mode").fetchone()
        return res is not None
    except Exception:
        return False

