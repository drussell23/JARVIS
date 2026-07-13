"""Slice 10 — the APPLY LiveWork gate DEFERS (bounded wait), not terminal.

Run #20 (bt-iso-1783924404): the repair op died at APPLY with
`LEDGER_TERMINAL state=failed` terminal_reason_code=human_active_on_target
while the log claimed "deferring APPLY" — a recoverable condition treated
as terminal. The gate now consumes ONE sensor evaluation per file per
iteration (`LiveWorkSensor.evaluate` — active/reason/horizon from a single
signal pass, killing the two-call boundary disagreement), re-clocks the
op's remaining budget from `ctx.pipeline_deadline` (the same source the
VALIDATE retry loop re-clocks from), waits exactly the sensor-derived
horizon, and re-runs the FULL scan. Terminal only when the wait is
infeasible (IDE lock → inf horizon, horizon over the remaining budget or
the cumulative-wait clamp) or the wait master is off.

Review round (pre-merge NEEDS-FIXES):
- C1: the stale-exploration drift hash check runs BEFORE the gate on both
  APPLY paths — a wait opens a TOCTOU window. The gate re-runs the SAME
  drift helper post-wait and surfaces a blocking result for the callers'
  `state_drift_unreconciled` terminal shape.
- I1: active + horizon 0.0 at the exact window boundary gets ONE bounded
  immediate rescan; a second consecutive zero-horizon-active goes terminal.
- I2: the deadline-less fallback budget shrinks by every slept horizon.
- I4: cumulative waits within one invocation are clamped by
  JARVIS_APPLY_LIVE_WORK_WAIT_MAX_S (0.0 = derive from the file-lock TTL
  that serializes writers).

Drives the shared `_live_work_apply_gate` seam directly — the smallest
real seam BOTH APPLY paths (inline orchestrator + Slice4bRunner)
delegate to — monkeypatching LiveWorkSensor.evaluate, never mocking the
orchestrator itself. Every wait is sensor-derived: asyncio.sleep is
recorded + fast-forwarded, never slept for real."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import live_work_sensor as lws
from backend.core.ouroboros.governance import orchestrator as om
from backend.core.ouroboros.governance import state_drift as sd


_LOGGER = "backend.core.ouroboros.governance.orchestrator"
_CAND = {"file_path": "pkg/mod.py", "full_content": "x = 1\n"}


def _make_orch(tmp_path):
    orch = object.__new__(om.Orchestrator)
    # Minimal config seam: _live_work_apply_gate reads project_root (sensor
    # anchor), validation_timeout_s (deadline-less fallback), and the
    # @staticmethod _iter_candidate_files. Verified by reading the gate.
    orch._config = om.OrchestratorConfig(project_root=tmp_path)
    return orch


class _Ctx:
    op_id = "op-slice10-defer-pin"
    target_files = ("pkg/mod.py",)
    generate_file_hashes = ()

    def __init__(self, budget_s=60.0):
        self.pipeline_deadline = (
            None if budget_s is None
            else datetime.now(tz=timezone.utc) + timedelta(seconds=budget_s)
        )


def _active(reason="git status: pkg/mod.py has uncommitted changes "
                   "(modified 150s ago)", horizon=30.0):
    return lws.SignalEval(True, reason, horizon)


def _patch_sensor(monkeypatch, evals):
    """Script the sensor: `evals` is one SignalEval (or None → idle) per
    scan pass (single target file → one evaluate call per pass).
    Returns the call ledger."""
    calls = {"scan": 0}
    idle = lws.SignalEval(False, None, 0.0)

    async def _fake_evaluate(self, rel_path):
        i = min(calls["scan"], len(evals) - 1)
        calls["scan"] += 1
        return evals[i] if evals[i] is not None else idle

    monkeypatch.setattr(lws.LiveWorkSensor, "evaluate", _fake_evaluate)
    return calls


def _patch_drift(monkeypatch, results):
    """Script state_drift.should_block_apply (the gate imports it at call
    time, so patching the module attribute intercepts). One (block,
    stale_files) per call. Returns the call ledger."""
    calls = {"n": 0}

    def _fake(prior_hashes, project_root):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    monkeypatch.setattr(sd, "should_block_apply", _fake)
    return calls


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Record every asyncio.sleep the gate awaits and fast-forward it."""
    sleeps: list = []
    real_sleep = asyncio.sleep

    async def _fast(delay, *args, **kwargs):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(om.asyncio, "sleep", _fast)
    return sleeps


def _run_gate(orch, ctx):
    return asyncio.run(orch._live_work_apply_gate(ctx, _CAND))


# ---------------------------------------------------------------------------
# (1) Finite horizon within budget → wait the horizon, then proceed
# ---------------------------------------------------------------------------


def test_finite_horizon_within_budget_waits_then_proceeds(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    _patch_sensor(monkeypatch, [_active(horizon=30.0), None])
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit is None, "gate must clear — APPLY proceeds"
    assert result.drift_stale_files is None
    assert result.waited_s == pytest.approx(30.0)
    assert recorded_sleeps == [30.0], (
        "the ONE wait must be exactly the sensor-derived horizon"
    )
    assert any(
        "waiting 30.0s for quiet" in rec.getMessage()
        for rec in caplog.records if rec.levelno == logging.INFO
    ), "honest INFO wait line missing"


# ---------------------------------------------------------------------------
# (2) Infinite horizon (IDE lock) → terminal, honest log
# ---------------------------------------------------------------------------


def test_infinite_horizon_goes_terminal_with_honest_log(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _patch_sensor(
        monkeypatch,
        [_active(reason="ide-lock: .mod.py.swp", horizon=float("inf"))],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit == ("pkg/mod.py", "ide-lock: .mod.py.swp")
    assert recorded_sleeps == [], "an infeasible wait must never sleep"
    infeasible = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING and "wait infeasible" in rec.getMessage()
    ]
    assert infeasible, "honest wait-infeasible WARNING missing"
    assert not any("deferring" in msg for msg in infeasible), (
        "the terminal path must never claim it is 'deferring'"
    )


# ---------------------------------------------------------------------------
# (3) Horizon exceeds remaining budget → terminal
# ---------------------------------------------------------------------------


def test_horizon_exceeding_budget_goes_terminal(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _patch_sensor(
        monkeypatch,
        [_active(reason="mtime: modified 10s ago (window=180s)", horizon=170.0)],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=15.0))
    assert result.active_hit is not None
    assert recorded_sleeps == []
    assert any(
        "wait infeasible" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# (4) Master flag off → today's immediate-terminal path
# ---------------------------------------------------------------------------


def test_master_flag_off_is_legacy_immediate_terminal(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    monkeypatch.setenv("JARVIS_APPLY_LIVE_WORK_WAIT_ENABLED", "false")
    _patch_sensor(
        monkeypatch,
        [_active(reason="git status: pkg/mod.py has uncommitted changes",
                 horizon=5.0)],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit == (
        "pkg/mod.py", "git status: pkg/mod.py has uncommitted changes",
    )
    assert result.waited_s == 0.0
    assert recorded_sleeps == []
    assert any(
        "deferring APPLY" in rec.getMessage() for rec in caplog.records
    ), "legacy WARNING text must be preserved under the kill switch"


# ---------------------------------------------------------------------------
# (5) Mid-wait re-edit → new horizon no longer affordable → terminal
#     after the recorded wait(s)
# ---------------------------------------------------------------------------


def test_mid_wait_reedit_exhausts_budget_then_terminal(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _patch_sensor(
        monkeypatch,
        [
            _active(reason="git status: pkg/mod.py has uncommitted changes "
                           "(modified 160s ago)", horizon=20.0),
            # Re-edited mid-wait: fresh mtime → a NEW, larger horizon.
            _active(reason="git status: pkg/mod.py has uncommitted changes "
                           "(modified 1s ago)", horizon=500.0),
        ],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit is not None, "exhausted budget must go terminal"
    assert recorded_sleeps == [20.0], (
        "exactly the first (affordable) horizon was waited before terminal"
    )
    assert any(
        "wait infeasible" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Quiet scan → gate clears without waiting at all
# ---------------------------------------------------------------------------


def test_quiet_scan_clears_immediately(tmp_path, monkeypatch, recorded_sleeps):
    _patch_sensor(monkeypatch, [None])
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit is None
    assert result.waited_s == 0.0
    assert recorded_sleeps == []


# ---------------------------------------------------------------------------
# C1 — post-wait drift recheck (TOCTOU: the pre-gate hash check is stale
# after any wait; a human edit made mid-wait must not be overwritten)
# ---------------------------------------------------------------------------


class _DriftCtx(_Ctx):
    generate_file_hashes = (("pkg/mod.py", "deadbeef"),)


def test_post_wait_drift_recheck_blocks(
    tmp_path, monkeypatch, recorded_sleeps
):
    """One wait happened → the gate re-runs should_block_apply; blocking
    → callers get the stale files for the SAME state_drift_unreconciled
    terminal shape (no APPLY, file untouched)."""
    _patch_sensor(monkeypatch, [_active(horizon=30.0), None])
    drift_calls = _patch_drift(monkeypatch, [(True, ["pkg/mod.py"])])
    result = _run_gate(_make_orch(tmp_path), _DriftCtx(budget_s=60.0))
    assert result.active_hit is None
    assert result.drift_stale_files == ["pkg/mod.py"]
    assert result.waited_s == pytest.approx(30.0)
    assert drift_calls["n"] == 1, "the SAME drift helper must re-run post-wait"
    assert recorded_sleeps == [30.0]


def test_post_wait_drift_recheck_clean_proceeds(
    tmp_path, monkeypatch, recorded_sleeps
):
    _patch_sensor(monkeypatch, [_active(horizon=30.0), None])
    drift_calls = _patch_drift(monkeypatch, [(False, [])])
    result = _run_gate(_make_orch(tmp_path), _DriftCtx(budget_s=60.0))
    assert result.active_hit is None
    assert result.drift_stale_files is None
    assert drift_calls["n"] == 1
    assert recorded_sleeps == [30.0]


def test_no_wait_skips_drift_recheck(tmp_path, monkeypatch, recorded_sleeps):
    """Preserves today's failure-class ordering for no-wait ops: the
    pre-gate check already ran on fresh hashes — a quiet scan with zero
    sleeps must NOT re-run the helper."""
    _patch_sensor(monkeypatch, [None])
    drift_calls = _patch_drift(monkeypatch, [(True, ["pkg/mod.py"])])
    result = _run_gate(_make_orch(tmp_path), _DriftCtx(budget_s=60.0))
    assert result.active_hit is None
    assert result.drift_stale_files is None
    assert drift_calls["n"] == 0
    assert recorded_sleeps == []


# ---------------------------------------------------------------------------
# I1 — active + horizon 0.0 (exact window boundary: age == window is
# inclusive-active with horizon 0): ONE bounded immediate rescan, a
# second consecutive zero-horizon-active goes terminal (never a busy-loop)
# ---------------------------------------------------------------------------


def test_zero_horizon_once_then_idle_proceeds(
    tmp_path, monkeypatch, recorded_sleeps
):
    _patch_sensor(
        monkeypatch,
        [_active(reason="mtime: modified 180s ago (window=180s)", horizon=0.0),
         None],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit is None, "boundary eval must rescan, not kill"
    assert recorded_sleeps == [], "the immediate rescan must not sleep"


def test_zero_horizon_twice_goes_terminal(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _patch_sensor(
        monkeypatch,
        [_active(reason="mtime: modified 180s ago (window=180s)", horizon=0.0),
         _active(reason="mtime: modified 180s ago (window=180s)", horizon=0.0)],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result.active_hit is not None
    assert recorded_sleeps == []
    assert any(
        "wait infeasible" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# I2 — deadline-less fallback budget SHRINKS by every slept horizon
# ---------------------------------------------------------------------------


def test_deadline_none_fallback_budget_shrinks_to_termination(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    """pipeline_deadline=None → the validation_timeout_s fallback (60.0
    default) is initialized ONCE and decremented per sleep: 25 + 25 = 50
    fits, the third 25 exceeds the remaining 10 → terminal. A constant
    re-read would loop forever."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _patch_sensor(monkeypatch, [_active(horizon=25.0)])  # active forever
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=None))
    assert result.active_hit is not None
    assert recorded_sleeps == [25.0, 25.0]
    assert any(
        "wait infeasible" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# I4 — cumulative-wait clamp (pool occupancy / file-lock TTL)
# ---------------------------------------------------------------------------


def test_cumulative_wait_clamp_explicit_env(
    tmp_path, monkeypatch, recorded_sleeps
):
    monkeypatch.setenv("JARVIS_APPLY_LIVE_WORK_WAIT_MAX_S", "40")
    _patch_sensor(monkeypatch, [_active(horizon=30.0)])  # active forever
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=600.0))
    assert result.active_hit is not None
    assert recorded_sleeps == [30.0], (
        "second 30s wait would breach the 40s clamp — must go terminal"
    )


def test_cumulative_wait_clamp_derives_from_file_lock_ttl(
    tmp_path, monkeypatch, recorded_sleeps
):
    """Unset/0.0 → the clamp derives from JARVIS_FILE_LOCK_TTL_S: a wait
    may never outlive the file lock that serializes writers."""
    monkeypatch.delenv("JARVIS_APPLY_LIVE_WORK_WAIT_MAX_S", raising=False)
    monkeypatch.setenv("JARVIS_FILE_LOCK_TTL_S", "35")
    _patch_sensor(monkeypatch, [_active(horizon=30.0)])  # active forever
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=600.0))
    assert result.active_hit is not None
    assert recorded_sleeps == [30.0]


# ---------------------------------------------------------------------------
# Wiring pins — BOTH APPLY paths delegate to the shared gate (the live
# path is Slice4bRunner: JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED default
# true; the inline orchestrator block is its kill-switch fallback) and
# BOTH handle the C1 post-wait drift result with the state-drift terminal
# ---------------------------------------------------------------------------


def test_inline_apply_path_delegates_to_shared_gate():
    src = Path(om.__file__).read_text(encoding="utf-8")
    assert "await self._live_work_apply_gate(ctx, best_candidate)" in src


def test_slice4b_apply_path_delegates_to_shared_gate():
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner
    src = Path(slice4b_runner.__file__).read_text(encoding="utf-8")
    assert "await orch._live_work_apply_gate(ctx, best_candidate)" in src


def test_both_apply_paths_handle_post_wait_drift_terminal():
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner
    for mod in (om, slice4b_runner):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "drift_stale_files is not None" in src, mod.__name__
        assert "post-LiveWork-wait" in src, mod.__name__
