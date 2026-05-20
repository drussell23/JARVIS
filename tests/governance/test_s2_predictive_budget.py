"""S2 spine — Predictive Budget Preemption + Dynamic MAD safety factor.

Pins PRD §11 in full:
  * Forecast math + composition (5 tests)
  * MAD volatility incl. LOAD-BEARING outlier-robustness pin (6 tests)
  * Dynamic safety factor (6 tests)
  * Admission + preemption signal (8 tests)
  * AST pins + flag registry (5 tests)
  * Observability (1 test)

Total: 31 tests (~30 per §11.7).

Composition discipline (PRD §3): all tests exercise the REAL
admission_estimator + sensor_governor singletons (no parallel mocks).
Sample providers are injectable for deterministic tests.
"""
from __future__ import annotations

import ast
import os
import statistics
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance import s2_predictive_budget as s2
from backend.core.ouroboros.governance.admission_estimator import (
    RecentDecisionsRing,
    get_default_history,
    reset_singletons_for_tests,
)
from backend.core.ouroboros.governance.sensor_governor import (
    SensorGovernor,
    Urgency,
)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    """Strip S2 env knobs so each test starts at PRD defaults; reset
    canonical singletons + pricing cache so cross-test contamination
    cannot leak."""
    for k in (
        "JARVIS_S2_PREDICTIVE_BUDGET_ENABLED",
        "JARVIS_S2_BASE_SAFETY_FACTOR",
        "JARVIS_S2_VOLATILITY_PENALTY",
        "JARVIS_S2_SAFETY_FLOOR",
        "JARVIS_S2_SAFETY_CEILING",
        "JARVIS_S2_CHARS_PER_TOKEN",
        "JARVIS_S2_COST_SAMPLE_WINDOW",
        "JARVIS_S2_PRICING_YAML_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    reset_singletons_for_tests()
    s2._reset_pricing_cache_for_tests()
    yield
    reset_singletons_for_tests()
    s2._reset_pricing_cache_for_tests()


# ============================================================================
# (1/6) Forecast math + composition — 5 tests
# ============================================================================


def test_master_default_false_byte_identical(monkeypatch):
    """Master OFF (default) ⇒ master_enabled() returns False. PRD §11.5."""
    assert s2.master_enabled() is False
    # Case-insensitive parser parity with S1 master
    for raw, exp in [
        ("true", True), ("1", True), ("YES", True), ("on", True),
        ("false", False), ("", False), ("garbage", False), ("0", False),
    ]:
        monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", raw)
        assert s2.master_enabled() is exp, raw


def test_forecasted_cost_deterministic_given_inputs():
    """Same (prompt, route, model, samples) → same cost. PRD §11.2."""
    def lookup(_r, _m):
        return (3e-6, 1.5e-5)

    def estimator(_r, _m):
        return 100.0

    a = s2.forecasted_cost(2000, "ide", "claude",
                            output_token_estimator=estimator,
                            pricing_lookup=lookup)
    b = s2.forecasted_cost(2000, "ide", "claude",
                            output_token_estimator=estimator,
                            pricing_lookup=lookup)
    assert a == b
    # Expected: in_tokens = 2000/4 = 500; in_cost = 500 × 3e-6 = 1.5e-3
    # out_tokens = 100; out_cost = 100 × 1.5e-5 = 1.5e-3 → total 3e-3
    assert a == pytest.approx(0.003, abs=1e-9)


def test_ewma_composed_no_parallel_impl():
    """The output-token estimator REUSES admission_estimator's alpha;
    no parallel EWMA class is defined in s2_predictive_budget.
    Composition discipline (PRD §11.6 AST pin)."""
    src = Path(s2.__file__).read_text(encoding="utf-8")
    # imports estimator_alpha from canonical module
    assert "from backend.core.ouroboros.governance.admission_estimator" in src
    assert "estimator_alpha" in src
    # NO parallel class definitions
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node.name not in (
                "WaitTimeEstimator", "RecentDecisionsRing",
                "SensorGovernor",
            ), f"parallel class {node.name!r} forbidden"


def test_pricing_from_yaml_fallback_safe(tmp_path, monkeypatch):
    """Missing yaml ⇒ forecast = 0 (no pricing configured); admission
    predicate clause becomes a no-op. PRD §11.5 graceful degradation."""
    nonexistent = tmp_path / "no_such_pricing.yaml"
    monkeypatch.setenv("JARVIS_S2_PRICING_YAML_PATH", str(nonexistent))
    s2._reset_pricing_cache_for_tests()
    assert s2._lookup_pricing("ide", "anything") is None
    fc = s2.forecasted_cost(1000, "ide", "anything",
                             output_token_estimator=lambda r, m: 50.0)
    assert fc == 0.0


def test_chars_per_token_env_tunable(monkeypatch):
    """chars_per_token honored from env, NOT hardcoded. PRD §11.5."""
    assert s2.chars_per_token() == pytest.approx(4.0)
    monkeypatch.setenv("JARVIS_S2_CHARS_PER_TOKEN", "2.5")
    assert s2.chars_per_token() == pytest.approx(2.5)
    # Garbage env → falls back to default
    monkeypatch.setenv("JARVIS_S2_CHARS_PER_TOKEN", "junk")
    assert s2.chars_per_token() == pytest.approx(4.0)
    # Clamp lower bound (avoid div-by-zero)
    monkeypatch.setenv("JARVIS_S2_CHARS_PER_TOKEN", "0.0")
    assert s2.chars_per_token() >= 0.5


# ============================================================================
# (2/6) MAD volatility — 6 tests incl. LOAD-BEARING outlier robustness
# ============================================================================


def test_cv_mad_zero_on_insufficient_samples():
    """< 3 samples → 0.0 (treated as 'no volatility, no penalty'). PRD §11.3.1."""
    assert s2.cost_volatility_cv_mad([]) == 0.0
    assert s2.cost_volatility_cv_mad([0.5]) == 0.0
    assert s2.cost_volatility_cv_mad([0.5, 0.6]) == 0.0


def test_cv_mad_zero_on_nonpositive_median():
    """median ≤ 0 → 0.0 (no div-by-zero). PRD §11.3.1."""
    assert s2.cost_volatility_cv_mad([0.0, 0.0, 0.0, 0.0]) == 0.0
    # Negative samples filtered to clean set; remaining median may be 0
    assert s2.cost_volatility_cv_mad([-1.0, -2.0, 0.0]) == 0.0


def test_cv_mad_stable_distribution_low_value():
    """Tight cluster around a positive median → low CV_MAD. PRD §11.3.1."""
    # Cluster around 0.002, ±10%
    samples = [0.0018, 0.0021, 0.0019, 0.0022, 0.0020, 0.0019, 0.0021]
    cv = s2.cost_volatility_cv_mad(samples)
    assert 0.0 <= cv < 0.20, f"stable cluster CV_MAD too high: {cv}"


def test_cv_mad_volatile_distribution_high_value():
    """Wide spread → high CV_MAD. PRD §11.3.1."""
    # Spread 100× between min and max — volatile
    samples = [0.001, 0.005, 0.010, 0.050, 0.100, 0.020, 0.003, 0.008]
    cv = s2.cost_volatility_cv_mad(samples)
    assert cv > 0.5, f"volatile cluster CV_MAD too low: {cv}"


def test_cv_mad_robust_to_single_outlier():
    """LOAD-BEARING (PRD §11.7 Bar C): a single 250× outlier shifts
    MAD by O(1), unlike sample stddev's O(n)."""
    base = [0.002, 0.0021, 0.0019, 0.0022, 0.0018, 0.0020, 0.0021]
    outlier = base + [0.500]   # one 250× spike
    cv_mad_base = s2.cost_volatility_cv_mad(base)
    cv_mad_outlier = s2.cost_volatility_cv_mad(outlier)
    # MAD-based CV stays close (within 50% relative); sample-stdev CV would
    # jump 10×+.
    if cv_mad_base > 0.0:
        rel_change = abs(cv_mad_outlier - cv_mad_base) / cv_mad_base
        assert rel_change < 0.5, (
            f"MAD CV swung too much under outlier: {cv_mad_base} → "
            f"{cv_mad_outlier} (rel={rel_change:.2%})"
        )
    # And the proof-of-superiority: sample-stdev CV must move >>5×
    stdev_base = statistics.stdev(base) / statistics.mean(base)
    stdev_outlier = statistics.stdev(outlier) / statistics.mean(outlier)
    assert stdev_outlier > 5.0 * stdev_base, (
        "sample stddev should swing dramatically under outlier — "
        "test data is wrong"
    )


def test_cv_mad_uses_1_4826_consistency_factor():
    """Numeric pin (PRD §11.3.1): the MAD→σ scaling constant 1.4826
    is present in the module source."""
    src = Path(s2.__file__).read_text(encoding="utf-8")
    assert "1.4826" in src, "MAD→σ consistency factor 1.4826 missing"


# ============================================================================
# (3/6) Dynamic safety factor — 6 tests
# ============================================================================


def test_dynamic_factor_clipped_to_floor():
    """Extreme volatility → clipped to safety_floor. PRD §11.3.2."""
    def big_volatility_samples(_r, _m, _w):
        # Force CV_MAD huge so the unclamped raw goes negative
        return (0.001, 0.001, 0.001, 100.0, 200.0, 300.0)

    sf = s2.dynamic_admit_safety_factor(
        "ide", "m", sample_provider=big_volatility_samples,
    )
    assert sf == pytest.approx(s2.safety_floor())


def test_dynamic_factor_clipped_to_ceiling(monkeypatch):
    """Negative penalty (synthetic) + low volatility → would exceed
    ceiling → clipped to safety_ceiling. PRD §11.3.2."""
    # Force base > ceiling so the clip ceiling arm fires
    monkeypatch.setenv("JARVIS_S2_BASE_SAFETY_FACTOR", "0.99")
    monkeypatch.setenv("JARVIS_S2_SAFETY_CEILING", "0.90")

    def stable_samples(_r, _m, _w):
        return (0.002, 0.002, 0.002, 0.002, 0.002)

    sf = s2.dynamic_admit_safety_factor(
        "ide", "m", sample_provider=stable_samples,
    )
    assert sf == pytest.approx(0.90)  # ceiling


def test_dynamic_factor_returns_base_on_zero_volatility():
    """CV_MAD = 0 → factor = base verbatim. PRD §11.3.2."""
    def perfectly_uniform_samples(_r, _m, _w):
        return (0.002, 0.002, 0.002, 0.002, 0.002)

    sf = s2.dynamic_admit_safety_factor(
        "ide", "m", sample_provider=perfectly_uniform_samples,
    )
    assert sf == pytest.approx(s2.base_safety_factor())


def test_dynamic_factor_fail_open_to_base():
    """Sample-provider raising → falls back to base_safety_factor.
    NEVER raise discipline. PRD §11.3.2."""
    def boom(_r, _m, _w):
        raise RuntimeError("synthetic stats fault")

    sf = s2.dynamic_admit_safety_factor(
        "ide", "m", sample_provider=boom,
    )
    assert sf == pytest.approx(s2.base_safety_factor())


def test_dynamic_factor_env_tunable(monkeypatch):
    """All four envelope knobs honored from env. PRD §11.5 no-hardcode."""
    monkeypatch.setenv("JARVIS_S2_BASE_SAFETY_FACTOR", "0.8")
    monkeypatch.setenv("JARVIS_S2_VOLATILITY_PENALTY", "2.0")
    monkeypatch.setenv("JARVIS_S2_SAFETY_FLOOR", "0.3")
    monkeypatch.setenv("JARVIS_S2_SAFETY_CEILING", "0.85")
    assert s2.base_safety_factor() == pytest.approx(0.8)
    assert s2.volatility_penalty() == pytest.approx(2.0)
    assert s2.safety_floor() == pytest.approx(0.3)
    assert s2.safety_ceiling() == pytest.approx(0.85)

    def cv_0_1(_r, _m, _w):
        # Synthetic samples producing CV_MAD ≈ 0.10
        return tuple([0.0018, 0.0020, 0.0022] * 5)

    sf = s2.dynamic_admit_safety_factor(
        "ide", "m", sample_provider=cv_0_1,
    )
    # base 0.8 - penalty 2.0 × CV_MAD ≈ 0.10 = 0.6 — well within [0.3, 0.85]
    assert 0.3 <= sf <= 0.85


def test_dynamic_factor_per_route_per_model():
    """Different (route, model) keys produce independent factors via
    the sample provider. PRD §11.3.2."""
    calls: List[tuple] = []

    def per_key_samples(route, model, _w):
        calls.append((route, model))
        if model == "claude":
            return (0.003, 0.005, 0.008, 0.020, 0.002, 0.006)   # volatile
        return (0.001, 0.001, 0.001, 0.001, 0.001)              # stable

    sf_claude = s2.dynamic_admit_safety_factor(
        "ide", "claude", sample_provider=per_key_samples,
    )
    sf_dw = s2.dynamic_admit_safety_factor(
        "ide", "dw", sample_provider=per_key_samples,
    )
    assert sf_claude < sf_dw, (
        "volatile claude should tighten more than stable dw "
        f"(claude={sf_claude}, dw={sf_dw})"
    )
    # The provider was queried with both keys independently
    assert ("ide", "claude") in calls
    assert ("ide", "dw") in calls


# ============================================================================
# (4/6) Admission + signal — 8 tests
# ============================================================================


def test_admit_when_budget_safe(monkeypatch):
    """Forecasted cost well under (budget × safety_factor) ⇒
    NO preemption signal needed (this test pins only the math
    surface — emission policy is the caller's; we verify the
    components a caller would compose)."""
    # Stable provider → factor ≈ base 0.9
    def stable(_r, _m, _w):
        return (0.001, 0.001, 0.001, 0.001, 0.001)
    sf = s2.dynamic_admit_safety_factor("ide", "m", sample_provider=stable)
    assert sf == pytest.approx(0.9)
    # Forecasted cost is small relative to a $10 session budget
    def out_est(_r, _m): return 50.0
    def prices(_r, _m): return (3e-6, 1.5e-5)
    fc = s2.forecasted_cost(2000, "ide", "m",
                             output_token_estimator=out_est,
                             pricing_lookup=prices)
    session_budget = 10.0
    current_spend = 0.5
    assert current_spend + fc <= session_budget * sf


def test_emit_signal_when_budget_tight():
    """High severity → governor accepts the signal + records it."""
    g = SensorGovernor()
    accepted = s2.emit_preemption_signal(
        severity=0.95, high_prio_queued=True, governor=g,
    )
    assert accepted is True
    decisions = g.recent_decisions(limit=5)
    assert any(d.sensor_name.startswith("_s2_preempt:") for d in decisions)


def test_signal_drives_existing_governor():
    """emit_preemption_signal calls SensorGovernor.apply_preemption_signal
    with the correct kwargs (composes-not-duplicates AST pin)."""
    g = SensorGovernor()
    calls: List[dict] = []
    real = g.apply_preemption_signal

    def captured(*, kind, severity, high_prio_queued, advice):
        calls.append({
            "kind": kind, "severity": severity,
            "high_prio_queued": high_prio_queued, "advice": advice,
        })
        return real(
            kind=kind, severity=severity,
            high_prio_queued=high_prio_queued, advice=advice,
        )

    g.apply_preemption_signal = captured
    s2.emit_preemption_signal(
        severity=0.6, high_prio_queued=True, kind="budget_forecast_tight",
        governor=g,
    )
    assert len(calls) == 1
    assert calls[0]["advice"] == "quarantine_low_prio_sensors"
    assert calls[0]["kind"] == "budget_forecast_tight"
    assert calls[0]["high_prio_queued"] is True


def test_governor_quarantines_only_low_prio():
    """The closed advice 'quarantine_low_prio_sensors' targets only
    BACKGROUND/SPECULATIVE — load-bearing PRD §11.4 invariant.
    Verified via the immune/quarantinable partition on SensorGovernor."""
    assert "background" in SensorGovernor._S2_QUARANTINABLE
    assert "speculative" in SensorGovernor._S2_QUARANTINABLE
    # No high-urgency leaks into the quarantinable set
    assert SensorGovernor._S2_QUARANTINABLE.isdisjoint(
        SensorGovernor._S2_HIGH_URGENCY_IMMUNE
    )


def test_high_urgency_op_never_quarantined():
    """LOAD-BEARING (PRD §11.4): IMMEDIATE / STANDARD / COMPLEX are
    in the IMMUNE set and never in the QUARANTINABLE set."""
    for u in (Urgency.IMMEDIATE, Urgency.STANDARD, Urgency.COMPLEX):
        assert u.value in SensorGovernor._S2_HIGH_URGENCY_IMMUNE
        assert u.value not in SensorGovernor._S2_QUARANTINABLE


def test_preemption_signal_severity_monotonic():
    """Higher severity is preserved verbatim (subject to [0,1] clip)
    in the recorded decision — operators can grep severity and see
    monotonic tightening over a soak window."""
    g = SensorGovernor()
    for sev in (0.1, 0.5, 0.9):
        s2.emit_preemption_signal(
            severity=sev, high_prio_queued=True, governor=g,
        )
    recs = [d for d in g.recent_decisions(limit=10)
            if d.sensor_name.startswith("_s2_preempt:")]
    severities = []
    for r in recs:
        # Severity is encoded in reason_code: "severity=X.XXX"
        for tok in r.reason_code.split():
            if tok.startswith("severity="):
                severities.append(float(tok.split("=")[1]))
    assert severities == [0.1, 0.5, 0.9]


def test_no_signal_when_governor_rejects():
    """If governor rejects (e.g., wrong advice surface), emit returns
    False. PRD §11.4 closed-advice discipline."""
    g = SensorGovernor()
    # Direct call with wrong advice → rejected
    assert g.apply_preemption_signal(
        kind="x", severity=0.5, high_prio_queued=True,
        advice="not_a_real_advice",
    ) is False


def test_forecasted_cost_fail_open():
    """Any internal fault → forecast = 0 (admission unaffected).
    NEVER raise discipline. PRD §11.4."""
    def boom_estimator(_r, _m):
        raise RuntimeError("synthetic")

    def boom_pricing(_r, _m):
        raise RuntimeError("synthetic")

    # Pricing fault → 0
    fc1 = s2.forecasted_cost(1000, "ide", "m",
                              output_token_estimator=lambda r, m: 50.0,
                              pricing_lookup=boom_pricing)
    assert fc1 == 0.0
    # Estimator fault is caught + estimator contributes 0
    fc2 = s2.forecasted_cost(1000, "ide", "m",
                              output_token_estimator=boom_estimator,
                              pricing_lookup=lambda r, m: (3e-6, 1.5e-5))
    # in_tokens × in_price still produces a small positive cost
    assert fc2 > 0.0


# ============================================================================
# (5/6) AST pins + flag registry — 5 tests
# ============================================================================


def test_ast_pin_composes_admission_estimator_ring():
    """AST pin: module composes admission_estimator (RecentDecisionsRing
    + estimator_alpha) and does NOT define a parallel class."""
    src = Path(s2.__file__).read_text(encoding="utf-8")
    assert "admission_estimator" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node.name != "RecentDecisionsRing"


def test_ast_pin_composes_sensor_governor():
    """AST pin: module composes sensor_governor; no parallel
    SensorGovernor class."""
    src = Path(s2.__file__).read_text(encoding="utf-8")
    assert "sensor_governor" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node.name != "SensorGovernor"


def test_ast_pin_no_static_0_9_in_predicate_body():
    """AST pin: the literal `0.9` only appears as a *default for the
    env-read fallback*, NEVER as an inline constant in the
    dynamic_admit_safety_factor body. PRD §11.6."""
    src = Path(s2.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (
            node.name == "dynamic_admit_safety_factor"
        ):
            # Walk the body looking for inline `Constant(0.9)` that
            # is NOT inside an os.environ.get(...) default arm.
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and (
                    isinstance(sub.value, float)
                ):
                    if sub.value == 0.9:
                        # Allowed only if it's stringified inside
                        # a Call to os.environ.get — pragmatic check
                        # via source slicing (line_no), not parent-
                        # link walking.
                        line = src.splitlines()[sub.lineno - 1]
                        assert "os.environ.get" in line, (
                            "literal 0.9 must come from env read, "
                            f"not inline: {line!r}"
                        )


def test_ast_pin_high_urgency_immune():
    """AST pin: SensorGovernor exposes the immune/quarantinable
    partition with high-urgency routes immune. PRD §11.4."""
    assert (
        SensorGovernor._S2_HIGH_URGENCY_IMMUNE.isdisjoint(
            SensorGovernor._S2_QUARANTINABLE
        )
    )
    assert SensorGovernor._S2_QUARANTINABLE == frozenset({
        Urgency.BACKGROUND.value, Urgency.SPECULATIVE.value,
    })


def test_register_flags_seeds_nine():
    """9 env knobs registered (master + 7 tunables + session_budget_usd).
    PRD §11.5 + §11 B1 revised."""
    seen: List[str] = []

    class _R:
        def register(self, spec):
            seen.append(spec.name)

    n = s2.register_flags(_R())
    assert n == 9, f"expected 9 seeds, got {n}: {seen}"
    expected = {
        "JARVIS_S2_PREDICTIVE_BUDGET_ENABLED",
        "JARVIS_S2_BASE_SAFETY_FACTOR",
        "JARVIS_S2_VOLATILITY_PENALTY",
        "JARVIS_S2_SAFETY_FLOOR",
        "JARVIS_S2_SAFETY_CEILING",
        "JARVIS_S2_CHARS_PER_TOKEN",
        "JARVIS_S2_COST_SAMPLE_WINDOW",
        "JARVIS_S2_PRICING_YAML_PATH",
        "JARVIS_S2_SESSION_BUDGET_USD",   # PRD §11 B1 wiring
    }
    assert set(seen) == expected


# ============================================================================
# (6/6) Observability — 1 test
# ============================================================================


def test_signal_observable_via_existing_decision_ring():
    """S2 preemption signals show up in SensorGovernor.recent_decisions
    AND .snapshot() (no new endpoint added — composes existing
    observability surfaces). PRD §11.4."""
    g = SensorGovernor()
    s2.emit_preemption_signal(severity=0.5, high_prio_queued=True,
                                governor=g)
    recs = g.recent_decisions(limit=10)
    signal_recs = [r for r in recs
                   if r.sensor_name.startswith("_s2_preempt:")]
    assert len(signal_recs) == 1
    snap = g.snapshot()
    assert isinstance(snap, dict)


# ============================================================================
# AST self-validation for module's shipped invariants
# ============================================================================


def test_module_ast_pin_self_validates_green():
    """The module's own register_shipped_invariants() must return
    a green validation against its own source — closes the AST
    pin loop. PRD §11.6."""
    invs = s2.register_shipped_invariants()
    assert len(invs) >= 1
    src = Path(s2.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for inv in invs:
        result = inv.validate(tree, src)
        assert result == (), (
            f"{inv.invariant_name} failed: {result}"
        )


# ============================================================================
# Op-outcome ring composition tests (additive RecentDecisionsRing surface)
# ============================================================================


def test_ring_record_op_outcome_roundtrip():
    """The additive record_op_outcome / op_outcome_samples API on
    RecentDecisionsRing roundtrips per-(route, model) cleanly."""
    r = RecentDecisionsRing()
    r.record_op_outcome("ide", "claude-sonnet-4", 100, 0.005)
    r.record_op_outcome("ide", "claude-sonnet-4", 120, 0.006)
    r.record_op_outcome("background", "dw-397b", 200, 0.0008)
    a = r.op_outcome_samples("ide", "claude-sonnet-4")
    b = r.op_outcome_samples("background", "dw-397b")
    assert len(a) == 2
    assert len(b) == 1
    assert a[0]["cost_usd"] == 0.005
    assert a[1]["cost_usd"] == 0.006


def test_ring_record_op_outcome_garbage_silently_rejected():
    """Garbage inputs (empty route/model, negative cost, NaN, non-
    numeric) silently no-op — never raise."""
    r = RecentDecisionsRing()
    r.record_op_outcome("", "claude", 100, 0.005)              # empty route
    r.record_op_outcome("ide", "", 100, 0.005)                 # empty model
    r.record_op_outcome("ide", "claude", -1, 0.005)            # negative tokens
    r.record_op_outcome("ide", "claude", 100, -0.005)          # negative cost
    r.record_op_outcome("ide", "claude", 100, float("nan"))    # NaN
    r.record_op_outcome("ide", "claude", "not-an-int", 0.005)  # type error
    assert r.op_outcome_samples("ide", "claude") == tuple()
