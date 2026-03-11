# tests/governance/self_dev/test_prompt_enrichment.py
"""Tests for Phase 2B prompt enrichment — file context, path safety, truncation."""
import hashlib
import json
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.providers import (
    _build_codegen_prompt,
    _find_context_files,
    _read_with_truncation,
    _safe_context_path,
)
from backend.core.ouroboros.governance.test_runner import BlockedPathError

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ctx(target_files, description="improve the code", repo_root=None):
    root = repo_root or REPO_ROOT
    return OperationContext.create(
        target_files=tuple(
            str(Path(f).relative_to(root)) if Path(f).is_absolute() else f
            for f in target_files
        ),
        description=description,
    )


# ── _safe_context_path ────────────────────────────────────────────────────

def test_safe_context_path_allows_valid_repo_file(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("x = 1")
    result = _safe_context_path(tmp_path, f)
    assert result == f.resolve()


def test_safe_context_path_rejects_file_outside_repo(tmp_path):
    outside = Path("/etc/passwd")
    with pytest.raises(BlockedPathError):
        _safe_context_path(tmp_path, outside)


def test_safe_context_path_rejects_symlink(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("x = 1")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    with pytest.raises(BlockedPathError):
        _safe_context_path(tmp_path, link)


# ── _read_with_truncation ─────────────────────────────────────────────────

def test_read_with_truncation_short_file_full(tmp_path):
    f = tmp_path / "short.py"
    content = "x = 1\n" * 10
    f.write_text(content)
    result = _read_with_truncation(f, max_chars=6000)
    assert result == content
    assert "TRUNCATED" not in result


def test_read_with_truncation_large_file_has_marker(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("# head\n" + "x = 1\n" * 2000)
    result = _read_with_truncation(f, max_chars=6000)
    assert "TRUNCATED" in result
    assert "# head" in result   # head preserved
    assert len(result) < len("# head\n" + "x = 1\n" * 2000)


# ── _build_codegen_prompt ─────────────────────────────────────────────────

def test_prompt_includes_file_content(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("def hello():\n    return 42\n")
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "def hello():" in prompt
    assert "return 42" in prompt


def test_prompt_includes_sha256_header(tmp_path):
    target = tmp_path / "mymod.py"
    content = "def hello():\n    return 42\n"
    target.write_text(content)
    expected_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert expected_hash in prompt


def test_prompt_includes_schema_version_2b1(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert '"schema_version"' in prompt
    assert "2b.1" in prompt


def test_prompt_includes_candidate_id_field(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "candidate_id" in prompt
    # Single-file tasks use unified_diff schema (2b.1-diff); multi-file tasks use full_content
    assert ("unified_diff" in prompt or "full_content" in prompt)
    assert "file_path" in prompt
    assert "rationale" in prompt


def test_prompt_truncates_large_file(tmp_path):
    target = tmp_path / "big.py"
    target.write_text("# TOP\n" + "x = 1\n" * 2000 + "# BOTTOM\n")
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "TRUNCATED" in prompt
    assert "# TOP" in prompt


def test_prompt_includes_op_id(tmp_path):
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert ctx.op_id in prompt


# ── _find_context_files ───────────────────────────────────────────────────

def test_find_context_files_cap_import_files(tmp_path):
    for i in range(10):
        (tmp_path / f"mod{i}.py").write_text(f"x{i} = {i}")
    target = tmp_path / "main.py"
    target.write_text("\n".join(f"from mod{i} import x{i}" for i in range(10)))
    import_files, test_files = _find_context_files(target, tmp_path)
    assert len(import_files) <= 5


def test_find_context_files_cap_test_files(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for i in range(5):
        (tests_dir / f"test_mod{i}.py").write_text("import main\n")
    target = tmp_path / "main.py"
    target.write_text("x = 1\n")
    _, test_files = _find_context_files(target, tmp_path)
    assert len(test_files) <= 2


# ── force_full_content (7B model schema selection) ─────────────────────────


def test_force_full_content_uses_full_schema_for_single_file(tmp_path):
    """When force_full_content=True, single-file tasks get 2b.1 (full_content)
    instead of 2b.1-diff (unified diff).  This is the structural fix for 7B
    models that hallucinate diff context lines from training data."""
    target = tmp_path / "mymod.py"
    target.write_text("x = 1\n")
    ctx = _ctx([str(target.relative_to(tmp_path))], repo_root=tmp_path)

    # Default (force_full_content=False) → 2b.1-diff for single-file
    prompt_diff = _build_codegen_prompt(ctx, repo_root=tmp_path, force_full_content=False)
    assert "2b.1-diff" in prompt_diff
    assert "unified_diff" in prompt_diff

    # force_full_content=True → 2b.1 (full_content) for single-file
    prompt_full = _build_codegen_prompt(ctx, repo_root=tmp_path, force_full_content=True)
    assert "2b.1-diff" not in prompt_full
    assert "unified_diff" not in prompt_full
    assert "full_content" in prompt_full
    assert '"2b.1"' in prompt_full or "'2b.1'" in prompt_full


def test_force_full_content_no_effect_on_multi_file(tmp_path):
    """force_full_content has no effect on multi-file tasks — they already
    use full_content schema."""
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.py"
    f1.write_text("x = 1\n")
    f2.write_text("y = 2\n")
    ctx = _ctx(
        [str(f1.relative_to(tmp_path)), str(f2.relative_to(tmp_path))],
        repo_root=tmp_path,
    )

    prompt_default = _build_codegen_prompt(ctx, repo_root=tmp_path)
    prompt_forced = _build_codegen_prompt(ctx, repo_root=tmp_path, force_full_content=True)

    # Both should use 2b.1 (full_content) — no unified_diff
    for p in (prompt_default, prompt_forced):
        assert "full_content" in p
        assert "unified_diff" not in p
