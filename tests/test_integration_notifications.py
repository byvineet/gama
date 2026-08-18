"""
Integration tests: notification router behavior matrix.
"""
from __future__ import annotations

import pytest
import core.notification_router as nr


@pytest.mark.integration
def test_priority_matrix(monkeypatch):
    delivered = []

    monkeypatch.setattr(nr, "_send_desktop", lambda title, message, kind: delivered.append(("desk", title)) or True)
    monkeypatch.setattr(nr, "_send_telegram", lambda message, kind="": delivered.append(("tg", message)) or True)

    with nr._lock:
        nr._queue.clear()

    # Normal while awake → desktop
    monkeypatch.setattr(nr, "_context", lambda: {"asleep": False, "meeting": False, "dnd": False, "active_app": ""})
    assert "desktop" in nr.route_notification("Hi", "there", priority="normal").lower()
    assert any(d[0] == "desk" for d in delivered)

    delivered.clear()
    # Meeting → queue
    monkeypatch.setattr(nr, "_context", lambda: {"asleep": False, "meeting": True, "dnd": False, "active_app": "zoom"})
    out = nr.route_notification("Soft", "nudge", priority="normal")
    assert "Queued" in out

    delivered.clear()
    # Critical while meeting still delivers
    out = nr.route_notification("Wake", "up", priority="critical")
    assert "Delivered" in out
