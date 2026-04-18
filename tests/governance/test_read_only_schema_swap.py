"""Regression spine — Option α weaponized read-only prompt schema.

Session 4 (bt-2026-04-18-034713) proved every structural guarantee fired
(read-only stamp + Advisor bypass + Nervous System Reflex + Option A tool
loop on BG), but Claude still produced ``tool_rounds=0`` because the
prompt told it to emit a code-generation JSON schema. A read-only op
being asked for ``full_content`` is semantically incoherent — the
orchestrator will refuse APPLY regardless.

The Option α fix: when ``ctx.is_read_only=True`` the code-gen schema is
replaced entirely by a CRITICAL_SYSTEM_DIRECTIVE that forbids code
generation and mandates ``dispatch_subagent``. This takes precedence
over every other schema branch (cross-repo, execution-graph, diff,
single-file, BG strict, default) because the read-only contract
overrides them all.

These tests pin the prompt-shape invariants:

1. Read-only prompt **contains** ``<CRITICAL_SYSTEM_DIRECTIVE>`` and
   mandates ``dispatch_subagent``.
2. Read-only prompt **does not contain** the code-gen schema markers
   (``schema_version``, ``full_content``, ``unified_diff``,
   ``candidates``, ``candidate_id``, or the multi-file contract
   header).
3. Normal ops still get the code-gen schema unchanged — this is a
   scoped override, not a blanket change.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.providers import _build_codegen_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Tuple[Path, Tuple[str, ...]]:
    """Minimal repo with a single target file for prompt-shape tests."""
    target = tmp_path / "sample.py"
    target.write_text("def hello():\n    return 'world'\n")
    return tmp_path, ("sample.py",)


def _make_ctx(
    target_files: Tuple[str, ...],
    *,
    is_read_only: bool = False,
    description: str = "test op",
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        is_read_only=is_read_only,
    )


# ---------------------------------------------------------------------------
# Read-only prompt shape
# ---------------------------------------------------------------------------


def test_read_only_prompt_contains_critical_system_directive(
    tiny_repo: Tuple[Path, Tuple[str, ...]],
) -> None:
    repo_root, target_files = tiny_repo
    ctx = _make_ctx(target_files, is_read_only=True)

    prompt = _build_codegen_prompt(
        ctx,
        repo_root=repo_root,
        tools_enabled=True,
        provider_route="background",
    )

    assert "<CRITICAL_SYSTEM_DIRECTIVE>" in prompt
    assert "</CRITICAL_SYSTEM_DIRECTIVE>" in prompt
    assert "mathematically locked into READ-ONLY mode" in prompt
    assert "forbidden from generating code" in prompt
    assert "dispatch_subagent" in prompt
    assert "subagent_type=explore" in prompt


def test_read_only_prompt_omits_codegen_schema_markers(
    tiny_repo: Tuple[Path, Tuple[str, ...]],
) -> None:
    """No candidates[]/full_content/unified_diff schema should leak
    through — they directly contradict the read-only contract."""
    repo_root, target_files = tiny_repo
    ctx = _make_ctx(target_files, is_read_only=True)

    prompt = _build_codegen_prompt(
        ctx,
        repo_root=repo_root,
        tools_enabled=True,
        provider_route="background",
    )

    # The code-gen schema emits JSON fragments with these exact markers —
    # if any of them appear in a read-only prompt, the weaponized swap
    # is leaking code-gen guidance.
    forbidden_markers = (
        '"full_content"',
        '"unified_diff"',
        '"candidates"',
        '"candidate_id"',
        '"file_path":',
    )
    for marker in forbidden_markers:
        assert marker not in prompt, (
            f"Read-only prompt must not contain code-gen schema "
            f"marker {marker!r} — the CRITICAL_SYSTEM_DIRECTIVE is "
            f"supposed to replace it entirely"
        )


def test_read_only_prompt_omits_multifile_contract_block(
    tmp_path: Path,
) -> None:
    """When is_read_only=True AND target_files >= 2, the multi-file
    contract block must be suppressed — it tells the model to emit
    ``files: [...]`` which is code-gen shape."""
    a = tmp_path / "a.py"
    a.write_text("a = 1\n")
    b = tmp_path / "b.py"
    b.write_text("b = 2\n")
    ctx = _make_ctx(("a.py", "b.py"), is_read_only=True)

    prompt = _build_codegen_prompt(
        ctx,
        repo_root=tmp_path,
        tools_enabled=True,
        provider_route="standard",
    )

    # The multi-file contract block is distinctive — its header text
    # stays stable across versions.
    assert "Multi-file" not in prompt or "CRITICAL_SYSTEM_DIRECTIVE" in prompt
    # Stricter: the files-array JSON stub must not appear.
    assert '"files"' not in prompt
    assert '"rationale"' not in prompt


# ---------------------------------------------------------------------------
# Normal (mutating) prompt — baseline preserved
# ---------------------------------------------------------------------------


def test_mutating_prompt_still_gets_codegen_schema(
    tiny_repo: Tuple[Path, Tuple[str, ...]],
) -> None:
    """The scoped override must not regress mutating ops — they must
    still receive the code-gen schema exactly as before."""
    repo_root, target_files = tiny_repo
    ctx = _make_ctx(target_files, is_read_only=False)

    prompt = _build_codegen_prompt(
        ctx,
        repo_root=repo_root,
        tools_enabled=True,
        provider_route="standard",
    )

    assert "<CRITICAL_SYSTEM_DIRECTIVE>" not in prompt
    # Default schema is full_content — should be present
    assert "Output Schema" in prompt
    assert '"full_content"' in prompt
    assert '"candidates"' in prompt


def test_mutating_prompt_on_bg_still_gets_bg_schema(
    tiny_repo: Tuple[Path, Tuple[str, ...]],
) -> None:
    """BACKGROUND route mutating ops still get the BG-strict schema."""
    repo_root, target_files = tiny_repo
    ctx = _make_ctx(target_files, is_read_only=False)

    prompt = _build_codegen_prompt(
        ctx,
        repo_root=repo_root,
        tools_enabled=True,
        provider_route="background",
    )

    assert "<CRITICAL_SYSTEM_DIRECTIVE>" not in prompt
    assert "BACKGROUND route — strict" in prompt


# ---------------------------------------------------------------------------
# Tool section reaches read-only prompt (the whole point of the rewrite)
# ---------------------------------------------------------------------------


def test_read_only_prompt_contains_dispatch_subagent_in_tool_manifest(
    tiny_repo: Tuple[Path, Tuple[str, ...]],
) -> None:
    """The CRITICAL_SYSTEM_DIRECTIVE tells Claude to call
    dispatch_subagent — the tool manifest must actually include that
    tool so the call is reachable."""
    repo_root, target_files = tiny_repo
    ctx = _make_ctx(target_files, is_read_only=True)

    prompt = _build_codegen_prompt(
        ctx,
        repo_root=repo_root,
        tools_enabled=True,
        provider_route="background",
    )

    # Dispatch_subagent manifest line is the structural anchor.
    assert "dispatch_subagent(subagent_type=" in prompt
