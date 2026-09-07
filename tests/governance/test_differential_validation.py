"""Differential VALIDATE gate — a candidate is judged by what it changes.

Root cause covered: goal-003 looped because its target file's own tests are
red in the soak environment (rt_gate's local tier answers before the test's
fake DW provider); the harness blamed every candidate and the lesson memory
learned a false lesson. The gate measures the baseline first and excludes
ambient failures from the verdict — residual failures still fail.
"""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import differential_validation as DV
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult


def _ar(adapter="python", passed=False, failed=(), timed_out=False):
    tr = TestResult(
        passed=passed, total=len(failed) + 1, failed=len(failed), failed_tests=tuple(failed),
        duration_seconds=0.1, stdout="", flake_suspected=False, timed_out=timed_out,
    )
    return AdapterResult(
        adapter=adapter, passed=passed, failure_class="none" if passed else "test",
        test_result=tr, duration_s=0.1,
    )


def _multi(*results):
    dom = next((r for r in results if not r.passed), None)
    return MultiAdapterResult(passed=dom is None, adapter_results=tuple(results), dominant_failure=dom, total_duration_s=0.1)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (DV._ENV_ENABLED, DV._ENV_FRACTION, DV._ENV_MIN_S):
        monkeypatch.delenv(k, raising=False)


def test_budget_is_bounded_fraction_with_floor(monkeypatch):
    assert DV.baseline_budget_s(100.0) == pytest.approx(30.0)
    assert DV.baseline_budget_s(10.0) == pytest.approx(5.0)      # floor wins
    assert DV.baseline_budget_s(2.0) == pytest.approx(2.0)       # never more than remaining
    monkeypatch.setenv(DV._ENV_FRACTION, "0.5"); monkeypatch.setenv(DV._ENV_MIN_S, "1")
    assert DV.baseline_budget_s(20.0) == pytest.approx(10.0)
    assert DV.baseline_budget_s(-3.0) == 0.0


def test_existing_runnable_targets_only_existing_relative_files(tmp_path):
    (tmp_path / "tests").mkdir(); (tmp_path / "tests" / "test_a.py").write_text("x")
    files = [("tests/test_a.py", ""), ("tests/test_new.py", ""), ("/abs/test_b.py", ""), ("tests/data.txt", "")]
    out = DV.existing_runnable_targets(files, tmp_path, (".py",))
    assert out == (tmp_path / "tests" / "test_a.py",)


class _Runner:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    async def run(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.result


def test_baseline_collects_red_ids_and_is_failsafe(tmp_path):
    red = _multi(_ar(failed=("tests/t.py::a", "tests/t.py::b")))
    ids = asyncio.run(DV.baseline_failed_tests(_Runner(red), (tmp_path / "t.py",), sandbox_dir=tmp_path, budget_s=5, op_id="op"))
    assert ids == frozenset({"tests/t.py::a", "tests/t.py::b"})
    assert asyncio.run(DV.baseline_failed_tests(_Runner(_multi(_ar(passed=True))), (tmp_path / "t.py",), sandbox_dir=tmp_path, budget_s=5, op_id="op")) == frozenset()
    assert asyncio.run(DV.baseline_failed_tests(_Runner(exc=RuntimeError("boom")), (tmp_path / "t.py",), sandbox_dir=tmp_path, budget_s=5, op_id="op")) == frozenset()
    assert asyncio.run(DV.baseline_failed_tests(_Runner(_multi(_ar(failed=("x",), timed_out=True))), (tmp_path / "t.py",), sandbox_dir=tmp_path, budget_s=5, op_id="op")) == frozenset()
    assert asyncio.run(DV.baseline_failed_tests(_Runner(red), (), sandbox_dir=tmp_path, budget_s=5, op_id="op")) == frozenset()
    assert asyncio.run(DV.baseline_failed_tests(_Runner(red), (tmp_path / "t.py",), sandbox_dir=tmp_path, budget_s=0, op_id="op")) == frozenset()


def test_apply_excludes_only_fully_ambient_failures():
    base = frozenset({"tests/t.py::old_a", "tests/t.py::old_b"})
    # candidate fails only on the ambient tests -> verdict flips to passed
    m, ignored = DV.apply_differential(_multi(_ar(failed=("tests/t.py::old_a",))), base)
    assert m.passed and m.dominant_failure is None and ignored == ("tests/t.py::old_a",)
    # a NEW failing test alongside ambient ones -> untouched, still failed
    m2, ignored2 = DV.apply_differential(_multi(_ar(failed=("tests/t.py::old_a", "tests/t.py::test_new"))), base)
    assert not m2.passed and ignored2 == () and m2.dominant_failure.test_result.failed_tests == ("tests/t.py::old_a", "tests/t.py::test_new")
    # crash / collection error with no ids -> never ignored
    m3, ignored3 = DV.apply_differential(_multi(_ar(failed=())), base)
    assert not m3.passed and ignored3 == ()
    # timed-out adapters are never ignored
    m4, ignored4 = DV.apply_differential(_multi(_ar(failed=("tests/t.py::old_a",), timed_out=True)), base)
    assert not m4.passed and ignored4 == ()
    # empty baseline or already passed -> identity
    ok = _multi(_ar(passed=True))
    assert DV.apply_differential(ok, base) == (ok, ())
    failed = _multi(_ar(failed=("tests/t.py::old_a",)))
    assert DV.apply_differential(failed, frozenset()) == (failed, ())


def test_apply_keeps_other_adapters_failures():
    base = frozenset({"tests/t.py::old"})
    m, ignored = DV.apply_differential(_multi(_ar(failed=("tests/t.py::old",)), _ar(adapter="cpp", failed=("c::x",))), base)
    assert not m.passed and ignored == ("tests/t.py::old",) and m.dominant_failure.adapter == "cpp"


def test_gate_is_wired_into_candidate_tree_validation_and_lessons():
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch, lesson_memory as LM
    src = inspect.getsource(orch.GovernedOrchestrator._run_validation_core)
    assert "baseline_failed_tests" in src and "apply_differential" in src and "AMBIENT_ERROR_CLASS" in src
    rec = LM.build_lesson_record(op_id="o", target_files=("tests/x.py",), phase="VALIDATE", failure_class="test",
                                 error_text="ambient red: tests/x.py::a", error_class=DV.AMBIENT_ERROR_CLASS)
    assert rec.error_class == "ambient_red" and "excluded from the verdict" in rec.mitigation_summary


def test_acceptance_tests_are_never_excluded():
    base = frozenset({"tests/t.py::test_old", "tests/t.py::test_fix_me[1]"})
    names = DV.acceptance_names("Fix test_fix_me so it passes", target_symbols=("test_declared",))
    assert names == frozenset({"test_fix_me", "test_declared"})
    m, ignored = DV.apply_differential(_multi(_ar(failed=("tests/t.py::test_fix_me[1]",))), base, protected=names)
    assert not m.passed and ignored == ()                      # the op's own acceptance test stays red
    m2, ignored2 = DV.apply_differential(_multi(_ar(failed=("tests/t.py::test_old",))), base, protected=names)
    assert m2.passed and ignored2 == ("tests/t.py::test_old",)  # unrelated ambient test excluded


def test_production_code_candidates_are_never_differential():
    base = frozenset({"tests/test_mod.py::test_f"})
    failed = _multi(_ar(failed=("tests/test_mod.py::test_f",)))
    assert DV.apply_differential(failed, base, test_authoring=False) == (failed, ())
    assert DV.candidate_is_test_authoring([("tests/governance/test_x.py", "")])
    assert not DV.candidate_is_test_authoring([("pkg/mod.py", ""), ("tests/test_mod.py", "")])
    assert not DV.candidate_is_test_authoring([])
