"""Runtime health: CPU work never falls onto the loop; a leak names itself.

bt-2026-09-22-201845 measured both:

* 152 ``sibling_entropy`` difflib stalls (up to 10 s each, ~345 s of blocked
  event loop) -- every one the CPU-bound fallback running IN-PROCESS after the
  process pool broke. Offload now degrades to the thread pool instead.
* The daemon's own heap grew 2.2 -> 3.9 GB in 3.7 h and nothing could say
  where. The watchdog now starts tracemalloc only when growth is sustained,
  linear and significant, and reports the growing allocation sites.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
import time
import tracemalloc

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io as cfi
from backend.core.ouroboros.governance import memory_growth_tracer as MGT
from backend.core.ouroboros.governance import reachability_ledger as RL
from backend.core.ouroboros.governance.memory_growth_tracer import (
    MemoryGrowthTracer,
    fit_growth,
    growth_verdict,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("JARVIS_MEMTRACE_", "JARVIS_OFFLOAD_THREAD_FALLBACK")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(RL, "_default", None)
    cfi.shutdown_fs_process_pool()
    yield
    cfi.shutdown_fs_process_pool()
    if tracemalloc.is_tracing():
        tracemalloc.stop()


# Module-level: the process pool imports these by reference.
def _where() -> tuple:
    return os.getpid(), threading.get_ident()


def _busy(seconds: float) -> int:
    end = time.monotonic() + seconds
    n = 0
    while time.monotonic() < end:
        n += 1
    return n


def _kill_own_worker() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _no_pool():
    raise OSError("spawn refused")


# ---------------------------------------------------------------------------
# Phase 1 — a pool that cannot take the work degrades to threads, not the loop
# ---------------------------------------------------------------------------

async def test_an_unavailable_pool_runs_the_work_on_a_thread(monkeypatch):
    monkeypatch.setattr(cfi, "_get_fs_process_pool", _no_pool)
    pid, tid = await cfi.offload(_where, cpu_bound=True)
    assert pid == os.getpid(), "expected the in-process thread fallback"
    assert tid != threading.get_ident(), "the work ran ON the event loop thread"


async def test_the_loop_keeps_scheduling_while_the_fallback_computes(monkeypatch):
    monkeypatch.setattr(cfi, "_get_fs_process_pool", _no_pool)
    ticks = 0
    stop = asyncio.Event()

    async def _ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.ensure_future(_ticker())
    await cfi.offload(_busy, 1.0, cpu_bound=True)
    stop.set()
    await ticker
    assert ticks >= 20, f"the loop was starved: {ticks} ticks in 1 s of CPU work"


async def test_a_pool_shut_down_under_the_submit_degrades_too(monkeypatch):
    class _ShutPool:
        _processes = {}

        def submit(self, *a, **k):
            raise RuntimeError("cannot schedule new futures after shutdown")

        def shutdown(self, *a, **k):
            pass

    monkeypatch.setattr(cfi, "_get_fs_process_pool", lambda: _ShutPool())
    pid, _ = await cfi.offload(_where, cpu_bound=True)
    assert pid == os.getpid()


async def test_a_fn_that_kills_its_worker_never_runs_in_the_daemon():
    got = await cfi.offload(_kill_own_worker, cpu_bound=True)
    # Reaching this line at all is the proof: run in-process, it would have
    # SIGKILLed the test runner.
    assert cfi.is_offload_error(got) and got.exc_type == "BrokenProcessPool"


async def test_the_fallback_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_OFFLOAD_THREAD_FALLBACK_ENABLED", "false")
    monkeypatch.setattr(cfi, "_get_fs_process_pool", _no_pool)
    assert cfi.is_offload_error(await cfi.offload(_where, cpu_bound=True))


async def test_a_masked_pool_outage_still_raises_its_own_alarm(monkeypatch):
    """The fallback succeeds, so per-fn health reads green; the pool's own
    capability must not."""
    monkeypatch.setattr(cfi, "_get_fs_process_pool", _no_pool)
    for _ in range(3):
        await cfi.offload(_where, cpu_bound=True)
    assert (await RL.default_ledger().state("offload.process_pool")).failing


async def test_sibling_entropy_falls_back_to_a_thread_not_the_loop(monkeypatch):
    from backend.core.ouroboros.governance import sibling_entropy as SE

    calls = []
    real = cfi.offload

    async def _spy(fn, *args, cpu_bound=False, **kw):
        calls.append(cpu_bound)
        if cpu_bound:
            return cfi.OffloadError(fn_name="x", exc_type="BrokenProcessPool", message="dead", cpu_bound=True)
        return await real(fn, *args, cpu_bound=False, **kw)

    monkeypatch.setattr(cfi, "offload", _spy)
    monkeypatch.setattr(SE, "entropy_enabled", lambda: True)
    got = await SE.is_structurally_redundant_async(["a b c"], ["a b c"], threshold=0.5)
    assert calls == [True, False], f"expected process then thread, got {calls}"
    assert isinstance(got, tuple)


# ---------------------------------------------------------------------------
# Phase 2 — the trigger
# ---------------------------------------------------------------------------

def _line(n, start_mb, mb_per_s, span_s, noise=0.0):
    return [
        (i * span_s / (n - 1), start_mb + mb_per_s * i * span_s / (n - 1) + (noise if i % 2 else -noise))
        for i in range(n)
    ]


def test_a_steady_leak_trips_the_trigger():
    # The soak's shape: ~450 MB/h on a ~3 GB process, over a 30-min window.
    fit = fit_growth(_line(120, 3000.0, 0.125, 1800.0, noise=5.0))
    ok, why = growth_verdict(fit, window=1800.0)
    assert ok, why
    assert 400 < fit.slope_mb_per_h < 500


@pytest.mark.parametrize("samples,why", [
    (_line(120, 3000.0, 0.0, 1800.0, noise=40.0), "not growing|noisy|noise"),
    (_line(120, 3000.0, 0.01, 1800.0, noise=40.0), "noisy|noise|floor"),
    (_line(120, 3000.0, 0.125, 600.0), "window"),
])
def test_noise_flat_or_short_windows_do_not(samples, why):
    import re

    ok, reason = growth_verdict(fit_growth(samples), window=1800.0)
    assert not ok and re.search(why, reason), reason


def test_warm_up_growth_is_not_a_leak(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMTRACE_WINDOW_S", "100")
    clock = iter(range(0, 10_000))
    tr = MemoryGrowthTracer(clock=lambda: 0.0, started_at=0.0)
    for t in range(0, 99, 3):  # all inside the first window
        assert tr.observe(1000.0 + t * 10, now=float(t)) is None


def test_a_full_window_of_growth_after_warm_up_trips(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMTRACE_WINDOW_S", "100")
    tr = MemoryGrowthTracer(clock=lambda: 0.0, started_at=0.0)
    fits = [tr.observe(1000.0 + t * 2.0, now=float(t)) for t in range(0, 260, 2)]
    assert any(f is not None for f in fits)


# ---------------------------------------------------------------------------
# Phase 2 end to end — a REAL leak is named by file:line
# ---------------------------------------------------------------------------

_LEAK: list = []


def _leak_some(n: int) -> None:
    for _ in range(n):
        _LEAK.append(bytearray(64 * 1024))  # THE LEAK LINE


_LEAK_LINE = next(
    i for i, l in enumerate(open(__file__).read().splitlines(), 1) if "THE LEAK LINE" in l
)


async def _drive(tr: MemoryGrowthTracer, rss_values, *, warn_mb=None, leak=True):
    from backend.core.ouroboros.governance import worker_lifeline as WL

    it = iter(rss_values)
    orig = WL._probe_self_memory_mb
    WL._probe_self_memory_mb = lambda: next(it)
    try:
        for _ in rss_values:
            if leak:
                _leak_some(20)
            await tr.tick(warn_mb=warn_mb)
    finally:
        WL._probe_self_memory_mb = orig


async def test_a_real_leak_is_attributed_to_its_line(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_MEMTRACE_WINDOW_S", "10")
    monkeypatch.setenv("JARVIS_MEMTRACE_CAPTURE_S", "0.0001")
    monkeypatch.setenv("JARVIS_MEMTRACE_REPORTS", "2")
    now = [0.0]

    def _clock():
        now[0] += 1.0
        return now[0]

    tr = MemoryGrowthTracer(clock=_clock, started_at=0.0)
    monkeypatch.setattr(MGT, "_default", tr)
    _LEAK.clear()
    await _drive(tr, [2000.0 + 20.0 * i for i in range(40)])
    report = tr.report()
    assert report, "sustained linear growth never triggered"
    sites = [s["site"] for r in report["reports"] for s in r["top_growth"]]
    assert any(s.endswith(f"test_runtime_health.py:{_LEAK_LINE}") for s in sites), sites
    assert "attribution complete" in report["stopped"]
    assert not tracemalloc.is_tracing(), "tracing was left running after attribution"

    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

    summary = json.loads(SessionRecorder(session_id="bt-leak").save_summary(
        output_dir=tmp_path, stop_reason="wall_clock_cap", duration_s=1.0,
        cost_total=0.0, cost_breakdown={}, branch_stats={},
        convergence_state="INSUFFICIENT_DATA", convergence_slope=0.0, convergence_r2=0.0,
    ).read_text())
    assert summary["memory_growth"]["reports"], "the attribution never reached summary.json"
    _LEAK.clear()


# ---------------------------------------------------------------------------
# Phase 3 — the diagnostic cannot become the incident
# ---------------------------------------------------------------------------

def _fast(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMTRACE_WINDOW_S", "10")
    monkeypatch.setenv("JARVIS_MEMTRACE_CAPTURE_S", "0.0001")
    now = [0.0]

    def _clock():
        now[0] += 1.0
        return now[0]
    return MemoryGrowthTracer(clock=_clock, started_at=0.0)


async def test_tracing_stops_itself_past_its_overhead_budget(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMTRACE_OVERHEAD_FRACTION", "0.0000001")
    tr = _fast(monkeypatch)
    await _drive(tr, [2000.0 + 20.0 * i for i in range(30)])
    assert "overhead" in tr.report()["stopped"]
    assert not tracemalloc.is_tracing()


async def test_it_will_not_start_next_to_the_warn_line(monkeypatch):
    tr = _fast(monkeypatch)
    await _drive(tr, [2000.0 + 20.0 * i for i in range(30)], warn_mb=2100.0)
    assert "not started" in tr.report()["stopped"]
    assert not tracemalloc.is_tracing()


async def test_someone_elses_tracemalloc_is_never_stopped(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMTRACE_REPORTS", "1")
    tracemalloc.start(1)
    tr = _fast(monkeypatch)
    await _drive(tr, [2000.0 + 20.0 * i for i in range(30)])
    assert tr.report()["stopped"], "tracing never ran to completion"
    assert tracemalloc.is_tracing(), "the tracer stopped a tracemalloc it did not own"


async def test_a_flat_process_never_pays_for_tracing(monkeypatch):
    tr = _fast(monkeypatch)
    await _drive(tr, [2000.0] * 30, leak=False)
    assert tr.report() == {} and not tracemalloc.is_tracing()
