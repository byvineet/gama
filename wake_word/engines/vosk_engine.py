"""
Gama - Wake Word Engine: Vosk (default backend)
================================================
Fully offline, no account/API key, no internet after the one-time model
download. Uses Vosk's grammar-constrained recognition mode: instead of
transcribing everything (expensive, slow, needs a big model), we tell
the recognizer the *only* phrases it's allowed to output. That keeps
the small (~40MB) model both fast and accurate for this narrow job.

CPU cost is higher than a dedicated neural spotter like Porcupine, but
a small Vosk model on grammar mode idles well under 5% of one CPU core
on a typical modern laptop — more than good enough for "always
listening" duty, and there's nothing to buy or register for.

Wake-candidate confirmation — "is the user still speaking?"
-------------------------------------------------------------
Vosk's own endpointer occasionally finalizes "gama" as a complete
utterance even when it's actually the first word of a longer sentence
("gama is a good assistant" gets endpointed into "gama" + "is a good
assistant" as two separate finals). Exact-string matching alone can't
catch that, since each half looks isolated on its own.

To fix this, a "wake" match is held as a *candidate* rather than fired
immediately, and the very same grammar-constrained recognizer keeps
listening through the confirmation window:

  * The recognizer produces more recognized content (a partial or final
    with actual words in it, e.g. "is", "in", "the", or another target
    phrase) almost immediately -> the wake word was embedded in a
    longer sentence ("gama is the ...", "gama in the ..."); drop the
    candidate.
  * The recognizer produces nothing (no words at all) for the whole
    confirmation window -> the user stopped talking right after the
    wake word -> genuine isolated wake word; fire "wake".

Importantly, "silence" here means *the user isn't producing recognized
speech content* — not "the microphone is reading zero amplitude."
Earlier versions of this gate used raw RMS loudness as a stand-in for
"is someone still talking," which broke on totally ordinary room noise,
fan hum, or breath: those are often loud enough to look like "sustained
speech" on an amplitude meter while containing zero actual words, which
silently swallowed real wake attempts. Driving the check off the
recognizer's own word output instead means ambient noise that isn't
speech no longer cancels a valid wake word, while a person who kept
talking ("gama is good", "gama in the kitchen") still gets caught and
correctly ignored.

Author : Vineet Machchal
"""

from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional

from utils.logger import get_logger
from .base import WakeEngineBase

log = get_logger(__name__)


def _normalize(text: str) -> str:
    text = re.sub(r"[.,!?;:]+$", "", text.strip())  # trailing punctuation only
    return re.sub(r"\s+", " ", text.lower()).strip()


class VoskWakeEngine(WakeEngineBase):
    sample_rate = 16000
    frame_length = None  # Vosk accepts variable-size chunks

    def __init__(self, model_path: str, wake_phrases: List[str], interrupt_words: List[str],
                 confirm_silence_ms: float = 900.0, confirm_rms_threshold: float = 500.0):
        try:
            import vosk  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "The 'vosk' package isn't installed. Run: pip install vosk"
            ) from exc

        from pathlib import Path
        if not Path(model_path).exists():
            raise RuntimeError(
                f"Vosk model not found at '{model_path}'. Run "
                "`python scripts/download_vosk_model.py` to fetch it, or "
                "point vosk.model_path in config/wake_word.json elsewhere."
            )

        vosk.SetLogLevel(-1)  # silence Kaldi's own console spam

        # Exactly two accepted wake phrases — "gama" and "wake up gama" —
        # ring the same "wake" label. Either one, spoken in isolation (see
        # the exact-match rule in _match), wakes Gama; there's no ranking/
        # preference between them. This set is intentionally small and
        # fixed: fewer accepted phrases means fewer chances for ordinary
        # conversation to accidentally collide with one of them. Config
        # (config/wake_word.json -> "wake_phrases") can still override
        # this list, but the shipped default is exactly these two.
        wake_list = [wake_phrases] if isinstance(wake_phrases, str) else list(wake_phrases)
        self._wake_phrases = [_normalize(w) for w in wake_list if _normalize(w)]
        if not self._wake_phrases:
            self._wake_phrases = ["gama", "wake up gama"]
        self._interrupt_words = [_normalize(w) for w in interrupt_words]
        self._targets = list(dict.fromkeys(self._wake_phrases + self._interrupt_words))

        grammar = json.dumps(self._targets + ["[unk]"])
        self._model = vosk.Model(model_path)
        self._rec = vosk.KaldiRecognizer(self._model, self.sample_rate, grammar)
        self._rec.SetWords(False)

        # Word-boundary regex per target — used for interrupt words only;
        # wake phrases use exact whole-utterance equality (see _match).
        self._patterns: Dict[str, "re.Pattern[str]"] = {
            t: re.compile(rf"(?<!\w){re.escape(t)}(?!\w)") for t in self._targets
        }

        # ── Wake-candidate confirmation state ────────────────────────────
        # confirm_rms_threshold is accepted for backward-compatible config
        # loading but is no longer used for the confirmation decision — see
        # module docstring. Confirmation is now driven entirely by whether
        # the recognizer itself produces any word output in the window.
        self._confirm_silence_s = max(0.0, confirm_silence_ms) / 1000.0
        self._pending_wake_since: Optional[float] = None

        log.info(
            f"VoskWakeEngine ready (wake_phrases={self._wake_phrases}, "
            f"interrupts={self._interrupt_words}, "
            f"wake_confirm_silence={self._confirm_silence_s * 1000:.0f}ms, "
            f"confirmation=speech-content-based)"
        )

    def _match(self, text: str, allow_wake: bool = True) -> Optional[str]:
        text = _normalize(text)
        if not text:
            return None
        if allow_wake and text in self._wake_phrases:
            # Exact match only — "okay, {phrase}" or "{phrase} start" must
            # NOT wake Gama. Vosk's grammar mode still allows [unk] to
            # soak up other speech in the same utterance, so a substring/
            # word-boundary search here would let leading or trailing
            # words slip through; requiring the *entire* finalized
            # utterance to equal one of the accepted wake phrases closes
            # that gap.
            return "wake"
        for w in self._interrupt_words:
            if self._patterns[w].search(text):
                return w
        return None

    def process(self, pcm_frame: bytes) -> Optional[str]:
        try:
            # ── Wake-candidate confirmation ─────────────────────────────
            # A "wake" match was finalized on a previous frame but not yet
            # fired — we're waiting to see whether the user keeps talking
            # (meaning the wake word was just the first word of a longer
            # sentence) or genuinely stops (a real isolated wake word).
            # This is driven by the recognizer's own word output, not raw
            # mic loudness — see module docstring for why.
            if self._pending_wake_since is not None:
                if self._rec.AcceptWaveform(pcm_frame):
                    result = json.loads(self._rec.Result() or "{}")
                    text = _normalize(result.get("text", ""))
                    self._rec.Reset()
                    if text:
                        # The user kept talking right after the wake word
                        # ("gama is good", "gama in the kitchen") — drop
                        # the candidate. The newly recognized text is fed
                        # straight through _match so a legitimate
                        # back-to-back wake ("gama" ... "gama") or
                        # interrupt word isn't lost.
                        log.debug(
                            f"[vosk] Wake candidate dropped — speech "
                            f"continued right after it ({text!r})."
                        )
                        self._pending_wake_since = None
                        label = self._match(text, allow_wake=True)
                        if label == "wake":
                            self._pending_wake_since = time.monotonic()
                            return None
                        return label
                    else:
                        # A genuine end-of-utterance with no words —
                        # strong confirmation the wake word was said alone.
                        self._pending_wake_since = None
                        return "wake"
                else:
                    partial = json.loads(self._rec.PartialResult() or "{}")
                    ptext = _normalize(partial.get("partial", ""))
                    if ptext:
                        # Words are already accumulating mid-utterance —
                        # don't wait for the final; the user is clearly
                        # still talking.
                        log.debug(
                            f"[vosk] Wake candidate dropped — partial "
                            f"speech detected right after it ({ptext!r})."
                        )
                        self._pending_wake_since = None
                        return None
                    if time.monotonic() - self._pending_wake_since >= self._confirm_silence_s:
                        self._pending_wake_since = None
                        return "wake"
                    return None  # keep waiting for the confirmation window to elapse

            if self._rec.AcceptWaveform(pcm_frame):
                # Final, end-of-utterance result — the only place the wake
                # phrase itself is allowed to match. Vosk's grammar mode is
                # constrained to a tiny vocabulary (wake phrase / interrupt
                # words / [unk]), which means *partial*, still-in-progress
                # results are prone to snapping ambiguous or unrelated
                # speech onto the wake phrase just because it's one of the
                # few things the recognizer is allowed to say. Waiting for
                # the final result — a real end-of-utterance decision, not
                # a mid-guess — cuts that false-wake rate down drastically.
                result = json.loads(self._rec.Result() or "{}")
                label = self._match(result.get("text", ""), allow_wake=True)
                # Reset immediately so the same utterance can't re-fire on
                # every subsequent frame while the recognizer catches up.
                self._rec.Reset()
                if label == "wake":
                    # Don't fire yet — hold as a candidate until we've
                    # confirmed no further speech follows it.
                    self._pending_wake_since = time.monotonic()
                    return None
                return label
            else:
                # Partial results only get checked against interrupt words
                # (so a short "stop" can still fire without waiting for a
                # pause) — never the wake phrase, for the reason above.
                partial = json.loads(self._rec.PartialResult() or "{}")
                label = self._match(partial.get("partial", ""), allow_wake=False)
                if label:
                    self._rec.Reset()
                return label
        except Exception as exc:
            log.debug(f"Vosk frame error (ignored): {exc}")
            return None

    def close(self) -> None:
        # vosk objects are GC'd; nothing to explicitly release.
        pass
