"""The local lane can finally ask the 30B for a patch.

Measured before this existed: `_build_lean_codegen_prompt` — the LOCAL lane's
builder, the only one the 30B ever sees — carried ZERO diff-schema references,
so every local candidate came back full_content while the gate recorded
"schema requested: diff" 14/14. The harness was asking for a patch in a
language it never spoke to the model, and the resulting whole-file re-emissions
measured 80-98% null churn (median 0.91).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import providers as P
from backend.core.ouroboros.governance.op_context import OperationContext


@pytest.fixture(autouse=True)
def _diff_armed(monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    yield


def _ctx(files=("backend/api/sse_contract.py",)):
    return OperationContext.create(
        target_files=tuple(files), description="log before degrading",
        op_id="op-lean",
    )


ROOT = Path("/home/jarvis_svc/jarvis")


# --------------------------------------------------------------------------
# Phase 1 — the schema reaches the local lane
# --------------------------------------------------------------------------

def test_the_lean_builder_emits_the_diff_schema_when_negotiated():
    """THE regression: this builder had no diff branch at all."""
    prompt = P._build_lean_codegen_prompt(_ctx(), repo_root=ROOT, force_full_content=False)
    assert "2b.1-diff" in prompt
    assert "unified_diff" in prompt


def test_it_still_emits_full_content_when_forced():
    prompt = P._build_lean_codegen_prompt(_ctx(), repo_root=ROOT, force_full_content=True)
    assert "2b.1-diff" not in prompt


def test_multi_file_never_gets_the_diff_schema():
    ctx = _ctx(("backend/api/sse_contract.py", "backend/api/other.py"))
    prompt = P._build_lean_codegen_prompt(ctx, repo_root=ROOT, force_full_content=False)
    assert "2b.1-diff" not in prompt


# --------------------------------------------------------------------------
# One definition, two anchors
# --------------------------------------------------------------------------

def test_the_template_is_not_duplicated():
    """The instruction text exists once; the builders call it."""
    import inspect

    src = inspect.getsource(P)
    assert src.count("CRITICAL ANCHORING REQUIREMENT") == 1


def test_the_anchor_names_the_section_the_prompt_actually_has():
    """Naming the wrong section is worse than naming none: the model would copy
    context lines from a section that does not exist and every hunk would fail
    to place. The lean prompt carries a Target Region, not a Source Snapshot."""
    prompt = P._build_lean_codegen_prompt(_ctx(), repo_root=ROOT, force_full_content=False)
    assert "Target Region" in prompt
    assert "Source Snapshot" not in P.diff_schema_instruction(
        "sha", source_label='the "### Target Region" section above',
    )


def test_the_full_builder_keeps_its_own_anchor():
    assert "Source Snapshot" in P.diff_schema_instruction("sha")


def test_the_sha_is_echoed_into_the_schema():
    assert "abc123def456" in P.diff_schema_instruction("abc123def456")


# --------------------------------------------------------------------------
# Phase 3 — the rejection reaches the next attempt
# --------------------------------------------------------------------------

class _Ctx:
    op_id = "op-x"
    strategic_memory_prompt = ""


def test_a_rejected_hunk_is_handed_to_the_retry():
    ctx = _Ctx()
    P._arm_diff_context_realignment(ctx, "backend/api/x.py", "hunk 1: 'def foo' not found")
    assert "diff_context_realignment" in ctx.strategic_memory_prompt
    assert "hunk 1: 'def foo' not found" in ctx.strategic_memory_prompt


def test_the_retry_is_told_NOT_to_fall_back_to_full_content():
    """A whole-file rewrite is not an acceptable repair for a patch that did
    not apply — that is the drift this whole chain exists to prevent."""
    ctx = _Ctx()
    P._arm_diff_context_realignment(ctx, "x.py", "boom")
    assert "not an acceptable repair" in ctx.strategic_memory_prompt


def test_existing_retry_context_is_appended_to_never_clobbered():
    ctx = _Ctx()
    ctx.strategic_memory_prompt = "<lesson>prior correction</lesson>"
    P._arm_diff_context_realignment(ctx, "x.py", "boom")
    assert "prior correction" in ctx.strategic_memory_prompt
    assert "diff_context_realignment" in ctx.strategic_memory_prompt


def test_it_reuses_the_existing_retry_channel():
    """A second feedback path would be a second thing to keep in agreement
    with adaptive_system_prompt's <previous_failure_context> envelope."""
    import inspect

    src = inspect.getsource(P._arm_diff_context_realignment)
    assert "strategic_memory_prompt" in src


def test_a_frozen_context_is_not_an_error():
    class _Frozen:
        __slots__ = ()
        op_id = "op-f"

    P._arm_diff_context_realignment(_Frozen(), "x.py", "boom")


def test_it_never_raises_on_junk():
    P._arm_diff_context_realignment(None, "", "")
