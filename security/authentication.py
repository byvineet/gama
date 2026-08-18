"""
security/authentication.py — Individual Authentication Factors
==================================================================
Wraps each individual verification factor (voice, face, verbal/code
confirmation) behind one small, uniformly-shaped result type, so
verification_pipeline.py can combine them without knowing anything
about SpeechBrain/confirmation-code internals.

Author: Gama Security Upgrade
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from utils.logger import get_logger
# speaker_verification removed

# --- speaker verification stack removed: provide local fallbacks ---
class _RemovedSV:
    @staticmethod
    def verify(*a, **k):
        class R:
            is_owner = True
            confidence = 1.0
            similarity = 1.0
            reason = "verification_disabled"
            matched_user = None
        return R()
    @staticmethod
    def warmup():
        return False
    LEGACY_MODEL_IDS = []

class _RemovedEnroll:
    @staticmethod
    def get_profile_records(*a, **k):
        return []
    @staticmethod
    def is_enrolled(*a, **k):
        return False
    @staticmethod
    def enroll(*a, **k):
        return {"ok": False, "error": "removed"}
    @staticmethod
    def list_enrolled():
        return []

speaker_engine = _RemovedSV()
voice_enrollment = _RemovedEnroll()
LEGACY_MODEL_IDS = []
# --- end fallbacks ---

# voice_enrollment removed
# speaker_verification removed

log = get_logger(__name__)


@dataclass
class FactorResult:
    factor: str                 # "voice" | "face" | "confirmation"
    passed: bool
    confidence: float = 0.0
    detail: str = ""
    latency_ms: float = 0.0


def check_voice(recent_pcm: Optional[bytes], expected_name: Optional[str] = None) -> FactorResult:
    t0 = time.monotonic()

    records = voice_enrollment.get_profile_records()

    if not records:
        return FactorResult(
            "voice",
            False,
            0.0,
            "No voice profile enrolled yet.",
            0.0,
        )

    if not recent_pcm:
        return FactorResult(
            "voice",
            False,
            0.0,
            "No recent audio available to verify.",
            0.0,
        )

    pcm = np.frombuffer(recent_pcm, dtype=np.int16)

    # -------------------------------------------------------
    # Verify against a specific enrolled user
    # -------------------------------------------------------
    if expected_name:
        record = records.get(expected_name)

        if record is None:
            return FactorResult(
                "voice",
                False,
                0.0,
                f"No voice profile named '{expected_name}'.",
                0.0,
            )

        # Legacy embeddings (old buggy MFCC fallback, or WeSpeaker
        # embeddings computed before the HTK-mel fbank fix) produce
        # near-zero similarities regardless of who is speaking —
        # surface a clear re-enrollment message instead of a confusing
        # rejection that will never pass no matter how many times the
        # owner confirms.
        if record.model_id in LEGACY_MODEL_IDS:
            log.warning(
                "Voice profile '%s' was enrolled with an outdated feature "
                "pipeline ('%s') — re-enrollment required for accurate verification.",
                expected_name, record.model_id,
            )
            return FactorResult(
                "voice", False, 0.0,
                f"Voice profile for '{expected_name}' is outdated and must be re-enrolled. "
                f"Please say 'enroll my voice' to set up a new voiceprint.",
                (time.monotonic() - t0) * 1000.0,
            )

        result = None and verify_against_embeddings(
            pcm_int16=pcm,
            name=expected_name,
            enrolled_embeddings=record.embeddings,
            threshold=record.threshold,
        )

    # -------------------------------------------------------
    # Verify against every enrolled profile
    # -------------------------------------------------------
    else:
        best_result = None

        for name, record in records.items():
            # Skip legacy profiles — they always score near-zero.
            if record.model_id in LEGACY_MODEL_IDS:
                log.warning(
                    "Skipping legacy voice profile '%s' (model '%s') — re-enrollment required.",
                    name, record.model_id,
                )
                continue

            result = None and verify_against_embeddings(
                pcm_int16=pcm,
                name=name,
                enrolled_embeddings=record.embeddings,
                threshold=record.threshold,
            )

            if (
                best_result is None
                or result.similarity > best_result.similarity
            ):
                best_result = result

        if best_result is None:
            # All profiles were legacy — nothing usable to compare against.
            latency = (time.monotonic() - t0) * 1000.0
            return FactorResult(
                "voice", False, 0.0,
                "All stored voice profiles are outdated. Please say 'enroll my voice' to re-enroll.",
                latency,
            )

        result = best_result

    latency = (time.monotonic() - t0) * 1000.0

    if result is None:
        return FactorResult(
            "voice",
            False,
            0.0,
            "Voice verification failed.",
            latency,
        )

    if not result.verified:
        return FactorResult(
            "voice",
            False,
            result.confidence,
            result.reason or "Voice did not match.",
            latency,
        )

    if expected_name and result.speaker != expected_name:
        return FactorResult(
            "voice",
            False,
            result.confidence,
            f"Voice matched '{result.speaker}', not '{expected_name}'.",
            latency,
        )

    return FactorResult(
        "voice",
        True,
        result.confidence,
        f"Voice verified as '{result.speaker}' "
        f"(similarity {result.similarity:.4f}).",
        latency,
    )


def check_confirmation_code(code: Optional[str]) -> FactorResult:
    """Delegates to the existing shared confirmation/security code
    system (guard/storage.py via actions/confirmation.py) so there is
    still exactly one code for the owner to remember."""
    from actions.confirmation import is_code_set, verify_confirmation_code

    if not is_code_set():
        return FactorResult("confirmation", False, 0.0,
                             "No confirmation code has been set up yet.")
    if not code:
        return FactorResult("confirmation", False, 0.0,
                             "A confirmation code (verbal or typed) is required.")
    verdict = verify_confirmation_code(code)
    if verdict == "VERIFIED":
        return FactorResult("confirmation", True, 1.0, "Confirmation code verified.")
    return FactorResult("confirmation", False, 0.0, "Confirmation code was incorrect.")


# Affirmative-intent detection.
#
# This used to be a fixed English word-list match ("yes", "confirm",
# "okay", ...). That broke naturally-spoken confirmations — including
# every Hindi/Hinglish reply ("haan kar do", "theek hai", "bilkul") —
# because the exact word simply wasn't in the list.
#
# Intent is now classified by a fast LLM call (Gemini Flash-Lite, same
# model main.py already uses for command routing) that reads the
# transcript and judges whether it's a genuine affirmative reply, a
# negative/refusal, or neither — in ANY language or phrasing. The
# word-list below survives only as an offline/fail-safe fallback for
# when no genai client is available or the classification call
# errors/times out, so the feature never hard-fails into "always deny".
_AFFIRMATIVE_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed",
    "correct", "right", "okay", "ok", "affirmative", "absolutely",
    "definitely", "proceed", "continue", "agreed", "agree", "fine",
    "good", "positive", "please", "do", "go",
    # common Hinglish/Hindi (romanized) affirmatives
    "haan", "han", "ha", "theek", "thik", "sahi", "bilkul", "zaroor",
    "karo", "kardo", "kar", "chalo", "acha", "achha",
})
_NEGATION_WORDS = frozenset({
    "no", "nope", "not", "dont", "don't", "wait", "stop", "cancel",
    "hold", "negative", "never", "nah",
    "nahi", "nahin", "ruk", "ruko", "mat",
})
_MAX_CONFIRM_WORDS = 12

_CLASSIFY_MODEL = "gemini-3.5-flash-lite"
_CLASSIFY_PROMPT = (
    "You are a strict yes/no intent classifier for a voice-assistant "
    "security confirmation. The user was asked to verbally confirm a "
    "sensitive action. Given their spoken reply (which may be in "
    "English, Hindi, Hinglish, or mixed), decide if it is a genuine, "
    "direct affirmative confirmation to proceed. Reply ONLY with JSON: "
    '{"confirmed": true} if it clearly means yes/proceed/go ahead, '
    '{"confirmed": false} if it means no/stop/cancel, or if it is '
    "unrelated, ambiguous, or not a direct reply to a confirmation "
    "prompt. No other text."
)

# Groq fast-path: llama-3.1-8b-instant classifies in ~80ms vs ~400ms for Gemini.
# Uses the OpenAI-compatible Groq API if `groq_api_key` is present in api_keys.json;
# silently falls back to the Gemini path when the key is absent or the call fails.
_GROQ_CLASSIFY_MODEL = "llama-3.1-8b-instant"
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _get_groq_key() -> str:
    """Read groq_api_key from config/api_keys.json, empty string if missing."""
    try:
        import sys, json as _json
        from pathlib import Path as _Path
        base = _Path(sys.executable).parent if getattr(sys, "frozen", False) \
               else _Path(__file__).resolve().parent.parent
        cfg = base / "config" / "api_keys.json"
        return _json.loads(cfg.read_text(encoding="utf-8")).get("groq_api_key", "")
    except Exception:
        return ""


def _classify_with_groq(transcript: str, timeout_s: float) -> Optional[bool]:
    """Try Groq llama-3.1-8b-instant for fast yes/no classification.

    Returns True/False on success, None on any error (key missing, network,
    parse failure) so the caller can seamlessly fall through to Gemini.
    """
    key = _get_groq_key()
    if not key:
        return None
    try:
        import json as _json
        import urllib.request as _req
        import urllib.error as _uerr

        body = _json.dumps({
            "model": _GROQ_CLASSIFY_MODEL,
            "messages": [
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user",   "content": f"User's reply: {transcript}"},
            ],
            "temperature": 0.0,
            "max_tokens": 20,
            "response_format": {"type": "json_object"},
        }).encode()

        request = _req.Request(
            f"{_GROQ_BASE_URL}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _req.urlopen(request, timeout=timeout_s) as resp:
            raw = _json.loads(resp.read().decode())

        text = raw["choices"][0]["message"]["content"].strip()
        parsed = _json.loads(text)
        val = parsed.get("confirmed")
        return bool(val) if isinstance(val, bool) else None
    except Exception as exc:
        log.debug(f"[security] Groq classification failed: {exc}")
        return None


def _normalize_transcript(text: str) -> str:
    import re
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def transcript_confirms(transcript: str) -> bool:
    """Offline fallback only — see classify_confirmation_intent() for the
    primary, language-agnostic path. Word-level match against a small
    seed list; used when no LLM classification is available."""
    norm = _normalize_transcript(transcript)
    if not norm:
        return False
    words = norm.split()
    if len(words) > _MAX_CONFIRM_WORDS:
        return False
    word_set = set(words)
    if word_set & _NEGATION_WORDS:
        return False
    return bool(word_set & _AFFIRMATIVE_WORDS)


def classify_confirmation_intent(transcript: str, genai_client=None,
                                  timeout_s: float = 1.5) -> Optional[bool]:
    """Classify whether `transcript` is a natural, freely-worded
    affirmative confirmation — in any language/phrasing, not just a
    fixed word list. Returns True/False, or None if classification
    couldn't run (caller should fall back to transcript_confirms()).

    Fast path: tries Groq llama-3.1-8b-instant (~80ms) before falling
    back to Gemini Flash-Lite (~400ms). Groq is skipped silently if the
    groq_api_key is absent from api_keys.json or the call errors.

    Blocking/sync by design — callers on the async path should run this
    via loop.run_in_executor(...) with their own timeout, mirroring the
    Flash-Lite routing call pattern in main.py.
    """
    norm = _normalize_transcript(transcript)
    if not norm:
        return False

    # ── Fast path: Groq (~80ms) ──────────────────────────────────────────────
    # Allocate up to half the timeout budget for Groq; if it fails the full
    # remaining budget is still available for the Gemini fallback.
    groq_timeout = min(timeout_s * 0.5, 0.8)
    groq_result = _classify_with_groq(transcript, timeout_s=groq_timeout)
    if groq_result is not None:
        log.debug("[security] Confirmation classified by Groq: %s", groq_result)
        return groq_result

    # ── Fallback: Gemini Flash-Lite (~400ms) ─────────────────────────────────
    if genai_client is None:
        return None
    try:
        from google.genai import types as _gtypes
        resp = genai_client.models.generate_content(
            model=_CLASSIFY_MODEL,
            contents=f"User's reply: {transcript}",
            config=_gtypes.GenerateContentConfig(
                system_instruction=_CLASSIFY_PROMPT,
                temperature=0.0,
                max_output_tokens=20,
                thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
            ),
        )
        import json as _json
        raw = (resp.text or "").strip()
        parsed = _json.loads(raw)
        val = parsed.get("confirmed")
        return bool(val) if isinstance(val, bool) else None
    except Exception as exc:
        log.debug(f"[security] Confirmation intent classification failed: {exc}")
        return None


def check_verbal_confirmation(verbal_confirmed: bool, phrase: Optional[str] = None,
                               transcript: Optional[str] = None,
                               transcript_age_s: Optional[float] = None,
                               max_transcript_age_s: float = 20.0,
                               intent: Optional[bool] = None) -> FactorResult:
    """A third factor for DESTRUCTIVE actions.

    `verbal_confirmed` alone is NOT trusted -- it is only a signal that
    the tool-calling model believes it heard a confirmation. This is
    cross-checked against `transcript`, the actual last recognized
    speech-to-text output from the live audio pipeline (owned by
    main.py), which must be fresh (within `max_transcript_age_s`
    seconds).

    Whether the transcript actually *reads* as an affirmative reply is
    decided naturally, not via a fixed word list:
      - `intent`, if provided, is the pre-computed verdict from
        classify_confirmation_intent() (an LLM judging the transcript
        freely, in any language/phrasing) — this is the primary path.
      - If `intent` is None (classifier unavailable/failed), falls back
        to the local transcript_confirms() keyword heuristic.
    """
    if not verbal_confirmed:
        return FactorResult("verbal", False, 0.0,
                             "Verbal confirmation not yet given — ask the user to confirm out loud.")

    if transcript_age_s is not None and transcript_age_s > max_transcript_age_s:
        return FactorResult("verbal", False, 0.0,
                             "No recent spoken confirmation found — ask the user to confirm out loud again.")

    confirmed = intent if intent is not None else transcript_confirms(transcript or "")
    if not confirmed:
        return FactorResult("verbal", False, 0.0,
                             "Could not recognize a clear spoken confirmation — ask the user to confirm out loud again.")

    extra = f' ("{phrase}")' if phrase else ""
    return FactorResult("verbal", True, 1.0, f"Verbal confirmation verified{extra}.")


__all__ = [
    "FactorResult", "check_voice",
    "check_confirmation_code", "check_verbal_confirmation",
]
