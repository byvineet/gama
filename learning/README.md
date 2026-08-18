# GAMA Learning Engine

Implements spec section 2 ("Self-Learning"). This package was **missing
from the repo** even though `main.py` already imported it
(`from learning.recommendation_engine import configure, tick, ...` and
`from learning import habit_tracker as _habit_tracker`) — so the app
could not start at all before this fix. The module names/signatures
below were built to match exactly what `main.py` already expects.

| File | Responsibility |
|---|---|
| `habit_tracker.py` | Cheap event logging (`record(kind, key)`), self-subscribes to the existing `state_engine.event_bus` (`ApplicationFocused`, `DownloadCompleted`, `CommandExecuted`) so no new polling is introduced. Batches writes to `learning/habits.db` (SQLite, WAL) every 30s / 50 events — never on the calling thread. `decay_sweep()` prunes events older than 120 days; `forget_key()` lets the user say "stop tracking X". |
| `routine_analyzer.py` | Pure read-side aggregation: buckets events by (weekday-vs-weekend, hour), recency-weights them (21-day half-life) so stale habits fade on their own, and turns that into a saturating confidence score. Requires ≥2 occurrences before anything counts as a "habit" at all (`MIN_OCCURRENCES_TO_CONSIDER`), and a habit only "counts" once confidence crosses `MIN_HABIT_CONFIDENCE` (0.55) — this is the "build confidence before assuming habits" / "distinguish one-time actions from recurring habits" requirement. |
| `recommendation_engine.py` | `configure(on_suggestion)` wires the delivery callback (same one `actions/proactive_suggestions.py` uses, which ultimately reaches TTS) and starts `habit_tracker`. `tick()` is called periodically by `main.py`'s own timer thread and fires at most one rate-limited suggestion (4h cooldown per habit) when a learned routine matches the current time. `whats_usual_now()` / `learning_status()` back the `habit_status` tool for on-demand queries. |

## Why this design

- **Zero idle cost**: recording is an in-memory append; analysis is a
  single indexed SQLite query run every few minutes, not a background
  loop that's constantly computing.
- **Local only**: everything lives in `learning/habits.db` next to
  `memory/long_term.db`. No network calls, no telemetry, matches the
  memory subsystem's privacy stance.
- **Adapts / forgets automatically**: recency-weighted confidence means
  a routine that stops happening quietly drops below the habit
  threshold on its own — no manual "unlearn" step needed (though
  `forget_key()` exists for the explicit case).
