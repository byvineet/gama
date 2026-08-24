"""
tests/test_live_vision.py — Unit tests for live_vision action and snapshot handling
"""

import pytest
from unittest.mock import MagicMock, patch

from vision.live_vision import live_vision_action, get_live_vision


def test_live_vision_status():
    res = live_vision_action(action="status")
    assert "Live vision mode=" in res


def test_live_vision_snapshot_injection():
    eng = get_live_vision()
    mock_sender = MagicMock()
    eng.set_sender(mock_sender)

    # Mock desktop capture to return dummy bytes
    with patch.object(eng, "_capture_camera_jpeg", return_value=b"\xff\xd8\xff\xe0testjpegdata"):
        res = live_vision_action(action="snapshot", mode="camera")
        assert "Exact-moment camera frame" in res
        assert "injected into the Live session" in res
        mock_sender.assert_called_once_with(b"\xff\xd8\xff\xe0testjpegdata", "image/jpeg")


def test_live_vision_enable_disable():
    res_enable = live_vision_action(action="enable", mode="desktop")
    assert "Live vision ON" in res_enable or "enabled" in res_enable.lower()

    res_disable = live_vision_action(action="disable")
    assert "Live vision OFF" in res_disable
