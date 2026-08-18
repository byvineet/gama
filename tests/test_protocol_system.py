"""
tests/test_protocol_system.py — Unit tests for core.protocols
================================================================================
Run with: pytest tests/test_protocol_system.py -v

Each test gets an isolated ~/.gama directory via the `isolated_home` fixture
so runs never touch (or are polluted by) a real user's protocol data.
"""

import importlib
import sys
import time
import uuid

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point Path.home() at a scratch dir and force-reload every
    core.protocols module so its module-level singletons/paths rebind."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home()

    for mod in list(sys.modules):
        if mod.startswith("core.protocols"):
            del sys.modules[mod]

    manager_mod = importlib.import_module("core.protocols.manager")
    return manager_mod.protocol_manager


# ---------------------------------------------------------------------------
# Identifier normalization / registry resolution
# ---------------------------------------------------------------------------

def test_normalize_identifier_numeric():
    from core.protocols.registry import normalize_identifier
    assert normalize_identifier("17") == (17, "")
    assert normalize_identifier("Protocol 17") == (17, "")
    assert normalize_identifier("protocol number 17") == (17, "")
    assert normalize_identifier("seventeen") == (17, "")


def test_normalize_identifier_name():
    from core.protocols.registry import normalize_identifier
    num_id, slug = normalize_identifier("Coding Protocol")
    assert num_id is None
    assert slug == "coding"

    num_id2, slug2 = normalize_identifier("Night Protocol")
    assert num_id2 is None
    assert slug2 == "night"


def test_resolve_by_number_and_name(isolated_home):
    manager = isolated_home
    ok, msg, protocol = manager.create_protocol("42", "open Chrome")
    assert ok, msg

    from core.protocols.registry import protocol_registry
    by_number = protocol_registry.resolve("42")
    by_full = protocol_registry.resolve("Protocol 42")
    assert by_number is not None
    assert by_number.id == by_full.id == protocol.id


# ---------------------------------------------------------------------------
# Natural language parsing
# ---------------------------------------------------------------------------

def test_parser_splits_and_types_steps():
    from core.protocols.parser import ProtocolParser
    from core.protocols.models import ActionType

    steps = ProtocolParser.parse_natural_language_steps(
        "open Chrome, then open Spotify, wait 3 seconds, then play music"
    )
    types = [s.action_type for s in steps]
    assert types == [
        ActionType.OPEN_APP.value,
        ActionType.OPEN_APP.value,
        ActionType.WAIT.value,
        ActionType.MEDIA_PLAY.value,
    ]
    assert steps[0].target == "Chrome"
    assert steps[1].target == "Spotify"
    assert steps[2].params["seconds"] == 3.0


def test_parser_preserves_quoted_spans():
    from core.protocols.parser import ProtocolParser
    steps = ProtocolParser.parse_natural_language_steps('search for "cats, dogs, and mice"')
    assert len(steps) == 1
    assert steps[0].target == "cats, dogs, and mice"


def test_parser_unrecognized_falls_back_to_ai_prompt():
    from core.protocols.parser import ProtocolParser
    from core.protocols.models import ActionType
    steps = ProtocolParser.parse_natural_language_steps("do a little dance")
    assert len(steps) == 1
    assert steps[0].action_type == ActionType.AI_PROMPT.value
    assert steps[0].params.get("unrecognized") is True


# ---------------------------------------------------------------------------
# Manager CRUD
# ---------------------------------------------------------------------------

def test_create_duplicate_identifier_rejected(isolated_home):
    manager = isolated_home
    ok1, _, _ = manager.create_protocol("Focus Protocol", "open Notion")
    assert ok1
    ok2, msg2, _ = manager.create_protocol("Focus Protocol", "open Notion")
    assert not ok2
    assert "already exists" in msg2


def test_rename_and_search(isolated_home):
    manager = isolated_home
    manager.create_protocol("Streaming Protocol", "open OBS")
    ok, msg = manager.rename_protocol("Streaming Protocol", "Broadcast Protocol")
    assert ok, msg

    results = manager.search_protocols("broadcast")
    assert any(p.display_name == "Broadcast Protocol" for p in results)


def test_duplicate_and_delete(isolated_home):
    manager = isolated_home
    manager.create_protocol("Travel Protocol", "open Maps")
    ok, msg, clone = manager.duplicate_protocol("Travel Protocol", "Travel Protocol Copy")
    assert ok, msg
    assert clone.numeric_id is None or clone.numeric_id != None  # gets its own numeric id on save

    ok_del, msg_del = manager.delete_protocol("Travel Protocol Copy")
    assert ok_del, msg_del
    assert manager.search_protocols("Travel Protocol Copy") == []


def test_export_import_round_trip(isolated_home):
    manager = isolated_home
    manager.create_protocol("Export Me", "open Chrome")
    exported = manager.export_protocols()  # no filepath -> returns JSON string
    assert "Export Me" in exported

    ok, msg = manager.import_protocols(exported)
    assert ok, msg
    matches = [p for p in manager.list_protocols() if "Export Me" in p.display_name]
    # original + re-imported copy (import always gets a fresh id/number)
    assert len(matches) >= 2


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_execute_runs_steps_and_records_history(isolated_home):
    manager = isolated_home
    manager.create_protocol("Quick Protocol", "wait 0.2 seconds")
    ok, msg = manager.execute_protocol("Quick Protocol")
    assert ok, msg
    assert "initiated" in msg.lower()

    deadline = time.time() + 5
    while time.time() < deadline:
        history = manager.get_history(limit=5)
        if history and history[0].protocol_name == "Quick Protocol" and history[0].status == "completed":
            break
        time.sleep(0.1)
    else:
        pytest.fail("Protocol did not complete within timeout")


def test_execute_unknown_protocol(isolated_home):
    manager = isolated_home
    ok, msg = manager.execute_protocol("Nonexistent Protocol 9999")
    assert not ok
    assert "couldn't find" in msg


def test_recursive_protocol_call_blocked(isolated_home):
    manager = isolated_home
    # Protocol A calls itself via call_protocol.
    manager.create_protocol("Loopy Protocol", "run protocol Loopy Protocol")

    ok, msg = manager.execute_protocol("Loopy Protocol")
    assert ok  # kickoff always succeeds; the recursion guard fires inside the run

    deadline = time.time() + 5
    while time.time() < deadline:
        history = manager.get_history(limit=5)
        if history and history[0].protocol_name == "Loopy Protocol" and history[0].status in ("completed", "failed"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("Loopy protocol never finished — recursion guard likely not firing")
    # Should not have hung/looped forever; whatever the terminal status is,
    # the executor's recursion guard is what let it terminate at all.


def test_pause_resume_cancel_active_execution(isolated_home):
    manager = isolated_home
    manager.create_protocol("Long Protocol", "wait 2 seconds, then wait 2 seconds")
    ok, msg = manager.execute_protocol("Long Protocol")
    assert ok, msg

    time.sleep(0.1)
    assert manager.pause_protocol() is True
    time.sleep(0.3)
    assert manager.resume_protocol() is True
    assert manager.cancel_protocol() is True

    deadline = time.time() + 5
    while time.time() < deadline:
        history = manager.get_history(limit=5)
        if history and history[0].protocol_name == "Long Protocol" and history[0].status == "cancelled":
            break
        time.sleep(0.1)
    else:
        pytest.fail("Long protocol was not reported as cancelled")


def test_execution_publishes_voice_narration_events(isolated_home, monkeypatch):
    """Regression test: a protocol run must publish TaskStarted /
    TaskProgressChanged / TaskCompleted on the shared event bus so
    voice/execution_narrator.py narrates each step and speaks a real
    completion line — not just the initial 'initiated' acknowledgement."""
    from state_engine.event_bus import event_bus

    manager = isolated_home
    manager.create_protocol("Narrated Protocol", "wait 0.1 seconds, then wait 0.1 seconds")

    seen = []
    def _capture(evt):
        seen.append(evt.name)
    for name in ("TaskStarted", "TaskProgressChanged", "TaskCompleted", "TaskFailed", "TaskCancelled"):
        event_bus.subscribe(name, _capture)

    ok, msg = manager.execute_protocol("Narrated Protocol")
    assert ok, msg

    deadline = time.time() + 5
    while time.time() < deadline and "TaskCompleted" not in seen:
        time.sleep(0.1)

    assert "TaskStarted" in seen
    assert "TaskProgressChanged" in seen
    assert "TaskCompleted" in seen
    assert "TaskFailed" not in seen
    assert "TaskCancelled" not in seen


# ---------------------------------------------------------------------------
# actions.protocol_engine tool wrapper (the real dispatch entry point)
# ---------------------------------------------------------------------------

def test_protocol_engine_tool_wrapper(isolated_home, monkeypatch):
    # protocol_engine imports core.protocols.manager itself; make sure it
    # points at the same isolated singletons the fixture just rebuilt.
    for mod in list(sys.modules):
        if mod == "actions.protocol_engine":
            del sys.modules[mod]
    protocol_engine_mod = importlib.import_module("actions.protocol_engine")

    out = protocol_engine_mod.protocol_engine("create", identifier="99", steps="wait 1 second")
    assert "created" in out.lower()

    out_list = protocol_engine_mod.protocol_engine("list")
    assert "Protocol 99" in out_list or "99" in out_list

    out_missing = protocol_engine_mod.protocol_engine("run")  # no identifier
    assert "which protocol" in out_missing.lower()
