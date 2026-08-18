# GAMA Wake Word

Always-on, local, offline detection of **"Wake up GAMA"** (configurable).
This is the *only* phrase the local, offline listener reacts to — there
are no local "stop"/"cancel"/"listen" interrupt words. (Barge-in — GAMA
stopping when you talk over it mid-sentence — is handled separately by
the cloud session itself, not by this local listener.)

## Why this exists

Previously, "wake word" support meant streaming your mic audio to the
Gemini Live API continuously, all the time, and only checking the cloud
transcript afterwards for the phrase "wake up gama". That's slow (a
network round-trip before anything happens), not private (audio leaves
your machine even while "asleep"), and needlessly expensive (constant
API usage).

Now: a small local model listens on-device. Nothing is sent anywhere
until *after* the wake word fires. GAMA also goes back to local-only
listening automatically after a period of silence (`auto_sleep_seconds`
in config), so a normal idle day costs zero cloud audio streaming.

## Quick start (default backend: Vosk — no account needed)

```bash
pip install vosk
python scripts/download_vosk_model.py
python main.py
```

That's it — `config/wake_word.json` already points at the model path
the downloader uses. Say "wake up gama" and check `logs/gama.log` for
`Local wake word detected`.

To test the listener by itself, without booting the rest of GAMA:

```bash
python -m wake_word.listener
```

It'll print `>> detected: wake` live — this is the fastest way to tune
sensitivity before wiring it into a full run.

## Switching the wake phrase

Edit `config/wake_word.json`:

```json
"wake_phrase": "hey jarvis"
```

No retraining needed on the Vosk backend — it's plain grammar-constrained
recognition, so any short phrase works immediately. (Porcupine, below,
needs a new `.ppn` file per phrase.)

## Tuning sensitivity

```json
"sensitivity": 0.55
```

Range 0.0–1.0. Lower = fewer false positives but might miss soft/fast
speech. Higher = catches more but may trigger on similar-sounding
phrases. Start at 0.55 and adjust in 0.05 steps while watching
`python -m wake_word.listener`.

## Optional backend: Porcupine (lower idle CPU)

Vosk is easiest to set up (just a model download) but, being a general
speech recognizer restricted to a small vocabulary, uses somewhat more
CPU than a purpose-built wake-word spotter. If idle CPU matters more
than setup time, switch to Porcupine:

1. Create a free account at https://console.picovoice.ai and grab your
   **AccessKey**.
2. In the console, use "Create Wake Word" to train a `.ppn` file for
   your exact phrase (e.g. "wake up gama"), targeting your OS
   (Windows/macOS/Linux). Download it into `models/porcupine/`.
3. Edit `config/wake_word.json`:

```json
{
  "backend": "porcupine",
  "porcupine": {
    "access_key": "YOUR_ACCESS_KEY",
    "keywords": [
      { "label": "wake", "path": "models/porcupine/wake-up-gama_en_windows.ppn", "sensitivity": 0.6 }
    ]
  }
}
```

4. `pip install pvporcupine` and run `python -m wake_word.listener` to
   confirm it loads before relying on it.

Porcupine's frame size (`porcupine.frame_length`, normally 512 samples
at 16kHz) must match GAMA's mic `CHUNK_SIZE` in `main.py` — they already
match by default (both 512 @ 16kHz), so no change should be needed
unless you've tuned `CHUNK_SIZE` elsewhere.

## Interrupting GAMA while it's speaking

There are no local interrupt words. Talking over GAMA while it's
speaking is handled by the cloud session's own barge-in detection
(Gemini reports `interrupted` on its own), which immediately stops
playback and clears the pending audio queue — no separate local
keyword is needed or supported.

## Turning it off

```json
"enabled": false
```

GAMA falls back to the old always-on behavior (starts awake, stays
awake). Also happens automatically if the configured backend fails to
load (missing model, bad access key, etc.) — check `logs/gama.log`,
GAMA will keep running rather than crash.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Vosk model not found" at startup | Run `python scripts/download_vosk_model.py` |
| Never triggers | Sensitivity too low, or mic device/volume issue — test with `python -m wake_word.listener` first |
| Triggers on unrelated speech | Sensitivity too high, or phrase too short/common — lengthen the phrase or lower sensitivity |
| Porcupine: "frame_length mismatch" warning in log | `CHUNK_SIZE` in `main.py` no longer matches `porcupine.frame_length` — align them or switch back to `backend: "vosk"` |
