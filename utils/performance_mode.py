"""
utils/performance_mode.py — Fast / Balanced / Full presets
==========================================================
Controls the latency-critical voice path: mic processing, tool-arm delay,
continuous listen, and reply pacing — without stripping all features.

Modes
-----
  fast      Low latency: no AEC, continuous stream when awake, no tools-armed
            delay, short pacing instruction, lean startup.
  balanced  Default compromise: AEC on, short tools-armed delay, normal prompt.
  full      Maximum intelligence: AEC+NS+AGC, full delays, full prompt stack.

Selection (first match wins)
----------------------------
  1. Environment:  GAMA_PERF_MODE=fast|balanced|full
  2. Config file:  config/performance.json  →  { "mode": "fast" }
  3. Default:      balanced

Usage
-----
    from utils.performance_mode import perf

    if perf.aec_enabled:
        ...
    delay = perf.tools_armed_delay_s
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ModeName = Literal["fast", "balanced", "full"]

_VALID = frozenset({"fast", "balanced", "full"})


@dataclass(frozen=True)
class PerfProfile:
    name: ModeName
    # Acoustic processing on the mic callback
    aec_enabled: bool
    ns_enabled: bool
    agc_enabled: bool
    # After boot greeting: seconds before action tools are armed (0 = immediate)
    tools_armed_delay_s: float
    # Prefer continuous mic→Gemini while awake (skip re-verify friction)
    continuous_listen_when_awake: bool
    # Skip non-critical per-chunk work on the audio callback
    thin_mic_callback: bool
    # Larger capture blocks (fewer Python callbacks/sec). None = leave default.
    preferred_chunk_size: int | None
    # Append a short "be brief / fast pacing" system directive
    short_pacing_prompt: bool
    # Defer heavy background monitors until after first interaction
    defer_background_monitors: bool
    description: str


_PROFILES: dict[ModeName, PerfProfile] = {
    "fast": PerfProfile(
        name="fast",
        aec_enabled=False,
        ns_enabled=False,
        agc_enabled=False,
        tools_armed_delay_s=0.0,
        continuous_listen_when_awake=True,
        thin_mic_callback=True,
        preferred_chunk_size=1024,
        short_pacing_prompt=True,
        defer_background_monitors=True,
        description=(
            "AEC off, continuous listen when awake, no tools-armed delay, "
            "short replies, deferred background work."
        ),
    ),
    "balanced": PerfProfile(
        name="balanced",
        aec_enabled=True,
        ns_enabled=True,
        agc_enabled=True,
        tools_armed_delay_s=2.0,
        continuous_listen_when_awake=True,
        thin_mic_callback=False,
        preferred_chunk_size=None,
        short_pacing_prompt=False,
        defer_background_monitors=False,
        description="Default: AEC on, short tools-armed delay, full prompt stack.",
    ),
    "full": PerfProfile(
        name="full",
        aec_enabled=True,
        ns_enabled=True,
        agc_enabled=True,
        tools_armed_delay_s=6.0,
        continuous_listen_when_awake=False,
        thin_mic_callback=False,
        preferred_chunk_size=None,
        short_pacing_prompt=False,
        defer_background_monitors=False,
        description="Maximum always-on intelligence; highest CPU and latency cost.",
    ),
}


def _read_config_mode() -> str | None:
    """Load mode from config/performance.json if present."""
    candidates: list[Path] = []
    env_home = (os.environ.get("GAMA_HOME") or "").strip()
    if env_home:
        candidates.append(Path(env_home) / "config" / "performance.json")
    # Relative to this file: ../../config/performance.json from utils/
    try:
        here = Path(__file__).resolve().parent.parent
        candidates.append(here / "config" / "performance.json")
    except Exception:
        pass
    data_dir = (os.environ.get("GAMA_DATA") or "").strip()
    if data_dir:
        candidates.append(Path(data_dir) / "config" / "performance.json")

    for path in candidates:
        try:
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            mode = str(raw.get("mode") or "").strip().lower()
            if mode in _VALID:
                return mode
        except Exception:
            continue
    return None


def resolve_mode() -> ModeName:
    env = (os.environ.get("GAMA_PERF_MODE") or "").strip().lower()
    if env in _VALID:
        return env  # type: ignore[return-value]
    cfg = _read_config_mode()
    if cfg in _VALID:
        return cfg  # type: ignore[return-value]
    return "balanced"


def get_profile(mode: ModeName | None = None) -> PerfProfile:
    name = mode or resolve_mode()
    if name not in _PROFILES:
        name = "balanced"
    return _PROFILES[name]


# Process-wide resolved profile (read once at import / first access)
class _PerfProxy:
    """Lazy proxy so env/config changes before first use still apply."""

    _cached: PerfProfile | None = None

    def _profile(self) -> PerfProfile:
        if self._cached is None:
            self._cached = get_profile()
        return self._cached

    def reload(self) -> PerfProfile:
        self._cached = get_profile()
        return self._cached

    @property
    def name(self) -> ModeName:
        return self._profile().name

    @property
    def aec_enabled(self) -> bool:
        return self._profile().aec_enabled

    @property
    def ns_enabled(self) -> bool:
        return self._profile().ns_enabled

    @property
    def agc_enabled(self) -> bool:
        return self._profile().agc_enabled

    @property
    def tools_armed_delay_s(self) -> float:
        return self._profile().tools_armed_delay_s

    @property
    def continuous_listen_when_awake(self) -> bool:
        return self._profile().continuous_listen_when_awake

    @property
    def thin_mic_callback(self) -> bool:
        return self._profile().thin_mic_callback

    @property
    def preferred_chunk_size(self) -> int | None:
        return self._profile().preferred_chunk_size

    @property
    def short_pacing_prompt(self) -> bool:
        return self._profile().short_pacing_prompt

    @property
    def defer_background_monitors(self) -> bool:
        return self._profile().defer_background_monitors

    @property
    def description(self) -> str:
        return self._profile().description

    def pacing_directive(self) -> str:
        if not self.short_pacing_prompt:
            return ""
        return (
            "PACING (Fast mode): Keep replies short and natural — one or two "
            "sentences when possible. Prefer complete concise answers so the "
            "conversation stays quick. Do not pad with filler."
        )


perf = _PerfProxy()

__all__ = ["PerfProfile", "get_profile", "resolve_mode", "perf"]
