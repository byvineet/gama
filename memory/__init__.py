"""Gama memory package.

Layers:
* memory_manager — small JSON profile store (identity/preferences/etc.),
  unchanged behavior, used for fast key-value settings like voice choice.
* long_term / reflection / context_builder — the long-term memory
  system: semantic search, importance scoring, decay, project memory,
  conversation + daily summaries.
* layered_memory — Phase 5 JARVIS architecture: five-layer memory
  (working, session, long-term, episodic, semantic graph) with decay,
  confidence scoring, and entity-relationship graph.
"""
from memory.memory_manager import (
    load_memory, save_memory, update_memory,
    get_memory, set_memory, format_memory_for_prompt, clear_memory,
)
from memory.context_builder import build_session_context, recall, remember_fact
from memory.reflection import reflect_session, maybe_daily_rollup
from memory.long_term import decay_sweep, stats as long_term_stats
from memory.layered_memory import layered_memory, LayeredMemory, MemoryItem, Episode
