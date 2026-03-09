"""Tests for multi-repo codegen prompt building (schema 2c.1)."""
from __future__ import annotations
from pathlib import Path


def _make_cross_repo_ctx(tmp_path: Path):
    from backend.core.ouroboros.governance.op_context import OperationContext
    jarvis_file = tmp_path / "jarvis" / "backend" / "utils.py"
    prime_file = tmp_path / "prime" / "api" / "handler.py"
    jarvis_file.parent.mkdir(parents=True, exist_ok=True)
    prime_file.parent.mkdir(parents=True, exist_ok=True)
    jarvis_file.write_text("def hello(): pass\n")
    prime_file.write_text("def handle(): pass\n")

    return OperationContext.create(
        target_files=(str(jarvis_file), str(prime_file)),
        description="Add cross-repo feature",
        op_id="op-multi-001",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    )


def test_cross_repo_prompt_includes_schema_2c1(tmp_path):
    """Prompt for cross_repo ctx must reference schema_version 2c.1."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = _make_cross_repo_ctx(tmp_path)
    assert ctx.cross_repo is True

    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}
    prompt = _build_codegen_prompt(ctx, repo_roots=repo_roots)
    assert "2c.1" in prompt, "Cross-repo prompt must specify schema 2c.1"
    assert "patches" in prompt, "Cross-repo prompt must describe patches dict"


def test_cross_repo_prompt_groups_files_by_repo(tmp_path):
    """Prompt must label each file with its repo name."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = _make_cross_repo_ctx(tmp_path)
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}
    prompt = _build_codegen_prompt(ctx, repo_roots=repo_roots)
    assert "jarvis" in prompt
    assert "prime" in prompt


def test_single_repo_prompt_unchanged(tmp_path):
    """Single-repo ctx must still produce schema 2b.1 prompt (no regression)."""
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt

    f = tmp_path / "backend" / "utils.py"
    f.parent.mkdir(parents=True)
    f.write_text("def hello(): pass\n")

    ctx = OperationContext.create(
        target_files=(str(f),),
        description="Add utility",
        op_id="op-single-001",
    )
    assert ctx.cross_repo is False
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "2b.1" in prompt
    assert "2c.1" not in prompt
