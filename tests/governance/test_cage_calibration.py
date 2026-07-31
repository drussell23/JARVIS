"""Regression spine for cage calibration — tighten only, never widen.

`worker_synthesizer` derives a worker's cage from static AST inspection. That
is a PRIOR with no posterior: a worker granted three mutations that never used
more than one taught it nothing. `ScopedToolBackend` was recording the
evidence all along (`mutations_count` vs `max_mutations`, `call_records`
stamped `type_denied` / `count_denied`) and nobody consumed it.

The load-bearing test here is `test_calibration_can_never_widen_a_cage`.
Observed under-use is safe to act on — dropping to what the evidence shows
grants strictly less than the prior already did. Observed DENIAL is not: a
system that widens a cage because a worker kept asking has built a
privilege-escalation ramp out of persistence, reachable by any worker
including a prompt-injected one. Denials become findings about the
SYNTHESIZER'S RULE instead, which is the root cause.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy import cage_calibration as cc
from backend.core.ouroboros.governance.autonomy.cage_calibration import (
    CageObservation,
    _is_tightening,
    calibrate_shape,
    findings,
    observe_unit,
    shape_signature,
)
from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
    WorkerShape,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CAGE_CALIBRATION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CAGE_CALIBRATION_MIN_OBS", "4")
    cc.reset_for_tests(tmp_path)
    yield
    cc.reset_for_tests(None)


def _shape(budget=4, tools=("read_file", "edit_file"), role="python-source mutator",
           read_only=False):
    return WorkerShape(
        role=role, allowed_tools=tuple(tools), mutation_budget=budget,
        context_budget_tokens=8000, read_only=read_only,
    )


class _Cage:
    """A stand-in ScopedToolBackend exposing the real public surface."""

    def __init__(self, granted=4, used=1, denied=(), count_denied=0):
        self.max_mutations = granted
        self.mutations_count = used
        self.call_records = tuple(
            (name, f"c{i}", "type_denied", time.time())
            for i, name in enumerate(denied)
        ) + tuple(
            ("edit_file", f"x{i}", "count_denied", time.time())
            for i in range(count_denied)
        )


class _Result:
    def __init__(self, ok=True):
        self.status = "completed" if ok else "failed"


def _feed(n, *, granted=4, used=1, ok=True, shape=None, **cage_kw):
    shape = shape or _shape(budget=granted)
    for _ in range(n):
        observe_unit(shape, _Cage(granted=granted, used=used, **cage_kw),
                     _Result(ok))
    return shape


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_calibration_can_never_widen_a_cage():
    """The load-bearing test.

    No sequence of observations — however many, however emphatic — may
    produce a shape granting anything the prior did not.
    """
    prior = _shape(budget=2, tools=("read_file",))
    # Hammer it with maximal demand: every run exhausts budget AND is denied
    # tools it does not have. A naive "learn from demand" calibrator widens.
    _feed(50, granted=2, used=2, ok=True, shape=prior,
          denied=("bash", "write_file", "web_fetch"), count_denied=9)
    out = calibrate_shape(prior)
    assert set(out.allowed_tools).issubset(set(prior.allowed_tools))
    assert out.mutation_budget <= prior.mutation_budget
    assert "bash" not in out.allowed_tools


def test_denial_pressure_produces_a_finding_not_a_widening():
    """Denials name the synthesizer's RULE as the defect — the root cause."""
    prior = _shape(budget=2, tools=("read_file",))
    _feed(10, granted=2, used=2, ok=True, shape=prior, denied=("run_tests",))
    found = findings()
    assert found, "chronic denial produced no finding"
    assert found[0].kind == "chronic_denial"
    assert "worker_synthesizer" in found[0].detail
    assert calibrate_shape(prior).allowed_tools == prior.allowed_tools


def test_tightening_predicate_rejects_any_grant():
    prior = _shape(budget=3, tools=("read_file", "edit_file"))
    assert _is_tightening(prior, _shape(budget=1, tools=("read_file",))) is True
    assert _is_tightening(prior, _shape(budget=5, tools=("read_file",))) is False
    assert _is_tightening(
        prior, _shape(budget=1, tools=("read_file", "bash"))) is False
    # A read-only prior may never become mutating.
    ro = _shape(budget=0, tools=("read_file",), read_only=True)
    assert _is_tightening(ro, _shape(budget=0, tools=("read_file",))) is False


# ---------------------------------------------------------------------------
# Tightening actually happens
# ---------------------------------------------------------------------------


def test_consistent_under_use_sheds_budget():
    prior = _shape(budget=8)
    _feed(12, granted=8, used=1, ok=True, shape=prior)
    out = calibrate_shape(prior)
    assert out.mutation_budget < prior.mutation_budget
    assert out.mutation_budget >= 1, "must never tighten to zero"


def test_full_use_keeps_the_budget():
    prior = _shape(budget=4)
    _feed(12, granted=4, used=4, ok=True, shape=prior)
    assert calibrate_shape(prior).mutation_budget == prior.mutation_budget


def test_margin_keeps_room_above_observed_peak(monkeypatch):
    """Tightening to the exact observed max would starve the next slightly
    harder instance of the same work."""
    monkeypatch.setenv("JARVIS_CAGE_HEADROOM_MARGIN", "2.0")
    prior = _shape(budget=8)
    _feed(12, granted=8, used=2, ok=True, shape=prior)
    out = calibrate_shape(prior)
    assert out.mutation_budget > 2


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_cold_start_returns_the_prior_unchanged():
    prior = _shape(budget=5)
    assert calibrate_shape(prior) is prior


def test_thin_evidence_returns_the_prior_unchanged(monkeypatch):
    monkeypatch.setenv("JARVIS_CAGE_CALIBRATION_MIN_OBS", "20")
    prior = _shape(budget=8)
    _feed(5, granted=8, used=1, shape=prior)
    assert calibrate_shape(prior) is prior


def test_disabled_is_a_strict_noop(monkeypatch):
    prior = _shape(budget=8)
    _feed(30, granted=8, used=1, shape=prior)
    monkeypatch.setenv("JARVIS_CAGE_CALIBRATION_ENABLED", "0")
    assert calibrate_shape(prior) is prior
    assert observe_unit(prior, _Cage(), _Result()) is None


def test_default_is_off(monkeypatch):
    """It narrows a live security boundary from data — earns default-on by soak."""
    monkeypatch.delenv("JARVIS_CAGE_CALIBRATION_ENABLED", raising=False)
    assert cc.calibration_enabled() is False


def test_failed_units_do_not_teach_the_cage_to_shrink():
    """A crashed worker may have stopped early for unrelated reasons.

    Counting its low usage as "this class needs less" would tighten the cage
    on the strength of a crash.
    """
    prior = _shape(budget=8)
    _feed(20, granted=8, used=0, ok=False, shape=prior)
    assert calibrate_shape(prior) is prior


def test_calibration_never_raises_on_a_foreign_shape():
    assert calibrate_shape(object()) is not None


def test_unattributable_shape_is_not_recorded():
    """An observation that belongs to no class must not be credited to one.

    A shape with no derivable role degrades to `unknown:rw`. Recording it
    would pollute a real class's statistics with a datum from nowhere — the
    same reason an unattributable op earns no memory topic any credit.
    """
    assert observe_unit(object(), object(), object()) is None
    assert cc._store("outcome").hashes() == ()


# ---------------------------------------------------------------------------
# Signature + headroom semantics
# ---------------------------------------------------------------------------


def test_signature_uses_the_synthesizers_own_clustering():
    """No second taxonomy — a new role becomes a new class with no code change."""
    assert shape_signature(_shape(role="python-source mutator")) == \
        "python-source-mutator:rw"
    assert shape_signature(_shape(role="test-suite analyzer", read_only=True)) == \
        "test-suite-analyzer:ro"


def test_distinct_roles_learn_independently():
    a = _shape(budget=8, role="python-source mutator")
    b = _shape(budget=8, role="config editor")
    _feed(12, granted=8, used=1, shape=a)
    assert calibrate_shape(a).mutation_budget < 8
    assert calibrate_shape(b) is b  # b has its own, empty, history


def test_zero_budget_worker_reports_full_headroom():
    """A read-only worker granted nothing has used all of its nothing.

    Reporting 0.0 would drag the class mean toward "needs nothing" using a
    worker that was never allowed anything.
    """
    obs = CageObservation(signature="s", granted_mutations=0, used_mutations=0,
                          denied_tools=(), count_denied=0, succeeded=True)
    assert obs.headroom == 1.0


def test_headroom_is_clamped():
    obs = CageObservation(signature="s", granted_mutations=2, used_mutations=99,
                          denied_tools=(), count_denied=0, succeeded=True)
    assert obs.headroom == 1.0


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_swarm_invoker_calibrates_and_observes():
    """Both seams live in the ONE place a caged worker is shaped and finishes."""
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/governance/autonomy/swarm_invoker.py"
           ).read_text(encoding="utf-8")
    assert "calibrate_shape" in src
    assert "observe_unit" in src
