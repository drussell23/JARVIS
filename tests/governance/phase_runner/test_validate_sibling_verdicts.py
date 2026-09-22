"""One candidate's early-return verdict must not end an op a sibling passes.

bt-2026-09-22-201845, op-01a0cb4f: candidate 2 failed ``infra`` (its test
imported Google Cloud with no credentials), candidate 3 PASSED. The infra
branch had already advanced the op's working context to POSTMORTEM and written
a FAILED ledger record; the loop then broke to GATE with that context and died
on ``Illegal phase transition: POSTMORTEM -> GATE`` -- a passing candidate
lost to its sibling. The same shape existed for the budget branch (CANCELLED
-> GATE) and the coverage-deficit branch, which also filed substitution goals
for work that was about to land.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.phase_runners import validate_runner as VR
from backend.core.ouroboros.governance.phase_runners.validate_runner import VALIDATERunner

from tests.governance.phase_runner.test_validate_runner_parity import (
    _FakeOrchestrator,
    _generation,
    _mk_validation,
    _orch,
    _validate_ctx,
)


@dataclass
class _PerCandidateOrchestrator(_FakeOrchestrator):
    """Each candidate gets its own verdict, like a real sibling pool."""

    verdicts: Dict[str, object] = field(default_factory=dict)

    async def _run_validation(self, ctx, cand, remaining_s):
        return self.verdicts[cand["candidate_id"]]


def _orch_with(tmp_path, **verdicts) -> _PerCandidateOrchestrator:
    base = _orch(tmp_path)
    return _PerCandidateOrchestrator(
        _stack=base._stack, _config=base._config, _generator=base._generator,
        verdicts={cid: v for cid, v in verdicts.items()},
    )


def _failed_ledger(orch) -> list:
    return [e for e in orch.ledger_records if str(e[1]).endswith("FAILED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("fc", ["infra", "budget", "no_covering_test"])
async def test_a_passing_sibling_wins_over_an_early_return_verdict(tmp_path, monkeypatch, fc):
    filed = []

    async def _no_substitution(ctx):
        filed.append(ctx.op_id)
        return "filed"

    monkeypatch.setattr(VR, "_substitute_for_coverage_deficit", _no_substitution)
    orch = _orch_with(tmp_path, c0=_mk_validation(False, fc), c1=_mk_validation(True))

    result = await VALIDATERunner(
        orch, None, generation=_generation(n_cands=2), generate_retries_remaining=3,
    ).run(
        _validate_ctx(tmp_path),
    )

    assert result.status == "ok", f"the passing sibling was lost: {result.reason}"
    assert result.next_phase is OperationPhase.GATE
    assert result.next_ctx.phase is not OperationPhase.POSTMORTEM
    assert not _failed_ledger(orch), "a FAILED record was written for an op that passed"
    assert not filed, "substitution goals were filed for work that is landing"


@pytest.mark.asyncio
@pytest.mark.parametrize("fc,phase,reason", [
    ("infra", OperationPhase.POSTMORTEM, "validation_infra_failure"),
    ("budget", OperationPhase.CANCELLED, "validation_budget_exhausted"),
])
async def test_with_no_passing_sibling_the_early_return_is_unchanged(tmp_path, fc, phase, reason):
    orch = _orch_with(tmp_path, c0=_mk_validation(False, "test"), c1=_mk_validation(False, fc))

    result = await VALIDATERunner(
        orch, None, generation=_generation(n_cands=2), generate_retries_remaining=3,
    ).run(
        _validate_ctx(tmp_path),
    )

    assert result.status == "fail" and result.reason == reason
    assert result.next_ctx.phase is phase
    assert len(_failed_ledger(orch)) == 1, "exactly one terminal record, as before"
    assert _failed_ledger(orch)[0][2]["reason"] == reason


@pytest.mark.asyncio
async def test_a_coverage_deficit_still_files_its_substitution_when_nothing_passes(tmp_path, monkeypatch):
    async def _sub(ctx):
        return "ov-dag-a+ov-dag-b"

    monkeypatch.setattr(VR, "_substitute_for_coverage_deficit", _sub)
    orch = _orch_with(tmp_path, c0=_mk_validation(False, "no_covering_test"))

    result = await VALIDATERunner(
        orch, None, generation=_generation(n_cands=1), generate_retries_remaining=3,
    ).run(
        _validate_ctx(tmp_path),
    )

    assert result.reason == "test_coverage_deficit"
    assert _failed_ledger(orch)[0][2]["substitution"] == "ov-dag-a+ov-dag-b"
