"""
scripts/download_models.py — fetch the local voice-stack model files
======================================================================
GAMA's text-based wake detection depends on three offline model files
that are NOT bundled in the repo (they're too large for git):

    models/vad/silero_vad.onnx                        (~1.8 MB)
    models/whisper/ggml-<size>-q5_1.bin                (40 MB - 1 GB+)
    models/speaker/voxceleb_resnet34.onnx              (~25 MB, optional)

If these are missing, every module that needs them (voice/vad.py,
voice/stt_whisper.py, voice/speaker_id.py) fails soft and logs a
warning — GAMA still starts, but the local Whisper transcription
pipeline never produces a transcript, so text-based wake ("gama...")
never fires. From the outside this looks exactly like "wake word does
nothing", with no crash and no obvious error unless you're watching
the console.

Run this once after cloning:

    python scripts/download_models.py --all

Or fetch pieces individually:

    python scripts/download_models.py --vad
    python scripts/download_models.py --whisper base   # tiny|base|small|medium
    python scripts/download_models.py --speaker
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"

VAD_URL = "https://github.com/snakers4/silero-vad/raw/master/files/silero_vad.onnx"
VAD_PATH = MODELS_DIR / "vad" / "silero_vad.onnx"

# ggml-org's official whisper.cpp GGUF model mirror on Hugging Face.
WHISPER_SIZES = {
    "tiny": "ggml-tiny-q5_1.bin",
    "base": "ggml-base-q5_1.bin",
    "small": "ggml-small-q5_1.bin",
    "medium": "ggml-medium-q5_1.bin",
}
WHISPER_URL_TMPL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{fname}"
WHISPER_DIR = MODELS_DIR / "whisper"

SPEAKER_URL = (
    "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/"
    "resolve/main/voxceleb_resnet34_LM.onnx"
)
SPEAKER_PATH = MODELS_DIR / "speaker" / "voxceleb_resnet34.onnx"


def _download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  already have {dest.relative_to(BASE_DIR)} ({dest.stat().st_size / 1e6:.1f} MB) — skipping")
        return True
    print(f"  downloading {url}")
    print(f"        -> {dest.relative_to(BASE_DIR)}")
    try:
        tmp = dest.with_suffix(dest.suffix + ".part")

        def _report(block_num, block_size, total_size):
            if total_size <= 0:
                return
            done = block_num * block_size
            pct = min(100, done * 100 // total_size)
            print(f"\r    {pct:3d}%", end="", flush=True)

        urllib.request.urlretrieve(url, tmp, reporthook=_report)
        print()
        tmp.rename(dest)
        return True
    except Exception as exc:
        print(f"  FAILED: {exc}")
        print(f"  You can also download it manually and place it at:\n    {dest}")
        return False


def fetch_vad() -> bool:
    print("[VAD] Silero VAD (ONNX)")
    return _download(VAD_URL, VAD_PATH)


def fetch_whisper(size: str) -> bool:
    if size not in WHISPER_SIZES:
        print(f"Unknown whisper size '{size}'. Choose from: {', '.join(WHISPER_SIZES)}")
        return False
    fname = WHISPER_SIZES[size]
    print(f"[Whisper] {size} (GGUF q5_1)")
    return _download(WHISPER_URL_TMPL.format(fname=fname), WHISPER_DIR / fname)


def fetch_speaker() -> bool:
    print("[Speaker-ID] WeSpeaker ResNet34 (ONNX)")
    return _download(SPEAKER_URL, SPEAKER_PATH)


def main() -> int:
    ap = argparse.ArgumentParser(description="Download GAMA's local voice-stack models.")
    ap.add_argument("--all", action="store_true", help="download VAD + whisper(base) + speaker-ID")
    ap.add_argument("--vad", action="store_true", help="download Silero VAD")
    ap.add_argument("--whisper", nargs="?", const="base", default=None,
                     metavar="SIZE", help="download whisper.cpp GGUF (tiny|base|small|medium, default base)")
    ap.add_argument("--speaker", action="store_true", help="download WeSpeaker speaker-ID model")
    args = ap.parse_args()

    if not any([args.all, args.vad, args.whisper, args.speaker]):
        ap.print_help()
        return 1

    ok = True
    if args.all or args.vad:
        ok &= fetch_vad()
    if args.all:
        ok &= fetch_whisper("base")
    elif args.whisper:
        ok &= fetch_whisper(args.whisper)
    if args.all or args.speaker:
        ok &= fetch_speaker()

    if ok:
        print("\nAll requested models are in place. Restart GAMA to pick them up.")
    else:
        print("\nOne or more downloads failed — see messages above for manual-download paths.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
