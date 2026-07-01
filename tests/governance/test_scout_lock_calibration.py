"""Asynchronous Calibration Mutex (Scout Lock) + Dynamic Global Audit Ceiling.

The BG pool dispatches concurrently, so on a cold profiler ALL ops read ewma=0 and
cold-start together -- the escalation never converges (thundering herd). The Scout
Lock fixes this WITHOUT serializing the DAG: the FIRST cold coroutine acquires the
lock and dispatches as the Scout; the herd awaits the lock; once the Scout finishes
(success or timeout+escalate) it marks the profiler calibrated and releases, and the
herd runs CONCURRENTLY reading the escalated EWMA seed. Steady-state = full
concurrency (calibrated -> no lock).

The audit ceiling then derives dynamically from the observed per-call budget x the
expected max agentic rounds (no hardcoded flat limit).
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


# --- Scout Lock primitives --------------------------------------------------

def test_profiler_starts_uncalibrated():
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    assert prof.is_calibrated() is False
    prof.mark_calibrated()
    assert prof.is_calibrated() is True


def test_calibrated_shortcircuits_no_lock():
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    prof.mark_calibrated()
    calls = []

    async def _factory():
        calls.append(1)
        return "ok"

    assert asyncio.run(prof.run_calibrated(_factory)) == "ok"
    assert calls == [1]


def test_scout_runs_alone_then_herd_concurrent():
    """Only the Scout runs while uncalibrated; the herd waits on the lock, then
    runs concurrently once the Scout marks calibrated."""
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    events = []
    scout_release = asyncio.Event()

    async def _run():
        async def _factory():
            is_scout = not prof.is_calibrated()
            events.append("start")
            if is_scout:
                await scout_release.wait()   # Scout holds the lock here
            events.append("end")
            return "ok"

        tasks = [asyncio.create_task(prof.run_calibrated(_factory)) for _ in range(3)]
        await asyncio.sleep(0.05)
        # Only the Scout has started; the herd is blocked on the scout lock.
        mid = events.count("start")
        scout_release.set()
        results = await asyncio.gather(*tasks)
        return mid, results

    mid, results = asyncio.run(_run())
    assert mid == 1                       # exactly one Scout ran while cold
    assert results == ["ok", "ok", "ok"]
    assert events.count("start") == 3 and events.count("end") == 3
    assert prof.is_calibrated() is True


def test_scout_marks_calibrated_even_on_exception():
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))

    async def _boom():
        raise RuntimeError("scout dispatch failed/timed out")

    with pytest.raises(RuntimeError):
        asyncio.run(prof.run_calibrated(_boom))
    assert prof.is_calibrated() is True    # calibrated in finally -> herd not stuck


# --- Dynamic Global Audit Ceiling -------------------------------------------

def test_audit_ceiling_derives_from_observed_budget(tmp_path, monkeypatch):
    import scripts.isomorphic_a1_local as iso
    monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
    log = tmp_path / "debug.log"
    log.write_text(
        "some line\n"
        "local_inference timeout: budget=243750ms warm=False\n"
        "[LocalPrimeClient] adaptive inference budget=366000ms warm=False\n"
    )
    ceiling = iso._a1_audit_ceiling_s(debug_log=str(log))
    # largest observed budget (366000ms) x 5 rounds = 1830s.
    assert ceiling == pytest.approx(366.0 * 5, rel=0.01)


def test_audit_ceiling_falls_back_without_budget(tmp_path):
    import scripts.isomorphic_a1_local as iso
    log = tmp_path / "empty.log"
    log.write_text("no budget lines here\n")
    # No observed budget -> heavy-scaled base (>= base), never crashes.
    ceiling = iso._a1_audit_ceiling_s(debug_log=str(log))
    assert ceiling >= _env_base()


def _env_base() -> float:
    import os
    try:
        return float(os.environ.get("JARVIS_A1_AUDIT_BASE_S", "300"))
    except ValueError:
        return 300.0
