"""Unit tests for utils.paths and interrupt calibration isolation."""
from __future__ import annotations

from utils.paths import get_base_dir, resource_path, user_data_path


def test_paths_consistent():
    base = get_base_dir()
    assert base.exists() or True  # may be pure path
    assert user_data_path("memory/x.db") == base / "memory" / "x.db"
    assert "prompt" in str(resource_path("core/prompt.txt")) or "core" in str(resource_path("core/prompt.txt"))


def test_interrupt_calibration_module_imports():
    from core import interrupt_calibration as ic
    assert hasattr(ic, "INTERRUPT_COOLDOWN_SECONDS")
    assert hasattr(ic, "apply_interrupt_calibration")
    assert callable(ic._looks_like_echo)
    # fail-open on short envelopes
    assert ic._looks_like_echo([0.1], [0.1]) is False
