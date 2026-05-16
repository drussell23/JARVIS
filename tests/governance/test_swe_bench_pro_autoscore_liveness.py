"""Spine — Stage 2 Fix A: autoscore session-liveness + clean shutdown.

Root cause (v16 bt-2026-05-16-085224): the fire-and-forget autoscore
``parallel_evaluate`` task is invisible to the harness idle counter
(only GLS-op "progressing" pokes the IdleWatchdog). The session
idle-reaped at 24 min while a discriminator eval was still solving,
and force-cancelling the task mid-`async for` produced
``RuntimeError: aclose(): asynchronous generator is already running``.

Pins (beef, not theater):

  * ``autoscore_work_in_flight`` reflects pending driver tasks and is
    total (never raises) — it is an ActivityMonitor probe.
  * ``_any_session_liveness_probe_hot`` is fail-open per-probe.
  * Harness wiring (AST): the ActivityMonitor poke/starve decision
    has a probe-hot branch that pokes BEFORE the all-stale starve;
    the autoscore probe is registered on INJECTED_AUTOSCORE; a
    bounded autoscore drain runs in _shutdown_components.
  * ``_drive_parallel_evaluate`` owns the generator and closes it in
    its own context on cancellation — no leak / no "already running".
  * ``await_autoscore_drain`` is bounded and cancels stragglers.
  * Persistence round-trip: record() → JSONL → from_dict yields
    non-null nested outcomes (regression guard — proves the schema
    the v16 triage initially misread is actually sound; B was not a
    defect).
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import harness_inject


_HSRC = Path(
    __import__(
        "backend.core.ouroboros.battle_test.harness",
        fromlist=["__file__"],
    ).__file__
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Probe behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autoscore_work_in_flight_reflects_pending(monkeypatch):
    harness_inject._AUTOSCORE_DRIVER_TASKS.clear()
    assert harness_inject.autoscore_work_in_flight() is False

    ev = asyncio.Event()

    async def _slow():
        await ev.wait()

    t = asyncio.create_task(_slow())
    harness_inject._AUTOSCORE_DRIVER_TASKS.add(t)
    try:
        assert harness_inject.autoscore_work_in_flight() is True
    finally:
        ev.set()
        await t
        harness_inject._AUTOSCORE_DRIVER_TASKS.discard(t)
    assert harness_inject.autoscore_work_in_flight() is False


def test_autoscore_work_in_flight_is_total(monkeypatch):
    class _Boom:
        def __iter__(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(harness_inject, "_AUTOSCORE_DRIVER_TASKS", _Boom())
    # Must not raise — a probe that breaks the ActivityMonitor is fatal
    assert harness_inject.autoscore_work_in_flight() is False


# ---------------------------------------------------------------------------
# Driver owns + closes the generator on cancellation (no leak)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_closes_generator_on_cancel(monkeypatch):
    closed = {"v": False}

    async def _fake_pe(specs, *, intake_service, **k):
        try:
            i = 0
            while True:
                yield type("R", (), {
                    "evaluation": type("E", (), {
                        "instance_id": f"i{i}",
                        "outcome": type("O", (), {"value": "unresolved"})(),
                    })(),
                    "scoring": type("S", (), {
                        "problem_instance_id": f"i{i}",
                        "outcome": type("O", (), {"value": "skipped"})(),
                        "diagnostic": "",
                    })(),
                })()
                i += 1
                await asyncio.sleep(0.01)
        finally:
            closed["v"] = True  # generator's own finally ran

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "parallel_eval.parallel_evaluate", _fake_pe)

    task = asyncio.create_task(
        harness_inject._drive_parallel_evaluate([object()], object())
    )
    await asyncio.sleep(0.05)  # let it consume a few records
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The driver's finally closed the generator in-context → the
    # fake generator's own finally ran (no dangling/leak).
    assert closed["v"] is True


@pytest.mark.asyncio
async def test_await_autoscore_drain_bounded_and_cancels(monkeypatch):
    harness_inject._AUTOSCORE_DRIVER_TASKS.clear()
    # no tasks → returns immediately
    await harness_inject.await_autoscore_drain(grace_s=0.0)

    never = asyncio.Event()

    async def _hang():
        await never.wait()

    t = asyncio.create_task(_hang())
    harness_inject._AUTOSCORE_DRIVER_TASKS.add(t)
    # grace 0 → straight to cancel + await straggler; must return
    await asyncio.wait_for(
        harness_inject.await_autoscore_drain(grace_s=0.0), timeout=2.0
    )
    assert t.cancelled() or t.done()
    harness_inject._AUTOSCORE_DRIVER_TASKS.discard(t)


def test_driver_has_finally_aclose():
    src = Path(harness_inject.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_drive_parallel_evaluate"
    )
    seg = ast.get_source_segment(src, fn) or ""
    assert "agen = parallel_evaluate(" in seg, (
        "driver must OWN an explicit generator ref (close it in "
        "its own context, not a bare async-for)"
    )
    assert "finally:" in seg and "agen.aclose()" in seg, (
        "driver must aclose the generator in a finally"
    )


# ---------------------------------------------------------------------------
# Harness wiring (AST/source — avoids booting the full harness)
# ---------------------------------------------------------------------------


def test_harness_probe_branch_pokes_before_starve():
    poke_if = _HSRC.index("if progressing_count > 0:")
    probe_elif = _HSRC.index("_any_session_liveness_probe_hot()")
    starve = _HSRC.index("ALL %d ops are stale — NOT poking watchdog")
    assert poke_if < probe_elif < starve, (
        "probe-hot branch must sit between the progressing-poke and "
        "the all-stale starve (source order)"
    )
    window = _HSRC[probe_elif:starve]
    assert "self._idle_watchdog.poke()" in window, (
        "probe-hot branch must poke the watchdog"
    )


def test_harness_registers_autoscore_probe_on_autoscore_verdict():
    assert "register_session_liveness_probe(" in _HSRC
    assert "autoscore_work_in_flight" in _HSRC
    assert "SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE" in _HSRC
    # registration gated by the autoscore verdict
    reg = _HSRC.index("register_session_liveness_probe(\n")
    gate = _HSRC.rindex("INJECTED_AUTOSCORE", 0, reg)
    assert gate < reg


def test_harness_shutdown_has_bounded_autoscore_drain():
    assert "await_autoscore_drain" in _HSRC
    drain = _HSRC.index("await await_autoscore_drain(")
    shut = _HSRC.index("async def _shutdown_components")
    assert shut < drain, "drain must live inside _shutdown_components"


# ---------------------------------------------------------------------------
# Persistence round-trip (mandate item — proves B is sound, not a defect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_roundtrip_nonnull_nested(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
        EvaluationResultStore,
        EvaluationRecord,
    )
    from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
        EvaluationResult,
        EvaluationOutcome,
    )
    from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
        ScoringResult,
        ScoreOutcome,
    )

    p = tmp_path / "results.jsonl"
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_RESULT_PATH", str(p))

    store = EvaluationResultStore()
    ev = EvaluationResult(
        outcome=EvaluationOutcome.UNRESOLVED,
        problem_instance_id="org__x-1",
        op_id="op-1",
    )
    sc = ScoringResult(
        outcome=ScoreOutcome.SKIPPED,
        problem_instance_id="org__x-1",
        diagnostic="evaluation_outcome=unresolved",
    )
    await store.record(ev, sc)

    raw = [l for l in p.read_text().splitlines() if l.strip()]
    assert len(raw) == 1
    payload = json.loads(raw[0])
    # Nested schema — the level the v16 triage initially misread
    assert payload["evaluation"]["outcome"] == "unresolved"
    assert payload["evaluation"]["problem_instance_id"] == "org__x-1"
    assert payload["scoring"]["outcome"] == "skipped"
    rec = EvaluationRecord.from_dict(payload)
    assert rec.evaluation.outcome == EvaluationOutcome.UNRESOLVED
    assert rec.scoring.outcome == ScoreOutcome.SKIPPED
