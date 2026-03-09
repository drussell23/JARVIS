"""Tests for multi-repo codegen prompt building (schema 2c.1)."""
from __future__ import annotations

import json
import pytest
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


# ---------------------------------------------------------------------------
# Task 5: _parse_generation_response() schema 2c.1 tests
# ---------------------------------------------------------------------------

import json


def _valid_2c1_response(repos=("jarvis", "prime")):
    patches = {
        repo: [
            {
                "file_path": "api.py",
                "full_content": f"def api(): return '{repo}'\n",
                "op": "modify",
            }
        ]
        for repo in repos
    }
    return json.dumps({
        "schema_version": "2c.1",
        "candidates": [
            {
                "candidate_id": "c1",
                "patches": patches,
                "rationale": "Fixed cross-repo API",
            }
        ],
        "provider_metadata": {"model_id": "test-model", "reasoning_summary": "ok"},
    })


def _single_ctx(tmp_path):
    from backend.core.ouroboros.governance.op_context import OperationContext
    f = tmp_path / "utils.py"
    f.write_text("def hello(): pass\n")
    return OperationContext.create(
        target_files=(str(f),),
        description="Add util",
        op_id="op-parse-001",
    )


def _multi_ctx(tmp_path, repos=("jarvis", "prime")):
    from backend.core.ouroboros.governance.op_context import OperationContext
    files = []
    for repo in repos:
        f = tmp_path / repo / "api.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("def api(): pass\n")
        files.append(str(f))
    return OperationContext.create(
        target_files=tuple(files),
        description="Cross-repo fix",
        op_id="op-parse-multi-001",
        repo_scope=repos,
        primary_repo=repos[0],
    )


def test_parse_valid_2c1_response(tmp_path):
    """Valid 2c.1 response must produce GenerationResult with RepoPatch candidates."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response
    from backend.core.ouroboros.governance.saga.saga_types import RepoPatch

    ctx = _multi_ctx(tmp_path)
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}
    raw = _valid_2c1_response()
    result = _parse_generation_response(
        raw, "gcp-jprime", 0.5, ctx, "hash-001", "api.py", repo_roots=repo_roots
    )

    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert "patches" in cand
    assert "jarvis" in cand["patches"]
    assert "prime" in cand["patches"]
    assert isinstance(cand["patches"]["jarvis"], RepoPatch)
    assert isinstance(cand["patches"]["prime"], RepoPatch)


def test_parse_2c1_file_content_in_new_content(tmp_path):
    """RepoPatch.new_content must contain the file bytes from the 2c.1 response."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    ctx = _multi_ctx(tmp_path)
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}
    raw = _valid_2c1_response()
    result = _parse_generation_response(
        raw, "gcp-jprime", 0.5, ctx, "hash-001", "api.py", repo_roots=repo_roots
    )

    jarvis_patch = result.candidates[0]["patches"]["jarvis"]
    contents = dict(jarvis_patch.new_content)
    assert "api.py" in contents
    assert b"return 'jarvis'" in contents["api.py"]


def test_parse_2c1_rejects_missing_patches(tmp_path):
    """2c.1 candidate missing patches dict must raise RuntimeError."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    ctx = _multi_ctx(tmp_path)
    repo_roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}
    bad = json.dumps({
        "schema_version": "2c.1",
        "candidates": [{"candidate_id": "c1", "rationale": "x"}],
        "provider_metadata": {"model_id": "m", "reasoning_summary": "s"},
    })
    with pytest.raises(RuntimeError):
        _parse_generation_response(bad, "gcp-jprime", 0.5, ctx, "h", "f", repo_roots=repo_roots)


def test_2b1_still_parses_after_2c1_added(tmp_path):
    """Existing 2b.1 single-repo responses must still parse correctly after this change."""
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    f = tmp_path / "utils.py"
    f.write_text("def hello(): pass\n")
    ctx = _single_ctx(tmp_path)
    raw = json.dumps({
        "schema_version": "2b.1",
        "candidates": [
            {"candidate_id": "c1", "file_path": "utils.py", "full_content": "def hello(): return 1\n", "rationale": "test"}
        ],
        "provider_metadata": {"model_id": "m", "reasoning_summary": "s"},
    })
    result = _parse_generation_response(raw, "gcp-jprime", 0.5, ctx, "h", "utils.py")
    assert len(result.candidates) == 1
    assert "file_path" in result.candidates[0]
