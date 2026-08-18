"""
Gama - Memory Manager
=====================
JSON-based persistent memory like Mark XLVII.
Categories: identity, preferences, projects, relationships, wishes, notes.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = get_logger(__name__)
logger = log  # back-compat alias
BASE_DIR = _get_base_dir()
MEMORY_PATH = BASE_DIR / "memory" / "long_term.json"
_lock = threading.Lock()
MAX_VALUE_LENGTH = 380
MEMORY_MAX_CHARS = 2200


def _empty_memory() -> Dict[str, Any]:
    return {
        "identity": {},
        "preferences": {},
        "projects": {},
        "relationships": {},
        "wishes": {},
        "notes": {},
    }


def _load_memory_nolock() -> Dict[str, Any]:
    """Load memory without acquiring _lock — caller must already hold it."""
    if not MEMORY_PATH.exists():
        return _empty_memory()
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _empty_memory()
            for key in base:
                if key not in data:
                    data[key] = {}
            return data
        return _empty_memory()
    except Exception:
        return _empty_memory()


def _save_memory_nolock(memory: Dict[str, Any]) -> None:
    """Save memory without acquiring _lock — caller must already hold it.

    Writes to a temp file then atomically replaces the real file, so a
    crash or power loss mid-write can never leave long_term.json half
    written / corrupted (the previous version wrote in place).
    """
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp_path = MEMORY_PATH.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(memory, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, MEMORY_PATH)
    except Exception:
        pass


def load_memory() -> Dict[str, Any]:
    with _lock:
        return _load_memory_nolock()


def save_memory(memory: Dict[str, Any]) -> None:
    with _lock:
        _save_memory_nolock(memory)


def update_memory(new_data: Dict[str, Any]) -> None:
    """Merge new_data into existing memory. new_data = {category: {key: {value, ...}}}

    NOTE: load + modify + save is done under a single lock acquisition to
    close a read-modify-write race — two threads calling update_memory()
    concurrently (e.g. a tool call + a conversation save) could otherwise
    interleave their load/save and silently drop one of the writes.
    """
    if not isinstance(new_data, dict):
        return
    now = datetime.now().isoformat()
    with _lock:
        memory = _load_memory_nolock()
        for category, items in new_data.items():
            if not isinstance(items, dict):
                continue
            if category not in memory:
                memory[category] = {}
            for key, entry in items.items():
                raw = str(entry.get("value", "")) if isinstance(entry, dict) else str(entry)
                if len(raw) > MAX_VALUE_LENGTH:
                    logger.warning(
                        "Memory value for %s.%s truncated from %d to %d chars — "
                        "long facts should be split into multiple entries instead.",
                        category, key, len(raw), MAX_VALUE_LENGTH,
                    )
                value = raw[:MAX_VALUE_LENGTH]
                memory[category][key] = {
                    "value": value,
                    "updated": now,
                    "truncated": len(raw) > MAX_VALUE_LENGTH,
                }
        _save_memory_nolock(memory)


def get_memory(category: str, key: str) -> Optional[str]:
    memory = load_memory()
    entry = memory.get(category, {}).get(key, {})
    if isinstance(entry, dict):
        return entry.get("value")
    return str(entry) if entry else None


def set_memory(category: str, key: str, value: str) -> None:
    update_memory({category: {key: {"value": value}}})


def format_memory_for_prompt(query: Optional[str] = None) -> str:
    """Return memory as a formatted string for the system prompt.

    When `query` is supplied the entries are relevance-ranked using a
    lightweight keyword-overlap score so the most pertinent facts appear
    first and low-relevance entries are trimmed if the total exceeds
    MEMORY_MAX_CHARS.  Without a query all entries are returned in their
    natural category order (original behaviour, kept for compatibility).

    Skips the legacy 'notes.recent_conversation' blob (superseded by the
    long-term memory system's conversation summaries — see
    memory/context_builder.py) and hard-caps total size at
    MEMORY_MAX_CHARS so a growing profile can never balloon the prompt.
    """
    memory = load_memory()

    # Collect every (category, key, value) triple we might include.
    all_entries: List[tuple] = []
    for category, items in memory.items():
        if not isinstance(items, dict) or not items:
            continue
        for key, entry in items.items():
            if category == "notes" and key == "recent_conversation":
                continue
            value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
            all_entries.append((category, key, value))

    # Relevance scoring — fast keyword overlap, no ML needed.
    if query:
        query_tokens = set(query.lower().split())

        def _score(cat: str, key: str, val: str) -> float:
            haystack = f"{cat} {key} {val}".lower()
            hay_tokens = set(haystack.split())
            overlap = len(query_tokens & hay_tokens)
            # Identity / preference categories are always slightly boosted
            # because they provide background context regardless of query.
            base_boost = 0.5 if cat in ("identity", "preferences") else 0.0
            return overlap + base_boost

        all_entries.sort(key=lambda t: _score(t[0], t[1], t[2]), reverse=True)

    # Render into grouped text, respecting the char cap.
    lines: List[str] = []
    last_cat: Optional[str] = None
    chars_used = 0

    for category, key, value in all_entries:
        line = f"  {key}: {value}"
        header = f"\n[{category.upper()}]"
        needed = len(header) + len(line) if category != last_cat else len(line)
        if chars_used + needed > MEMORY_MAX_CHARS:
            lines.append("  …(additional profile entries omitted)")
            break
        if category != last_cat:
            lines.append(header)
            chars_used += len(header)
            last_cat = category
        lines.append(line)
        chars_used += len(line)

    return "\n".join(lines) if lines else ""


SUPPORTED_LANGUAGES = ("english", "hindi", "hinglish")


def get_language_preference() -> str:
    """Reply-language preference — 'english' (default), 'hindi', or
    'hinglish' (a natural mix of Hindi and English, Roman script).
    Stored at identity.language — the same slot core/prompt.txt's
    LANGUAGE DETECTION rule already saves to via save_memory, so a
    value the model saves ('Hindi', 'English', 'Hinglish') and a value
    this helper writes both land in the same place."""
    lang = get_memory("identity", "language")
    lang = (lang or "english").strip().lower()
    return lang if lang in SUPPORTED_LANGUAGES else "english"


def set_language_preference(language: str) -> None:
    language = (language or "").strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        language = "english"
    set_memory("identity", "language", language.capitalize())


def clear_memory() -> None:
    save_memory(_empty_memory())


__all__ = [
    "load_memory", "save_memory", "update_memory",
    "get_memory", "set_memory", "format_memory_for_prompt", "clear_memory",
    "SUPPORTED_LANGUAGES", "get_language_preference", "set_language_preference",
]
