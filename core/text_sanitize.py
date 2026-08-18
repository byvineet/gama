"""
core/text_sanitize.py — Spoken-text sanitization utilities (extracted from main.py, C3 refactor)
====================================================================================================
Pure text-processing helpers used to clean model output before it's spoken
or logged: strips leaked control tokens / internal reasoning, filters
explicit-reasoning requests, collapses repeated sentences, strips code-leak
patterns, and dedupes near-identical spoken lines within a short window.

No GamaAssistant coupling — safe to import and unit-test standalone.
"""

from __future__ import annotations

import re
import threading
import time

from utils.logger import get_logger

log = get_logger(__name__)

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Patterns that indicate Gemini is narrating its own internal reasoning rather
# than speaking to the user. These are stripped from output_transcription so
# they never appear in logs or accumulate in out_buf. The audio side cannot
# be filtered post-hoc (it's already queued), so prompt + tool-description
# changes are the primary prevention; this is a secondary safety net for
# transcription text only.
_THINKING_TOKENS_RE = re.compile(
    r"""
    # XML-style thinking blocks the model may leak into output
    <think(?:ing)?>.*?</think(?:ing)?>
    |
    # Standalone open tags without a close (truncated thinking)
    <think(?:ing)?>.*?$
    |
    # Bracketed internal markers (e.g. [NEEDS_CLARIFICATION], [INTERNAL])
    \[\s*(?:NEEDS_CLARIFICATION|INTERNAL|SILENT|THINKING|REASONING|DO_NOT_SPEAK)\s*\]
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

# Whole-fragment phrases that are artefacts of the reason tool response
# being echoed back by the model. Checked case-insensitively against the
# stripped fragment; if it matches entirely, the fragment is dropped.
_REASONING_ECHO_PHRASES: tuple[str, ...] = (
    "reasoning noted.",
    "reasoning noted. proceed with the plan.",
    "proceed with the plan.",
    "reasoning recorded (empty).",
    "reasoning complete.",
    "clarification needed",
)


def _is_reasoning_echo(text: str) -> bool:
    """Return True if `text` is a verbatim echo of the reason-tool response
    or another pure-internal phrase that should never be spoken aloud."""
    t = text.strip().lower().rstrip(".")
    # Also strip trailing punctuation variants
    for phrase in _REASONING_ECHO_PHRASES:
        if t == phrase.rstrip(".").lower() or t == phrase.lower():
            return True
    return False


def _clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    # Strip thinking-block tokens that occasionally leak into transcription
    text = _THINKING_TOKENS_RE.sub("", text)
    return text.strip()


_EXPLICIT_REASONING_RE = re.compile(
    r"""
    \b(?:think\s+about\s+(?:it|this)|think\s+this\s+through|reason\s+through|
       reason\s+about|work\s+through\s+(?:it|this)|analyse\s+(?:this|it|that)?|
       analyze\s+(?:this|it|that)?|walk\s+me\s+through|what\s+do\s+you\s+think|
       what(?:'s| is)\s+your\s+opinion|give\s+me\s+a\s+detailed|
       provide\s+(?:a\s+)?detailed\s+(?:answer|explanation)|explain\s+in\s+detail|
       explain\b|why\b|deliberate\s+on)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _explicit_reasoning_requested(text: str) -> bool:
    """Return True only for an explicit request to deliberate or explain deeply.

    Ordinary questions, multi-step commands, debugging, and ambiguity must stay
    on the fast path. This check backs up the system prompt at runtime.
    """
    return bool(_EXPLICIT_REASONING_RE.search(text or ""))


def _dedupe_repeated_sentences(text: str) -> str:
    """Remove repeated sentences or blocks from a streamed response.

    Gemini can replay a sentence after a tool-call round trip with other
    fragments in between, so this intentionally deduplicates the complete
    turn rather than only comparing adjacent fragments.
    """
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    if len(parts) < 2:
        return text.strip()

    normalized = [
        re.sub(r"[\s.!?]+$", "", re.sub(r"\s+", " ", p)).casefold()
        for p in parts
    ]
    kept: list[str] = []
    kept_norm: list[str] = []
    seen: set[str] = set()
    for part, norm in zip(parts, normalized):
        if len(norm) >= 8 and norm in seen:
            continue
        kept.append(part)
        kept_norm.append(norm)
        if len(norm) >= 8:
            seen.add(norm)

    # Also collapse a repeated multi-sentence block when the individual
    # sentence boundaries were lost in streaming transcription.
    if len(kept_norm) >= 2:
        collapsed: list[str] = []
        collapsed_norm: list[str] = []
        for part, norm in zip(kept, kept_norm):
            if collapsed_norm and norm == collapsed_norm[-1]:
                continue
            collapsed.append(part)
            collapsed_norm.append(norm)
        kept = collapsed
    return " ".join(kept).strip()


# ---------------------------------------------------------------------------
# Security: redact confirmation codes from any text about to be spoken or
# logged.  Catches phrases like "Your code is 1234", "Setting code to abc",
# "Code received: XYZ" so the code never appears in spoken audio or logs.
# ---------------------------------------------------------------------------
_CODE_LEAK_PATTERNS = re.compile(
    r"""
    (?:
        (?:your\s+)?(?:confirmation\s+)?code\s+(?:is|was|:)\s*\S+  |
        (?:setting|set|changed?|update\w*)\s+(?:your\s+)?(?:confirmation\s+)?code\s+to\s+\S+  |
        code\s+(?:received|accepted|verified)\s*[:—]\s*\S+  |
        got\s+it[,—]?\s+\S+\s+(?:is\s+)?(?:the\s+)?code  |
        code\s*=\s*\S+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _sanitize_spoken_text(text: str) -> str:
    """Strip any fragment that would verbally reveal a confirmation code.

    Replaces matched segments with a safe, neutral phrase so the surrounding
    sentence still makes grammatical sense.  Applied to every string that
    goes through the local TTS engine or is logged as Gama's spoken output.
    """
    if not text:
        return text
    sanitized = _CODE_LEAK_PATTERNS.sub("[code hidden]", text)
    if sanitized != text:
        log.warning(
            "[security] Possible confirmation-code leak suppressed in spoken output."
        )
    return sanitized


# ---------------------------------------------------------------------------
# Deduplication for locally-spoken lines (non-Gemini TTS path).
# Prevents the same sentence from being spoken twice within a short window,
# which can happen when a fast-intent ack races against the real result, or
# when a session reconnect re-fires a cached announcement.
# ---------------------------------------------------------------------------
_SPOKEN_DEDUP_LOCK = threading.Lock()
_SPOKEN_DEDUP_TEXTS: dict[str, float] = {}
# Increased from 3.0 → 6.0 s — covers the case where a Gemini Live
# session audio response and a local TTS ack for the same text arrive
# within the same turn (the session can replay a queued response after
# a brief reconnect, causing the identical line to be spoken twice if
# the dedup window was already exhausted).
_SPOKEN_DEDUP_WINDOW_S: float = 6.0


def _spoken_dedup_check_and_mark(text: str) -> bool:
    """Return True (= duplicate, skip it) if the same text was already
    spoken within the dedup window.  Thread-safe."""
    global _SPOKEN_DEDUP_TEXTS
    normalized = text.strip().lower()
    with _SPOKEN_DEDUP_LOCK:
        now = time.monotonic()
        _SPOKEN_DEDUP_TEXTS = {
            key: ts for key, ts in _SPOKEN_DEDUP_TEXTS.items()
            if (now - ts) < _SPOKEN_DEDUP_WINDOW_S
        }
        if normalized in _SPOKEN_DEDUP_TEXTS:
            return True
        _SPOKEN_DEDUP_TEXTS[normalized] = now
        if len(_SPOKEN_DEDUP_TEXTS) > 32:
            oldest = min(_SPOKEN_DEDUP_TEXTS, key=_SPOKEN_DEDUP_TEXTS.get)
            _SPOKEN_DEDUP_TEXTS.pop(oldest, None)
        return False
