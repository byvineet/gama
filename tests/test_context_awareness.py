"""Legacy context-awareness tests (outdated API) — placeholder skip."""
import pytest

pytestmark = pytest.mark.skip(
    reason="Legacy test targets outdated context_engine API; tracked for rewrite"
)


def test_context_awareness_placeholder():
    assert False  # never runs
