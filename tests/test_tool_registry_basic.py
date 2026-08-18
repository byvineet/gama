"""tests/test_tool_registry_basic.py — registry smoke tests"""

from __future__ import annotations

from core.confidence import ActionRisk
from core.tool_registry import ToolRegistry


def test_register_and_dispatch():
    reg = ToolRegistry()
    reg.register(
        "unit_ping",
        handler=lambda args: f"pong:{args.get('v', '')}",
        risk=ActionRisk.SAFE,
        description="unit test tool",
        category="test",
    )
    assert "unit_ping" in reg.list_names()
    result = reg.dispatch("unit_ping", {"v": "x"})
    assert result == "pong:x"


def test_missing_tool():
    reg = ToolRegistry()
    out = reg.dispatch("does_not_exist", {})
    assert "No handler" in out or "not" in out.lower()
