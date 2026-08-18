"""
core/interrupt_calibration.py — Legacy interrupt thresholds + mic calibration
=============================================================================
Isolated from main.py (Phase 1). Primary barge-in is transcription-match;
these RMS/envelope constants remain only for the optional mic-calibration
wizard and _looks_like_echo helper.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from utils.logger import get_logger

log = get_logger(__name__)

# Audio format constants shared with the stream (must stay in sync with main)
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 512

# --- Interruption (transcription-match barge-in) --------------------------
# Full-duplex: mic PCM is always streamed to Gemini while a session is live.
# Barge-in uses ONLY Gemini Live audio transcriptions:
#   - Track recent output_audio_transcription (what Gama is saying)
#   - On input_audio_transcription (what the mic hears):
#       * If the input text substantially matches recent output text → treat
#         as Gama's own voice / echo → DO NOT interrupt
#       * If it does NOT match → real user speech → interrupt (_immediate_barge_in)
# All energy / RMS / correlation / mic-calibration based interruption paths
# are disabled. Legacy INTERRUPT_* constants are kept only so old calibration
# code does not crash; they are never used for barge-in decisions.
INTERRUPT_AMP_THRESHOLD   = 0.10         # legacy (unused by primary barge-in)
INTERRUPT_SUSTAIN_SECONDS      = 0.28    # legacy
INTERRUPT_SUSTAIN_SECONDS_FAST = 0.10    # legacy
INTERRUPT_FAST_AMP_RATIO = 1.35           # legacy
INTERRUPT_SUSTAIN_FRAMES  = max(1, int(INTERRUPT_SUSTAIN_SECONDS * SEND_SAMPLE_RATE / CHUNK_SIZE))
INTERRUPT_SUSTAIN_FRAMES_FAST = max(1, int(INTERRUPT_SUSTAIN_SECONDS_FAST * SEND_SAMPLE_RATE / CHUNK_SIZE))
INTERRUPT_COOLDOWN_SECONDS = 0.70         # still used to debounce repeated flushes

# Spoken the instant the wake word is confirmed — ALWAYS this exact
# phrase, every time, with no variation. A short, respectful question —
# GAMA attentively waiting to hear the command — never a status
# statement ("I'm awake."), never a greeting, never a briefing/summary.
_WAKE_ACK_LINE = "Yes, Sir?"

# Explicit *system*-sleep phrases — the ONLY utterances allowed to reach
# computer_settings(action="sleep") and actually put the OS to sleep.
# Requires an explicit reference to the machine (system/computer/pc/
# laptop/machine), not just the bare word "sleep" — so "go to sleep"
# (GAMA's own session-sleep command, see _sleep_word_re / _enter_sleep_mode)
# can never be misrouted into a real OS sleep just because Gemini's
# server-side tool-call decision happened to pick computer_settings for it.
_SYSTEM_SLEEP_RE = re.compile(
    r"\b(put|set|switch|send|force)\b[\s\w]{0,20}\b(system|computer|pc|laptop|machine|it)\b"
    r"[\s\w]{0,20}\bto\s+sleep\b"
    r"|\b(system|computer|pc|laptop|machine)\b[\s\w]{0,20}\bto\s+sleep\b"
    r"|\bsleep\s+mode\b[\s\w]{0,20}\b(system|computer|pc|laptop|machine)\b"
    r"|\b(system|computer|pc|laptop|machine)\b[\s\w]{0,20}\bsleep\s+mode\b",
    re.IGNORECASE,
)
INTERRUPT_ENV_HISTORY_MAX = INTERRUPT_SUSTAIN_FRAMES * 3   # cap on envelope-history length
INTERRUPT_ECHO_CORR_THRESHOLD = 0.55     # mic/TTS envelope correlation at/above this = echo, suppress

# Layer 1: coupling factor — see above.  0.5 = tolerate up to 50 % speaker
# output reaching the mic before calling it a barge-in.  Worst-case laptop
# coupling is ~25 %, so this gives 2× margin.
SPEAKER_COUPLING_FACTOR = 0.42
# Hard ceiling so very-loud TTS never makes barge-in impossible.
# A person speaking at conversation level produces ~0.15–0.30 RMS on a
# close mic; capping at 0.28 ensures they can always interrupt.
INTERRUPT_AMP_MAX_THRESHOLD = 0.22

# Layer 2: seconds to keep the Gemini gate closed after Gama goes silent.
POST_SPEECH_GATE_SECONDS = 0.03

# --- Per-microphone calibration overrides -----------------------------------
# All the INTERRUPT_* / SPEAKER_COUPLING_FACTOR constants above are sane,
# generic defaults. voice/mic_calibration.py's Microphone Calibration
# Wizard (Settings → Calibrate Microphone) measures the user's actual mic
# and derives tighter, per-device values for them. apply_interrupt_calibration()
# rebinds the module globals in place (the audio callback below reads them
# as plain globals, so this takes effect immediately, no restart needed).
def apply_interrupt_calibration(params: dict) -> None:
    """Live-apply a calibrated parameter set (see voice/mic_calibration.py
    CalibrationResult.apply()). Silently ignores unknown keys so a partial
    or forward-compatible profile never raises."""
    global INTERRUPT_AMP_THRESHOLD, INTERRUPT_FAST_AMP_RATIO, INTERRUPT_AMP_MAX_THRESHOLD
    global SPEAKER_COUPLING_FACTOR, INTERRUPT_ECHO_CORR_THRESHOLD
    global INTERRUPT_SUSTAIN_SECONDS, INTERRUPT_SUSTAIN_SECONDS_FAST, INTERRUPT_COOLDOWN_SECONDS
    global INTERRUPT_SUSTAIN_FRAMES, INTERRUPT_SUSTAIN_FRAMES_FAST, INTERRUPT_ENV_HISTORY_MAX
    global POST_SPEECH_GATE_SECONDS

    if not params:
        return
    if "INTERRUPT_AMP_THRESHOLD" in params:
        INTERRUPT_AMP_THRESHOLD = float(params["INTERRUPT_AMP_THRESHOLD"])
    if "INTERRUPT_FAST_AMP_RATIO" in params:
        INTERRUPT_FAST_AMP_RATIO = float(params["INTERRUPT_FAST_AMP_RATIO"])
    if "INTERRUPT_AMP_MAX_THRESHOLD" in params:
        INTERRUPT_AMP_MAX_THRESHOLD = float(params["INTERRUPT_AMP_MAX_THRESHOLD"])
    if "SPEAKER_COUPLING_FACTOR" in params:
        SPEAKER_COUPLING_FACTOR = float(params["SPEAKER_COUPLING_FACTOR"])
    if "INTERRUPT_ECHO_CORR_THRESHOLD" in params:
        INTERRUPT_ECHO_CORR_THRESHOLD = float(params["INTERRUPT_ECHO_CORR_THRESHOLD"])
    if "INTERRUPT_SUSTAIN_SECONDS" in params:
        INTERRUPT_SUSTAIN_SECONDS = float(params["INTERRUPT_SUSTAIN_SECONDS"])
    if "INTERRUPT_SUSTAIN_SECONDS_FAST" in params:
        INTERRUPT_SUSTAIN_SECONDS_FAST = float(params["INTERRUPT_SUSTAIN_SECONDS_FAST"])
    if "INTERRUPT_COOLDOWN_SECONDS" in params:
        INTERRUPT_COOLDOWN_SECONDS = float(params["INTERRUPT_COOLDOWN_SECONDS"])
    if "POST_SPEECH_GATE_SECONDS" in params:
        POST_SPEECH_GATE_SECONDS = float(params["POST_SPEECH_GATE_SECONDS"])

    # Recompute derived frame counts from the (possibly new) sustain seconds.
    INTERRUPT_SUSTAIN_FRAMES = max(1, int(INTERRUPT_SUSTAIN_SECONDS * SEND_SAMPLE_RATE / CHUNK_SIZE))
    INTERRUPT_SUSTAIN_FRAMES_FAST = max(1, int(INTERRUPT_SUSTAIN_SECONDS_FAST * SEND_SAMPLE_RATE / CHUNK_SIZE))
    INTERRUPT_ENV_HISTORY_MAX = INTERRUPT_SUSTAIN_FRAMES * 3

    log.info(f"main: interrupt/barge-in parameters calibrated → {params}")


def _load_calibration_profile_for_active_mic() -> None:
    """Called once at startup: if the current default microphone has a
    saved calibration profile (voice/mic_calibration.py), apply it
    immediately instead of the generic defaults above."""
    try:
        from voice.mic_calibration import get_active_device_name
        from state_engine import mic_profiles
        device = get_active_device_name()
        profile = mic_profiles.get_profile(device)
        if profile and profile.get("params"):
            apply_interrupt_calibration(profile["params"])
            log.info(f"main: applied saved calibration profile for microphone {device!r}.")
        else:
            log.info(
                f"main: no calibration profile for microphone {device!r} — "
                "using default interruption tuning. Run the Microphone "
                "Calibration Wizard (Settings) for device-specific tuning."
            )
    except Exception as exc:
        log.debug(f"main: calibration profile load skipped: {exc}")


try:
    _load_calibration_profile_for_active_mic()
except Exception:
    pass


def _looks_like_echo(mic_env: list, tts_env: list,
                      corr_threshold: float = INTERRUPT_ECHO_CORR_THRESHOLD) -> bool:
    """Cheap, model-free check: does the mic's amplitude envelope track
    Gama's own TTS output envelope closely enough over this window to be
    acoustic echo/bleed, rather than an independent human voice talking
    over it?

    Pure numpy correlation over the last accumulated hot-frame window
    (a handful of samples) — runs in well under a millisecond, so it
    never delays barge-in and can't hang (no model, no thread hop).
    Fails open (returns False = "not echo, allow barge-in") whenever
    there isn't enough signal to make a confident call, since a missed
    echo-suppression is far less annoying than a barge-in that never
    fires.
    """
    if len(mic_env) < 4 or len(tts_env) < 4:
        return False
    try:
        import numpy as _np_echo
        tts_arr = _np_echo.asarray(tts_env, dtype=_np_echo.float64)
        if float(_np_echo.mean(tts_arr)) < 0.02:
            return False  # Gama isn't really outputting anything — can't be echo
        mic_arr = _np_echo.asarray(mic_env, dtype=_np_echo.float64)
        if _np_echo.std(mic_arr) < 1e-6 or _np_echo.std(tts_arr) < 1e-6:
            return False
        corr = float(_np_echo.corrcoef(mic_arr, tts_arr)[0, 1])
        if _np_echo.isnan(corr):
            return False
        return corr >= corr_threshold
    except Exception:
        return False  # fail open — never let a math hiccup block barge-in


