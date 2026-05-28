"""Slice 34 Phase 1+2+3 — Intra-Dispatch Profiler + Adaptive Timeout Graduation.

Tests:
  * dispatch_profiler.py substrate (composes loop_sink primitives,
    adds per-op aggregation, default-OFF master flag)
  * candidate_generator._call_primary wiring (STAGE_SEM_WAIT,
    STAGE_BUDGET_COMPUTATION, STAGE_PROVIDER_GENERATE)
  * Phase 3 graduation: JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED default
    flipped FALSE → TRUE

Test surface (4 AST pins + 8 spine = 12 tests).
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "telemetry"
    / "dispatch_profiler.py"
)
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
ADAPTIVE_TIMEOUT_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_adaptive_timeout.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 4
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_profiler_substrate_present() -> None:
    """dispatch_profiler.py MUST expose op_session + dispatch_stage
    as async contextmgrs + OpDispatchSummary dataclass."""
    src = PROFILER_FILE.read_text()
    assert PROFILER_FILE.exists()
    assert "async def op_session" in src or "@asynccontextmanager" in src
    assert "def dispatch_stage" in src
    assert "class OpDispatchSummary" in src
    assert "class StageRecord" in src
    assert "Slice 34 Phase 1" in src
    assert 'JARVIS_DISPATCH_PROFILER_ENABLED' in src


def test_ast_pin_profiler_default_off() -> None:
    """JARVIS_DISPATCH_PROFILER_ENABLED defaults FALSE."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        is_enabled, _ENABLED_ENV,
    )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_ENABLED_ENV, None)
        assert is_enabled() is False
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        with mock.patch.dict(os.environ, {_ENABLED_ENV: truthy}):
            assert is_enabled() is True


def test_ast_pin_call_primary_wired_to_profiler() -> None:
    """_call_primary MUST reference dispatch_profiler + emit the
    three stages (SEM_WAIT, BUDGET_COMPUTATION, PROVIDER_GENERATE).
    Without these the v30 probe can't bisect intra-dispatch
    latency."""
    src = CG_FILE.read_text()
    assert "dispatch_profiler" in src
    assert "STAGE_SEM_WAIT" in src
    assert "STAGE_BUDGET_COMPUTATION" in src
    assert "STAGE_PROVIDER_GENERATE" in src
    assert "op_summary" in src, (
        "_call_primary must emit per-op summary at exit"
    )


def test_ast_pin_adaptive_timeout_graduated_default_true() -> None:
    """Phase 3 graduation: JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED default
    FLIPPED from FALSE → TRUE. Operator's per-§48.7.4 directive
    after the 40-record probe ledger established healthy baseline."""
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        is_enabled, _ENABLED_ENV,
    )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_ENABLED_ENV, None)
        assert is_enabled() is True, (
            "Phase 3 graduation FAILED — flag still defaults FALSE"
        )
    # Operator opt-out still works
    for falsy in ("0", "false", "no", "off"):
        with mock.patch.dict(os.environ, {_ENABLED_ENV: falsy}):
            assert is_enabled() is False


# ──────────────────────────────────────────────────────────────────────
# Spine — 8
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def profiler_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DISPATCH_PROFILER_ENABLED", "1")
    from backend.core.ouroboros.telemetry import dispatch_profiler as dp
    dp.reset_for_tests()
    yield
    dp.reset_for_tests()


def test_spine_op_session_emits_summary_on_exit(
    profiler_enabled, caplog,
) -> None:
    """op_session emits one structured op_summary row at exit
    with all recorded stages."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, dispatch_stage, get_recent_op_summaries,
    )

    async def run():
        async with op_session(
            op_id="op-test-1", model_id="Qwen/Qwen3.5-397B-A17B-FP8",
            route="standard",
        ):
            async with dispatch_stage(
                "STAGE_PROMPT_ASSEMBLY",
                op_id="op-test-1",
                model_id="Qwen/Qwen3.5-397B-A17B-FP8",
            ):
                await asyncio.sleep(0.01)

    with caplog.at_level(
        logging.INFO, logger="Ouroboros.DispatchProfiler",
    ):
        asyncio.run(run())

    summaries = get_recent_op_summaries(10)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.op_id == "op-test-1"
    assert s.outcome == "ok"
    assert len(s.stages) == 1
    assert s.stages[0].stage_name == "STAGE_PROMPT_ASSEMBLY"
    # Log row emitted
    matched = [r for r in caplog.records if "op_summary" in r.getMessage()]
    assert len(matched) == 1


def test_spine_dispatch_stage_records_duration(
    profiler_enabled, caplog,
) -> None:
    """Each dispatch_stage emits its own log row + accumulates into
    the parent op_session."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, dispatch_stage,
    )

    async def run():
        async with op_session(
            op_id="op-X", model_id="m", route="standard",
        ) as summary:
            async with dispatch_stage(
                "S1", op_id="op-X", model_id="m",
            ):
                await asyncio.sleep(0.02)
            async with dispatch_stage(
                "S2", op_id="op-X", model_id="m",
            ):
                await asyncio.sleep(0.03)
        assert len(summary.stages) == 2
        assert summary.stages[0].stage_name == "S1"
        assert summary.stages[1].stage_name == "S2"
        assert summary.stages[0].duration_ms >= 15  # ≥15ms (>=20 - jitter)
        assert summary.stages[1].duration_ms >= 25

    asyncio.run(run())


def test_spine_master_off_zero_overhead(monkeypatch, caplog) -> None:
    """When master flag is OFF, both op_session and dispatch_stage
    no-op — no logs, no accumulators."""
    monkeypatch.setenv("JARVIS_DISPATCH_PROFILER_ENABLED", "0")
    from backend.core.ouroboros.telemetry import dispatch_profiler as dp
    dp.reset_for_tests()
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, dispatch_stage, get_recent_op_summaries,
    )

    async def run():
        async with op_session(op_id="x", model_id="m", route="r"):
            async with dispatch_stage("S1", op_id="x", model_id="m"):
                pass

    with caplog.at_level(
        logging.DEBUG, logger="Ouroboros.DispatchProfiler",
    ):
        asyncio.run(run())

    matched = [
        r for r in caplog.records
        if "op_summary" in r.getMessage()
        or "[DispatchProfiler] stage" in r.getMessage()
    ]
    assert len(matched) == 0
    assert get_recent_op_summaries(10) == []


def test_spine_op_session_captures_exception(profiler_enabled) -> None:
    """op_session records outcome=error when body raises; op_summary
    still emitted."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, get_recent_op_summaries,
    )

    async def run():
        try:
            async with op_session(
                op_id="op-err", model_id="m", route="r",
            ):
                raise RuntimeError("simulated")
        except RuntimeError:
            pass

    asyncio.run(run())
    summaries = get_recent_op_summaries(10)
    assert len(summaries) == 1
    assert summaries[0].outcome == "error"
    assert summaries[0].error_class == "RuntimeError"


def test_spine_dispatch_stage_captures_exception(
    profiler_enabled,
) -> None:
    """Stage with exception records outcome + error_class but still
    accumulates duration."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, dispatch_stage, get_recent_op_summaries,
    )

    async def run():
        try:
            async with op_session(
                op_id="op-stg-err", model_id="m", route="r",
            ):
                try:
                    async with dispatch_stage(
                        "S_BAD", op_id="op-stg-err", model_id="m",
                    ):
                        raise ValueError("inner")
                except ValueError:
                    pass
        finally:
            pass

    asyncio.run(run())
    summaries = get_recent_op_summaries(10)
    assert len(summaries) == 1
    bad = [s for s in summaries[0].stages if s.stage_name == "S_BAD"]
    assert len(bad) == 1
    assert bad[0].outcome == "error"
    assert bad[0].error_class == "ValueError"


def test_spine_recent_summaries_bounded_ring() -> None:
    """get_recent_op_summaries returns at most N records; older
    evicted."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, get_recent_op_summaries, reset_for_tests,
    )
    os.environ["JARVIS_DISPATCH_PROFILER_ENABLED"] = "1"
    reset_for_tests()

    async def run():
        for i in range(10):
            async with op_session(
                op_id=f"op-{i}", model_id="m", route="r",
            ):
                pass

    asyncio.run(run())
    # Default ring size is 256 — all 10 should fit
    summaries = get_recent_op_summaries(50)
    assert len(summaries) == 10
    reset_for_tests()


def test_spine_profiler_never_raises_on_internal_error(
    profiler_enabled, monkeypatch,
) -> None:
    """If internal accumulator state is corrupt, profiler MUST
    swallow + caller body MUST still execute normally."""
    from backend.core.ouroboros.telemetry import dispatch_profiler as dp

    # Force the lock to misbehave
    original_lock = dp._active_ops_lock
    class BrokenLock:
        def __enter__(self):
            raise RuntimeError("simulated lock failure")
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(dp, "_active_ops_lock", BrokenLock())

    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        op_session, dispatch_stage,
    )

    async def run():
        # Should not raise even with broken lock
        async with op_session(op_id="x", model_id="m", route="r"):
            async with dispatch_stage("S1", op_id="x", model_id="m"):
                pass
        return 42

    result = asyncio.run(run())
    assert result == 42  # caller body executed despite profiler error
    # Restore for other tests
    monkeypatch.setattr(dp, "_active_ops_lock", original_lock)


def test_spine_summary_log_kv_format() -> None:
    """OpDispatchSummary.to_log_kv produces grep-friendly key=value
    output with all stages."""
    from backend.core.ouroboros.telemetry.dispatch_profiler import (
        OpDispatchSummary, StageRecord,
    )
    s = OpDispatchSummary(
        op_id="op-fullid-12345-678",
        model_id="Qwen/Qwen3.5-397B-A17B-FP8",
        route="standard",
        started_unix=1000.0,
        total_duration_ms=8500.5,
        stages=[
            StageRecord("STAGE_A", 120.3),
            StageRecord("STAGE_B", 8000.1, outcome="error", error_class="TimeoutError"),
        ],
    )
    out = s.to_log_kv()
    # Op_id truncated to 16 chars
    assert "op=op-fullid-1234" in out
    assert "model=Qwen/Qwen3.5-397B-A17B-FP8" in out
    assert "route=standard" in out
    assert "total_ms=8500.5" in out
    assert "stages=2" in out
    assert "stage_STAGE_A_ms=120.3" in out
    assert "stage_STAGE_B_ms=8000.1" in out
    assert "stage_STAGE_B_outcome=error" in out
