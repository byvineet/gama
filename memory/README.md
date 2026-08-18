# GAMA Long-Term Memory

GAMA's memory is split into two layers that work together. Nothing here
was replaced from scratch — the original profile store still exists and
still works exactly as before; the long-term system is a new layer on
top of it.

## 1. Profile store (`memory_manager.py`) — unchanged

A small JSON file (`memory/long_term.json`) holding structured
key/value settings: identity, preferences (voice, confirmation code),
etc. Fast, simple, and used by things like voice preference and the
confirmation-code system. `format_memory_for_prompt()` now skips the
old raw conversation dump and hard-caps output at `MEMORY_MAX_CHARS`.

## 2. Long-term memory (new)

| File | Responsibility |
|---|---|
| `long_term.py` | SQLite store (`memory/long_term.db`): memories, conversation summaries, daily summaries. Local embeddings, importance scoring, decay/pruning, semantic search. |
| `reflection.py` | After each session: summarize + auto-extract durable facts (LLM-assisted, with a zero-dependency heuristic fallback if the API/network is unavailable). Also rolls up daily summaries once a day. |
| `context_builder.py` | Builds the small, budget-capped block that actually goes into the system prompt (`build_session_context`), and powers on-demand search via the `recall_memory` tool (`recall`). |

### Why this design

- **Never inject the whole database.** `build_session_context()` caps
  output at `CONTEXT_CHAR_BUDGET` (~1400 chars) and only includes: the
  small profile block, the most recent conversation/daily summary, and
  the top ~8 highest-(importance × recency)-scored memories. Everything
  else is reachable only through the `recall_memory` tool, which GAMA
  calls with a specific query when it actually needs something —
  genuine retrieval, not a static dump.
- **Semantic search without a model download.** `embed_text()` is a
  deterministic feature-hashing vectorizer (word + char-trigram tokens
  hashed into a 384-dim vector, cosine similarity via numpy). It's
  offline, ~0 CPU cost, and needs no GPU or API key. It's intentionally
  a single, isolated function — swap in a real embedding model later
  without touching anything else that calls `search()`.
- **Importance scoring** is a cheap heuristic (`score_importance()`):
  cues like "remember", "important", names/preferences/dates push a
  memory's base importance up. The LLM reflection step can also assign
  importance directly when it extracts a fact.
- **Decay** only ever applies to memories marked `temporary=True`.
  Their *effective* importance decays exponentially with age (half-life
  ~21 days, extended each time the memory is actually recalled), and
  `decay_sweep()` prunes anything that decays below a minimum threshold
  plus a hard 6-week cutoff as a safety net. Permanent facts never decay.
- **Project memory** is just a `project` column — `remember(..., project="gama")`
  and `search(query, project="gama")` scope to it, while still pulling
  in general (non-project) memories unless `include_global=False`.
- **Conversation + daily summaries** are produced by `reflection.py`:
  `reflect_session()` runs once per finished Live session (in a
  background thread, so it never blocks reconnect), and
  `maybe_daily_rollup()` produces one summary per calendar day, skipped
  automatically if today's rollup already exists.

### Tools exposed to the LLM

- `remember` — store a durable fact/preference/decision, optionally
  scoped to a project, optionally marked temporary.
- `recall_memory` — semantic search over everything remembered, scoped
  to a project if given. Returns a short human-readable list, never raw
  rows or JSON.
- `save_memory` — unchanged; still used for the small structured
  preference set (voice, confirmation code, language).

### Tuning

All knobs are plain module constants at the top of `long_term.py`:
`EMBED_DIM`, `CONTEXT_CHAR_BUDGET`, `TEMPORARY_TTL_DAYS`,
`IMPORTANCE_HALF_LIFE_DAYS`, `MIN_EFFECTIVE_IMPORTANCE`. No config file
needed for a single-user assistant, but they're grouped together
specifically so they're easy to find and change.

### Storage

Everything lives in `memory/long_term.db` (SQLite, WAL mode for safe
concurrent access) next to the existing `memory/long_term.json`. Both
are user-writable data, git-ignored, and portable — copy the `memory/`
folder to migrate an installation.
