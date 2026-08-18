"""
actions/context_resolver.py — Conversational file/folder reference resolver.

Turns things like "delete it", "remove that folder", or "delete the
last downloaded file" into a concrete, validated Path — or a friendly
clarification / not-found message instead of a raw exception.

Resolution order (cheapest + most-specific first):
  1. Explicit path/known-folder text ("C:/Users/me/x.txt", "desktop/x.txt")
     -> resolved directly via utils.windows_paths.resolve_user_path.
  2. Referential phrase ("it", "that", "this folder", "them", "", ...)
     -> most recent entry from actions/recent_files.py.
  3. "the/my last downloaded file" / "the download"
     -> actions/desktop_context.py's live 'latest_download' snapshot,
        falling back to the newest file physically in Downloads/.
  4. Anything else with real content ("the budget spreadsheet")
     -> semantic search via the existing knowledge/ index
        (actions/knowledge_action.py's search machinery), if available.

Every path returned by `resolved` has already been checked to exist
on disk — callers still don't need to trust it blindly, but they never
have to worry about acting on an empty/garbage string.

Author : Gama
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger
from utils.windows_paths import resolve_user_path
from actions import recent_files

log = get_logger(__name__)

# Phrases that carry no target info of their own — "delete it", "remove
# that", bare "delete" with nothing else — must fall through to context.
_REFERENTIAL = {
    "", "it", "that", "this", "them", "those", "the file", "the folder",
    "that file", "that folder", "this file", "this folder", "the last one",
    "the last file", "the last folder", "last one", "it please", "that one",
}

_DOWNLOAD_HINTS = ("download", "downloaded", "downloads")
_RECENT_MAX_AGE_S = 30 * 60  # a reference this old is unlikely to be "it"


@dataclass
class ResolveResult:
    status: str  # "resolved" | "ambiguous" | "not_found"
    path: Optional[Path] = None
    candidates: List[str] = field(default_factory=list)
    message: str = ""


def _looks_like_explicit_path(text: str) -> bool:
    t = text.strip().strip("'\"")
    if not t:
        return False
    if "/" in t or "\\" in t or re.match(r"^[a-zA-Z]:", t):
        return True
    # A bare filename with an extension ("report.pdf") counts too.
    if re.search(r"\.[A-Za-z0-9]{1,6}$", t) and " " not in t.strip():
        return True
    return False


def _latest_download() -> Optional[Path]:
    try:
        from actions.desktop_context import get_desktop_snapshot
        snap = get_desktop_snapshot()
        name = snap.get("latest_download") if snap else None
    except Exception:
        name = None
    downloads_dir = resolve_user_path("downloads")
    if name:
        p = downloads_dir / name
        if p.exists():
            return p
    # Fall back to scanning the folder directly (tracker may not be running).
    try:
        if downloads_dir.is_dir():
            files = [f for f in downloads_dir.iterdir() if f.is_file()]
            if files:
                return max(files, key=lambda f: f.stat().st_mtime)
    except Exception:
        pass
    return None


def _semantic_candidates(text: str, limit: int = 5) -> List[str]:
    """Last-resort fuzzy lookup for a named-but-not-exact target, e.g.
    'delete the budget spreadsheet'. Reuses the existing knowledge index
    instead of standing up a second search path."""
    try:
        from knowledge import index as kindex
        from knowledge import ranking
        candidates = kindex.semantic_search(text, limit=limit)
        ranked = ranking.rerank(candidates)
        return [r.record.path for r in ranked[:limit]]
    except Exception:
        return []


def resolve_file_reference(text: str) -> ResolveResult:
    """Resolve free-text `text` (whatever the user/planner supplied as
    the target) to a single validated Path, or report why it can't."""
    raw = (text or "").strip()
    norm = raw.lower().strip(" .!?")

    # 1) Explicit path or known-folder-relative path.
    if raw and _looks_like_explicit_path(raw):
        p = resolve_user_path(raw)
        if p.exists():
            return ResolveResult(status="resolved", path=p)
        return ResolveResult(
            status="not_found",
            message=f"I can't find '{p}'. Double-check the name/location and try again.",
        )

    # 2) "the last downloaded file" / "the download"
    if any(h in norm for h in _DOWNLOAD_HINTS):
        dl = _latest_download()
        if dl is not None:
            return ResolveResult(status="resolved", path=dl)
        return ResolveResult(
            status="not_found",
            message="I couldn't find a recent download to act on.",
        )

    # 3) Pure referential phrase ("it", "that", "", ...) -> recent history.
    if norm in _REFERENTIAL:
        recents = recent_files.recent(limit=5, max_age_seconds=_RECENT_MAX_AGE_S)
        recents = [e for e in recents if Path(e.path).exists()]
        if not recents:
            return ResolveResult(
                status="not_found",
                message="I'm not sure which file or folder you mean — nothing recent to go on. "
                        "Could you tell me the name or path?",
            )
        if len(recents) == 1 or (
            len(recents) > 1 and recents[0].timestamp - recents[1].timestamp > 5
        ):
            # Clearly one dominant, most-recent target.
            return ResolveResult(status="resolved", path=Path(recents[0].path))
        candidates = [e.path for e in recents[:4]]
        return ResolveResult(
            status="ambiguous",
            candidates=candidates,
            message="I found a few things that might match — which one did you mean?",
        )

    # 4) Named-but-fuzzy target ("the budget spreadsheet") -> try resolving
    #    it as a known-folder path first (cheap), then semantic search.
    p = resolve_user_path(raw)
    if p.exists():
        return ResolveResult(status="resolved", path=p)

    hits = _semantic_candidates(raw)
    hits = [h for h in hits if Path(h).exists()]
    if not hits:
        return ResolveResult(
            status="not_found",
            message=f"I couldn't find anything matching '{raw}'.",
        )
    if len(hits) == 1:
        return ResolveResult(status="resolved", path=Path(hits[0]))
    return ResolveResult(
        status="ambiguous",
        candidates=hits[:4],
        message="A few files match that description — which one did you mean?",
    )


def resolve_local_quick_action(utterance: str) -> Optional[dict]:
    """Fast, zero-latency (<1ms) local intent parser for common OS actions.
    Provides instant offline execution when Gemini API calls time out or fail.
    """
    u = (utterance or "").strip().lower()
    if not u:
        return None

    if re.search(r"\b(mute|silence) (audio|sound|volume|pc)\b", u) or u in ("mute", "silence"):
        return {"tool": "media_controller", "action": "mute"}
    if re.search(r"\b(unmute) (audio|sound|volume|pc)\b", u) or u == "unmute":
        return {"tool": "media_controller", "action": "unmute"}
    if re.search(r"\b(volume up|louder|increase volume)\b", u):
        return {"tool": "media_controller", "action": "volume_up"}
    if re.search(r"\b(volume down|quieter|decrease volume)\b", u):
        return {"tool": "media_controller", "action": "volume_down"}
    if re.search(r"\b(take screenshot|capture screen|screen shot)\b", u):
        return {"tool": "computer_agent", "action": "screenshot"}
    if re.search(r"\b(open|launch) chrome\b", u):
        return {"tool": "open_app", "action": "open", "app_name": "chrome"}
    if re.search(r"\b(open|launch) notepad\b", u):
        return {"tool": "open_app", "action": "open", "app_name": "notepad"}
    if re.search(r"\b(what time is it|current time|tell me the time)\b", u):
        return {"tool": "utilities", "action": "get_time"}

    return None


__all__ = ["resolve_file_reference", "ResolveResult", "resolve_local_quick_action"]
