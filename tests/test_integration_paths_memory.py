"""Integration tests: utils.paths + memory path consistency."""
from __future__ import annotations

import pytest


@pytest.mark.integration
def test_user_data_path_under_base():
    from utils.paths import get_base_dir, user_data_path

    base = get_base_dir()
    p = user_data_path("memory/layered_memory.db")
    assert "layered_memory.db" in str(p)
    assert p == base / "memory" / "layered_memory.db"


@pytest.mark.integration
def test_get_base_dir_shared_across_modules():
    from utils.paths import get_base_dir
    from actions import email_sender
    from memory import memory_manager

    assert get_base_dir() == email_sender._get_base_dir() == memory_manager._get_base_dir()


@pytest.mark.integration
def test_layered_memory_db_path_uses_user_data():
    import importlib
    try:
        lm = importlib.import_module("memory.layered_memory")
    except Exception as exc:
        pytest.skip(f"layered_memory import requires extra deps: {exc}")
    assert "layered_memory.db" in str(lm._DB_PATH)
