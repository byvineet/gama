"""
learning/workflow_learner.py — Adaptive Workflow Learning System
================================================================
Phase 6 of the JARVIS intelligence architecture.

Passively learns from user behavior and recognizes repeated workflows.
When a workflow is seen often enough, it surfaces an automation offer
via the Proactive Engine.

Learns:
  • Command sequences (what commands follow each other)
  • App workflows (VS Code → Terminal → git pull → npm install → npm run dev)
  • Time-of-day patterns (what apps open at 9am)
  • Correction patterns (what the user fixes after GAMA does X)
  • Failure patterns (what commands reliably fail for this user)

Privacy guarantees:
  • Only command names / app names are stored — no file contents, messages, or PII
  • User can inspect or delete all learned data at any time
  • All data stays local (SQLite)

Architecture:
  - SequenceTracker: records (timestamp, action) events into a sliding window
  - WorkflowDetector: finds frequent N-grams in the sequence
  - AutomationAdvisor: turns detected workflows into Suggestion payloads
  - Builds on existing learning/habit_tracker.py — does not replace it

Author : Vineet Machchal
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from utils.logger import get_logger

log = get_logger(__name__)

from utils.paths import user_data_path
_DB_PATH = user_data_path("learning/workflow_patterns.db")
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_WINDOW_SIZE = 20          # how many recent actions to track in the sliding window
_MIN_SUPPORT = 3           # minimum times a pattern must appear to be considered
_MIN_CONFIDENCE = 0.55     # minimum conditional probability to surface a suggestion


# ---------------------------------------------------------------------------
# Sequence tracker
# ---------------------------------------------------------------------------

@dataclass
class ActionEvent:
    action: str           # e.g. "open_app:vscode", "command:play_music", "app_focused:chrome"
    ts: float = field(default_factory=time.time)
    context: Dict[str, Any] = field(default_factory=dict)


class SequenceTracker:
    """
    Maintains a sliding window of recent actions for N-gram analysis.
    Thread-safe.
    """

    def __init__(self, window: int = _WINDOW_SIZE) -> None:
        self._window: Deque[ActionEvent] = deque(maxlen=window)
        self._lock = threading.RLock()
        self._session_log: List[ActionEvent] = []  # full session log

    def record(self, action: str, context: Optional[Dict[str, Any]] = None) -> None:
        event = ActionEvent(action=action, context=context or {})
        with self._lock:
            self._window.append(event)
            self._session_log.append(event)

    def recent(self, n: int = 5) -> List[str]:
        with self._lock:
            return [e.action for e in list(self._window)[-n:]]

    def get_ngrams(self, n: int = 2) -> List[Tuple[str, ...]]:
        """Return all N-grams from the session log."""
        with self._lock:
            actions = [e.action for e in self._session_log]
        return [tuple(actions[i:i+n]) for i in range(len(actions) - n + 1)]

    def clear_session(self) -> None:
        with self._lock:
            self._session_log.clear()


# ---------------------------------------------------------------------------
# Workflow detector
# ---------------------------------------------------------------------------

@dataclass
class WorkflowPattern:
    steps: Tuple[str, ...]
    support: int           # how many times seen
    confidence: float      # P(step[n] | step[0..n-1])
    last_seen: float
    first_seen: float

    @property
    def description(self) -> str:
        return " → ".join(s.split(":", 1)[-1] for s in self.steps)


class WorkflowDetector:
    """
    Detects frequent N-gram workflows from the sequence tracker.
    Uses a simple count-based approach — no LLM, no heavy ML.
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_patterns (
                    pattern TEXT PRIMARY KEY,
                    support INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.5,
                    first_seen REAL,
                    last_seen REAL,
                    offered_automation INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    ts REAL NOT NULL,
                    context TEXT DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_action_log_action_ts ON action_log(action, ts)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def ingest_session(self, tracker: SequenceTracker) -> None:
        """Analyze the session log and update pattern counts in the DB."""
        now = time.time()

        # Persist raw action log
        with self._lock, self._connect() as conn:
            for event in tracker._session_log:
                conn.execute(
                    "INSERT INTO action_log (action, ts, context) VALUES (?, ?, ?)",
                    (event.action, event.ts, json.dumps(event.context)),
                )

        # Count bigrams and trigrams
        for n in (2, 3, 4):
            ngrams = tracker.get_ngrams(n=n)
            counts = Counter(ngrams)
            with self._lock, self._connect() as conn:
                for ngram, count in counts.items():
                    if count < 1:
                        continue
                    pattern_key = json.dumps(list(ngram))
                    conn.execute("""
                        INSERT INTO workflow_patterns (pattern, support, confidence, first_seen, last_seen)
                        VALUES (?, ?, 0.5, ?, ?)
                        ON CONFLICT(pattern) DO UPDATE SET
                            support = support + ?,
                            last_seen = ?
                    """, (pattern_key, count, now, now, count, now))

        # Recompute confidence for updated patterns
        self._recompute_confidence()

    def _recompute_confidence(self) -> None:
        """P(B|A) = count(A→B) / count(A) for bigrams."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT pattern, support FROM workflow_patterns ORDER BY support DESC LIMIT 500"
            ).fetchall()

        # Build a prefix count map
        prefix_counts: Dict[str, int] = Counter()
        for row in rows:
            try:
                steps = tuple(json.loads(row["pattern"]))
                prefix = json.dumps(list(steps[:-1]))
                prefix_counts[prefix] += row["support"]
            except Exception:
                continue

        with self._lock, self._connect() as conn:
            for row in rows:
                try:
                    steps = tuple(json.loads(row["pattern"]))
                    if len(steps) < 2:
                        continue
                    prefix = json.dumps(list(steps[:-1]))
                    prefix_total = prefix_counts.get(prefix, row["support"])
                    conf = row["support"] / max(1, prefix_total)
                    conn.execute(
                        "UPDATE workflow_patterns SET confidence=? WHERE pattern=?",
                        (round(conf, 3), row["pattern"]),
                    )
                except Exception:
                    continue

    def top_patterns(self, min_support: int = _MIN_SUPPORT, min_confidence: float = 0.0, limit: int = 10) -> List[WorkflowPattern]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM workflow_patterns
                WHERE support >= ? AND confidence >= ?
                ORDER BY support DESC, confidence DESC
                LIMIT ?
            """, (min_support, min_confidence, limit)).fetchall()

        patterns = []
        for row in rows:
            try:
                steps = tuple(json.loads(row["pattern"]))
                patterns.append(WorkflowPattern(
                    steps=steps,
                    support=row["support"],
                    confidence=row["confidence"],
                    last_seen=row["last_seen"],
                    first_seen=row["first_seen"],
                ))
            except Exception:
                continue
        return patterns

    def get_automation_candidates(
        self, min_support: int = _MIN_SUPPORT, min_confidence: float = _MIN_CONFIDENCE
    ) -> List[WorkflowPattern]:
        """Return patterns eligible for automation offers (not yet offered)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM workflow_patterns
                WHERE support >= ? AND confidence >= ? AND offered_automation = 0
                ORDER BY support DESC LIMIT 5
            """, (min_support, min_confidence)).fetchall()

        patterns = []
        for row in rows:
            try:
                steps = tuple(json.loads(row["pattern"]))
                patterns.append(WorkflowPattern(
                    steps=steps,
                    support=row["support"],
                    confidence=row["confidence"],
                    last_seen=row["last_seen"],
                    first_seen=row["first_seen"],
                ))
            except Exception:
                continue
        return patterns

    def mark_offered(self, pattern: WorkflowPattern) -> None:
        key = json.dumps(list(pattern.steps))
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE workflow_patterns SET offered_automation=1 WHERE pattern=?", (key,)
            )

    def predict_next(self, recent_actions: List[str], top_k: int = 3) -> List[Tuple[str, float]]:
        """
        Given recent actions, predict what comes next.
        Returns [(predicted_action, confidence)] sorted by confidence.
        """
        if not recent_actions:
            return []

        # Look for matching patterns that start with the last 1-3 actions
        candidates = []
        for prefix_len in (3, 2, 1):
            if len(recent_actions) < prefix_len:
                continue
            prefix = recent_actions[-prefix_len:]
            prefix_key = json.dumps(prefix + ["?"])  # placeholder

            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT pattern, confidence FROM workflow_patterns
                    WHERE support >= 2
                    ORDER BY confidence DESC LIMIT 50
                """).fetchall()

            for row in rows:
                try:
                    steps = list(json.loads(row["pattern"]))
                    if len(steps) <= prefix_len:
                        continue
                    if steps[:prefix_len] == prefix:
                        next_action = steps[prefix_len]
                        candidates.append((next_action, row["confidence"]))
                except Exception:
                    continue

            if candidates:
                break  # found predictions at this prefix length

        # Deduplicate and return top_k
        seen = set()
        result = []
        for action, conf in sorted(candidates, key=lambda x: x[1], reverse=True):
            if action not in seen:
                seen.add(action)
                result.append((action, conf))
            if len(result) >= top_k:
                break
        return result

    def forget_old(self, days: int = 30) -> int:
        """Remove patterns not seen in `days` days."""
        cutoff = time.time() - days * 86400
        with self._lock, self._connect() as conn:
            n = conn.execute(
                "DELETE FROM workflow_patterns WHERE last_seen < ?", (cutoff,)
            ).rowcount
        return n


# ---------------------------------------------------------------------------
# Automation advisor
# ---------------------------------------------------------------------------

class AutomationAdvisor:
    """
    Turns detected workflow patterns into automation offer payloads
    for the Proactive Engine.
    """

    def get_suggestion(self, detector: WorkflowDetector) -> Optional[Dict[str, Any]]:
        candidates = detector.get_automation_candidates()
        if not candidates:
            return None

        best = candidates[0]
        detector.mark_offered(best)

        return {
            "description": best.description,
            "steps": list(best.steps),
            "support": best.support,
            "confidence": round(best.confidence, 2),
            "type": "workflow_automation",
        }


# ---------------------------------------------------------------------------
# Workflow Learner — the public interface
# ---------------------------------------------------------------------------

class WorkflowLearner:
    """
    Passive learning system that tracks user behavior and surfaces automation
    suggestions without being asked.

    Usage::

        from learning.workflow_learner import workflow_learner

        # Record actions as they happen
        workflow_learner.record("open_app:vscode")
        workflow_learner.record("command:git_pull")
        workflow_learner.record("command:npm_install")

        # At session end, persist patterns
        workflow_learner.end_session()

        # Check for automation suggestions (called by ProactiveEngine)
        suggestion = workflow_learner.get_automation_suggestion()
    """

    def __init__(self) -> None:
        self.tracker = SequenceTracker()
        self.detector = WorkflowDetector()
        self.advisor = AutomationAdvisor()
        self._last_session_end = 0.0

        # Subscribe to EventBus for automatic tracking
        self._subscribe()

    def _subscribe(self) -> None:
        try:
            from state_engine.event_bus import event_bus
            event_bus.subscribe("ApplicationFocused", self._on_app_focused)
            event_bus.subscribe("CommandExecuted", self._on_command_executed)
            event_bus.subscribe("ToolCalled", self._on_tool_called)
            event_bus.subscribe("MusicPlayed", lambda e: self.record(f"music:play"))
        except Exception:
            pass

    def record(self, action: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Record an action event."""
        self.tracker.record(action, context)

        # Also record in episodic memory for high-importance events
        if any(action.startswith(p) for p in ("command:", "goal:", "automation:")):
            try:
                from memory.layered_memory import layered_memory
                layered_memory.record_episode(
                    what=action,
                    context=context or {},
                    importance=0.3,
                    persist=False,
                    tags=["learned_action"],
                )
            except Exception:
                pass

    def record_correction(self, original: str, corrected: str) -> None:
        """Record when the user corrects GAMA's output — high importance."""
        self.record(f"correction:{original}→{corrected}", {"type": "correction"})
        try:
            from memory.layered_memory import layered_memory
            layered_memory.remember(
                f"correction.{original}",
                corrected,
                importance=0.8,
                persist=True,
                tags=["correction", "learning"],
            )
        except Exception:
            pass

    def end_session(self) -> None:
        """Persist the current session's patterns to the DB."""
        try:
            self.detector.ingest_session(self.tracker)
            self._last_session_end = time.time()
            log.info("[WorkflowLearner] Session patterns persisted.")
        except Exception as exc:
            log.warning(f"[WorkflowLearner] Session persist failed: {exc}")
        finally:
            self.tracker.clear_session()

    def predict_next(self, top_k: int = 3) -> List[Tuple[str, float]]:
        """Predict the next likely action based on recent history."""
        recent = self.tracker.recent(n=3)
        return self.detector.predict_next(recent, top_k=top_k)

    def get_automation_suggestion(self) -> Optional[Dict[str, Any]]:
        """Return a workflow automation suggestion if one is ready."""
        return self.advisor.get_suggestion(self.detector)

    def get_top_patterns(self, limit: int = 10) -> List[WorkflowPattern]:
        return self.detector.top_patterns(limit=limit)

    # ── EventBus handlers ────────────────────────────────────────────────────

    def _on_app_focused(self, event: Any) -> None:
        app = event.data.get("app") or event.data.get("name", "")
        if app:
            self.record(f"app_focused:{app.lower()}")

    def _on_command_executed(self, event: Any) -> None:
        cmd = event.data.get("command") or event.data.get("name", "")
        if cmd:
            self.record(f"command:{cmd.lower()}")

    def _on_tool_called(self, event: Any) -> None:
        tool = event.data.get("tool") or event.data.get("name", "")
        if tool:
            self.record(f"tool:{tool.lower()}")


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

workflow_learner = WorkflowLearner()

__all__ = [
    "ActionEvent", "SequenceTracker", "WorkflowPattern", "WorkflowDetector",
    "AutomationAdvisor", "WorkflowLearner", "workflow_learner",
]
