"""A successful micro-fix repair reaches GATE -- once.

bt-2026-09-22-201845 recorded the first two live micro-fix repairs:
``micro_fix_returned fixed=True`` then ``micro_fix_revalidated passed=True``.
Both ops then died ``Illegal phase transition: GATE -> GATE``: the success
branch advanced the op to GATE itself and broke out of the loop, and the
post-loop path -- the one owner of that transition, which runs source drift,
shadow, entropy and the read-only short-circuit first -- advanced it again.
The repairs were thrown away by the crash.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import interactive_repair as IR
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

REPAIRED = "x = 2  # repaired\n"


@dataclass
class _RepairResult:
    fixed: bool = True
    iterations_used: int = 1
    repaired_content: str = REPAIRED


class _FakeRepairLoop:
    def __init__(self, *a, **k):
        pass

    async def repair(self, **kwargs):
        return _RepairResult()


class _FakeSandbox:
    baseline_fidelity = "test"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def sandbox_root(self):
        return Path("/nonexistent-micro-fix-root")

    async def apply_full_content(self, content, path):
        return None


@dataclass
class _JudgingOrchestrator(_FakeOrchestrator):
    """Fails what the model wrote, passes what the micro-fix repaired."""

    judged: List[str] = None

    async def _run_validation(self, ctx, cand, remaining_s):
        self.judged.append(cand.get("full_content", ""))
        return _mk_validation(cand.get("full_content") == REPAIRED, "test")


@pytest.fixture
def repaired_world(monkeypatch):
    monkeypatch.setattr(VR, "RepairSandbox", _FakeSandbox)
    monkeypatch.setattr(IR, "InteractiveRepairLoop", _FakeRepairLoop)


@pytest.mark.asyncio
async def test_a_repaired_candidate_is_handed_to_gate_not_crashed(repaired_world, tmp_path):
    base = _orch(tmp_path, cfg_max_validate_retries=2)
    orch = _JudgingOrchestrator(
        _stack=base._stack, _config=base._config, _generator=base._generator, judged=[],
    )
    result = await VALIDATERunner(
        orch, None, generation=_generation(n_cands=1), generate_retries_remaining=3,
    ).run(_validate_ctx(tmp_path))

    assert REPAIRED in orch.judged, "the repair was never re-validated"
    assert result.status == "ok", f"the repaired op was lost: {result.reason}"
    assert result.next_phase is OperationPhase.GATE
    assert result.next_ctx.phase is OperationPhase.GATE
    assert result.artifacts["best_candidate"]["full_content"] == REPAIRED
