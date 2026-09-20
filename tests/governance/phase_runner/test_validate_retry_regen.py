"""VALIDATE_RETRY must regenerate, not re-validate a frozen pool.

Root (``bt-2026-09-20-005641``, ``op-01a0bc61-852a``): the ladder built a
retry-with-feedback prompt from the accumulated test failures, threaded it
onto the VALIDATE_RETRY ctx as ``strategic_memory_prompt`` -- and then the
next iteration re-validated ``generation.candidates``, the SAME pool GENERATE
froze. Nothing sent that prompt to a model. Live proof: three iterations,
byte-identical failures each time (``NameError: name 'monitor_cpu' is not
defined`` + two more), ~41s apiece, and both retries spent before L2 -- the
one rung that does regenerate -- got its single BACKGROUND-route dispatch.

The regression this file exists to catch is *silence*: a ladder that reports
"3 retries" while learning nothing looks healthy in every log line it emits.
So the load-bearing test asserts on the candidate ids VALIDATE actually saw,
not on a call count a future refactor could satisfy without regenerating.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.phase_runners.validate_runner import (
    VALIDATERunner,
    _RETRY_REGEN_MIN_BUDGET_S,
    _regenerate_for_retry,
    _retry_regen_enabled,
)

from tests.governance.phase_runner.test_validate_runner_parity import (
    _FakeGeneration,
    _FakeOrchestrator,
    _generation,
    _mk_validation,
    _orch,
    _validate_ctx,
)


# A budget comfortably above the regeneration floor, so these tests exercise
# the regeneration path rather than the budget guard. Left as the config
# fallback (``pipeline_deadline`` unset) so the ladder and the regeneration
# read the same number the production loop reads.
_AMPLE_S = 4000.0


@pytest.fixture
def ctx(tmp_path):
    return _validate_ctx(tmp_path)


class _CountingGenerator:
    """Hands back a fresh, distinctly-named pool on every call."""

    def __init__(self, *, raises: Exception | None = None, empty: bool = False):
        self.calls: List[Any] = []
        self._raises = raises
        self._empty = empty

    async def generate(self, ctx, deadline):
        self.calls.append((ctx, deadline))
        if self._raises is not None:
            raise self._raises
        if self._empty:
            return _FakeGeneration(candidates=[])
        n = len(self.calls)
        return _FakeGeneration(
            candidates=[{
                "candidate_id": f"regen{n}",
                "candidate_hash": f"regenhash{n}",
                "file_path": "a.py",
                "full_content": f"x = {n}\n",
                "source_hash": "src",
                "source_path": "a.py",
            }],
        )


@dataclass
class _RecordingOrchestrator(_FakeOrchestrator):
    """Records the candidate ids each VALIDATE iteration actually judged."""

    seen: List[str] = field(default_factory=list)

    async def _run_validation(self, ctx, cand, remaining_s):
        self.seen.append(cand.get("candidate_id", "?"))
        return self._run_validation_result


def _recording_orch(tmp_path, **overrides) -> _RecordingOrchestrator:
    base = _orch(tmp_path, **overrides)
    return _RecordingOrchestrator(
        _stack=base._stack,
        _config=base._config,
        _generator=base._generator,
        _run_validation_result=base._run_validation_result,
    )


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


def test_regen_enabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_VALIDATE_RETRY_REGENERATE", raising=False)
    assert _retry_regen_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " Off "])
def test_regen_kill_switch_honored(monkeypatch, value):
    monkeypatch.setenv("JARVIS_VALIDATE_RETRY_REGENERATE", value)
    assert _retry_regen_enabled() is False


# ---------------------------------------------------------------------------
# Fail-soft: every failure path must return the frozen pool unchanged, so the
# worst case of the fix is exactly the behavior it replaces.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regen_without_generator_returns_frozen_pool(ctx, tmp_path):
    orch = _orch(tmp_path, cfg_validation_timeout_s=_AMPLE_S, _generator=None)
    frozen = _generation(n_cands=2)

    result, outcome = await _regenerate_for_retry(
        ctx=ctx, orch=orch, previous=frozen,
    )

    assert result is frozen
    assert outcome == "no_generator"


@pytest.mark.asyncio
async def test_regen_below_budget_floor_returns_frozen_pool(ctx, tmp_path):
    gen = _CountingGenerator()
    orch = _orch(
        tmp_path,
        cfg_validation_timeout_s=_RETRY_REGEN_MIN_BUDGET_S - 1.0,
        _generator=gen,
    )
    frozen = _generation(n_cands=2)

    result, outcome = await _regenerate_for_retry(
        ctx=ctx, orch=orch, previous=frozen,
    )

    assert result is frozen
    assert outcome.startswith("budget_floor")
    assert gen.calls == [], "must not spend a model call it cannot afford"


@pytest.mark.asyncio
async def test_regen_exception_returns_frozen_pool(ctx, tmp_path):
    gen = _CountingGenerator(raises=RuntimeError("provider down"))
    orch = _orch(tmp_path, cfg_validation_timeout_s=_AMPLE_S, _generator=gen)
    frozen = _generation(n_cands=2)

    result, outcome = await _regenerate_for_retry(
        ctx=ctx, orch=orch, previous=frozen,
    )

    assert result is frozen
    assert outcome == "exception(RuntimeError)"


@pytest.mark.asyncio
async def test_regen_empty_result_returns_frozen_pool(ctx, tmp_path):
    gen = _CountingGenerator(empty=True)
    orch = _orch(tmp_path, cfg_validation_timeout_s=_AMPLE_S, _generator=gen)
    frozen = _generation(n_cands=2)

    result, outcome = await _regenerate_for_retry(
        ctx=ctx, orch=orch, previous=frozen,
    )

    assert result is frozen
    assert outcome == "empty"


@pytest.mark.asyncio
async def test_regen_cancellation_propagates(ctx, tmp_path):
    import asyncio

    gen = _CountingGenerator(raises=asyncio.CancelledError())
    orch = _orch(tmp_path, cfg_validation_timeout_s=_AMPLE_S, _generator=gen)

    with pytest.raises(asyncio.CancelledError):
        await _regenerate_for_retry(
            ctx=ctx, orch=orch, previous=_generation(n_cands=1),
        )


@pytest.mark.asyncio
async def test_regen_success_returns_fresh_pool(ctx, tmp_path):
    gen = _CountingGenerator()
    orch = _orch(tmp_path, cfg_validation_timeout_s=_AMPLE_S, _generator=gen)
    frozen = _generation(n_cands=2)

    result, outcome = await _regenerate_for_retry(
        ctx=ctx, orch=orch, previous=frozen,
    )

    assert outcome == "ok"
    assert result is not frozen
    assert [c["candidate_id"] for c in result.candidates] == ["regen1"]
    assert len(gen.calls) == 1


@pytest.mark.asyncio
async def test_regen_reads_the_retry_context(ctx, tmp_path):
    """The feedback prompt only matters if the *retry* ctx is what is sent."""
    gen = _CountingGenerator()
    orch = _orch(tmp_path, cfg_validation_timeout_s=_AMPLE_S, _generator=gen)
    retry_ctx = ctx.advance(OperationPhase.VALIDATE).advance(
        OperationPhase.VALIDATE_RETRY, strategic_memory_prompt="prior failures",
    )

    await _regenerate_for_retry(
        ctx=retry_ctx, orch=orch, previous=_generation(n_cands=1),
    )

    sent_ctx = gen.calls[0][0]
    assert sent_ctx is retry_ctx
    assert sent_ctx.strategic_memory_prompt == "prior failures"


# ---------------------------------------------------------------------------
# The load-bearing test: the ladder must judge DIFFERENT code each iteration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_iterations_validate_fresh_candidates(ctx, tmp_path):
    """Before the fix this asserted list was ['c0', 'c0', 'c0']."""
    gen = _CountingGenerator()
    orch = _recording_orch(
        tmp_path,
        cfg_max_validate_retries=2,
        cfg_validation_timeout_s=_AMPLE_S,
        _run_validation_result=_mk_validation(False, "test"),
        _generator=gen,
    )

    await VALIDATERunner(
        orch, None, generation=_generation(n_cands=1),
        generate_retries_remaining=3,
    ).run(ctx)

    # Three iterations: the frozen pool once, then a fresh pool per retry.
    assert orch.seen == ["c0", "regen1", "regen2"], (
        f"ladder re-judged the same code: {orch.seen}"
    )
    assert len(gen.calls) == 2, "one regeneration per retry, no more"


@pytest.mark.asyncio
async def test_retry_falls_back_to_frozen_pool_when_regen_fails(ctx, tmp_path):
    """A dead provider must degrade to the old behavior, not kill the op."""
    gen = _CountingGenerator(raises=RuntimeError("provider down"))
    orch = _recording_orch(
        tmp_path,
        cfg_max_validate_retries=2,
        cfg_validation_timeout_s=_AMPLE_S,
        _run_validation_result=_mk_validation(False, "test"),
        _generator=gen,
    )

    await VALIDATERunner(
        orch, None, generation=_generation(n_cands=1),
        generate_retries_remaining=3,
    ).run(ctx)

    assert orch.seen == ["c0", "c0", "c0"]


@pytest.mark.asyncio
async def test_kill_switch_restores_frozen_pool_behavior(
    ctx, tmp_path, monkeypatch,
):
    monkeypatch.setenv("JARVIS_VALIDATE_RETRY_REGENERATE", "false")
    gen = _CountingGenerator()
    orch = _recording_orch(
        tmp_path,
        cfg_max_validate_retries=2,
        cfg_validation_timeout_s=_AMPLE_S,
        _run_validation_result=_mk_validation(False, "test"),
        _generator=gen,
    )

    await VALIDATERunner(
        orch, None, generation=_generation(n_cands=1),
        generate_retries_remaining=3,
    ).run(ctx)

    assert orch.seen == ["c0", "c0", "c0"]
    assert gen.calls == []
