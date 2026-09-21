"""Landed work: the one number, on screen, counted where landings already pass.

For two days the pipeline looked alive at every layer the cockpit could show —
phases advancing, tokens streaming, gates firing — while landing nothing, and
that was only ever discovered by grepping ``debug.log`` afterwards.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import status_line as sl
from backend.core.ouroboros.governance import landed_metrics as lm
from backend.core.ouroboros.governance.comm_protocol import CommProtocol
from backend.core.ouroboros.governance.reachability_ledger import ReachabilityLedger
from tests.support.ast_contract import calls_to, parse_module


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh():
    lm.reset_for_tests()
    yield
    lm.reset_for_tests()


# ---------------------------------------------------------------------------
# The rate does not extrapolate, and never divides by zero
# ---------------------------------------------------------------------------


def test_at_the_instant_of_boot_nothing_divides_by_zero():
    snap = lm.LandedMetrics(clock=_Clock()).snapshot()
    assert (snap.total, snap.per_hour, snap.settled) == (0, 0.0, False)
    assert snap.last_landed_age_s is None


def test_a_landing_in_the_first_minute_is_not_180_per_hour():
    clock = _Clock()
    metrics = lm.LandedMetrics(clock=clock)
    clock.now += 20
    metrics.record("op-1")
    snap = metrics.snapshot()
    assert snap.per_hour == pytest.approx(1.0), "extrapolated from 20 seconds"
    assert snap.settled is False


def test_the_figure_converges_on_the_true_rate_at_the_hour():
    clock = _Clock()
    metrics = lm.LandedMetrics(clock=clock)
    for i in range(6):
        metrics.record(f"op-{i}")
    clock.now += 2 * 3600
    snap = metrics.snapshot()
    assert snap.per_hour == pytest.approx(3.0) and snap.settled is True


def test_it_never_overstates_at_any_point_in_the_first_hour():
    clock = _Clock()
    metrics = lm.LandedMetrics(clock=clock)
    metrics.record("op-1")
    for elapsed in (0, 1, 59, 600, 3599):
        clock.now = 1000.0 + elapsed
        assert metrics.snapshot().per_hour <= 1.0


def test_a_clock_that_runs_backwards_is_survived():
    clock = _Clock()
    metrics = lm.LandedMetrics(clock=clock)
    metrics.record("op-1")
    clock.now -= 500
    snap = metrics.snapshot()
    assert snap.uptime_s == 0.0 and snap.per_hour == 1.0 and snap.last_landed_age_s == 0.0


def test_a_replayed_decision_is_one_landing():
    metrics = lm.LandedMetrics(clock=_Clock())
    assert metrics.record("op-1") is True
    assert metrics.record("op-1") is False
    assert metrics.snapshot().total == 1


def test_recent_landings_are_bounded():
    metrics = lm.LandedMetrics(clock=_Clock())
    for i in range(500):
        metrics.record(f"op-{i}", f"file{i}.py")
    assert metrics.snapshot().total == 500 and len(metrics.snapshot().recent) <= 8


def test_rendering_names_the_regime():
    assert lm.render_landed(3, 2.72, True, 4000) == "landed 3 · 2.7/h"
    assert lm.render_landed(1, 1.0, False, 12 * 60) == "landed 1 · 12m in"
    assert lm.render_landed(0, 0.0, False, 0) == "landed 0 · 0m in"
    assert "landed" in lm.render_landed(None, "x", False, None)  # never raises


# ---------------------------------------------------------------------------
# Counted on the stream every engine already writes to
# ---------------------------------------------------------------------------


async def _drain(transport):
    while transport._background:
        await asyncio.gather(*list(transport._background), return_exceptions=True)


@pytest.mark.asyncio
async def test_only_an_applied_decision_is_a_landing(tmp_path):
    metrics = lm.LandedMetrics()
    ledger = ReachabilityLedger(path=tmp_path / "reach.jsonl")
    transport = lm.LandedMetricsTransport(metrics, ledger=ledger)
    comm = CommProtocol(transports=[transport])

    await comm.emit_heartbeat(op_id="op-a", phase="apply", progress_pct=70.0)
    await comm.emit_decision(op_id="op-a", outcome="validation_failed", reason_code="syntax_error")
    await comm.emit_decision(op_id="op-b", outcome="noop", reason_code="noop")
    await comm.emit_decision(op_id="op-c", outcome="escalated", reason_code="x")
    assert metrics.snapshot().total == 0

    await comm.emit_decision(op_id="op-d", outcome="applied", reason_code="safe_auto_passed",
                             diff_summary="Applied change to backend/api/sse_contract.py")
    snap = metrics.snapshot()
    assert snap.total == 1 and "sse_contract.py" in snap.recent[0]

    await _drain(transport)
    state = ledger._states[lm.CAPABILITY]
    assert (state.registered, state.invoked, state.effective) == (1, 4, 1), (
        "busy-but-inert must be visible: 4 decisions seen, 1 landed"
    )
    assert lm.CAPABILITY in (tmp_path / "reach.jsonl").read_text()


@pytest.mark.asyncio
async def test_a_slow_ledger_never_holds_the_pipeline(tmp_path):
    """``send`` is awaited by the CommProtocol for every message."""
    class _SlowLedger:
        async def registered(self, *a, **k):
            await asyncio.sleep(5)

        invoked = effective = registered

    transport = lm.LandedMetricsTransport(lm.LandedMetrics(), ledger=_SlowLedger())
    comm = CommProtocol(transports=[transport])
    t0 = time.monotonic()
    await comm.emit_decision(op_id="op-1", outcome="applied", reason_code="safe_auto_passed")
    assert time.monotonic() - t0 < 1.0
    for task in list(transport._background):
        task.cancel()


@pytest.mark.asyncio
async def test_a_broken_ledger_still_counts_the_landing():
    class _Boom:
        async def registered(self, *a, **k):
            raise OSError("disk full")

        invoked = effective = registered

    metrics = lm.LandedMetrics()
    transport = lm.LandedMetricsTransport(metrics, ledger=_Boom())
    await CommProtocol(transports=[transport]).emit_decision(
        op_id="op-1", outcome="applied", reason_code="safe_auto_passed",
    )
    await _drain(transport)
    assert metrics.snapshot().total == 1


@pytest.mark.asyncio
async def test_a_malformed_message_is_ignored():
    transport = lm.LandedMetricsTransport(lm.LandedMetrics())
    for junk in (None, object(), "DECISION"):
        await transport.send(junk)


# ---------------------------------------------------------------------------
# On screen, idle or busy
# ---------------------------------------------------------------------------


def test_the_status_line_shows_it_while_busy():
    snap = sl.StatusSnapshot(phase="GENERATE", landed_total=3, landed_per_hour=2.72,
                             landed_settled=True, landed_uptime_s=4000)
    assert "landed 3 · 2.7/h" in sl._format_plain(snap, compact=False)


def test_the_status_line_shows_it_while_idle():
    """The idle breadcrumb returns early — and idle-with-zero-landed is the
    exact state this metric exists to make impossible to miss."""
    snap = sl.StatusSnapshot(phase="IDLE", landed_total=0, landed_uptime_s=1500)
    assert "landed 0 · 25m in" in sl._format_plain(snap, compact=False)


def test_the_builder_samples_the_live_counter():
    lm.get_landed_metrics().record("op-1", "x.py")
    builder = sl.StatusLineBuilder.__new__(sl.StatusLineBuilder)
    total, rate, settled, uptime = builder._sample_landed()
    assert (total, rate, settled) == (1, 1.0, False) and uptime >= 0.0


def test_the_transport_is_actually_registered():
    tree = parse_module(Path("backend/core/ouroboros/governance/integration.py"))
    assert calls_to(tree, "LandedMetricsTransport")
