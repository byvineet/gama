"""tests/test_email_read.py — Phase 2 email IMAP helpers (no network)"""

from __future__ import annotations

import actions.email_sender as es


def test_read_without_config(monkeypatch):
    monkeypatch.setattr(es, "_get_email_config", lambda: {"email": "", "password": "", "provider": "gmail"})
    out = es._read_emails()
    assert "not configured" in out.lower() or "setup" in out.lower()


def test_dispatcher_read_action(monkeypatch):
    monkeypatch.setattr(es, "_read_emails", lambda **kw: "LISTED")
    assert es.email_sender("unread") == "LISTED"
    assert es.email_sender("list", limit=3) == "LISTED"


def test_dispatcher_summarize(monkeypatch):
    monkeypatch.setattr(es, "_summarize_emails", lambda **kw: "SUM")
    assert es.email_sender("summarize") == "SUM"


def test_decode_header_plain():
    assert es._decode_mime_header("Hello") == "Hello"


def test_unknown_action():
    out = es.email_sender("nope")
    assert "Unknown" in out
