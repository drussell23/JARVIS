"""The memory gate sees the HOST, and the Sentinel listens to the gate.

Soak bt-2026-09-20-183259 was stopped for host memory pressure while, at the
same instant, the WSL guest read 47.9 of 49.3 GB free (97%) and Windows commit
read 40.9 of 105.6 GB free (39%). ``psutil`` inside the guest measures the
first number. The desktop dies on the second. And the Sentinel — the component
that starts work — had never asked the gate anything.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import host_commit_probe as hp
from backend.core.ouroboros.governance import memory_pressure_gate as mpg
from backend.core.ouroboros.governance import process_session as ps
from backend.core.ouroboros.governance.autonomy.sentinel_loop import SentinelLoop
from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel


def _sample(free_pct: float) -> hp.HostCommit:
    limit = 100_000_000
    return hp.HostCommit(int(limit * free_pct / 100.0), limit, 1, 2, time.monotonic())


@pytest.fixture(autouse=True)
def _no_live_sampler():
    hp.stop_sampler()
    yield
    hp.stop_sampler()


# ---------------------------------------------------------------------------
# Reading the host
# ---------------------------------------------------------------------------


def test_parse_reads_commit_free_over_commit_limit():
    got = hp.parse("40953076 105607136 32836476 64647136\r\n", 1.0)
    assert got is not None and got.free_pct == pytest.approx(38.78, abs=0.01)


@pytest.mark.parametrize("raw", ["", "garbage", "1 0 1 1", "-5 100 1 1", "200 100 1 1", "1 2"])
def test_a_malformed_read_is_unknown_not_a_number(raw):
    assert hp.parse(raw, 1.0) is None


def test_the_cadence_tightens_as_commit_falls():
    s = hp.HostCommitSampler(warn_pct=lambda: 30.0, critical_pct=lambda: 10.0, reader=lambda: None)
    healthy, close, critical = s.interval_for(60.0), s.interval_for(20.0), s.interval_for(5.0)
    assert healthy > close > critical
    assert s.interval_for(None) == healthy  # unknown is not an emergency


def test_inverted_thresholds_do_not_divide_by_zero():
    s = hp.HostCommitSampler(warn_pct=lambda: 10.0, critical_pct=lambda: 10.0, reader=lambda: None)
    assert s.interval_for(5.0) > 0


def test_a_stale_sample_is_not_acted_on(monkeypatch):
    monkeypatch.setenv("JARVIS_HOST_COMMIT_INTERVAL_S", "1")
    monkeypatch.setenv("JARVIS_HOST_COMMIT_TIMEOUT_S", "1")
    old = hp.HostCommit(1, 100, 1, 2, time.monotonic() - 3600)
    s = hp.HostCommitSampler(warn_pct=lambda: 30.0, critical_pct=lambda: 10.0, reader=lambda: old)
    s.sample_once()
    assert s.latest() is None


def test_a_failing_reader_is_counted_and_survived():
    def boom():
        raise OSError("interop down")

    s = hp.HostCommitSampler(warn_pct=lambda: 30.0, critical_pct=lambda: 10.0, reader=boom)
    assert s.sample_once() is None and s.failures == 1 and s.latest() is None


def test_the_sampler_runs_on_its_own_thread_and_stops():
    seen = []

    def reader():
        seen.append(1)
        return _sample(50.0)

    s = hp.HostCommitSampler(warn_pct=lambda: 30.0, critical_pct=lambda: 10.0, reader=reader)
    t0 = time.monotonic()
    s.start()
    assert time.monotonic() - t0 < 0.5, "start() blocked on the read"
    for _ in range(50):
        if s.latest() is not None:
            break
        time.sleep(0.02)
    s.stop()
    assert s.latest() is not None and seen


def test_reading_the_gate_never_starts_a_sampler():
    """A unit test or a CLI touching ``pressure()`` must not begin spawning
    PowerShell. Arming is explicit, at daemon boot."""
    mpg.MemoryPressureGate().pressure()
    assert hp._sampler is None


# ---------------------------------------------------------------------------
# The gate's new dimension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("free_pct, expected", [
    (60.0, PressureLevel.OK), (25.0, PressureLevel.WARN),
    (15.0, PressureLevel.HIGH), (5.0, PressureLevel.CRITICAL),
])
def test_host_commit_is_graded_on_the_gates_own_ladder(monkeypatch, free_pct, expected):
    monkeypatch.setattr(hp, "latest_sample", lambda: _sample(free_pct))
    level, seen = mpg.MemoryPressureGate()._host_commit_dim()
    assert level is expected and seen == pytest.approx(free_pct)


def test_a_healthy_guest_no_longer_hides_a_dying_host(monkeypatch):
    """The exact blind spot: guest 97% free, host critical."""
    healthy_guest = mpg.MemoryProbe(
        free_pct=97.0, total_bytes=49 * 2**30, available_bytes=47 * 2**30, source="test", ok=True,
    )
    gate = mpg.MemoryPressureGate(probe_fn=lambda: healthy_guest)
    monkeypatch.setattr(hp, "latest_sample", lambda: None)
    assert gate.pressure() is PressureLevel.OK
    monkeypatch.setattr(hp, "latest_sample", lambda: _sample(5.0))
    assert gate.pressure() is PressureLevel.CRITICAL
    assert gate.can_fanout(8).n_allowed == mpg.critical_fanout_cap()


def test_no_host_reading_changes_nothing(monkeypatch):
    monkeypatch.setattr(hp, "latest_sample", lambda: None)
    assert mpg.MemoryPressureGate()._host_commit_dim() == (PressureLevel.OK, None)


def test_the_dimension_has_its_own_switch(monkeypatch):
    monkeypatch.setattr(hp, "latest_sample", lambda: _sample(1.0))
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_HOST_COMMIT_DIM_ENABLED", "false")
    assert mpg.MemoryPressureGate()._host_commit_dim() == (PressureLevel.OK, None)


# ---------------------------------------------------------------------------
# The Sentinel sheds — with hysteresis
# ---------------------------------------------------------------------------


class _Gate:
    def __init__(self):
        self.level = PressureLevel.OK

    def pressure(self):
        return self.level


@pytest.fixture
def loop_and_gate(tmp_path, monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(mpg, "get_default_gate", lambda: gate)
    shed = []
    monkeypatch.setattr(ps, "shed_live_sessions", lambda reason="": shed.append(reason) or ())

    async def dispatch(*_a, **_k):
        return None

    loop = SentinelLoop(repo_root=tmp_path, dispatch=dispatch, watcher=None, observer=lambda _o: None)
    return loop, gate, shed


@pytest.mark.asyncio
async def test_no_pressure_no_shedding(loop_and_gate):
    loop, _gate, shed = loop_and_gate
    assert await loop._memory_shedding() == "" and shed == []


@pytest.mark.asyncio
async def test_high_pauses_dispatch_but_kills_nothing(loop_and_gate):
    loop, gate, shed = loop_and_gate
    gate.level = PressureLevel.HIGH
    assert "high" in await loop._memory_shedding()
    assert shed == []


@pytest.mark.asyncio
async def test_critical_also_ends_live_sessions(loop_and_gate):
    loop, gate, shed = loop_and_gate
    gate.level = PressureLevel.CRITICAL
    assert "critical" in await loop._memory_shedding()
    assert len(shed) == 1


@pytest.mark.asyncio
async def test_it_does_not_resume_at_warn_only_at_ok(loop_and_gate):
    """Starting and stopping on one boundary flaps: dispatch, tip over, kill,
    recover, dispatch again."""
    loop, gate, _shed = loop_and_gate
    gate.level = PressureLevel.HIGH
    assert await loop._memory_shedding()
    gate.level = PressureLevel.WARN
    assert "recovering" in await loop._memory_shedding()
    gate.level = PressureLevel.OK
    assert await loop._memory_shedding() == ""
    gate.level = PressureLevel.WARN
    assert await loop._memory_shedding() == "", "WARN alone must never START shedding"


@pytest.mark.asyncio
async def test_a_shedding_pass_dispatches_nothing_and_is_not_counted(loop_and_gate, monkeypatch):
    loop, gate, _shed = loop_and_gate
    gate.level = PressureLevel.CRITICAL
    outcome = await loop.run_once()
    assert outcome.state == "shedding"
    monkeypatch.setattr(loop, "_shed_recheck_s", lambda: 0.01)
    task = asyncio.create_task(loop._run())
    await asyncio.sleep(0.15)
    loop._stopping.set()
    await asyncio.wait_for(task, timeout=2)
    assert loop.passes == 0


@pytest.mark.asyncio
async def test_a_broken_gate_never_stops_the_loop(loop_and_gate, monkeypatch):
    loop, _gate, _shed = loop_and_gate

    def boom():
        raise RuntimeError("gauge exploded")

    monkeypatch.setattr(mpg, "get_default_gate", boom)
    assert await loop._memory_shedding() == ""


# ---------------------------------------------------------------------------
# A shed run is infrastructure, not a failure
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions")
def test_a_shed_session_is_ended_and_remembered_once():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        ps.register_session(proc.pid, "t")
        assert proc.pid in ps.shed_live_sessions("test")
        proc.wait(timeout=5)
        assert ps.was_shed(proc.pid) is True
        assert ps.was_shed(proc.pid) is False, "the mark is consumed by whoever classifies the run"
    finally:
        ps.unregister_session(proc.pid)
        if proc.poll() is None:
            proc.kill()


def test_an_unregistered_session_is_never_shed():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        assert proc.pid not in ps.shed_live_sessions("test")
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
@pytest.mark.asyncio
async def test_a_shed_test_run_is_reported_as_infra(tmp_path):
    from backend.core.ouroboros.governance.test_runner import TestRunner

    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_slow.py"
    test.write_text("import time\n\ndef test_slow():\n    time.sleep(60)\n")
    runner = TestRunner(repo_root=tmp_path, timeout=120.0, per_test_timeout_s=90)

    async def shed_soon():
        for _ in range(200):
            await asyncio.sleep(0.05)
            if ps._live:
                await asyncio.sleep(0.5)
                ps.shed_live_sessions("test")
                return

    shedder = asyncio.create_task(shed_soon())
    result = await runner.run(test_files=(test,), sandbox_dir=None)
    await shedder
    assert result.passed is False
    assert result.timed_out is True, "a run WE killed was filed as the model's failure"
    assert "memory-pressure shedding" in (result.stdout or "")
