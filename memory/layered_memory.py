"""
memory/layered_memory.py — Five-Layer Memory System
=====================================================
Phase 5 of the JARVIS intelligence architecture.

Implements layered memory with decay, confidence scoring, and relationship graphs:

  Layer 1 — Working Memory    (volatile, current turn only)
  Layer 2 — Session Memory    (in-memory, cleared at session end)
  Layer 3 — Long-Term Memory  (SQLite, persists across sessions)
  Layer 4 — Episodic Memory   (event timeline, what happened when)
  Layer 5 — Semantic Graph    (entity-relationship knowledge graph)

NOTE (C2, GAMA_ARCHITECTURE_AUDIT.md — memory consolidation): Layer 3 here
is intentionally narrow-scope — it exists for `learning/workflow_learner.py`
to persist workflow-pattern facts (e.g. "user usually opens VS Code then a
terminal") with confidence/decay semantics this module already provides.
It is NOT where general user facts/preferences should be written. That
canonical path is `memory/facade.py` (`remember_fact`, `set_preference`),
which fronts `memory/long_term.py` (freeform facts, dedup-aware) and
`memory/memory_manager.py` (structured preferences). If you're adding a new
fact-writing code path and it isn't specifically workflow-pattern learning,
use the facade instead of calling this module's `.remember()` directly.

Each memory item carries:
  • importance  — how relevant is this? (0.0–1.0)
  • confidence  — how certain are we? (0.0–1.0)
  • frequency   — how often does this come up?
  • recency     — time-weighted freshness
  • decay       — exponential decay factor

The Memory System enriches the World Model. The World Model provides
context for retrieval. Both communicate via the process-wide EventBus.

Author : Vineet Machchal
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import get_logger

log = get_logger(__name__)

from utils.paths import user_data_path
_DB_PATH = user_data_path("memory/layered_memory.db")
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Memory item
# ---------------------------------------------------------------------------

@dataclass
class MemoryItem:
    """A single memory with full scoring metadata."""
    key: str                              # unique identifier
    value: Any                            # what was remembered
    layer: str = "session"                # working / session / long_term / episodic
    importance: float = 0.5              # 0.0 → trivial, 1.0 → critical
    confidence: float = 1.0             # 0.0 → guessed, 1.0 → confirmed
    frequency: int = 1                   # how many times seen
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    decay_rate: float = 0.1              # higher = forgets faster
    tags: List[str] = field(default_factory=list)
    source: str = ""

    @property
    def recency(self) -> float:
        """0.0 (very old) → 1.0 (just now). Decays over 24h."""
        age_h = (time.time() - self.last_accessed) / 3600.0
        return math.exp(-self.decay_rate * age_h)

    @property
    def salience(self) -> float:
        """Overall memory strength: importance × confidence × recency × log(frequency)."""
        freq_boost = math.log1p(self.frequency) / math.log1p(10)  # normalized [0,1]
        return self.importance * self.confidence * self.recency * (0.5 + 0.5 * freq_boost)

    def touch(self) -> None:
        """Record an access — raises recency and frequency."""
        self.last_accessed = time.time()
        self.frequency += 1


# ---------------------------------------------------------------------------
# Layer 1 — Working Memory (current turn, no persistence)
# ---------------------------------------------------------------------------

class WorkingMemory:
    """
    Volatile slot store for the current conversation turn.
    Cleared at the start of each new turn / on demand.
    """

    def __init__(self) -> None:
        self._slots: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._slots[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._slots.get(key, default)

    def clear(self) -> None:
        with self._lock:
            self._slots.clear()

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._slots)

    def resolve(self, pronoun: str) -> Optional[Any]:
        """Resolve common pronouns to their referents."""
        mapping = {
            "it": ["file", "song", "app", "url", "folder", "message"],
            "that": ["file", "song", "url", "last_result"],
            "this": ["file", "clipboard", "selection"],
            "there": ["folder", "directory", "location"],
            "them": ["files", "results", "contacts"],
        }
        p = pronoun.lower().strip()
        with self._lock:
            for candidate in mapping.get(p, [p]):
                if candidate in self._slots:
                    return self._slots[candidate]
        return None


# ---------------------------------------------------------------------------
# Layer 2 — Session Memory (in-memory, lasts the session)
# ---------------------------------------------------------------------------

class SessionMemory:
    """
    Short-lived facts from the current session.
    Decays fast — high decay_rate by default.
    """

    def __init__(self, max_items: int = 500) -> None:
        self._items: Dict[str, MemoryItem] = {}
        self._max = max_items
        self._lock = threading.RLock()

    def remember(
        self,
        key: str,
        value: Any,
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        source: str = "",
    ) -> MemoryItem:
        with self._lock:
            if key in self._items:
                item = self._items[key]
                item.value = value
                item.touch()
                item.importance = max(item.importance, importance)
            else:
                item = MemoryItem(
                    key=key, value=value, layer="session",
                    importance=importance, decay_rate=0.3,
                    tags=tags or [], source=source,
                )
                self._items[key] = item
                self._trim()
            return item

    def recall(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._items.get(key)
        if item:
            item.touch()
            return item.value
        return None

    def search(self, query: str, top_k: int = 5) -> List[MemoryItem]:
        q = query.lower()
        with self._lock:
            matches = [
                i for i in self._items.values()
                if q in str(i.value).lower() or q in i.key.lower()
                   or any(q in t for t in i.tags)
            ]
        return sorted(matches, key=lambda x: x.salience, reverse=True)[:top_k]

    def _trim(self) -> None:
        if len(self._items) > self._max:
            sorted_keys = sorted(self._items.keys(), key=lambda k: self._items[k].salience)
            for k in sorted_keys[:len(self._items) - self._max]:
                del self._items[k]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# ---------------------------------------------------------------------------
# Layer 4 — Episodic Memory (event timeline)
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    """A single event in the episodic timeline."""
    episode_id: str
    what: str                  # description of what happened
    context: Dict[str, Any]   # snapshot of context at the time
    outcome: str = ""          # what was the result
    importance: float = 0.5
    timestamp: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)


class EpisodicMemory:
    """
    Ordered log of significant events (what happened, when, in what context).
    Supports temporal search: "what was I doing at 3pm?", "last time I opened Chrome".
    """

    def __init__(self, max_episodes: int = 1000) -> None:
        self._episodes: List[Episode] = []
        self._max = max_episodes
        self._lock = threading.RLock()
        self._counter = 0

    def record(
        self,
        what: str,
        context: Optional[Dict[str, Any]] = None,
        outcome: str = "",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
    ) -> Episode:
        with self._lock:
            self._counter += 1
            ep = Episode(
                episode_id=f"ep_{self._counter}",
                what=what,
                context=context or {},
                outcome=outcome,
                importance=importance,
                tags=tags or [],
            )
            self._episodes.append(ep)
            if len(self._episodes) > self._max:
                # Remove lowest-importance old episodes
                self._episodes.sort(key=lambda e: (e.importance, e.timestamp))
                self._episodes = self._episodes[len(self._episodes) - self._max:]
            return ep

    def search(self, query: str, top_k: int = 5, since: Optional[float] = None) -> List[Episode]:
        q = query.lower()
        with self._lock:
            episodes = list(self._episodes)
        if since:
            episodes = [e for e in episodes if e.timestamp >= since]
        matches = [
            e for e in episodes
            if q in e.what.lower() or any(q in t for t in e.tags)
               or q in str(e.context).lower()
        ]
        # Sort by importance × recency
        now = time.time()
        matches.sort(
            key=lambda e: e.importance * math.exp(-0.1 * (now - e.timestamp) / 3600),
            reverse=True,
        )
        return matches[:top_k]

    def recent(self, n: int = 10) -> List[Episode]:
        with self._lock:
            return list(self._episodes[-n:])


# ---------------------------------------------------------------------------
# Layer 5 — Semantic Graph Memory (entity-relationship knowledge graph)
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    entity: str
    entity_type: str = "thing"   # person / app / file / concept / preference
    attributes: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)


@dataclass
class GraphEdge:
    from_entity: str
    relation: str   # "uses", "prefers", "owns", "knows", "dislikes", etc.
    to_entity: str
    weight: float = 1.0
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)


class SemanticGraph:
    """
    Entity-relationship graph for long-term semantic knowledge.

    Examples:
      ("Vineet", "prefers", "dark mode")
      ("Vineet", "uses", "VS Code")
      ("VS Code", "opens_files_of_type", ".py")
      ("morning", "starts_with", "Spotify")
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, GraphNode] = {}
        self._edges: List[GraphEdge] = []
        self._lock = threading.RLock()

    def add_node(self, entity: str, entity_type: str = "thing", **attributes: Any) -> GraphNode:
        with self._lock:
            if entity in self._nodes:
                self._nodes[entity].attributes.update(attributes)
                return self._nodes[entity]
            node = GraphNode(entity=entity, entity_type=entity_type, attributes=attributes)
            self._nodes[entity] = node
            return node

    def add_edge(
        self,
        from_entity: str,
        relation: str,
        to_entity: str,
        weight: float = 1.0,
        confidence: float = 1.0,
    ) -> GraphEdge:
        # Ensure nodes exist
        self.add_node(from_entity)
        self.add_node(to_entity)

        with self._lock:
            # Check for existing edge — update weight instead of duplicating
            for e in self._edges:
                if e.from_entity == from_entity and e.relation == relation and e.to_entity == to_entity:
                    e.weight = min(1.0, e.weight + 0.1)
                    e.confidence = max(e.confidence, confidence)
                    return e
            edge = GraphEdge(
                from_entity=from_entity,
                relation=relation,
                to_entity=to_entity,
                weight=weight,
                confidence=confidence,
            )
            self._edges.append(edge)
            return edge

    def neighbors(self, entity: str, relation: Optional[str] = None) -> List[Tuple[str, str, float]]:
        """Return [(relation, to_entity, weight)] for an entity."""
        with self._lock:
            results = [
                (e.relation, e.to_entity, e.weight)
                for e in self._edges
                if e.from_entity == entity and (relation is None or e.relation == relation)
            ]
        return sorted(results, key=lambda x: x[2], reverse=True)

    def get_preferences(self, entity: str = "Vineet") -> Dict[str, str]:
        """Return entity's preferences as {subject: preferred_value}."""
        prefs = {}
        for rel, target, _ in self.neighbors(entity, "prefers"):
            prefs[rel] = target
        return prefs

    def find(self, entity: str) -> Optional[GraphNode]:
        with self._lock:
            return self._nodes.get(entity)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"nodes": len(self._nodes), "edges": len(self._edges)}


# ---------------------------------------------------------------------------
# Layer 3 — Long-Term Memory (SQLite-backed, persists across sessions)
# ---------------------------------------------------------------------------

class LongTermLayeredMemory:
    """
    SQLite-backed memory for facts that should survive across sessions.
    Complements (does not replace) the existing memory/long_term.py.
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS layered_memories (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    importance REAL DEFAULT 0.5,
                    confidence REAL DEFAULT 1.0,
                    frequency INTEGER DEFAULT 1,
                    created_at REAL,
                    last_accessed REAL,
                    decay_rate REAL DEFAULT 0.05,
                    tags TEXT DEFAULT '[]',
                    source TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodic_log (
                    episode_id TEXT PRIMARY KEY,
                    what TEXT NOT NULL,
                    context TEXT,
                    outcome TEXT,
                    importance REAL DEFAULT 0.5,
                    timestamp REAL,
                    tags TEXT DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS graph_edges (
                    from_entity TEXT,
                    relation TEXT,
                    to_entity TEXT,
                    weight REAL DEFAULT 1.0,
                    confidence REAL DEFAULT 1.0,
                    created_at REAL,
                    PRIMARY KEY (from_entity, relation, to_entity)
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # H2 (GAMA_ARCHITECTURE_AUDIT.md): WAL mode is the standard SQLite
        # protection against corruption on crash/power-loss. A prior
        # non-WAL crash left `layered_memory.db.corrupt_backup` in this
        # directory — this closes that gap the same way long_term.py
        # already does.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def persist(self, item: MemoryItem) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO layered_memories
                    (key, value, importance, confidence, frequency, created_at,
                     last_accessed, decay_rate, tags, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    importance=MAX(importance, excluded.importance),
                    confidence=excluded.confidence,
                    frequency=frequency + 1,
                    last_accessed=excluded.last_accessed,
                    tags=excluded.tags,
                    source=excluded.source
            """, (
                item.key,
                json.dumps(item.value) if not isinstance(item.value, str) else item.value,
                item.importance, item.confidence, item.frequency,
                item.created_at, item.last_accessed, item.decay_rate,
                json.dumps(item.tags), item.source,
            ))

    def recall(self, key: str) -> Optional[MemoryItem]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM layered_memories WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        item = MemoryItem(
            key=row["key"], value=value, layer="long_term",
            importance=row["importance"], confidence=row["confidence"],
            frequency=row["frequency"], created_at=row["created_at"],
            last_accessed=row["last_accessed"], decay_rate=row["decay_rate"],
            tags=json.loads(row["tags"] or "[]"), source=row["source"] or "",
        )
        # Update access time
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE layered_memories SET last_accessed=?, frequency=frequency+1 WHERE key=?",
                (time.time(), key),
            )
        return item

    def search(self, query: str, top_k: int = 5, min_importance: float = 0.0) -> List[MemoryItem]:
        q = f"%{query.lower()}%"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM layered_memories
                   WHERE (LOWER(key) LIKE ? OR LOWER(value) LIKE ? OR LOWER(tags) LIKE ?)
                   AND importance >= ?
                   ORDER BY importance DESC, last_accessed DESC
                   LIMIT ?""",
                (q, q, q, min_importance, top_k * 2),
            ).fetchall()
        items = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                value = row["value"]
            items.append(MemoryItem(
                key=row["key"], value=value, layer="long_term",
                importance=row["importance"], confidence=row["confidence"],
                frequency=row["frequency"], created_at=row["created_at"],
                last_accessed=row["last_accessed"], decay_rate=row["decay_rate"],
                tags=json.loads(row["tags"] or "[]"), source=row["source"] or "",
            ))
        # Sort by salience
        items.sort(key=lambda x: x.salience, reverse=True)
        return items[:top_k]

    def decay_sweep(self, min_salience: float = 0.05) -> int:
        """Remove memories whose salience has decayed below the threshold."""
        now = time.time()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT key, importance, confidence, frequency, last_accessed, decay_rate FROM layered_memories"
            ).fetchall()
            to_delete = []
            for row in rows:
                age_h = (now - row["last_accessed"]) / 3600.0
                recency = math.exp(-row["decay_rate"] * age_h)
                freq_boost = math.log1p(row["frequency"]) / math.log1p(10)
                salience = row["importance"] * row["confidence"] * recency * (0.5 + 0.5 * freq_boost)
                if salience < min_salience:
                    to_delete.append(row["key"])
            if to_delete:
                conn.executemany(
                    "DELETE FROM layered_memories WHERE key=?",
                    [(k,) for k in to_delete],
                )
            return len(to_delete)

    def persist_episode(self, episode: Episode) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO episodic_log
                    (episode_id, what, context, outcome, importance, timestamp, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                episode.episode_id, episode.what,
                json.dumps(episode.context), episode.outcome,
                episode.importance, episode.timestamp,
                json.dumps(episode.tags),
            ))

    def persist_edge(self, edge: GraphEdge) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO graph_edges (from_entity, relation, to_entity, weight, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_entity, relation, to_entity) DO UPDATE SET
                    weight=MIN(1.0, weight + 0.1),
                    confidence=MAX(confidence, excluded.confidence)
            """, (
                edge.from_entity, edge.relation, edge.to_entity,
                edge.weight, edge.confidence, edge.created_at,
            ))


# ---------------------------------------------------------------------------
# Unified Layered Memory — the public interface
# ---------------------------------------------------------------------------

class LayeredMemory:
    """
    Public interface to all five memory layers.

    Usage::

        from memory.layered_memory import layered_memory

        # Remember something (auto-selects layer)
        layered_memory.remember("user.prefers_dark_mode", True, importance=0.8, persist=True)

        # Recall (searches all layers, returns best match)
        val = layered_memory.recall("user.prefers_dark_mode")

        # Record an event
        layered_memory.record_episode("Opened VS Code", context={"folder": "~/project"})

        # Semantic relationships
        layered_memory.graph.add_edge("Vineet", "prefers", "dark mode")
        prefs = layered_memory.graph.get_preferences("Vineet")
    """

    def __init__(self) -> None:
        self.working = WorkingMemory()
        self.session = SessionMemory()
        self.episodic = EpisodicMemory()
        self.graph = SemanticGraph()
        self.long_term = LongTermLayeredMemory()
        self._bus = None

    def remember(
        self,
        key: str,
        value: Any,
        importance: float = 0.5,
        persist: bool = False,
        tags: Optional[List[str]] = None,
        source: str = "",
    ) -> MemoryItem:
        """Store a fact. If persist=True, also writes to long-term SQLite."""
        item = self.session.remember(key, value, importance=importance, tags=tags, source=source)
        if persist or importance >= 0.7:
            try:
                self.long_term.persist(item)
            except Exception as exc:
                log.warning(f"[LayeredMemory] Long-term persist failed: {exc}")
        return item

    def recall(self, key: str) -> Optional[Any]:
        """Recall from working → session → long_term (first match wins)."""
        v = self.working.get(key)
        if v is not None:
            return v
        v = self.session.recall(key)
        if v is not None:
            return v
        item = self.long_term.recall(key)
        if item is not None:
            return item.value
        return None

    def search(self, query: str, top_k: int = 5) -> List[MemoryItem]:
        """Search across session and long-term memory, merged and ranked by salience."""
        session_hits = self.session.search(query, top_k=top_k)
        lt_hits = self.long_term.search(query, top_k=top_k)
        seen = set()
        merged = []
        for item in session_hits + lt_hits:
            if item.key not in seen:
                seen.add(item.key)
                merged.append(item)
        return sorted(merged, key=lambda x: x.salience, reverse=True)[:top_k]

    def record_episode(
        self,
        what: str,
        context: Optional[Dict[str, Any]] = None,
        outcome: str = "",
        importance: float = 0.4,
        persist: bool = True,
        tags: Optional[List[str]] = None,
    ) -> Episode:
        ep = self.episodic.record(
            what=what, context=context, outcome=outcome,
            importance=importance, tags=tags,
        )
        if persist and importance >= 0.4:
            try:
                self.long_term.persist_episode(ep)
            except Exception as exc:
                log.warning(f"[LayeredMemory] Episode persist failed: {exc}")
        return ep

    def sync_to_world(self) -> None:
        """Push high-salience session memories into the World Model."""
        try:
            from core.world_model import world
            prefs = self.graph.get_preferences()
            if prefs:
                world.update_user(preferences=prefs)
        except Exception:
            pass

    def run_decay_sweep(self) -> int:
        """Remove stale long-term memories. Call periodically (e.g. daily)."""
        try:
            removed = self.long_term.decay_sweep()
            if removed:
                log.info(f"[LayeredMemory] Decay sweep removed {removed} stale memories.")
            return removed
        except Exception as exc:
            log.warning(f"[LayeredMemory] Decay sweep failed: {exc}")
            return 0


# Process-wide singleton
layered_memory = LayeredMemory()

__all__ = [
    "MemoryItem", "Episode", "GraphNode", "GraphEdge",
    "WorkingMemory", "SessionMemory", "EpisodicMemory",
    "SemanticGraph", "LongTermLayeredMemory", "LayeredMemory",
    "layered_memory",
]
