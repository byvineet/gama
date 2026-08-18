"""
Gama - Vosk Model Downloader
=============================
Fetches the small English Vosk model used by the default ("vosk") wake
word backend and extracts it to models/vosk-model-small-en-us-0.15/.

This is a one-time, ~40MB download. After it's done, wake word
detection is fully offline — no network calls, no account.

Run from the project root:
    python scripts/download_vosk_model.py

Author : Vineet Machchal
"""

from __future__ import annotations

import sys
import urllib.request
import zipfile
from pathlib import Path

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"
DEST_DIR = MODELS_DIR / MODEL_NAME
ZIP_PATH = MODELS_DIR / f"{MODEL_NAME}.zip"


def _progress(block_num: int, block_size: int, total_size: int) -> None:
    if total_size <= 0:
        return
    done = block_num * block_size
    pct = min(100, done * 100 // total_size)
    sys.stdout.write(f"\r  downloading... {pct}% ({done // 1_000_000}MB/{total_size // 1_000_000}MB)")
    sys.stdout.flush()


def main() -> int:
    if DEST_DIR.exists():
        print(f"Model already present at {DEST_DIR} — nothing to do.")
        print("(Delete that folder first if you want to re-download.)")
        return 0

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {MODEL_NAME} from {MODEL_URL} ...")
    try:
        urllib.request.urlretrieve(MODEL_URL, ZIP_PATH, reporthook=_progress)
        print()
    except Exception as exc:
        print(f"\nDownload failed: {exc}")
        print(f"You can also download it manually from https://alphacephei.com/vosk/models "
              f"and extract it to: {DEST_DIR}")
        return 1

    print("Extracting...")
    try:
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            zf.extractall(MODELS_DIR)
    finally:
        ZIP_PATH.unlink(missing_ok=True)

    if DEST_DIR.exists():
        print(f"Done. Model ready at {DEST_DIR}")
        print("wake_word backend is already set to 'vosk' by default in "
              "config/wake_word.json — just run GAMA.")
        return 0

    print("Extraction finished but the expected folder wasn't found — "
          f"check {MODELS_DIR} manually.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
