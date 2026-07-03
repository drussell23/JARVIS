"""Fleet write-intent investigation (bt-iso-1783042643, 2026-07-02) —
BACKGROUND-route batch preloaded-prompt exploration credit.

ROOT CAUSE: fleet-wide, all 52 bt-iso-* sessions resolved
terminal_reason_code=read_only_complete with files_changed=0 and ZERO
ChangeEngine.execute invocations ever. Tracing the ONE genuinely
mutation-scoped op per session (the A1 roadmap's 3-file docstring-
expansion goal, dag_weight=heavy files=3) showed operation_advisor
correctly derives read_only=False for it (see
test_read_only_advisor_bypass.py) — the bug is NOT there.

The real locus: the goal routes to BACKGROUND (cost-optimized, DW-only).
BACKGROUND/SPECULATIVE routes structurally suppress the Venom tool loop
(``should_skip_venom_for_route`` / ``compute_tool_loop_suppressed`` —
Slice 226/227) and are meant to satisfy the Iron Gate's exploration floor
via "preloaded-prompt credit" instead: the prompt builder embeds the
target files' real content directly, and the model is deemed to have
"seen" them without a live ``read_file`` call. That credit mechanism
(``GenerationResult.prompt_preloaded_files``) was wired for the RT/lean
prompt path (``_generate_realtime`` -> ``_build_lean_codegen_prompt``) but
NEVER for the batch dispatch path that BACKGROUND route actually uses
(``submit_batch`` -> ``_build_codegen_prompt`` -> ``poll_and_retrieve``).
Result: every BACKGROUND-routed non-trivial candidate reports
``_op_explore_credit = 0 explore_count + 0 preloaded_credit`` at
orchestrator.py's Iron Gate (legacy path,
``JARVIS_EXPLORATION_LEDGER_ENABLED=false``), fails
``exploration_insufficient`` identically on every retry (same 0/2, same
hash), trips ForwardProgress's stuck-hash detector, and the op terminates
``state=failed``/``state=blocked`` before ever reaching APPLY /
ChangeEngine.execute.

Fix: ``_build_codegen_prompt`` gained a ``preloaded_out`` parameter
(mirroring ``_build_lean_codegen_prompt``'s existing contract) that
records every target file whose real content it actually embedded.
``submit_batch`` threads it onto the new ``PendingBatch.prompt_
preloaded_files`` field; ``poll_and_retrieve`` carries it onto the
retrieved ``GenerationResult`` — the same ``dataclasses.replace(...,
prompt_preloaded_files=...)`` pattern the RT path already uses. This does
NOT bypass the Iron Gate, SemanticGuardian, or risk-tier: the credit is
only ever granted for files whose content genuinely landed in the
prompt the model received (same schema field the RT path already used
to earn the SAME credit legitimately) — a write-scoped op still has to
pass VALIDATE, SemanticGuardian, and (for non-SAFE_AUTO risk tiers)
human/async approval before APPLY.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordProvider,
    PendingBatch,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)


def _ctx(tmp_path, target_files):
    return OperationContext.create(
        target_files=tuple(target_files),
        description=(
            "Task: expand each docstring into a fuller description. "
            "Safe docstring-only multi-file edit; zero behavioral change."
        ),
        provider_route="background",
    )


@pytest.mark.asyncio
async def test_submit_batch_populates_prompt_preloaded_files(tmp_path, monkeypatch):
    """RED (pre-fix): ``PendingBatch`` had no ``prompt_preloaded_files``
    field and ``submit_batch`` never collected it -- this attribute access
    raised/returned the dataclass default ``()`` regardless of what was
    actually embedded in the prompt. GREEN (post-fix): the two target
    files whose real content was embedded in the submitted prompt are
    recorded, exactly mirroring the RT/lean path's existing credit
    contract."""
    (tmp_path / "hypothesis_envelope_factory.py").write_text(
        '"""Factory module."""\n\ndef build():\n    return {}\n'
    )
    (tmp_path / "context_memory_loader.py").write_text(
        '"""Loader module."""\n\ndef load():\n    return []\n'
    )
    provider = DoublewordProvider(api_key="test-key", repo_root=tmp_path)

    async def _fake_upload_file(jsonl_content, *, op_id="dw-batch-upload"):
        return "file-abc123"

    async def _fake_create_batch(input_file_id, *, op_id="dw-batch-create", _s181_attempt=0):
        return "batch-abc123"

    monkeypatch.setattr(provider, "_upload_file", _fake_upload_file)
    monkeypatch.setattr(provider, "_create_batch", _fake_create_batch)

    ctx = _ctx(tmp_path, [
        "hypothesis_envelope_factory.py",
        "context_memory_loader.py",
    ])
    pending = await provider.submit_batch(ctx)

    assert pending is not None
    assert "def build" in pending.prompt
    assert "def load" in pending.prompt
    assert pending.prompt_preloaded_files == (
        "hypothesis_envelope_factory.py",
        "context_memory_loader.py",
    )


@pytest.mark.asyncio
async def test_poll_and_retrieve_carries_preloaded_credit_into_result(tmp_path, monkeypatch):
    """RED (pre-fix): even if ``PendingBatch`` somehow carried preloaded
    file names, ``poll_and_retrieve`` never copied them onto the returned
    ``GenerationResult`` -- the orchestrator's Iron Gate reads
    ``generation.prompt_preloaded_files`` (orchestrator.py ~6143), so a
    batch-won candidate always reported 0 preloaded credit regardless.
    GREEN (post-fix): the retrieved result carries the submit-time
    preloaded file list through, exactly like the RT path's
    ``dataclasses.replace(result, prompt_preloaded_files=...)``."""
    provider = DoublewordProvider(api_key="test-key", repo_root=tmp_path)

    pending = PendingBatch(
        op_id="op-test",
        batch_id="batch-abc123",
        file_id="file-abc123",
        prompt="<prompt>",
        submitted_at=0.0,
        prompt_preloaded_files=("hypothesis_envelope_factory.py", "context_memory_loader.py"),
    )

    async def _fake_await_batch_result(batch_id, *, op_id="dw-batch-await"):
        return "outfile-abc123"

    async def _fake_retrieve_result(output_file_id, operation_id):
        return ("{}", None)

    _fixed_result = GenerationResult(
        candidates=(), provider_name="doubleword", generation_duration_s=0.1,
    )

    async def _fake_parse_with_heal(**kwargs):
        return _fixed_result

    monkeypatch.setattr(provider, "_await_batch_result", _fake_await_batch_result)
    monkeypatch.setattr(provider, "_retrieve_result", _fake_retrieve_result)
    monkeypatch.setattr(provider, "_parse_with_heal", _fake_parse_with_heal)

    ctx = _ctx(tmp_path, [])
    result = await provider.poll_and_retrieve(pending, ctx)

    assert result is not None
    assert result.prompt_preloaded_files == (
        "hypothesis_envelope_factory.py",
        "context_memory_loader.py",
    )


@pytest.mark.asyncio
async def test_poll_and_retrieve_no_preloaded_files_is_byte_identical(tmp_path, monkeypatch):
    """A ``PendingBatch`` with no preloaded files (e.g. an RT-style op, or
    every pre-fix batch) must NOT gain spurious credit -- the ``if
    pending.prompt_preloaded_files:`` guard means the result's field stays
    at its dataclass default ``()``, matching legacy behavior exactly."""
    provider = DoublewordProvider(api_key="test-key", repo_root=tmp_path)

    pending = PendingBatch(
        op_id="op-test",
        batch_id="batch-abc123",
        file_id="file-abc123",
        prompt="<prompt>",
        submitted_at=0.0,
    )

    async def _fake_await_batch_result(batch_id, *, op_id="dw-batch-await"):
        return "outfile-abc123"

    async def _fake_retrieve_result(output_file_id, operation_id):
        return ("{}", None)

    _fixed_result = GenerationResult(
        candidates=(), provider_name="doubleword", generation_duration_s=0.1,
    )

    async def _fake_parse_with_heal(**kwargs):
        return _fixed_result

    monkeypatch.setattr(provider, "_await_batch_result", _fake_await_batch_result)
    monkeypatch.setattr(provider, "_retrieve_result", _fake_retrieve_result)
    monkeypatch.setattr(provider, "_parse_with_heal", _fake_parse_with_heal)

    ctx = _ctx(tmp_path, [])
    result = await provider.poll_and_retrieve(pending, ctx)

    assert result is not None
    assert result.prompt_preloaded_files == ()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
