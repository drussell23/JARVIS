"""Slice 197 — Autonomous Graduation Contract + Adaptive Synthesis Governor.

The M10 ArchitectureProposer was frozen behind a static operator-set env flag
(§30.5.2). This slice converts that into OPERATOR-DELEGATED CONDITIONAL
AUTHORIZATION: the operator authorizes the unlock criteria once (this merged
PR is the operator act); the organism then proves the criteria against the
durable mmap registry and executes the unlock itself — with two invariants
that are NOT negotiable:

  * **Kill switch supreme** — explicit JARVIS_M10_ARCH_PROPOSER_ENABLED=0
    beats any autonomous unlock, always (Slice 136 precedent).
  * **governance_boundary_gate untouched** — proposals modifying
    governance/ still route APPROVAL_REQUIRED. The recursion guard does not
    weaken in the same slice that unlocks the proposer (grep-pinned).

Pins:
  * Two new charter counters: provider_exhaustions +
    control_plane_starvation_events (the graduation criteria become pure
    reads of the .bin — no log parsing).
  * evaluate_graduation: evidence floor (min dispatches) + exhaustions==0 +
    abandoned-ratio + starvation-threshold; unlock persists durably with a
    stamped metrics snapshot (audit artifact).
  * Phase-4 acceptance: feeding stable metrics into the binary registry
    flips m10_arch_proposer_enabled() True with NO env change.
  * Adaptive Synthesis Governor: cadence scales with traffic + cost burn —
    conserve when busy/expensive, compile aggressively when idle.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.m10.primitives import (
    m10_arch_proposer_enabled,
)
from backend.core.ouroboros.governance.m10_autonomous_graduation import (
    GraduationDecision,
    _reset_for_tests,
    autonomous_graduation_enabled,
    effective_cadence_n,
    evaluate_graduation,
    is_autonomously_unlocked,
)
from backend.core.ouroboros.governance.observability_registry import (
    CONTROL_PLANE_STARVATION_EVENTS,
    HEDGE_CONCURRENCY_DISPATCHES,
    HEDGE_RACES_ABANDONED,
    PROVIDER_EXHAUSTIONS,
    _reset_singleton_for_tests,
    get_observability_registry,
    record_control_plane_starvation,
    record_provider_exhaustion,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "reg.bin"),
    )
    monkeypatch.setenv(
        "JARVIS_M10_GRADUATION_STATE_PATH", str(tmp_path / "m10_state.json"),
    )
    for var in (
        "JARVIS_OBSERVABILITY_REGISTRY_ENABLED",
        "JARVIS_M10_ARCH_PROPOSER_ENABLED",
        "JARVIS_M10_AUTONOMOUS_GRADUATION_ENABLED",
        "JARVIS_M10_GRAD_MIN_DISPATCHES",
        "JARVIS_M10_GRAD_MAX_EXHAUSTIONS",
        "JARVIS_M10_GRAD_MAX_ABANDONED_RATIO",
        "JARVIS_M10_GRAD_MAX_STARVATION_EVENTS",
        "JARVIS_M10_PACING_BUSY_DISPATCH_DELTA",
        "JARVIS_M10_PACING_BUSY_FACTOR",
        "JARVIS_M10_PACING_IDLE_FACTOR",
        "JARVIS_M10_PACING_COST_CONSERVE_RATIO",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_singleton_for_tests()
    _reset_for_tests()
    yield
    _reset_singleton_for_tests()
    _reset_for_tests()


def _seed_healthy_registry(dispatches: int = 6, victories: int = 2) -> None:
    reg = get_observability_registry()
    reg.incr(HEDGE_CONCURRENCY_DISPATCHES, dispatches)
    reg.incr("hedge_rt_victories", victories)


# ===========================================================================
# A — new charter counters (criteria become pure .bin reads)
# ===========================================================================

def test_new_counters_preregistered_at_zero():
    snap = get_observability_registry().snapshot()
    assert snap[PROVIDER_EXHAUSTIONS] == 0
    assert snap[CONTROL_PLANE_STARVATION_EVENTS] == 0


def test_record_helpers_increment():
    record_provider_exhaustion()
    record_control_plane_starvation()
    record_control_plane_starvation()
    reg = get_observability_registry()
    assert reg.get(PROVIDER_EXHAUSTIONS) == 1
    assert reg.get(CONTROL_PLANE_STARVATION_EVENTS) == 2


# ===========================================================================
# B — precedence: operator kill switch is SUPREME
# ===========================================================================

def test_explicit_zero_beats_autonomous_unlock(monkeypatch):
    """Slice 136 precedent: operator =0 wins over ANY autonomous state."""
    _seed_healthy_registry()
    assert evaluate_graduation().unlocked is True  # state now persisted
    monkeypatch.setenv("JARVIS_M10_ARCH_PROPOSER_ENABLED", "0")
    assert m10_arch_proposer_enabled() is False


def test_explicit_one_enables(monkeypatch):
    monkeypatch.setenv("JARVIS_M10_ARCH_PROPOSER_ENABLED", "1")
    assert m10_arch_proposer_enabled() is True


def test_unset_with_no_graduation_state_stays_locked():
    assert m10_arch_proposer_enabled() is False


def test_unset_with_autonomous_master_off_stays_locked(monkeypatch):
    _seed_healthy_registry()
    evaluate_graduation()
    monkeypatch.setenv("JARVIS_M10_AUTONOMOUS_GRADUATION_ENABLED", "false")
    assert autonomous_graduation_enabled() is False
    assert m10_arch_proposer_enabled() is False


def test_garbage_env_value_is_safe_off(monkeypatch):
    monkeypatch.setenv("JARVIS_M10_ARCH_PROPOSER_ENABLED", "banana")
    assert m10_arch_proposer_enabled() is False


# ===========================================================================
# C — the graduation evaluation
# ===========================================================================

def test_healthy_metrics_unlock():
    _seed_healthy_registry(dispatches=6)
    d = evaluate_graduation()
    assert isinstance(d, GraduationDecision)
    assert d.unlocked is True
    assert d.metrics[HEDGE_CONCURRENCY_DISPATCHES] == 6


def test_evidence_floor_blocks_empty_registry():
    """Zero traffic is not evidence of health — it's evidence of nothing."""
    d = evaluate_graduation()
    assert d.unlocked is False
    assert "evidence_floor" in d.reason


def test_any_exhaustion_blocks():
    _seed_healthy_registry()
    record_provider_exhaustion()
    d = evaluate_graduation()
    assert d.unlocked is False
    assert "exhaustion" in d.reason


def test_abandoned_ratio_blocks():
    _seed_healthy_registry(dispatches=6)
    get_observability_registry().incr(HEDGE_RACES_ABANDONED, 3)  # 0.5 > 0.25
    d = evaluate_graduation()
    assert d.unlocked is False
    assert "abandoned" in d.reason


def test_starvation_threshold_blocks():
    _seed_healthy_registry()
    get_observability_registry().incr(CONTROL_PLANE_STARVATION_EVENTS, 51)
    d = evaluate_graduation()
    assert d.unlocked is False
    assert "starvation" in d.reason


def test_unlock_persists_audit_artifact(tmp_path, monkeypatch):
    state = tmp_path / "audit_state.json"
    monkeypatch.setenv("JARVIS_M10_GRADUATION_STATE_PATH", str(state))
    _reset_for_tests()
    _seed_healthy_registry()
    d = evaluate_graduation()
    assert d.unlocked is True
    payload = json.loads(state.read_text())
    assert payload["unlocked"] is True
    assert payload["metrics"][HEDGE_CONCURRENCY_DISPATCHES] == 6
    assert payload["criteria"]["min_dispatches"] == 5


def test_unlock_is_sticky_across_evaluations():
    """Graduation, not oscillation: once unlocked + persisted, a later noisy
    window doesn't silently re-lock (revocation = the operator kill switch)."""
    _seed_healthy_registry()
    assert evaluate_graduation().unlocked is True
    record_provider_exhaustion()  # health degrades AFTER graduation
    assert is_autonomously_unlocked() is True


def test_evaluation_never_raises_without_registry(monkeypatch):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", "false")
    _reset_singleton_for_tests()
    d = evaluate_graduation()
    assert d.unlocked is False


# ===========================================================================
# D — Phase-4 acceptance: metrics in the .bin flip the proposer, no env edit
# ===========================================================================

def test_stable_binary_metrics_activate_m10_without_env_change():
    """The user-pinned Slice 197 acceptance: feed stable metrics into the
    memory-mapped registry → M10 goes active. No environment files touched."""
    assert m10_arch_proposer_enabled() is False  # locked at birth
    _seed_healthy_registry(dispatches=7, victories=3)  # the live soak shape
    _reset_for_tests()  # drop the lazy-eval TTL so the next check re-runs
    assert m10_arch_proposer_enabled() is True


# ===========================================================================
# E — Adaptive Synthesis Governor (pacing)
# ===========================================================================

def test_idle_window_compiles_aggressively():
    """Zero traffic since the last check → cadence shrinks (more proposals)."""
    n = effective_cadence_n(base_n=8, dispatch_delta=0)
    assert n < 8
    assert n >= 1


def test_busy_window_conserves_capital():
    n = effective_cadence_n(base_n=8, dispatch_delta=20)
    assert n > 8


def test_high_cost_burn_conserves_even_when_idle():
    n = effective_cadence_n(base_n=8, dispatch_delta=0, cost_burn_ratio=0.95)
    assert n >= 8


def test_moderate_traffic_keeps_base():
    n = effective_cadence_n(base_n=8, dispatch_delta=3)
    assert n == 8


def test_pacing_never_raises_on_garbage():
    n = effective_cadence_n(base_n=-1, dispatch_delta=-9, cost_burn_ratio=None)
    assert n >= 1


# ===========================================================================
# F — wiring + doctrine pins
# ===========================================================================

def test_exhaustion_funnel_wired_to_registry():
    src = (_GOV / "candidate_generator.py").read_text(encoding="utf-8")
    assert "record_provider_exhaustion" in src


def test_starvation_watchdog_wired_to_registry():
    src = (_GOV / "control_plane_watchdog.py").read_text(encoding="utf-8")
    assert "record_control_plane_starvation" in src


def test_cadence_runner_consults_the_pacing_governor():
    src = (_GOV / "m10" / "cadence_runner.py").read_text(encoding="utf-8")
    assert "effective_cadence_n" in src


def test_boundary_gate_not_weakened():
    """The RRD §1 recursion guard survives this slice byte-for-byte in
    spirit: governance-modifying proposals still APPROVAL_REQUIRED, and the
    gate has NO coupling to the autonomous graduation module."""
    src = (_GOV / "governance_boundary_gate.py").read_text(encoding="utf-8")
    assert "APPROVAL_REQUIRED" in src
    assert "m10_autonomous_graduation" not in src
    assert "is_autonomously_unlocked" not in src


def test_authority_invariant_no_wide_imports():
    src = (_GOV / "m10_autonomous_graduation.py").read_text(encoding="utf-8")
    for forbidden in (
        "from backend.core.ouroboros.governance.orchestrator",
        "iron_gate", "change_engine",
        "semantic_guardian", "risk_tier",
    ):
        assert forbidden not in src, f"authority leak: {forbidden}"
