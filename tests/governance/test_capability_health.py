"""A fail-soft path that fails on every call must not be silent.

bt-2026-09-22-201845: cooperative_fs_io's process pool broke 16 minutes in and
every cpu-bound ``offload`` failed for the next 2.5 hours. Each failure was
correct fail-soft behaviour -- an OffloadError returned, a DEBUG "degraded"
line -- and 349 call sites work that way. Nothing aggregated them, a soak log
keeps WARNING and above, and ``summary.json`` said nothing: DOWN looked
identical to healthy. The reachability ledger already existed for exactly this
class ("invoked, never effective"); it had no notion of a failed invocation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io as cfi
from backend.core.ouroboros.governance import reachability_ledger as RL
from backend.core.ouroboros.governance.reachability_ledger import (
    ReachabilityLedger,
    Tier,
    track_reachability,
)

CAP = "probe.capability"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for var in ("JARVIS_REACHABILITY_FAILING_STREAK", "JARVIS_REACHABILITY_LEDGER_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(RL, "_default", None)
    cfi.shutdown_fs_process_pool()
    yield
    cfi.shutdown_fs_process_pool()


def _lines(path: Path) -> list:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# ---------------------------------------------------------------------------
# The streak rule
# ---------------------------------------------------------------------------

async def test_consecutive_failures_raise_one_alarm_not_one_line_per_call(caplog):
    book = ReachabilityLedger()
    with caplog.at_level(logging.INFO, logger="Ouroboros.Reachability"):
        for _ in range(2):
            await book.settle(CAP, ok=False, detail="BrokenProcessPool: pool broken")
        assert not (await book.state(CAP)).failing, "two is a blip, not down"
        for _ in range(10):
            await book.settle(CAP, ok=False, detail="BrokenProcessPool: pool broken")
    state = await book.state(CAP)
    assert state.failing and state.failed == 12 and state.alarms == 1
    alarms = [r for r in caplog.records if "HEALTH ALARM" in r.getMessage()]
    assert len(alarms) == 1 and alarms[0].levelno == logging.WARNING
    assert "BrokenProcessPool" in alarms[0].getMessage()


async def test_a_success_ends_the_episode_loudly(caplog):
    book = ReachabilityLedger()
    for _ in range(3):
        await book.settle(CAP, ok=False, detail="x")
    with caplog.at_level(logging.WARNING, logger="Ouroboros.Reachability"):
        await book.settle(CAP, ok=True)
    state = await book.state(CAP)
    assert not state.failing and state.recoveries == 1 and state.consecutive_failures == 0
    assert any("RECOVERED" in r.getMessage() for r in caplog.records)


async def test_a_flaky_path_never_alarms():
    book = ReachabilityLedger()
    for _ in range(20):
        await book.settle(CAP, ok=False, detail="FileNotFoundError")
        await book.settle(CAP, ok=True)
    state = await book.state(CAP)
    assert state.failed == 20 and state.alarms == 0 and not state.failing


async def test_the_threshold_is_the_operators(monkeypatch):
    monkeypatch.setenv("JARVIS_REACHABILITY_FAILING_STREAK", "1")
    book = ReachabilityLedger()
    await book.settle(CAP, ok=False, detail="x")
    assert (await book.state(CAP)).failing


async def test_only_transitions_are_durable(tmp_path):
    path = tmp_path / "reach.jsonl"
    book = ReachabilityLedger(path=path)
    for _ in range(50):
        await book.record(CAP, Tier.INVOKED, durable=False)
        await book.settle(CAP, ok=False, detail="down")
    await book.settle(CAP, ok=True)
    lines = _lines(path)
    assert [(ln["tier"], ln.get("health", "")) for ln in lines] == [
        ("failed", "first"), ("failed", "alarm"), ("recovered", ""),
    ], "a path down for hours must cost three lines, not one per call"


async def test_record_routes_outcome_tiers_through_the_streak():
    book = ReachabilityLedger()
    for _ in range(3):
        await book.record(CAP, Tier.FAILED, detail="x")
    assert (await book.state(CAP)).failing


# ---------------------------------------------------------------------------
# track_reachability settles every ending -- except cancellation
# ---------------------------------------------------------------------------

async def test_a_raise_is_a_failure_and_still_raises():
    book = ReachabilityLedger()
    for _ in range(3):
        with pytest.raises(ValueError):
            async with track_reachability(CAP, ledger=book):
                raise ValueError("boom")
    state = await book.state(CAP)
    assert state.failing and state.last_failure == "ValueError: boom"


async def test_a_fail_soft_sentinel_is_a_failure():
    book = ReachabilityLedger()
    async with track_reachability(CAP, ledger=book) as effect:
        effect.fail("returned None")
    assert (await book.state(CAP)).failed == 1


async def test_a_normal_exit_resets_the_streak():
    book = ReachabilityLedger()
    await book.settle(CAP, ok=False, detail="x")
    async with track_reachability(CAP, ledger=book):
        pass
    assert (await book.state(CAP)).consecutive_failures == 0


async def test_cancellation_is_neither_a_failure_nor_a_success():
    book = ReachabilityLedger()
    await book.settle(CAP, ok=False, detail="x")
    await book.settle(CAP, ok=False, detail="x")

    async def _body():
        async with track_reachability(CAP, ledger=book):
            await asyncio.sleep(10)

    task = asyncio.ensure_future(_body())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await book.state(CAP)
    assert state.consecutive_failures == 2, "a torn-down op must not move the streak"


def test_the_ledger_is_safe_across_event_loops_and_threads():
    """offload settles here from many loops and worker threads; an
    asyncio.Lock would bind to the first and raise on the others."""
    book = ReachabilityLedger()

    def _worker():
        async def _go():
            for _ in range(200):
                await book.settle(CAP, ok=False, detail="x")
                await book.settle(CAP, ok=True)
        asyncio.run(_go())

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert asyncio.run(book.state(CAP)).failed == 800


# ---------------------------------------------------------------------------
# The seam: offload, replaying the soak
# ---------------------------------------------------------------------------

def _raise_permission() -> None:
    raise PermissionError("denied")


def _ok() -> int:
    return 7


def _kill_own_worker() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def test_a_thread_offload_failing_every_call_alarms_and_recovers():
    for _ in range(3):
        assert cfi.is_offload_error(await cfi.offload(_raise_permission))
    cap = cfi._offload_capability(_raise_permission, False)
    state = await RL.default_ledger().state(cap)
    assert state.failing and "PermissionError" in state.last_failure
    assert await cfi.offload(_ok) == 7
    assert not (await RL.default_ledger().state(cfi._offload_capability(_ok, False))).failing


async def test_a_dead_process_pool_alarms_by_name_and_reaches_the_summary(tmp_path, caplog):
    """The soak's shape, end to end: a cpu-bound offload that cannot get a
    live worker, call after call."""
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

    with caplog.at_level(logging.WARNING, logger="Ouroboros.Reachability"):
        for _ in range(3):
            got = await cfi.offload(_kill_own_worker, cpu_bound=True)
            assert got.exc_type == "BrokenProcessPool"
    cap = cfi._offload_capability(_kill_own_worker, True)
    assert cap.startswith("offload.process:") and cap.endswith("_kill_own_worker")
    assert any("HEALTH ALARM" in r.getMessage() and cap in r.getMessage() for r in caplog.records)

    summary = json.loads(SessionRecorder(session_id="bt-health").save_summary(
        output_dir=tmp_path, stop_reason="wall_clock_cap", duration_s=1.0,
        cost_total=0.0, cost_breakdown={}, branch_stats={},
        convergence_state="INSUFFICIENT_DATA", convergence_slope=0.0,
        convergence_r2=0.0,
    ).read_text())
    failing = summary["capability_health"]["failing"]
    assert [f["capability"] for f in failing] == [cap]
    assert failing[0]["consecutive_failures"] == 3


def test_a_healthy_session_adds_no_health_key(tmp_path):
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

    summary = json.loads(SessionRecorder(session_id="bt-quiet").save_summary(
        output_dir=tmp_path, stop_reason="wall_clock_cap", duration_s=1.0,
        cost_total=0.0, cost_breakdown={}, branch_stats={},
        convergence_state="INSUFFICIENT_DATA", convergence_slope=0.0,
        convergence_r2=0.0,
    ).read_text())
    assert "capability_health" not in summary


def test_a_partial_offload_is_named_for_what_it_calls():
    import functools

    assert cfi._offload_capability(functools.partial(_ok), False) == cfi._offload_capability(_ok, False)


# ---------------------------------------------------------------------------
# The cockpit panel shows it
# ---------------------------------------------------------------------------

def test_the_panel_puts_a_failing_capability_first():
    from backend.core.ouroboros.cli.ov_reachability_panel import aggregate, render_rows

    lines = [json.dumps(r) for r in (
        {"capability": "offload.process:x.f", "tier": "failed", "health": "first", "detail": "BrokenProcessPool"},
        {"capability": "offload.process:x.f", "tier": "failed", "health": "alarm", "detail": "BrokenProcessPool"},
        {"capability": "other", "tier": "invoked", "detail": ""},
        {"capability": "other", "tier": "effective", "detail": ""},
    )]
    model = aggregate(lines)
    assert [r.capability for r in model.failing] == ["offload.process:x.f"]
    rendered = render_rows(model)
    assert rendered[0].startswith("FAILING")
    recovered = aggregate(lines + [json.dumps(
        {"capability": "offload.process:x.f", "tier": "recovered", "detail": "after 3"},
    )])
    assert not recovered.failing
