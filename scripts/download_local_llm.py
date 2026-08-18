"""
Gama - Local LLM Model Downloader
===================================
Fetches a small, fast, accurate GGUF model for offline reasoning
(core/llm_local.py) and writes its path into config/api_keys.json.

Recommended default: Llama-3.2-3B-Instruct-Q4_K_M
  - ~2.0 GB download, ~2.3 GB RAM at runtime
  - Strong accuracy for a 3B model on instruction-following and
    general Q&A — noticeably better reasoning than 1B models while
    still running comfortably on CPU-only laptops.
  - Q4_K_M quantization: the standard "sweet spot" — ~1-2% quality
    loss vs Q8/FP16 for roughly a quarter of the size/RAM and a large
    speed win, which matters most on CPU-only inference.

A smaller/faster option is also available (--fast) for lower-RAM
machines:
  Gemma-2-2B-IT-Q4_K_M — ~1.6 GB, still solid quality, ~30-40% faster
  token generation than the 3B model on typical CPUs.

Run from the project root:
    python scripts/download_local_llm.py            # recommended 3B model
    python scripts/download_local_llm.py --fast      # smaller/faster 2B model
    python scripts/download_local_llm.py --model-url <url> --name <file.gguf>

After downloading, the model path is written to
config/api_keys.json -> "llama_model_path" automatically (existing
keys/comments are preserved). Restart Gama (or wait for the next
offline transition) to pick it up — the model loads lazily on first
actual offline use, never at startup.

Author : Vineet Machchal
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models" / "llama"
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# (name, url, approx_size_gb) — both hosted on Hugging Face (bartowski's
# well-maintained GGUF quant repos), no account/token required for
# these public files.
MODELS = {
    "recommended": (
        "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/"
        "resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        2.0,
    ),
    "fast": (
        "gemma-2-2b-it-Q4_K_M.gguf",
        "https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/"
        "resolve/main/gemma-2-2b-it-Q4_K_M.gguf",
        1.6,
    ),
}


def _progress(block_num: int, block_size: int, total_size: int) -> None:
    if total_size <= 0:
        return
    done = block_num * block_size
    pct = min(100, done * 100 // total_size)
    sys.stdout.write(
        f"\r  downloading... {pct}% ({done // 1_000_000}MB/{total_size // 1_000_000}MB)"
    )
    sys.stdout.flush()


def _update_config(model_path: Path) -> None:
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        else:
            example = BASE_DIR / "config" / "api_keys.example.json"
            cfg = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {}
    except Exception as exc:
        print(f"  Could not read config/api_keys.json ({exc}) — skipping auto-config.")
        print(f"  Set \"llama_model_path\": \"{model_path.relative_to(BASE_DIR).as_posix()}\" manually.")
        return

    rel = model_path.relative_to(BASE_DIR).as_posix()
    cfg["llama_model_path"] = rel
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"  Updated config/api_keys.json -> llama_model_path = \"{rel}\"")
    except Exception as exc:
        print(f"  Could not write config/api_keys.json ({exc}).")
        print(f"  Set \"llama_model_path\": \"{rel}\" manually.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true",
                         help="Download the smaller/faster 2B model instead of the recommended 3B.")
    parser.add_argument("--model-url", default=None, help="Override: download this URL instead.")
    parser.add_argument("--name", default=None, help="Override: filename to save as (with --model-url).")
    args = parser.parse_args()

    if args.model_url:
        if not args.name:
            print("--model-url requires --name <filename.gguf>")
            return 1
        name, url, size_gb = args.name, args.model_url, None
    else:
        key = "fast" if args.fast else "recommended"
        name, url, size_gb = MODELS[key]

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / name

    if dest.exists():
        print(f"Model already present at {dest} — nothing to download.")
        _update_config(dest)
        return 0

    size_note = f" (~{size_gb} GB)" if size_gb else ""
    print(f"Downloading {name}{size_note} from:\n  {url}\n")
    try:
        urllib.request.urlretrieve(url, dest, reporthook=_progress)
        print()
    except Exception as exc:
        print(f"\nDownload failed: {exc}")
        print(f"You can also download manually and place the .gguf file at:\n  {dest}")
        return 1

    # Sanity check: valid GGUF files start with the magic bytes "GGUF".
    try:
        with open(dest, "rb") as f:
            magic = f.read(4)
        if magic != b"GGUF":
            print("Downloaded file does not look like a valid GGUF model "
                  "(bad header) — it may be incomplete or corrupted. Deleting.")
            dest.unlink(missing_ok=True)
            return 1
    except Exception as exc:
        print(f"Could not verify downloaded file: {exc}")
        return 1

    print(f"Done. Model saved to: {dest}")
    _update_config(dest)
    print(
        "\nOffline AI is now configured. It loads lazily — nothing extra "
        "happens at startup; the model loads the first time Gama actually "
        "goes offline (or you can trigger it early by running with no "
        "internet connection once)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
