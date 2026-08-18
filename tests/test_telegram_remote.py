"""tests/test_telegram_remote.py — Phase 3 remote (no network)"""
from __future__ import annotations
import actions.telegram_remote as tr

def test_status():
    out = tr.telegram_remote("status")
    assert "telegram_remote" in out

def test_start_without_config(monkeypatch):
    import actions.telegram_sender as ts
    monkeypatch.setattr(ts, "is_configured", lambda: False)
    out = tr.start_telegram_remote()
    assert "not configured" in out.lower() or "setup" in out.lower()
