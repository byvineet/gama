"""
Shared pytest fixtures for G.A.M.A tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path (pytest.ini pythonpath also sets this)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def gama_data_dir(tmp_path, monkeypatch):
    """Isolated GAMA_DATA directory for tests that write state."""
    data = tmp_path / "gama_data"
    data.mkdir()
    monkeypatch.setenv("GAMA_DATA", str(data))
    return data


@pytest.fixture
def clean_tool_registry():
    """Fresh ToolRegistry instance (not the process singleton)."""
    from core.tool_registry import ToolRegistry
    from core.confidence import ActionRisk

    reg = ToolRegistry()
    return reg
