"""Fault-Injection Matrix — resilience verification spine (autonomy Gap 1).

Proves the systematic stability sweep: the runner enforces revert-ALWAYS and the
criteria-dict verdict (DEFENDED iff all criteria hold, DEFENSE_FAILED with a
first-failing locus, INCONCLUSIVE never a fake pass); the coverage matrix marks
uncovered classes NO_SCENARIO (predicted death sites) distinct from BROKEN; and
the real seeded scenarios each DEFEND their stability defense (worker_lifeline,
child_reaper, external_watchdog, memory_pressure_gate, provider_quarantine, and
the Gap 2 oracle) with zero leaked side effects.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp.test_utils import make_mocked_request

import backend.core.ouroboros.governance.fault_injection_matrix as fim
from backend.core.ouroboros.governance.fault_injection_matrix import (
    FaultScenario, FaultVerdict, ResilienceFaultClass, CoverageVerdict,
)


def _scenario(fault_class, *, inject, verify, revert=lambda _h: None, sid="s"):
    return FaultScenario(
        id=sid, fault_class=fault_class, defense="test", description="t",
        inject=inject, verify=verify, revert=revert,
    )


# ---------------------------------------------------------------------------
# Runner mechanics — verdict + revert-always
# ---------------------------------------------------------------------------


def test_all_criteria_pass_is_defended():
    r = fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER,
        inject=lambda: {"x": 1},
        verify=lambda h: {"a": True, "b": True},
    ))
    assert r.verdict == FaultVerdict.DEFENDED.value
    assert r.failure_locus is None


def test_failing_criterion_is_defense_failed_with_locus():
    r = fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER,
        inject=lambda: None,
        verify=lambda h: {"a": True, "the_broken_one": False, "c": True},
    ))
    assert r.verdict == FaultVerdict.DEFENSE_FAILED.value
    assert r.failure_locus == "the_broken_one"


def test_inject_or_verify_raise_is_inconclusive_never_fake_pass():
    def _boom():
        raise RuntimeError("inject blew up")
    r = fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER, inject=_boom, verify=lambda h: {"a": True},
    ))
    assert r.verdict == FaultVerdict.INCONCLUSIVE.value
    assert "inject blew up" in r.detail


def test_empty_criteria_is_inconclusive():
    r = fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER, inject=lambda: 1, verify=lambda h: {},
    ))
    assert r.verdict == FaultVerdict.INCONCLUSIVE.value


def test_revert_always_runs_even_when_verify_raises():
    reverted = {"n": 0}

    def _verify(h):
        raise RuntimeError("verify blew up")

    def _revert(h):
        reverted["n"] += 1

    r = fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER,
        inject=lambda: {"resource": True}, verify=_verify, revert=_revert,
    ))
    assert r.verdict == FaultVerdict.INCONCLUSIVE.value
    assert reverted["n"] == 1  # revert ran despite the verify fault


def test_revert_not_run_if_inject_failed():
    reverted = {"n": 0}

    def _boom():
        raise RuntimeError("no resource acquired")

    fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER,
        inject=_boom, verify=lambda h: {"a": True},
        revert=lambda h: reverted.__setitem__("n", reverted["n"] + 1),
    ))
    assert reverted["n"] == 0  # nothing acquired → nothing to revert


def test_revert_fault_never_propagates():
    def _bad_revert(h):
        raise RuntimeError("revert blew up")
    # DEFENDED verdict preserved; revert fault swallowed.
    r = fim.run_scenario(_scenario(
        ResilienceFaultClass.ORPHAN_WORKER,
        inject=lambda: 1, verify=lambda h: {"a": True}, revert=_bad_revert,
    ))
    assert r.verdict == FaultVerdict.DEFENDED.value


# ---------------------------------------------------------------------------
# Coverage matrix — NO_SCENARIO vs BROKEN vs DEFENDED
# ---------------------------------------------------------------------------


def test_coverage_enumerates_full_taxonomy(monkeypatch):
    monkeypatch.setattr(fim, "ensure_seeded", lambda: [])
    report = fim.run_matrix()
    # every enum value present, all NO_SCENARIO when nothing registered.
    assert set(report.coverage.keys()) == {f.value for f in ResilienceFaultClass}
    assert all(v == CoverageVerdict.NO_SCENARIO.value for v in report.coverage.values())
    assert report.resilience_score == 0.0


def test_coverage_marks_defended_and_broken(monkeypatch):
    good = _scenario(ResilienceFaultClass.ORPHAN_WORKER,
                     inject=lambda: 1, verify=lambda h: {"a": True}, sid="good")
    bad = _scenario(ResilienceFaultClass.MEMORY_EXHAUSTION,
                    inject=lambda: 1, verify=lambda h: {"a": False}, sid="bad")
    monkeypatch.setattr(fim, "ensure_seeded", lambda: [good, bad])
    report = fim.run_matrix()
    assert report.coverage[ResilienceFaultClass.ORPHAN_WORKER.value] == "defended"
    assert report.coverage[ResilienceFaultClass.MEMORY_EXHAUSTION.value] == "broken"
    assert report.coverage[ResilienceFaultClass.LOOP_STARVATION.value] == "no_scenario"


def test_coverage_gaps_surfaced_in_dict(monkeypatch):
    monkeypatch.setattr(fim, "ensure_seeded", lambda: [])
    d = fim.run_matrix().to_dict()
    assert set(d["coverage_gaps"]) == {f.value for f in ResilienceFaultClass}


def test_matrix_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("seed blew up")
    monkeypatch.setattr(fim, "ensure_seeded", _boom)
    report = fim.run_matrix()
    assert report.reason_code == "matrix_error"


# ---------------------------------------------------------------------------
# Real seeded scenarios — each DEFENDS its actual stability defense
# ---------------------------------------------------------------------------


def test_real_matrix_all_seeded_scenarios_defended():
    fim.reset_registry_for_tests()
    fim.reset_cache_for_tests()
    report = fim.run_matrix()
    # Every seeded scenario must DEFEND (no DEFENSE_FAILED / INCONCLUSIVE).
    non_defended = [r for r in report.results if r.verdict != "defended"]
    assert not non_defended, f"non-defended: {[(r.scenario_id, r.verdict, r.detail) for r in non_defended]}"
    assert len(report.results) >= 8
    # The known deterministic gaps are honestly surfaced.
    for gap in ("embed_correlated_failure", "loop_starvation",
                "cost_runaway", "teardown_wedge"):
        assert report.coverage[gap] == "no_scenario"


def test_real_matrix_leaves_no_orphan_processes():
    import subprocess
    before = subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True, text=True)
    fim.reset_registry_for_tests()
    fim.run_matrix()
    after = subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True, text=True)
    # the child_reaper scenario's sacrificial `sleep 60` must be reaped/reverted.
    assert after.stdout.count("\n") <= before.stdout.count("\n")


def test_gap2_oracle_scenario_present():
    """The Gap 2 self-perception oracle is wired as the detection backbone."""
    fim.reset_registry_for_tests()
    scenarios = fim.ensure_seeded()
    ids = {s.id for s in scenarios}
    assert "capability.gap2_oracle_sees_dormancy" in ids


# ---------------------------------------------------------------------------
# Master flag + endpoint
# ---------------------------------------------------------------------------


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", raising=False)
    assert fim.master_enabled() is True
    monkeypatch.setenv("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", "0")
    assert fim.master_enabled() is False


def test_snapshot_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", "false")
    fim.reset_cache_for_tests()
    d = fim.snapshot(force=True)
    assert d["enabled"] is False and d["reason_code"] == "disabled"


def _req(path="/observability/resilience"):
    r = make_mocked_request("GET", path, headers={})
    r._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return r


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _router():
    from backend.core.ouroboros.governance.ide_observability import IDEObservabilityRouter
    return IDEObservabilityRouter()


def test_endpoint_disabled_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = _run(_router()._handle_resilience_matrix(_req()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.disabled"


def test_endpoint_substrate_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", "false")
    resp = _run(_router()._handle_resilience_matrix(_req()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.resilience_disabled"


def test_endpoint_returns_matrix(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", "true")
    fim.reset_cache_for_tests()
    resp = _run(_router()._handle_resilience_matrix(_req()))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["schema_version"] == fim.FAULT_INJECTION_MATRIX_SCHEMA_VERSION
    assert "coverage" in body and "resilience_score" in body
    assert resp.headers.get("Cache-Control") == "no-store"


def test_endpoint_never_500s(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAULT_INJECTION_MATRIX_ENABLED", "true")
    monkeypatch.setattr(fim, "snapshot", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    resp = _run(_router()._handle_resilience_matrix(_req()))
    assert resp.status == 200
    assert json.loads(resp.body.decode())["reason_code"] == "resilience.unavailable"
