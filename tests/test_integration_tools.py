"""
Integration tests: tool registry + plugins.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core.confidence import ActionRisk


@pytest.mark.integration
def test_registry_dispatch_chain(clean_tool_registry):
    reg = clean_tool_registry
    calls = []

    def handler(args):
        calls.append(dict(args))
        return f"ok:{args.get('x')}"

    reg.register("itest_echo", handler, risk=ActionRisk.SAFE, description="t", category="test")
    assert reg.dispatch("itest_echo", {"x": 42}) == "ok:42"
    assert calls == [{"x": 42}]
    assert reg.dispatch("missing_tool_xyz", {}) == "Unknown tool: missing_tool_xyz"


@pytest.mark.integration
def test_plugin_registers_into_registry(tmp_path, monkeypatch, clean_tool_registry):
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "__init__.py").write_text("", encoding="utf-8")
    (plug / "itest_plugin.py").write_text(
        textwrap.dedent(
            """
            def _h(args):
                return f"plugin:{args.get('n', '')}"
            PLUGIN = {
                "name": "itest_plugin_tool",
                "description": "integration plugin",
                "handler": _h,
                "risk": "SAFE",
                "category": "plugin",
            }
            """
        ),
        encoding="utf-8",
    )

    import core.plugin_loader as pl
    from core import tool_registry as tr_mod

    monkeypatch.setattr(pl, "_plugins_dir", lambda: plug)
    pl._loaded.clear()
    pl._loaded_names.clear()
    monkeypatch.setattr(tr_mod, "tool_registry", clean_tool_registry)

    entries = pl.load_plugins(register=True)
    assert any(e["name"] == "itest_plugin_tool" for e in entries)
    assert clean_tool_registry.dispatch("itest_plugin_tool", {"n": "hi"}) == "plugin:hi"
