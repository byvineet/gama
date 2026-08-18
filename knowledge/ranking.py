"""
knowledge/ranking.py — Intelligent Ranking
=============================================
Takes the similarity-ordered candidates from `knowledge/index.py`'s
`semantic_search()` and re-scores them using signals that raw cosine
similarity can't see: what project/folder the user is currently in,
how recently and how often a file was opened, and what's already in
working memory / conversation context.

Deliberately reads signals from existing modules instead of storing
its own copy of "current project" or "recent files" — working_memory
and the index's own access_count/last_accessed are the single source
of truth for those, so this stays a pure function of state that
already exists elsewhere (no new state to keep in sync, no extra
writes on the hot path).

Kept as a pure, cheap re-scoring pass (list comprehension over an
already-small candidate set from semantic_search) — this must not
reintroduce the latency semantic_search's cache/pre-filter design
worked to avoid.

Author : Gama Knowledge Layer
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import List, Optional

from knowledge.index import SearchResult

# Weights: similarity dominates, everything else nudges the ordering.
# Tuned so a strong topical match never loses to a weak one just
# because it was opened five minutes ago — recency/project/popularity
# break ties and reorder near-equal matches, they don't override
# actual relevance.
W_SIMILARITY = 1.0
W_RECENCY = 0.18
W_PROJECT_MATCH = 0.22
W_FOLDER_MATCH = 0.10
W_POPULARITY = 0.08

RECENCY_HALF_LIFE_DAYS = 7  # a file opened a week ago still gets a modest boost


def _recency_score(last_accessed: Optional[str]) -> float:
    if not last_accessed:
        return 0.0
    try:
        ts = time.mktime(time.strptime(last_accessed, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0
    age_days = max(0.0, (time.time() - ts) / 86400.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def _popularity_score(access_count: int) -> float:
    # log-scaled so a file opened 100x doesn't completely dominate one
    # opened 3x — popularity is a nudge, not a ranking override.
    return min(1.0, math.log1p(access_count) / math.log1p(20))


def _current_context():
    """Best-effort read of working memory — never raises, never blocks
    a search if working memory isn't available for any reason."""
    try:
        from context_engine import working_memory
        return {
            "project": working_memory.get_slot("project"),
            "folder": working_memory.get_slot("folder"),
        }
    except Exception:
        return {"project": None, "folder": None}


def rerank(results: List[SearchResult], *, top_k: Optional[int] = None) -> List[SearchResult]:
    """Re-score and re-sort semantic_search() results using recency,
    current project/folder context, and popularity. Returns a new list
    — does not mutate the input or touch the index/cache."""
    if not results:
        return results

    ctx = _current_context()
    current_project = (ctx.get("project") or "").lower()
    current_folder = (ctx.get("folder") or "").lower()

    rescored: List[SearchResult] = []
    for r in results:
        rec = r.record
        score = W_SIMILARITY * r.score
        score += W_RECENCY * _recency_score(rec.last_accessed)
        score += W_POPULARITY * _popularity_score(rec.access_count)

        if current_project and rec.project and current_project in rec.project.lower():
            score += W_PROJECT_MATCH
        if current_folder and current_folder in rec.path.lower():
            score += W_FOLDER_MATCH

        rescored.append(SearchResult(record=rec, score=score))

    rescored.sort(key=lambda r: r.score, reverse=True)
    return rescored[:top_k] if top_k else rescored


__all__ = ["rerank"]
