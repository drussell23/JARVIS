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


def test_harness_probe_is_unconditional_single_seam():
    """A-fix-v2: the probe must be consulted BEFORE the
    `if not active_ops` early-out (so empty-active_ops cannot bypass
    it — the v16/v17 idle path) and EXACTLY ONCE in the activity
    monitor (single consult site; the redundant A-v1 elif is gone)."""
    # Anchor to the ActivityMonitor TICK gate specifically — the
    # `if not active_ops:` that immediately follows the tick's
    # `active_ops = getattr(gls, "_active_ops", set())`. (The
    # function has other unrelated `if not active_ops:` lines; a
    # bare .index() would match the wrong one — that imprecision is
    # itself what the v17 triage taught us to guard against.)
    tick_anchor = _HSRC.index(
        'active_ops: set = getattr(gls, "_active_ops", set())'
    )
    gate = _HSRC.index("if not active_ops:", tick_anchor)
    probe = _HSRC.index("_any_session_liveness_probe_hot()")
    assert probe < gate, (
        "probe must be evaluated BEFORE the tick's `if not "
        "active_ops: continue` gate — else empty active_ops "
        "bypasses it (the exact v16/v17 idle-reap path)"
    )
    # Exactly one consult site inside _run_activity_monitor — no
    # duplicate (the A-v1 elif must have been reverted to a plain
    # else).
    assert _HSRC.count("_any_session_liveness_probe_hot()") == 1, (
        "single probe-consult site only (no duplicate in-tree elif)"
    )
    # The unconditional check pokes when hot.
    seg = _HSRC[probe:probe + 220]
    assert "_probe_hot = self._any_session_liveness_probe_hot()" in (
        _HSRC[probe - 40:probe + 60]
    ) or "_probe_hot" in seg
    assert "self._idle_watchdog.poke()" in seg, (
        "a hot probe must poke the watchdog at the unconditional seam"
    )
    # The reverted branch is a plain `else:` (no probe elif remains).
    assert "elif self._any_session_liveness_probe_hot()" not in _HSRC


def test_harness_probe_falls_through_when_ops_present():
    """Refinement: poke when hot, but `continue` ONLY when not
    active_ops — when ops exist the normal progressing/stale
    classification (incl. stale-op force-cancel) must still run."""
    probe = _HSRC.index("_probe_hot = self._any_session_liveness")
    gate = _HSRC.index("if not active_ops:", probe)
    # Between the probe poke and the gate there is NO unconditional
    # `continue` (continue lives inside the `if not active_ops` block).
    between = _HSRC[_HSRC.index("self._idle_watchdog.poke()", probe):gate]
    assert "continue" not in between, (
        "must NOT continue right after the unconditional poke — only "
        "the `if not active_ops` branch continues"
    )


def test_any_session_liveness_probe_hot_behavioral_kernel():
    """Behavioral tick-kernel: when active_ops is empty the new
    code's ONLY action is `_probe_hot = _any_session_liveness_probe_
    hot(); if _probe_hot: poke`. Prove that decision kernel on a real
    method (via __new__ to avoid booting the full harness)."""
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness,
    )
    h = BattleTestHarness.__new__(BattleTestHarness)
    h._session_liveness_probes = []
    assert h._any_session_liveness_probe_hot() is False  # cold → no poke
    h._session_liveness_probes = [lambda: False, lambda: True]
    assert h._any_session_liveness_probe_hot() is True    # hot → poke
    # raising probe is fail-open (never breaks the monitor tick)
    h._session_liveness_probes = [
        lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    ]
    assert h._any_session_liveness_probe_hot() is False
    # register_session_liveness_probe wires a real probe in
    h._session_liveness_probes = []
    h.register_session_liveness_probe(harness_inject.autoscore_work_in_flight)
    harness_inject._AUTOSCORE_DRIVER_TASKS.clear()
    assert h._any_session_liveness_probe_hot() is False   # no tasks
    # (the pending-task → True path is covered by
    # test_autoscore_work_in_flight_reflects_pending)


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
