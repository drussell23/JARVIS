# tests/governance/test_correction_writer.py
import pytest
from datetime import datetime, timezone
from pathlib import Path
from backend.core.ouroboros.governance.correction_writer import write_correction


def test_creates_ouroboros_md_if_missing(tmp_path):
    write_correction(
        project_root=tmp_path,
        op_id="op-001",
        reason="Don't use subprocess.run in async context",
        timestamp=datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    md = tmp_path / "OUROBOROS.md"
    assert md.exists()
    content = md.read_text()
    assert "## Auto-Learned Corrections" in content
    assert "op:op-001" in content
    assert "Don't use subprocess.run" in content


def test_appends_to_existing_section(tmp_path):
    md = tmp_path / "OUROBOROS.md"
    md.write_text("# Project Config\n\n## Auto-Learned Corrections\n- old correction\n")
    write_correction(
        project_root=tmp_path,
        op_id="op-002",
        reason="Use pathlib not os.path",
        timestamp=datetime(2026, 3, 20, 13, 0, 0, tzinfo=timezone.utc),
    )
    content = md.read_text()
    assert "old correction" in content
    assert "op:op-002" in content
    assert "Use pathlib not os.path" in content


def test_creates_section_in_existing_file_without_it(tmp_path):
    md = tmp_path / "OUROBOROS.md"
    md.write_text("# Project Config\n\nSome project notes.\n")
    write_correction(
        project_root=tmp_path,
        op_id="op-003",
        reason="new lesson",
        timestamp=datetime(2026, 3, 20, 14, 0, 0, tzinfo=timezone.utc),
    )
    content = md.read_text()
    assert "## Auto-Learned Corrections" in content
    assert "op:op-003" in content


def test_empty_reason_is_skipped(tmp_path):
    write_correction(project_root=tmp_path, op_id="op-004", reason="  ", timestamp=datetime.now(timezone.utc))
    md = tmp_path / "OUROBOROS.md"
    assert not md.exists() or "op:op-004" not in md.read_text()


def test_write_error_does_not_raise(tmp_path):
    """IO failures must be silently swallowed — never crash the approval path."""
    # Pass a file path as project_root — write will fail gracefully
    fake_root = tmp_path / "nonexistent_dir" / "deep"
    write_correction(project_root=fake_root, op_id="op-005", reason="test", timestamp=datetime.now(timezone.utc))
    # No exception raised
