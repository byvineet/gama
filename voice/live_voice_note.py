"""
voice/live_voice_note.py — Gemini Live native-audio voice notes
===============================================================
Short-lived Live session → raw PCM (24 kHz s16le mono) → OGG Opus for Telegram.

Quality:
  - Collect ONLY response.data (no model_turn duplicate audio)
  - Align int16 frames; light peak normalize
  - ffmpeg: s16le → libopus voip @ 96 kbps / 48 kHz
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

LIVE_MODEL = os.environ.get(
    "LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
).strip() or "gemini-2.5-flash-native-audio-preview-12-2025"

DEFAULT_SAMPLE_RATE = 24000
DEFAULT_VOICE = "Charon"
MAX_COLLECT_SECONDS = 60.0
TRAILING_SILENCE_S = 0.35
OPUS_BITRATE = "96k"


def _resolve_api_key() -> str:
    try:
        from core.config_manager import config as _cfg
        key = (_cfg.gemini_key() or "").strip()
        if key:
            return key
    except Exception:
        pass
    try:
        import json
        import sys
        base = (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent
        )
        with open(base / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return (json.load(f).get("gemini_api_key") or "").strip()
    except Exception:
        return ""


def _resolve_voice_name(explicit: Optional[str] = None) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    try:
        import json
        import sys
        base = (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent
        )
        for candidate in (
            base / "config" / "user_settings.json",
            base / "storage" / "user_settings.json",
            base / "config" / "api_keys.json",
        ):
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                for key in ("voice_name", "tts_voice", "LIVE_VOICE", "voice"):
                    v = data.get(key)
                    if v and str(v).strip():
                        return str(v).strip()
    except Exception:
        pass
    return (os.environ.get("TTS_VOICE") or os.environ.get("LIVE_VOICE") or "").strip() or DEFAULT_VOICE


def _align_pcm(pcm: bytes) -> bytes:
    if len(pcm) % 2 == 1:
        return pcm[:-1]
    return pcm


def _peak_normalize_pcm(pcm: bytes, peak_target: float = 0.89) -> bytes:
    try:
        import array

        if len(pcm) < 4:
            return pcm
        samples = array.array("h")
        samples.frombytes(pcm)
        peak = max(abs(s) for s in samples) if samples else 0
        if peak < 200:
            return pcm
        target = int(32767 * peak_target)
        if peak >= target:
            return pcm
        gain = min(target / float(peak), 3.5)
        for i, s in enumerate(samples):
            v = int(s * gain)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            samples[i] = v
        return samples.tobytes()
    except Exception as exc:
        log.debug("peak normalize skipped: %s", exc)
        return pcm


def _pcm_to_wav(pcm: bytes, sample_rate: int, path: Path) -> Path:
    pcm = _align_pcm(pcm)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return path


def _encode_ogg_opus(pcm: bytes, sample_rate: int, ogg_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    pcm = _align_pcm(pcm)
    if not pcm:
        return False
    try:
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "s16le",
            "-ar", str(int(sample_rate)),
            "-ac", "1",
            "-i", "pipe:0",
            "-af", "highpass=f=60",
            "-c:a", "libopus",
            "-application", "voip",
            "-b:a", OPUS_BITRATE,
            "-vbr", "on",
            "-compression_level", "5",
            "-frame_duration", "20",
            "-ar", "48000",
            str(ogg_path),
        ]
        proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=90)
        if proc.returncode != 0:
            log.warning(
                "ffmpeg opus encode failed (%s): %s",
                proc.returncode,
                (proc.stderr or b"")[:400].decode("utf-8", errors="ignore"),
            )
            return False
        return ogg_path.is_file() and ogg_path.stat().st_size > 64
    except Exception as exc:
        log.warning("ffmpeg encode exception: %s", exc)
        return False


def _encode_ogg_from_wav(wav_path: Path, ogg_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(wav_path),
                "-af", "highpass=f=60",
                "-c:a", "libopus",
                "-application", "voip",
                "-b:a", OPUS_BITRATE,
                "-vbr", "on",
                "-compression_level", "5",
                "-ar", "48000",
                str(ogg_path),
            ],
            capture_output=True,
            timeout=90,
        )
        return proc.returncode == 0 and ogg_path.is_file() and ogg_path.stat().st_size > 64
    except Exception as exc:
        log.debug("wav→ogg failed: %s", exc)
        return False


async def _collect_live_pcm(text: str, voice_name: str, api_key: str) -> tuple[bytes, int]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1alpha"},
    )

    # Pull the same voice-style contract used by the main Live session
    try:
        from core.personality_prompt import get_personality_prompt
        persona = get_personality_prompt().strip()
    except Exception:
        persona = (
            "You are GAMA, a calm, precise, highly competent personal AI for Sir. "
            "Tone: composed, efficient, respectful — never chatty, never sycophantic."
        )

    system = (
        f"{persona}\n\n"
        "[VOICE NOTE MODE]\n"
        "You are leaving a short Telegram voice note for Sir. "
        "This must sound like GAMA speaking in person — not a flat text-to-speech reading. "
        "Composed, confident, slightly formal, warm without softness. "
        "Natural pacing: brief pauses after commas, light emphasis on key words, "
        "unhurried rhythm. Faithful to the message; smooth phrasing for speech is fine. "
        "No greetings, no questions, no extra commentary, no tools."
    )

    cfg_kwargs = dict(
        response_modalities=["AUDIO"],
        system_instruction=system,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name,
                )
            )
        ),
    )
    # Affective dialog → less flat, more natural delivery (same as main Live)
    cfg_kwargs["enable_affective_dialog"] = True
    try:
        config = types.LiveConnectConfig(**cfg_kwargs)
    except TypeError:
        cfg_kwargs.pop("enable_affective_dialog", None)
        try:
            config = types.LiveConnectConfig(**cfg_kwargs)
        except Exception:
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=system,
            )
    except Exception:
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system,
        )

    chunks: list[bytes] = []
    sample_rate = DEFAULT_SAMPLE_RATE
    script = (text or "").strip()
    if not script:
        raise ValueError("Empty text for voice note")
    if len(script) > 2500:
        script = script[:2500].rsplit(" ", 1)[0] + "…"

    # Naturalize phrasing so it is spoken, not read aloud flatly
    try:
        # response_styler removed
        styled = script  # response_styler removed (pass-through)
        script = (styled.speech or script).strip()
    except Exception:
        pass

    prompt = (
        "Deliver this as GAMA speaking to Sir on a voice note. "
        "Natural pacing and composed confidence — not a flat reading:\n\n"
        f"{script}"
    )

    async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
        await session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text=prompt)],
            ),
            turn_complete=True,
        )

        loop = asyncio.get_event_loop()
        deadline = loop.time() + MAX_COLLECT_SECONDS
        last_audio_at: Optional[float] = None

        async for response in session.receive():
            now = loop.time()
            if now > deadline:
                log.warning("live_voice_note: hard timeout")
                break

            data = getattr(response, "data", None)
            if data:
                blob = bytes(data)
                if len(blob) >= 2:
                    chunks.append(blob)
                    last_audio_at = now
                continue

            sc = getattr(response, "server_content", None)
            if sc is None:
                continue
            if getattr(sc, "turn_complete", False) or getattr(sc, "generation_complete", False):
                break

    pcm = _align_pcm(b"".join(chunks))
    if not pcm or len(pcm) < sample_rate // 10:
        raise RuntimeError(
            "Live session returned no usable audio. Check LIVE_MODEL access and API key."
        )

    pcm = _peak_normalize_pcm(pcm)
    log.info(
        "live_voice_note: collected %.2fs PCM (%d bytes) voice=%s",
        len(pcm) / 2 / sample_rate,
        len(pcm),
        voice_name,
    )
    return pcm, sample_rate


def synthesize_live_voice_note(
    text: str,
    *,
    voice_name: Optional[str] = None,
    out_dir: Optional[Path] = None,
) -> Path:
    api_key = _resolve_api_key()
    if not api_key:
        raise RuntimeError("Gemini API key not configured — cannot synthesize Live voice note.")

    voice = _resolve_voice_name(voice_name)
    text = (text or "").strip()
    if not text:
        raise ValueError("No text to speak")

    async def _run():
        return await _collect_live_pcm(text, voice, api_key)

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pcm, sr = pool.submit(lambda: asyncio.run(_run())).result(
                    timeout=MAX_COLLECT_SECONDS + 25
                )
        else:
            pcm, sr = asyncio.run(_run())
    except Exception as exc:
        log.warning("live_voice_note synthesis failed: %s", exc)
        raise

    out_dir = Path(out_dir) if out_dir else Path(tempfile.gettempdir()) / "gama_voice_notes"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"gama_live_note_{os.getpid()}_{abs(hash(text)) & 0xFFFF:x}"
    wav_path = out_dir / f"{stem}.wav"
    ogg_path = out_dir / f"{stem}.ogg"

    if _encode_ogg_opus(pcm, sr, ogg_path):
        return ogg_path

    _pcm_to_wav(pcm, sr, wav_path)
    if _encode_ogg_from_wav(wav_path, ogg_path):
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
        return ogg_path

    log.warning("ffmpeg/opus unavailable or failed — sending WAV (lower Telegram quality)")
    return wav_path


__all__ = ["synthesize_live_voice_note", "LIVE_MODEL"]
