"""
Gama - Wake Word Configuration
===============================
Loads config/wake_word.json (falling back to the bundled example so the
app never crashes just because the file is missing) and exposes it as a
typed, validated dataclass.

Author : Vineet Machchal
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from utils.logger import get_logger
from utils.paths import user_data_path

log = get_logger(__name__)

CONFIG_PATH  = user_data_path("config/wake_word.json")
EXAMPLE_PATH = user_data_path("config/wake_word.example.json")


@dataclass
class PorcupineKeyword:
    label:       str
    path:        str
    sensitivity: float = 0.6


@dataclass
class WakeWordConfig:
    enabled:    bool  = True
    backend:    str   = "vosk"   # "vosk" | "porcupine"

    wake_phrase:     str       = "wake up gama"
    # All phrases that wake Gama when spoken in isolation — e.g. both the
    # bare name and the fuller "wake up <name>" form, the same pattern
    # you'd want for "jarvis" / "wake up jarvis". `wake_phrase` above is
    # kept as the "primary" phrase (used in a couple of UI strings/log
    # lines); `wake_phrases` is the full accepted set actually used for
    # detection. If wake_phrases isn't set in config, it's derived from
    # wake_phrase plus the "gama"/"wake up gama" defaults.
    wake_phrases:    List[str] = field(default_factory=lambda: [
        "gama", "wake up gama", "hey gama", "ok gama", "okay gama",
    ])
    # No local interrupt words — only wake word is handled locally.
    # "Go to sleep" is handled by Gemini transcript in main.py.
    interrupt_words: List[str] = field(default_factory=list)

    sensitivity:        float = 0.55
    cooldown_seconds:   float = 1.5
    # 5 minutes of silence before auto-sleep (was 25s).
    auto_sleep_seconds: float = 60.0
    greet_on_wake:      bool  = True
    follow_up_timeout_seconds: float = 15.0
    proactivity_level: str = "normal"

    vosk_model_path: str = "models/vosk-model-small-en-us-0.15"

    # ── Wake-candidate silence confirmation ─────────────────────────────
    # Vosk's own endpointer occasionally finalizes "gama" as a complete
    # utterance even when it's actually the first word of a longer
    # sentence ("gama is a good assistant"). To stop that from waking
    # Gama, a "wake" match is held as a *candidate* until either the
    # confirmation window elapses with no sustained speech following it,
    # or several consecutive loud frames show speech resumed right away
    # (see wake_word/engines/vosk_engine.py for the hysteresis logic —
    # a single noisy blip does not cancel a candidate on its own).
    wake_confirm_silence_ms:  float = 900.0
    # RMS (int16 scale) above which several *consecutive* frames count as
    # "real speech resumed" for the purpose of the check above. This is a
    # "this is a person talking" bar, not a "this is total silence" bar —
    # set too low, ordinary room/mic noise floor will look like sustained
    # speech and wake will never fire. Tune up for a noisy room, down for
    # a very quiet/sensitive mic.
    wake_confirm_rms_threshold: float = 500.0

    # Lightweight DSP double-clap wake (no ML model — see
    # wake_word/clap_detector.py). Runs on the same shared mic frames
    # as the wake-word engine, so it costs no extra audio device/stream.
    clap_wake_enabled:       bool  = True
    # 0.0 (very sensitive / more false positives) .. 1.0 (strict / needs
    # a very sharp, loud, broadband transient).
    clap_sensitivity:        float = 0.5
    clap_min_gap_ms:         float = 150.0
    clap_max_gap_ms:         float = 500.0

    porcupine_access_key: str              = ""
    porcupine_keywords:   List[PorcupineKeyword] = field(default_factory=list)

    # Resolved absolute base dir, used to make relative model paths work
    # whether Gama runs from source or as a frozen .exe.
    base_dir: Path = field(default_factory=lambda: user_data_path("."))

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p)


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_wake_word_config() -> WakeWordConfig:
    """Load config/wake_word.json, tolerating a missing/corrupt file.

    Priority: config/wake_word.json -> config/wake_word.example.json ->
    hardcoded defaults. Never raises — a broken config should degrade to
    "wake word disabled", not crash the assistant.
    """
    raw: Dict[str, Any] = {}
    for path in (CONFIG_PATH, EXAMPLE_PATH):
        try:
            if path.exists():
                raw = _read_json(path)
                break
        except Exception as exc:
            log.warning(f"Could not parse {path.name}: {exc}")

    if not raw:
        log.warning("No wake_word.json found — wake word detection disabled.")
        return WakeWordConfig(enabled=False)

    try:
        vosk_cfg = raw.get("vosk", {}) or {}
        porc_cfg = raw.get("porcupine", {}) or {}
        keywords = [
            PorcupineKeyword(
                label=kw.get("label", "wake"),
                path=kw.get("path", ""),
                sensitivity=float(kw.get("sensitivity", raw.get("sensitivity", 0.6))),
            )
            for kw in porc_cfg.get("keywords", [])
        ]

        # Interrupt words: always empty (user removed local interrupts)
        interrupt_words: List[str] = []
        raw_interrupts = raw.get("interrupt_words", [])
        if isinstance(raw_interrupts, list):
            # Accept from config only if explicitly set; but filter to empty by default
            # to honour user's request to remove local interrupt words.
            interrupt_words = [str(w).lower().strip() for w in raw_interrupts
                               if str(w).lower().strip() not in ("stop", "cancel", "listen")]

        wake_phrase = str(raw.get("wake_phrase", "wake up gama")).lower().strip()
        raw_wake_phrases = raw.get("wake_phrases")
        if isinstance(raw_wake_phrases, list) and raw_wake_phrases:
            wake_phrases = [str(w).lower().strip() for w in raw_wake_phrases if str(w).strip()]
        else:
            # Not explicitly configured — default to a small family of
            # natural variants ("gama", "wake up gama", "hey gama",
            # "ok gama", "okay gama"), same pattern you'd want for
            # "jarvis" / "hey jarvis" / "wake up jarvis". Always include
            # whatever wake_phrase was configured too, so old configs
            # that only set wake_phrase keep working unchanged.
            wake_phrases = list(dict.fromkeys(
                ["gama", "wake up gama", "hey gama", "ok gama", "okay gama", wake_phrase]
            ))

        return WakeWordConfig(
            enabled=bool(raw.get("enabled", True)),
            backend=str(raw.get("backend", "vosk")).lower().strip(),
            wake_phrase=wake_phrase,
            wake_phrases=wake_phrases,
            interrupt_words=interrupt_words,
            sensitivity=float(raw.get("sensitivity", 0.55)),
            cooldown_seconds=float(raw.get("cooldown_seconds", 1.5)),
            # Default 5 minutes; config can override
            auto_sleep_seconds=float(raw.get("auto_sleep_seconds", 300.0)),
            greet_on_wake=bool(raw.get("greet_on_wake", True)),
            follow_up_timeout_seconds=float(raw.get("follow_up_timeout_seconds", 15.0)),
            proactivity_level=str(raw.get("proactivity_level", "normal")).lower().strip(),
            vosk_model_path=str(vosk_cfg.get("model_path", "models/vosk-model-small-en-us-0.15")),
            wake_confirm_silence_ms=float(raw.get("wake_confirm_silence_ms", 900.0)),
            wake_confirm_rms_threshold=float(raw.get("wake_confirm_rms_threshold", 200.0)),
            porcupine_access_key=str(porc_cfg.get("access_key", "")),
            porcupine_keywords=keywords,
            clap_wake_enabled=bool(raw.get("clap_wake_enabled", True)),
            clap_sensitivity=float(raw.get("clap_sensitivity", 0.5)),
            clap_min_gap_ms=float(raw.get("clap_min_gap_ms", 150.0)),
            clap_max_gap_ms=float(raw.get("clap_max_gap_ms", 500.0)),
        )
    except Exception as exc:
        log.error(f"Invalid wake_word.json ({exc}) — wake word detection disabled.")
        return WakeWordConfig(enabled=False)


__all__ = ["WakeWordConfig", "PorcupineKeyword", "load_wake_word_config"]
