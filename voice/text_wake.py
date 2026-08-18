"""
voice/text_wake.py — Whisper Text-Based Wake Detection
=======================================================
Replaces Vosk grammar wake spotting. After Whisper transcribes an
utterance, decide whether the user addressed Gama — flexibly:

  * "Gama, open YouTube"
  * "Open YouTube, Gama"
  * "Can you open YouTube Gama?"
  * Mis-hears: mama, gamma, gema, gemma, …

No confirmation silence window — fire as soon as the transcript contains
a wake name (or known mis-hear) as a word-boundary token.
"""

from __future__ import annotations

import re
from typing import FrozenSet, Optional, Tuple

# Canonical name + common Whisper / ASR confusions for "Gama".
WAKE_CANONICAL = "gama"

WAKE_ALIASES: FrozenSet[str] = frozenset({
    "gama",
    "gamma",
    "gema",
    "gemma",
    "mama",
    "goma",
    "guma",
    "jama",
    "cama",
    "kama",
    "garma",
    "gala",
    "gima",
    "ganna",
    "gana",
    "karma",  # occasional mis-hear
})

# Multi-word wake openers (normalized, lowercased).
_MULTI_WAKE: Tuple[str, ...] = (
    "wake up gama",
    "hey gama",
    "ok gama",
    "okay gama",
    "hi gama",
    "yo gama",
)

# Build alias alternation once.
_ALIAS_ALT = "|".join(re.escape(a) for a in sorted(WAKE_ALIASES, key=len, reverse=True))
_WAKE_TOKEN_RE = re.compile(rf"(?<!\w)(?:{_ALIAS_ALT})(?!\w)", re.IGNORECASE)

# Multi-word patterns with any alias in the name slot.
_MULTI_RE = re.compile(
    rf"(?<!\w)(?:wake\s+up|hey|ok|okay|hi|yo)\s+(?:{_ALIAS_ALT})(?!\w)",
    re.IGNORECASE,
)

# Leading address fluff to strip after removing the name.
_LEADING_FLUFF_RE = re.compile(
    r"^(?:hey|ok|okay|hi|yo|please|can\s+you|could\s+you|would\s+you)\s+",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\"'`]", "", t)
    t = re.sub(r"[.,!?;:]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def contains_wake_name(text: str) -> bool:
    """True if *text* addresses Gama (name or mis-hear anywhere in the utterance)."""
    n = _normalize(text)
    if not n:
        return False
    if _MULTI_RE.search(n):
        return True
    return bool(_WAKE_TOKEN_RE.search(n))


def is_isolated_wake(text: str) -> bool:
    """True when the utterance is only the wake name / hey-gama style opener."""
    n = _normalize(text)
    if not n:
        return False
    if n in WAKE_ALIASES:
        return True
    if n in _MULTI_WAKE:
        return True
    # "hey gamma" / "ok mama" etc.
    if _MULTI_RE.fullmatch(n):
        return True
    return False


def strip_wake_name(text: str) -> str:
    """Remove wake name / mis-hears and common address fluff; return the command body."""
    raw = (text or "").strip()
    if not raw:
        return ""
    # Work on a punctuation-light copy but preserve original casing via rebuild.
    n = _normalize(raw)
    n = _MULTI_RE.sub(" ", n)
    n = _WAKE_TOKEN_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = _LEADING_FLUFF_RE.sub("", n).strip()
    n = re.sub(r"^(?:and|,|\s)+", "", n).strip()
    n = re.sub(r"[,]+$", "", n).strip()
    return n


def wake_match_span(text: str) -> Optional[Tuple[int, int]]:
    """Return (start, end) of the first wake token in normalized text, if any."""
    n = _normalize(text)
    m = _MULTI_RE.search(n) or _WAKE_TOKEN_RE.search(n)
    if not m:
        return None
    return m.start(), m.end()


__all__ = [
    "WAKE_CANONICAL",
    "WAKE_ALIASES",
    "contains_wake_name",
    "is_isolated_wake",
    "strip_wake_name",
    "wake_match_span",
]
