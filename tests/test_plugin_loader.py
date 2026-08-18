"""tests/test_plugin_loader.py — Phase 2 plugin system"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def test_example_echo_plugin_loads(tmp_path, monkeypatch):
    # Point plugins dir at a temp folder with a minimal plugin
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "__init__.py").write_text("", encoding="utf-8")
    (plug / "demo.py").write_text(
        textwrap.dedent(
            '''
            def _h(args):
                return f"pong:{args.get('x', '')}"
            PLUGIN = {
                "name": "demo_ping",
                "description": "demo",
                "handler": _h,
                "risk": "SAFE",
                "category": "plugin",
            }
            '''
        ),
        encoding="utf-8",
    )

    import core.plugin_loader as pl

    monkeypatch.setattr(pl, "_plugins_dir", lambda: plug)
    # reset state
    pl._loaded.clear()
    pl._loaded_names.clear()

    entries = pl.load_plugins(register=False)
    names = [e["name"] for e in entries]
    assert "demo_ping" in names
    entry = next(e for e in entries if e["name"] == "demo_ping")
    assert entry["handler"]({"x": "1"}) == "pong:1"


def test_get_plugin_declarations_shape(tmp_path, monkeypatch):
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "__init__.py").write_text("", encoding="utf-8")
    (plug / "demo2.py").write_text(
        textwrap.dedent(
            '''
            def _h(args):
                return "ok"
            PLUGIN = {
                "name": "demo_decl",
                "description": "decl test",
                "handler": _h,
                "parameters": {"type": "OBJECT", "properties": {}, "required": []},
            }
            '''
        ),
        encoding="utf-8",
    )
    import core.plugin_loader as pl

    monkeypatch.setattr(pl, "_plugins_dir", lambda: plug)
    pl._loaded.clear()
    pl._loaded_names.clear()
    decls = pl.get_plugin_declarations()
    assert any(d["name"] == "demo_decl" for d in decls)
    d = next(d for d in decls if d["name"] == "demo_decl")
    assert "description" in d
    assert "parameters" in d
