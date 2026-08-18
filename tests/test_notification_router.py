"""tests/test_notification_router.py — Phase 3 smart routing"""
from __future__ import annotations
import core.notification_router as nr

def test_queue_when_asleep(monkeypatch):
    monkeypatch.setattr(nr, "_context", lambda: {"asleep": True, "meeting": False, "dnd": False, "active_app": ""})
    monkeypatch.setattr(nr, "_send_desktop", lambda *a, **k: True)
    monkeypatch.setattr(nr, "_send_telegram", lambda *a, **k: False)
    # clear queue
    with nr._lock:
        nr._queue.clear()
    out = nr.route_notification("T", "m", priority="normal")
    assert "Queued" in out
    assert "asleep" in out.lower()

def test_urgent_uses_telegram(monkeypatch):
    monkeypatch.setattr(nr, "_context", lambda: {"asleep": False, "meeting": False, "dnd": False, "active_app": ""})
    calls = []
    monkeypatch.setattr(nr, "_send_telegram", lambda msg, kind="": calls.append("tg") or True)
    monkeypatch.setattr(nr, "_send_desktop", lambda *a, **k: calls.append("desk") or True)
    out = nr.route_notification("Alert", "now", priority="urgent")
    assert "tg" in calls
    assert "Delivered" in out

def test_flush(monkeypatch):
    with nr._lock:
        nr._queue.clear()
        nr._queue.append(nr.QueuedNote("a", "b", "info", "normal"))
    monkeypatch.setattr(nr, "_send_desktop", lambda *a, **k: True)
    out = nr.flush_queue()
    assert "Flushed 1" in out
