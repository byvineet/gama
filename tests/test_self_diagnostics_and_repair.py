"""
tests/test_self_diagnostics_and_repair.py — Unit tests for self-diagnostics and self-repair
"""

import logging
import pytest
import tempfile
import time
from pathlib import Path

from utils.logger import get_logger, get_recent_errors, get_log_tail
from actions.self_diagnostics import self_diagnostics, record_soft_error
from actions.self_repair import (
    self_repair,
    read_code,
    edit_code,
    revert_edit,
    verify_syntax,
    _validate_safe_path,
    SafetyViolationError,
)


def test_logger_error_ring_buffer():
    log = get_logger("test_diagnostics_logger")
    unique_msg = f"Test diagnostic error message {time.time()}"
    log.error(unique_msg)
    
    errors = get_recent_errors(limit=10, min_level="ERROR")
    assert any(unique_msg in e.get("message", "") for e in errors)


def test_self_diagnostics_explain_last_error():
    log = get_logger("test_diagnostics_explain")
    test_msg = f"ConnectionError: getaddrinfo failed on api.telegram.org {time.time()}"
    log.error(test_msg)

    explanation = self_diagnostics(action="explain_last_error")
    assert "error occurred in module" in explanation
    assert "Plain-language diagnosis:" in explanation or "Details:" in explanation


def test_self_diagnostics_recent_errors_and_logs():
    res = self_diagnostics(action="recent_errors", limit=5)
    assert isinstance(res, str)

    logs = self_diagnostics(action="read_logs", lines=10)
    assert isinstance(logs, str)


def test_self_repair_path_safety():
    # Paths outside codebase must raise SafetyViolationError
    with pytest.raises(SafetyViolationError):
        _validate_safe_path("C:/Windows/System32/cmd.exe")

    with pytest.raises(SafetyViolationError):
        _validate_safe_path("../../outside_file.txt")

    with pytest.raises(SafetyViolationError):
        _validate_safe_path("config/api_keys.json")

    # Valid internal path should pass
    safe_path = _validate_safe_path("actions/telegram_sender.py")
    assert safe_path.exists()


def test_self_repair_read_and_edit_cycle(tmp_path):
    # Test read_code on an existing file
    read_res = read_code("actions/self_repair.py", start_line=1, end_line=10)
    assert "actions/self_repair.py" in read_res
    assert "1 |" in read_res

    # Create a dummy python test file in the codebase under storage/ or actions/
    base_dir = Path(__file__).resolve().parent.parent
    test_file = base_dir / "storage" / "temp_self_repair_test.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def hello():\n    return 'initial'\n", encoding="utf-8")

    try:
        rel_path = "storage/temp_self_repair_test.py"

        # 1. Edit code successfully
        edit_res = edit_code(
            path=rel_path,
            target_content="return 'initial'",
            replacement_content="return 'updated'",
        )
        assert "Successfully updated" in edit_res
        assert "Syntax check passed" in edit_res
        assert test_file.read_text(encoding="utf-8") == "def hello():\n    return 'updated'\n"

        # 2. Syntax validation rejection on invalid python
        bad_edit = edit_code(
            path=rel_path,
            target_content="return 'updated'",
            replacement_content="return (broken syntax def",
        )
        assert "Code edit rejected due to syntax error" in bad_edit
        # File should remain untouched
        assert test_file.read_text(encoding="utf-8") == "def hello():\n    return 'updated'\n"

        # 3. Revert edit
        rev_res = revert_edit(rel_path)
        assert "Reverted" in rev_res
        assert test_file.read_text(encoding="utf-8") == "def hello():\n    return 'initial'\n"

        # 4. Verify syntax
        syntax_res = verify_syntax(rel_path)
        assert "Syntax check passed" in syntax_res
    finally:
        test_file.unlink(missing_ok=True)


def test_self_repair_dispatch_router():
    res = self_repair(action="read_code", path="actions/self_repair.py", start_line=1, end_line=5)
    assert "actions/self_repair.py" in res
