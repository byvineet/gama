"""Legacy conversation-engine tests (module removed/renamed) — placeholder skip."""
import pytest

pytestmark = pytest.mark.skip(
    reason="Legacy test targets missing conversation module; tracked for rewrite"
)


def test_conversation_engine_placeholder():
    assert False  # never runs
