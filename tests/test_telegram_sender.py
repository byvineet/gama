"""
tests/test_telegram_sender.py — Tests for telegram_sender resolution & routing
"""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from actions.telegram_sender import (
    telegram_sender,
    _resolve_schedule_day,
    _content_from_class_schedule,
    _resolve_content,
)


def test_resolve_schedule_day_default_is_today():
    # When no day is provided or empty, it MUST default to today, never tomorrow
    assert _resolve_schedule_day("") == "today"
    assert _resolve_schedule_day(None) == "today"
    assert _resolve_schedule_day("schedule") == "today"


def test_resolve_schedule_day_explicit():
    assert _resolve_schedule_day("today") == "today"
    assert _resolve_schedule_day("tomorrow") == "tomorrow"
    assert _resolve_schedule_day("week") == "week"
    assert _resolve_schedule_day("next") == "next"
    assert _resolve_schedule_day("wednesday") == "wednesday"
    assert _resolve_schedule_day("monday") == "monday"


def test_resolve_schedule_day_from_fallback_text():
    assert _resolve_schedule_day("", fallback_text="send my today's class schedule") == "today"
    assert _resolve_schedule_day("", fallback_text="tomorrow's class schedule please") == "tomorrow"
    assert _resolve_schedule_day("", fallback_text="classes on friday") == "friday"


def test_content_from_class_schedule_text_vs_voice():
    # Text format should return structured schedule description
    text_content = _content_from_class_schedule("wednesday", is_voice=False)
    assert "Wednesday" in text_content
    assert not text_content.startswith("Sir,")

    # Voice format should include conversational prefix
    voice_content = _content_from_class_schedule("wednesday", is_voice=True)
    assert voice_content.startswith("Sir,")
    assert "Wednesday" in voice_content


def test_resolve_content_explicit_message():
    # If the user/model provides explicit text, it should be preserved
    msg = "Today you have Mathematics at 4:00 PM and Physics at 6:15 PM."
    resolved = _resolve_content(is_voice=False, message=msg)
    assert resolved == msg


def test_resolve_content_schedule_regarding():
    # If regarding=class_schedule without day, defaults to today
    resolved_text = _resolve_content(is_voice=False, regarding="class_schedule")
    assert "classes:" in resolved_text or "No classes" in resolved_text
    assert not resolved_text.startswith("Sir,")

    resolved_voice = _resolve_content(is_voice=True, regarding="class_schedule", day="tomorrow")
    assert "Sir, here is your class schedule for tomorrow." in resolved_voice


def test_resolve_content_referential_schedule():
    # Referential phrase like "today's class schedule" resolves schedule for today
    resolved = _resolve_content(is_voice=False, message="today's class schedule")
    assert "classes:" in resolved or "No classes" in resolved


def test_telegram_sender_text_dispatch():
    with patch("actions.telegram_sender._get_chat_id", return_value="123456"), \
         patch("actions.telegram_sender._send_message", return_value="OK. Telegram text message sent.") as mock_send:
        
        # Test sending explicit message
        res = telegram_sender(action="send", message="Hello from tests")
        assert "OK" in res
        mock_send.assert_called_once_with("123456", "Hello from tests")

        # Test sending today's schedule via regarding
        mock_send.reset_mock()
        res = telegram_sender(action="send", regarding="class_schedule", day="today")
        assert "OK" in res
        called_args = mock_send.call_args[0]
        assert called_args[0] == "123456"
        assert "classes:" in called_args[1] or "No classes" in called_args[1]


def test_telegram_sender_voice_dispatch():
    with patch("actions.telegram_sender._get_chat_id", return_value="123456"), \
         patch("actions.telegram_sender._send_voice", return_value="Voice message sent on Telegram (Live native audio).") as mock_voice:
        
        res = telegram_sender(action="send_voice", regarding="class_schedule", day="today")
        assert "Voice message sent" in res
        called_args = mock_voice.call_args[0]
        assert called_args[0] == "123456"
        assert "Sir, here is your class schedule for today." in called_args[1]
