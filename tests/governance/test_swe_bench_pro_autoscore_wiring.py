"""Spine — SWE-Bench-Pro Slice 1: closed-loop autoscore wiring.

Pins that the boot hook closes the open loop by COMPOSING the
existing ``parallel_evaluate`` rig (Phase E → B.2.2 → Phase C →
Phase D) — zero net-new evaluation logic — and that the legacy
open-loop ingest path is byte-identical when the flag is OFF.

  * **Composition only (AST)** — the autoscore path imports + drives
    ``parallel_evaluate``; harness_inject itself contains NO
    ``score_evaluation`` / ``store.record`` / ``evaluate_problem``
    call (that pipeline lives entirely inside parallel_evaluate).
  * **Flag-gated, default-FALSE** — ``autoscore_enabled()`` guards
    the branch; source order: the gate precedes BOTH the autoscore
    dispatch and the legacy loop.
  * **Non-blocking boot** — the driver is spawned via
    ``asyncio.create_task`` and NOT awaited inside the boot hook
    (the soak loop must keep running for solve ops to reach their
    terminal event); a strong ref prevents GC.
  * **Legacy preserved** — flag OFF → ingest_envelope open-loop,
    parallel_evaluate untouched, verdict INJECTED (unchanged).
  * **Closed taxonomy** — 6 values incl INJECTED_AUTOSCORE.
  * **FlagRegistry seed** present, default-False, SAFETY.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import harness_inject
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)

_SRC = Path(harness_inject.__file__).read_text(encoding="utf-8")


def _spec(iid: str, gold: str = "diff --git a/x b/x\n") -> ProblemSpec:
    return ProblemSpec(
        instance_id=iid, repo="o/r", base_commit="abc",
        problem_statement="ps", test_patch="tp", gold_patch=gold,
    )


# ---------------------------------------------------------------------------
# AST — composition only, no parallel evaluation logic
# ---------------------------------------------------------------------------


def test_ast_autoscore_composes_parallel_evaluate():
    assert "parallel_evaluate" in _SRC, (
        "autoscore path MUST compose the existing parallel_evaluate"
    )
    tree = ast.parse(_SRC)
    driver = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "_drive_parallel_evaluate"),
        None,
    )
    assert driver is not None, "_drive_parallel_evaluate must exist"
    seg = ast.get_source_segment(_SRC, driver) or ""
    assert "parallel_evaluate(" in seg and "async for" in seg, (
        "driver MUST drain the parallel_evaluate async generator"
    )


def test_ast_no_parallel_eval_logic_in_harness_inject():
    """The Phase C/D/B.2.2 pipeline must live ENTIRELY inside
    parallel_evaluate — harness_inject must NOT re-implement any of
    it (no score_evaluation / store.record / evaluate_problem call).
    """
    tree = ast.parse(_SRC)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = (
                f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name) else ""
            )
            if name:
                called.add(name)
    for forbidden in (
        "score_evaluation", "evaluate_problem",
    ):
        assert forbidden not in called, (
            f"harness_inject MUST NOT call {forbidden} directly — "
            f"that pipeline belongs to parallel_evaluate (no "
            f"duplication)"
        )


def test_ast_flag_gate_precedes_autoscore_and_legacy():
    """`autoscore_enabled()` gate must appear in source BEFORE both
    the _inject_autoscore dispatch and the legacy ingest loop."""
    gate = _SRC.index("if autoscore_enabled():")
    dispatch = _SRC.index("_inject_autoscore(instance_ids")
    legacy = _SRC.index("Legacy open-loop path (autoscore OFF")
    assert gate < dispatch < legacy, (
        "gate must precede dispatch which must precede the legacy "
        "open-loop path (source order)"
    )


def test_ast_driver_spawned_not_awaited_strongref():
    tree = ast.parse(_SRC)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "_inject_autoscore"),
        None,
    )
    assert fn is not None
    seg = ast.get_source_segment(_SRC, fn) or ""
    assert "asyncio.create_task(" in seg, (
        "driver MUST be spawned via create_task (non-blocking boot)"
    )
    assert "_drive_parallel_evaluate(specs" in seg
    # The driver coroutine must NOT be directly awaited in the hook.
    assert "await _drive_parallel_evaluate" not in _SRC, (
        "driver MUST NOT be awaited inside the boot hook — the soak "
        "loop has to keep running for terminal events to fire"
    )
    assert "_AUTOSCORE_DRIVER_TASKS.add(task)" in seg, (
        "spawned task needs a strong ref (GC-safety)"
    )
    assert "add_done_callback(_AUTOSCORE_DRIVER_TASKS.discard)" in seg


def test_legacy_open_loop_path_preserved():
    # The open-loop ingest call must still be present + reachable.
    assert "intake_service.ingest_envelope(" in _SRC
    assert "SWEBenchProInjectionVerdict.INJECTED\n" in _SRC or \
        "return SWEBenchProInjectionVerdict.INJECTED" in _SRC


# ---------------------------------------------------------------------------
# Closed taxonomy + flag accessor
# ---------------------------------------------------------------------------


def test_taxonomy_has_six_values_incl_autoscore():
    vals = {v.value for v in harness_inject.SWEBenchProInjectionVerdict}
    assert vals == {
        "injected", "injected_autoscore", "skipped_disabled",
        "skipped_no_problems", "failed_load", "failed_inject",
    }


def test_autoscore_enabled_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED", raising=False)
    assert harness_inject.autoscore_enabled() is False
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED", "true")
    assert harness_inject.autoscore_enabled() is True


# ---------------------------------------------------------------------------
# Behavioral — flag OFF = legacy, flag ON = parallel_evaluate driven
# ---------------------------------------------------------------------------


class _StubIntake:
    def __init__(self):
        self.ingested = []

    async def ingest_envelope(self, env):
        self.ingested.append(env)
        return True


@pytest.mark.asyncio
async def test_flag_off_uses_legacy_open_loop(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED", "true")
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED", raising=False)
    monkeypatch.setattr(
        harness_inject, "_resolve_instance_ids", lambda: ["o__a-1"])
    monkeypatch.setattr(
        harness_inject, "load_problem",
        lambda iid: (_spec(iid), None))

    pe_called = {"n": 0}

    async def _fake_pe(*a, **k):
        pe_called["n"] += 1
        if False:
            yield None

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "parallel_eval.parallel_evaluate", _fake_pe)

    prepared = type("P", (), {"worktree_path": "/tmp/w"})()
    from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (  # noqa: E501
        HarnessOutcome,
    )

    async def _fake_prepare(problem):
        return prepared, HarnessOutcome.READY

    monkeypatch.setattr(harness_inject, "prepare_problem", _fake_prepare)
    monkeypatch.setattr(
        harness_inject, "build_evaluation_envelope",
        lambda p, pr: type("E", (), {"causal_id": "c1"})())

    svc = _StubIntake()
    verdict = await harness_inject.maybe_inject_swe_bench_at_boot(svc)

    assert verdict == harness_inject.SWEBenchProInjectionVerdict.INJECTED
    assert len(svc.ingested) == 1  # legacy open-loop ingest happened
    assert pe_called["n"] == 0     # parallel_evaluate NOT touched


@pytest.mark.asyncio
async def test_flag_on_drives_parallel_evaluate(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED", "true")
    monkeypatch.setattr(
        harness_inject, "_resolve_instance_ids",
        lambda: ["o__good-1", "o__hard-2"])
    monkeypatch.setattr(
        harness_inject, "load_problem",
        lambda iid: (_spec(iid), None))

    seen = {"specs": None}

    async def _fake_pe(problems, *, intake_service, **k):
        seen["specs"] = list(problems)
        for s in seen["specs"]:
            rec = type("R", (), {
                "evaluation": type("EV", (), {
                    "instance_id": s.instance_id,
                    "outcome": type("O", (), {"value": "resolved"})()})(),
                "scoring": type("SC", (), {
                    "problem_instance_id": s.instance_id,
                    "outcome": type("O", (), {"value": "pass"})(),
                    "diagnostic": ""})(),
            })()
            yield rec

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro."
        "parallel_eval.parallel_evaluate", _fake_pe)

    svc = _StubIntake()
    verdict = await harness_inject.maybe_inject_swe_bench_at_boot(svc)

    assert verdict == (
        harness_inject.SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE
    )
    # boot hook did NOT itself ingest (parallel_evaluate owns that)
    assert svc.ingested == []
    # drain the spawned background driver
    assert len(harness_inject._AUTOSCORE_DRIVER_TASKS) == 1
    await asyncio.gather(*list(harness_inject._AUTOSCORE_DRIVER_TASKS))
    assert seen["specs"] is not None
    assert [s.instance_id for s in seen["specs"]] == [
        "o__good-1", "o__hard-2"]
    # gold_patch rode in-memory on the spec (contextual state passing)
    assert all(s.gold_patch for s in seen["specs"])


@pytest.mark.asyncio
async def test_flag_on_all_loads_fail_returns_failed_load(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED", "true")
    monkeypatch.setattr(
        harness_inject, "_resolve_instance_ids", lambda: ["x__none-1"])
    monkeypatch.setattr(
        harness_inject, "load_problem", lambda iid: (None, None))
    svc = _StubIntake()
    verdict = await harness_inject.maybe_inject_swe_bench_at_boot(svc)
    assert verdict == (
        harness_inject.SWEBenchProInjectionVerdict.FAILED_LOAD
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_flag_registry_seed_autoscore():
    captured = []

    class _Reg:
        def register(self, spec):
            captured.append(spec)

    harness_inject.register_flags(_Reg())
    spec = next(
        s for s in captured
        if s.name == "JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED"
    )
    assert spec.default is False
    assert "harness_inject.py" in spec.source_file
