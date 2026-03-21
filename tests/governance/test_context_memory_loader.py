"""Tests for ContextMemoryLoader — 3-tier OUROBOROS.md injection."""
import pathlib
import pytest
from backend.core.ouroboros.governance.context_memory_loader import ContextMemoryLoader


def test_loads_nothing_when_no_files_exist(tmp_path):
    """Returns empty string when no OUROBOROS.md files exist."""
    loader = ContextMemoryLoader(
        global_dir=tmp_path / "global",
        project_root=tmp_path / "project",
    )
    result = loader.load()
    assert result == ""


def test_loads_global_instructions(tmp_path):
    """~/.jarvis/OUROBOROS.md content is returned."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("# Global Rule\nNever use subprocess.\n")

    loader = ContextMemoryLoader(
        global_dir=global_dir,
        project_root=tmp_path / "project",
    )
    result = loader.load()
    assert "Never use subprocess." in result


def test_project_overrides_merge_with_global(tmp_path):
    """Both global and project OUROBOROS.md content appear in output."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("Global instruction.\n")

    project_root = tmp_path / "myproject"
    project_root.mkdir()
    (project_root / "OUROBOROS.md").write_text("Project instruction.\n")

    loader = ContextMemoryLoader(global_dir=global_dir, project_root=project_root)
    result = loader.load()
    assert "Global instruction." in result
    assert "Project instruction." in result


def test_local_override_included(tmp_path):
    """<repo>/.jarvis/OUROBOROS.md also included (all 3 levels merge)."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("Global.\n")

    project_root = tmp_path / "myproject"
    project_root.mkdir()
    (project_root / "OUROBOROS.md").write_text("Project.\n")

    local_dir = project_root / ".jarvis"
    local_dir.mkdir()
    (local_dir / "OUROBOROS.md").write_text("Local personal override.\n")

    loader = ContextMemoryLoader(global_dir=global_dir, project_root=project_root)
    result = loader.load()
    assert "Global." in result
    assert "Project." in result
    assert "Local personal override." in result


def test_load_is_idempotent(tmp_path):
    """Calling load() twice returns identical content."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("Stable content.\n")
    loader = ContextMemoryLoader(global_dir=global_dir, project_root=tmp_path)
    assert loader.load() == loader.load()


def test_with_human_instructions_sets_field():
    """OperationContext.with_human_instructions() sets human_instructions and changes hash."""
    from backend.core.ouroboros.governance.op_context import OperationContext
    ctx = OperationContext.create(target_files=("backend/foo.py",), description="test")
    assert ctx.human_instructions == ""
    ctx2 = ctx.with_human_instructions("Never use subprocess.")
    assert ctx2.human_instructions == "Never use subprocess."
    assert ctx2.context_hash != ctx.context_hash


def test_codegen_prompt_includes_human_instructions(tmp_path):
    """_build_codegen_prompt prepends human_instructions block when set."""
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = OperationContext.create(target_files=(), description="fix bug")
    ctx = ctx.with_human_instructions("Always write tests before code.")
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "Always write tests before code." in prompt
    # Human instructions must appear before the Task section
    assert prompt.index("Always write tests before code.") < prompt.index("## Task")
