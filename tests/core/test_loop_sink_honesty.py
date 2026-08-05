"""A slow await is not a blocked loop, and the telemetry must say which.

`sink_async` measures wall-clock around a `yield`. Work correctly offloaded to
a thread takes just as long there while the loop runs perfectly freely, so a
large number is a QUESTION. The emitted line answered it "on-loop call
exceeded threshold" regardless.

On 2026-08-05 that sent an investigation at
`posture_observer.run_one_cycle` (7,146 ms) for hours. Posture turned out to
be innocent — its collectors were already off-loop via
`cooperative_fs_io.offload`, and the elapsed time was inflated by boot work
elsewhere: a 15s learning-DB timeout, a 10s CloudSQL timeout, a 22s capability
federation, seven provider entitlement probes. The metric was measuring a
victim and naming it a culprit.

These pin both readings, because a diagnostic that cannot be wrong about this
is worth more than one that is usually right.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.hud import loop_sentinel as ls


@pytest.fixture(autouse=True)
def _reset_sentinel():
    ls._SENTINEL = None
    yield
    ls._SENTINEL = None


def _sentinel_with(stalls):
    """A sentinel carrying a known stall history."""
    s = ls.LoopSentinel()
    for started, lag in stalls:
        s._health.recent.append(
            ls.Stall(started_at=started, lag_s=lag, started_wall="test"))
    ls._SENTINEL = s
    return s


# ---------------------------------------------------------------------------
# stalled_ms_since
# ---------------------------------------------------------------------------


def test_no_sentinel_reports_no_evidence_not_proof():
    """Absence of a sentinel is 'nothing observed', which callers must not
    read as 'nothing happened'. Returning 0.0 keeps the emit conservative."""
    ls._SENTINEL = None
    assert ls.stalled_ms_since(time.monotonic()) == 0.0


def test_a_stall_that_finished_before_the_region_does_not_count():
    now = time.monotonic()
    _sentinel_with([(now - 10.0, 2.0)])          # ended at now-8
    assert ls.stalled_ms_since(now) == 0.0


def test_a_stall_inside_the_region_counts_in_full():
    now = time.monotonic()
    _sentinel_with([(now + 0.1, 1.5)])
    assert ls.stalled_ms_since(now) == pytest.approx(1500.0, abs=1.0)


def test_a_stall_straddling_the_start_counts_only_its_overlap():
    """Half of it belongs to whatever came before; charging this region for
    the whole thing would over-attribute and re-create the original sin."""
    now = time.monotonic()
    _sentinel_with([(now - 1.0, 3.0)])           # 1s before, 2s inside
    assert ls.stalled_ms_since(now) == pytest.approx(2000.0, abs=50.0)


def test_multiple_stalls_accumulate():
    now = time.monotonic()
    _sentinel_with([(now + 0.1, 0.5), (now + 1.0, 0.75)])
    assert ls.stalled_ms_since(now) == pytest.approx(1250.0, abs=5.0)


# ---------------------------------------------------------------------------
# The verdict the emit prints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_but_off_loop_is_reported_as_off_loop(monkeypatch, caplog):
    """THE CASE THAT COST HOURS. A long await with a responsive loop must not
    be described as an on-loop call."""
    from backend.core.ouroboros.telemetry import loop_sink

    monkeypatch.setattr(loop_sink, "is_enabled", lambda: True)
    monkeypatch.setattr(loop_sink, "_resolve_threshold_ms", lambda: 50.0)
    _sentinel_with([])                            # loop never stalled

    caplog.set_level("WARNING")
    async with loop_sink.sink_async("test.offloaded_but_slow"):
        await asyncio.sleep(0.12)

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "OFF-LOOP" in line, line
    assert "on-loop call exceeded threshold" not in line, (
        "the async path still asserts the loop was blocked — this is the "
        "mislabel that sent the investigation at an innocent subsystem")


@pytest.mark.asyncio
async def test_a_genuinely_starved_region_is_reported_as_starved(monkeypatch, caplog):
    from backend.core.ouroboros.telemetry import loop_sink

    monkeypatch.setattr(loop_sink, "is_enabled", lambda: True)
    monkeypatch.setattr(loop_sink, "_resolve_threshold_ms", lambda: 50.0)

    caplog.set_level("WARNING")
    t_region = time.monotonic()
    _sentinel_with([(t_region, 0.30)])            # loop dead for most of it
    async with loop_sink.sink_async("test.really_blocking"):
        await asyncio.sleep(0.10)

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "LOOP STARVED" in line, line


@pytest.mark.asyncio
async def test_the_grep_contract_survives(monkeypatch, caplog):
    """The v27 runbook greps `[LoopSink] callsite=... blocked_ms=`. Renaming
    the field would silently empty somebody's dashboard."""
    from backend.core.ouroboros.telemetry import loop_sink

    monkeypatch.setattr(loop_sink, "is_enabled", lambda: True)
    monkeypatch.setattr(loop_sink, "_resolve_threshold_ms", lambda: 10.0)
    _sentinel_with([])

    caplog.set_level("WARNING")
    async with loop_sink.sink_async("test.contract"):
        await asyncio.sleep(0.05)

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "[LoopSink] callsite=test.contract" in line
    assert "blocked_ms=" in line
    assert "loop_stalled_ms=" in line, "the new field must be present too"
