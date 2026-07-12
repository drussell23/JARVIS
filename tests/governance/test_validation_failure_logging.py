"""Slice 8 §7: a failed ValidationResult must surface its failure_class +
short_summary + error in the log at WARNING — Run #18's security-class
rejection (BlockedPathError) was recoverable nowhere post-hoc."""
from __future__ import annotations

import asyncio
import logging

import pytest


def test_failed_validation_logs_summary(caplog, monkeypatch):
    from backend.core.ouroboros.governance import orchestrator as orch_mod

    orch = object.__new__(orch_mod.Orchestrator)  # no __init__ — seam test

    class _Ctx:
        op_id = "op-slice8-logpin-0000"

    failed = orch_mod.ValidationResult(
        passed=False,
        best_candidate=None,
        validation_duration_s=0.01,
        error="BlockedPathError: Path /tmp/x resolves outside repo root /r",
        failure_class="security",
        short_summary="BlockedPathError: Path /tmp/x ...",
        adapter_names_run=(),
    )

    async def _fake_core(self, ctx, candidate, remaining_s):
        return failed

    monkeypatch.setattr(
        orch_mod.Orchestrator, "_run_validation_core", _fake_core,
    )
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            orch_mod.Orchestrator._run_validation(orch, _Ctx(), {}, 10.0)
        )
    assert result.passed is False
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "[Validation] FAILED" in joined
    assert "fc=security" in joined
    assert "BlockedPathError" in joined
    assert "op-slice8-logp" in joined
