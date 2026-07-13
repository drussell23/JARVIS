"""Slice 10 — the APPLY LiveWork gate DEFERS (bounded wait), not terminal.

Run #20 (bt-iso-1783924404): the repair op died at APPLY with
`LEDGER_TERMINAL state=failed` terminal_reason_code=human_active_on_target
while the log claimed "deferring APPLY" — a recoverable condition treated
as terminal. The gate now asks the sensor for an exact wait horizon
(`seconds_until_quiet`), re-clocks the op's remaining budget from
`ctx.pipeline_deadline` (the same source the VALIDATE retry loop
re-clocks from), waits exactly that horizon, and re-runs the FULL scan.
Terminal only when the wait is infeasible (IDE lock → inf horizon,
horizon > remaining budget) or the wait master is off.

Drives the shared `_live_work_apply_gate` seam directly — the smallest
real seam BOTH APPLY paths (inline orchestrator + Slice4bRunner)
delegate to — monkeypatching LiveWorkSensor methods, never mocking the
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

    def __init__(self, budget_s: float = 60.0):
        self.pipeline_deadline = (
            datetime.now(tz=timezone.utc) + timedelta(seconds=budget_s)
        )


def _patch_sensor(monkeypatch, scans, horizons):
    """Script the sensor: `scans` is one (active, reason) per scan pass
    (single target file → one is_human_active call per pass); `horizons`
    is one float per seconds_until_quiet call. Returns the call ledger."""
    calls = {"scan": 0, "horizon": 0}

    async def _fake_active(self, rel_path):
        i = min(calls["scan"], len(scans) - 1)
        calls["scan"] += 1
        return scans[i]

    async def _fake_horizon(self, rel_path):
        i = min(calls["horizon"], len(horizons) - 1)
        calls["horizon"] += 1
        return horizons[i]

    monkeypatch.setattr(lws.LiveWorkSensor, "is_human_active", _fake_active)
    monkeypatch.setattr(lws.LiveWorkSensor, "seconds_until_quiet", _fake_horizon)
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
    _patch_sensor(
        monkeypatch,
        scans=[(True, "git status: pkg/mod.py has uncommitted changes "
                      "(modified 150s ago)"), (False, None)],
        horizons=[30.0],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result is None, "gate must clear — APPLY proceeds, no terminal"
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
        scans=[(True, "ide-lock: .mod.py.swp")],
        horizons=[float("inf")],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result == ("pkg/mod.py", "ide-lock: .mod.py.swp")
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
        scans=[(True, "mtime: modified 10s ago (window=180s)")],
        horizons=[170.0],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=15.0))
    assert result is not None
    assert recorded_sleeps == []
    assert any(
        "wait infeasible" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# (4) Master flag off → today's immediate-terminal path, no horizon asked
# ---------------------------------------------------------------------------


def test_master_flag_off_is_legacy_immediate_terminal(
    tmp_path, monkeypatch, recorded_sleeps, caplog
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    monkeypatch.setenv("JARVIS_APPLY_LIVE_WORK_WAIT_ENABLED", "false")
    calls = _patch_sensor(
        monkeypatch,
        scans=[(True, "git status: pkg/mod.py has uncommitted changes")],
        horizons=[5.0],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result == (
        "pkg/mod.py", "git status: pkg/mod.py has uncommitted changes",
    )
    assert calls["horizon"] == 0, "legacy path must not consult the horizon"
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
        scans=[
            (True, "git status: pkg/mod.py has uncommitted changes "
                   "(modified 160s ago)"),
            # Re-edited mid-wait: fresh mtime → a NEW, larger horizon.
            (True, "git status: pkg/mod.py has uncommitted changes "
                   "(modified 1s ago)"),
        ],
        horizons=[20.0, 500.0],
    )
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result is not None, "exhausted budget must go terminal"
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
    _patch_sensor(monkeypatch, scans=[(False, None)], horizons=[0.0])
    result = _run_gate(_make_orch(tmp_path), _Ctx(budget_s=60.0))
    assert result is None
    assert recorded_sleeps == []


# ---------------------------------------------------------------------------
# Wiring pins — BOTH APPLY paths delegate to the shared gate (the live
# path is Slice4bRunner: JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED default
# true; the inline orchestrator block is its kill-switch fallback)
# ---------------------------------------------------------------------------


def test_inline_apply_path_delegates_to_shared_gate():
    src = Path(om.__file__).read_text(encoding="utf-8")
    assert "await self._live_work_apply_gate(ctx, best_candidate)" in src


def test_slice4b_apply_path_delegates_to_shared_gate():
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner
    src = Path(slice4b_runner.__file__).read_text(encoding="utf-8")
    assert "await orch._live_work_apply_gate(ctx, best_candidate)" in src
