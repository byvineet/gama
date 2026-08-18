"""
Integration tests: email dispatcher + clipboard offline paths.
"""
from __future__ import annotations

import pytest
import actions.email_sender as es
import actions.clipboard as clip


@pytest.mark.integration
def test_email_action_routing(monkeypatch):
    monkeypatch.setattr(es, "_read_emails", lambda **kw: f"listed limit={kw.get('limit')}")
    monkeypatch.setattr(es, "_summarize_emails", lambda **kw: "sum")
    monkeypatch.setattr(es, "_read_one", lambda **kw: "one")
    assert "listed" in es.email_sender("unread", limit=3)
    assert es.email_sender("summarize") == "sum"
    assert es.email_sender("read_one", index=1) == "one"
    assert "Unknown" in es.email_sender("nope")


@pytest.mark.integration
def test_clipboard_ai_actions_wired(monkeypatch):
    """clipboard.py exposes summarize/translate via its own clipboard_ai helper."""
    def fake_ai(action="summarize", language="English", write_back=False):
        if action == "summarize":
            return "SUMMARY"
        if action == "translate":
            return "TRANSLATED"
        return f"AI {action}"

    monkeypatch.setattr(clip, "clipboard_ai", fake_ai)
    assert "SUMMARY" in clip.clipboard("summarize")
    assert "TRANSLATED" in clip.clipboard("translate", language="Hindi")
