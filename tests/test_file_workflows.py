"""
tests/test_file_workflows.py — Tests for unified file workflows and adaptive canvas stages.
"""

import os
import shutil
import tempfile
from pathlib import Path
import pytest

from actions.file_controller import file_controller
from actions.display_stage import display_stage, show_code_on_display, show_workflow_on_display


@pytest.fixture
def temp_workspace():
    tmp = tempfile.mkdtemp(prefix="gama_test_fs_")
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def test_organize_folder(temp_workspace):
    # Create loose files of various types
    (temp_workspace / "report.pdf").write_text("dummy pdf", encoding="utf-8")
    (temp_workspace / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (temp_workspace / "script.py").write_text("print('hello')", encoding="utf-8")
    (temp_workspace / "data.csv").write_text("a,b,c\n1,2,3", encoding="utf-8")
    (temp_workspace / "archive.zip").write_bytes(b"PK\x03\x04")

    res = file_controller(action="organize", path=str(temp_workspace))
    assert "Organized 5 files" in res

    # Verify directory structure
    assert (temp_workspace / "Documents" / "report.pdf").exists()
    assert (temp_workspace / "Images" / "photo.png").exists()
    assert (temp_workspace / "Code_and_Scripts" / "script.py").exists()
    assert (temp_workspace / "Data_and_Sheets" / "data.csv").exists()
    assert (temp_workspace / "Archives" / "archive.zip").exists()


def test_batch_rename(temp_workspace):
    # Create test files
    for i in range(3):
        (temp_workspace / f"doc_{i}.txt").write_text(f"content {i}", encoding="utf-8")

    res = file_controller(action="batch_rename", path=str(temp_workspace), prefix="physics_note")
    assert "Renamed 3 files" in res or "Renamed 3 file" in res
    assert (temp_workspace / "physics_note_001.txt").exists()
    assert (temp_workspace / "physics_note_002.txt").exists()
    assert (temp_workspace / "physics_note_003.txt").exists()


def test_compress_and_extract(temp_workspace):
    subfolder = temp_workspace / "project"
    subfolder.mkdir()
    (subfolder / "main.py").write_text("print(1)", encoding="utf-8")
    (subfolder / "readme.md").write_text("# Readme", encoding="utf-8")

    zip_target = temp_workspace / "project.zip"
    res_comp = file_controller(action="compress", src=str(subfolder), dest=str(zip_target))
    assert "Compressed" in res_comp and zip_target.exists()

    extract_target = temp_workspace / "extracted"
    res_ext = file_controller(action="extract", src=str(zip_target), dest=str(extract_target))
    assert "Extracted" in res_ext
    assert (extract_target / "main.py").exists()
    assert (extract_target / "readme.md").exists()


def test_clean_empty(temp_workspace):
    empty_dir = temp_workspace / "empty_sub"
    empty_dir.mkdir()
    res = file_controller(action="clean_empty", path=str(temp_workspace))
    assert "Removed 1 empty subfolder" in res
    assert not empty_dir.exists()


def test_display_stage_code_and_workflow():
    res_code = display_stage(
        action="code",
        code="def hello(): return 'world'",
        language="python",
        title="hello.py",
        explanation="Simple greeting function",
    )
    assert "Nexus" in res_code or "Display" in res_code

    res_wf = display_stage(
        action="workflow",
        title="Organize Downloads",
        steps=["Documents: 5 files", "Images: 3 files"],
        summary="Done",
        stats={"total_files": 8},
    )
    assert "Nexus" in res_wf or "Display" in res_wf

    res_helper_code = show_code_on_display("x = 10", language="python")
    assert "Nexus" in res_helper_code or "Display" in res_helper_code

    res_helper_wf = show_workflow_on_display("Pipeline", ["Step 1", "Step 2"])
    assert "Nexus" in res_helper_wf or "Display" in res_helper_wf
