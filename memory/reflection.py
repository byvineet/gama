"""
Gama - Memory Reflection
========================
Turns raw conversation exchanges into durable memory:

* After each session: summarize what was discussed + auto-extract any
  facts worth remembering (preferences, project details, commitments).
* Once per day: roll up the day's conversation summaries into one
  short daily summary.

Uses the Gemini text model when available (best quality); falls back
to a fully local heuristic extractor if the API/network is unavailable
so memory creation never blocks or crashes the assistant.

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.paths import get_base_dir as _get_base_dir

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

from utils.logger import get_logger
from memory import long_term as lt

log = get_logger(__name__)




BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
REFLECTION_MODEL = "gemini-3.5-flash-lite"  # cheap + fast, this is a background task

_FACT_PROMPT = """You help a personal assistant remember things about its user.
Read the conversation exchanges below and reply with ONLY compact JSON
(no markdown fences, no commentary) in this exact shape:

{{"summary": "one or two sentence summary of what was discussed",
  "facts": [{{"text": "a durable fact/preference/commitment worth remembering long-term",
             "kind": "fact | project | profile | routine",
             "project": "project_name or null",
             "importance": 0.0-1.0, "temporary": true/false}}]}}

Specifically categorize facts:
- "profile" for user profile updates or personal preferences.
- "project" for project memories (mention the project name in the "project" field).
- "routine" for repeated workflows or daily routines.
- "fact" for general episodic details.

Only include facts that would matter in a FUTURE conversation (names,
preferences, ongoing projects, deadlines, decisions). Skip small talk.
Return at most 5 facts. If nothing is worth remembering, use an empty list.

Conversation:
{transcript}
"""

_INTERACTION_PROMPT = """You help a personal assistant learn and remember details about its owner from a single exchange.
Given the exchange below, determine:
1. Did the user teach something new about themselves or their preferences?
2. Was a new preference discovered?
3. Was a specific coding project or workspace mentioned?
4. Was a repeated workflow or routine mentioned?
5. Is this worth remembering for future sessions?

If yes, reply with ONLY compact JSON (no markdown fences, no commentary) in this shape:
{{"facts": [{{"text": "the fact/preference/project detail/routine to remember",
             "kind": "fact | project | profile | routine",
             "project": "project_name or null",
             "importance": 0.0-1.0,
             "temporary": true/false}}]}}

Specifically categorize facts:
- "profile" for user profile updates or personal preferences.
- "project" for project memories (specify project name in the "project" field).
- "routine" for repeated workflows or routines.
- "fact" for general episodic details.

If nothing is worth remembering (e.g. small talk, simple system queries), reply with:
{{"facts": []}}

Exchange:
User: {user_text}
Gama: {gama_text}
"""


def _get_api_key() -> str:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8")).get("gemini_api_key", "")
    except Exception:
        return ""


def _llm_reflect(transcript: str) -> Tuple[str, List[dict]]:
    """Ask Gemini to summarize + extract facts. Raises on any failure —
    caller is expected to fall back to the heuristic path."""
    from google import genai
    api_key = _get_api_key()
    if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
        raise RuntimeError("no api key configured")

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=REFLECTION_MODEL,
        contents=_FACT_PROMPT.format(transcript=transcript[:6000]),
    )
    text = (resp.text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)
    summary = str(data.get("summary", "")).strip()
    facts = data.get("facts", []) if isinstance(data.get("facts"), list) else []
    return summary, facts


def _heuristic_reflect(transcript: str) -> Tuple[str, List[dict]]:
    """Zero-dependency fallback: naive summary + keyword-based fact pull.
    Never raises."""
    lines = [l for l in transcript.splitlines() if l.strip()]
    summary = " / ".join(lines[-4:])[:280] if lines else ""
    facts: List[dict] = []
    for line in lines:
        low = line.lower()
        if any(h in low for h in ("my name is", "i am", "i'm", "remember",
                                   "prefer", "favorite", "favourite", "project")):
            facts.append({"text": line.strip()[:280], "importance": lt.score_importance(line),
                          "temporary": "remember" not in low})
    return summary, facts[:5]


def reflect_session(exchanges: List[str], session_start: datetime,
                     project: str | None = None) -> None:
    """Self-learning disabled — no session fact extraction or profile learning.

    Session transcript buffering remains elsewhere; this no longer calls the LLM
    or writes auto-learned facts/profiles.
    """
    return


def reflect_interaction(user_text: str, gama_text: str) -> None:
    """Self-learning disabled — no per-turn profile/fact extraction."""
    return


def maybe_daily_rollup(force: bool = False) -> None:
    """Roll up yesterday's conversation summaries into one daily summary,
    at most once per day. Cheap no-op most of the time."""
    yesterday = (datetime.now() - timedelta(days=1)).date()
    date_str = yesterday.isoformat()
    if not force and lt.get_daily_summary(date_str) is not None:
        return
    start = datetime.combine(yesterday, datetime.min.time()).isoformat()
    end = datetime.combine(yesterday, datetime.max.time()).isoformat()
    summaries = lt.conversation_summaries_between(start, end)
    if not summaries:
        return
    joined = "\n".join(f"- {s}" for s in summaries)
    try:
        summary, _facts = _llm_reflect(f"Summaries from the day:\n{joined}")
        daily = summary or joined[:400]
    except Exception:
        daily = joined[:400]
    lt.upsert_daily_summary(date_str, daily)
    log.info(f"Daily summary rolled up for {date_str}.")


__all__ = ["reflect_session", "maybe_daily_rollup", "reflect_interaction"]
