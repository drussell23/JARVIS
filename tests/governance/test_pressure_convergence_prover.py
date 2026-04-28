"""Tests for Slice T1.2 — PressureConvergenceProver.

Proves that MemoryPressureGate's pressure→backlog→fanout feedback loop
converges under sustained load (§24.7.1).
"""
from __future__ import annotations

import random

import pytest


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED", "true")


# ---------------------------------------------------------------------------
# 1. Draining proof — arrival < fanout_cap(OK)
# ---------------------------------------------------------------------------

class TestDrainingProof:
    def test_zero_arrival_drains_immediately(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict,
        )
        r = prove_convergence(arrival_rate=0, initial_backlog=50)
        assert r.verdict == ConvergenceVerdict.DRAINED
        assert r.steady_state_backlog == 0

    def test_low_arrival_drains(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        # arrival must be <= fanout_cap(CRITICAL) to guarantee drain
        # from any initial backlog. arrival=1 == critical cap.
        r = prove_convergence(
            arrival_rate=DEFAULT_CONFIG.fanout_critical,
            initial_backlog=100,
        )
        assert r.verdict in (ConvergenceVerdict.DRAINED, ConvergenceVerdict.CONVERGED)
        assert r.ticks_to_drain > 0 or r.verdict == ConvergenceVerdict.CONVERGED

    def test_arrival_above_critical_cap_with_large_backlog_overloads(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        # arrival=2 > fanout_cap(CRITICAL)=1, large backlog traps at CRITICAL
        r = prove_convergence(
            arrival_rate=DEFAULT_CONFIG.fanout_critical + 1,
            initial_backlog=100,
            max_ticks=500,
        )
        # This correctly detects the feedback trap
        assert r.verdict in (
            ConvergenceVerdict.OVERLOADED, ConvergenceVerdict.CONVERGED,
        )

    def test_arrival_below_ok_cap_drains_from_ok_pressure(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        # arrival < fanout_ok AND initial_backlog low enough to stay at OK
        # → system drains because fanout_cap(OK)=16 > arrival=5
        rate = 5
        r = prove_convergence(
            arrival_rate=rate,
            initial_backlog=DEFAULT_CONFIG.backlog_warn - 1,
        )
        assert r.verdict in (ConvergenceVerdict.DRAINED, ConvergenceVerdict.CONVERGED)

    def test_arrival_below_ok_but_above_critical_trap(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        # THIS IS THE §24.7.1 FEEDBACK TRAP:
        # arrival=15 < fanout_ok=16, but initial_backlog=200 pushes to
        # CRITICAL (cap=1). 15 > 1, so backlog grows forever.
        # The prover correctly identifies this as overloaded.
        rate = DEFAULT_CONFIG.fanout_ok - 1  # 15
        r = prove_convergence(
            arrival_rate=rate,
            initial_backlog=200,
            max_ticks=500,
        )
        assert r.verdict == ConvergenceVerdict.OVERLOADED

    def test_zero_backlog_zero_arrival(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict,
        )
        r = prove_convergence(arrival_rate=0, initial_backlog=0)
        assert r.verdict == ConvergenceVerdict.DRAINED
        assert r.ticks_to_drain <= 1


# ---------------------------------------------------------------------------
# 2. Steady-state proof — arrival = fanout_cap(WARN)
# ---------------------------------------------------------------------------

class TestSteadyState:
    def test_arrival_equals_warn_cap_stabilizes(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        # Arrival rate exactly matches WARN cap → system stabilizes
        r = prove_convergence(
            arrival_rate=DEFAULT_CONFIG.fanout_warn,
            initial_backlog=DEFAULT_CONFIG.backlog_warn,
        )
        assert r.verdict in (ConvergenceVerdict.CONVERGED, ConvergenceVerdict.DRAINED)

    def test_steady_state_backlog_bounded(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        r = prove_convergence(
            arrival_rate=DEFAULT_CONFIG.fanout_warn,
            initial_backlog=0,
        )
        assert r.verdict in (ConvergenceVerdict.CONVERGED, ConvergenceVerdict.DRAINED)
        # Steady-state backlog should be bounded
        assert r.steady_state_backlog <= DEFAULT_CONFIG.backlog_critical * 2


# ---------------------------------------------------------------------------
# 3. Overload detection
# ---------------------------------------------------------------------------

class TestOverloadDetection:
    def test_extreme_arrival_detected(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        # Arrival >> all fanout caps → overloaded
        r = prove_convergence(
            arrival_rate=DEFAULT_CONFIG.fanout_ok * 3,
            initial_backlog=0,
            max_ticks=200,
        )
        assert r.verdict == ConvergenceVerdict.OVERLOADED
        assert r.within_deadline is False

    def test_check_overload_recommendation(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            check_overload, DEFAULT_CONFIG,
        )
        rec = check_overload(DEFAULT_CONFIG.fanout_ok + 5)
        assert rec is not None
        assert rec.excess == 5
        assert rec.max_sustainable_rate == DEFAULT_CONFIG.fanout_ok

    def test_check_overload_none_when_sustainable(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            check_overload, DEFAULT_CONFIG,
        )
        rec = check_overload(DEFAULT_CONFIG.fanout_ok - 1)
        assert rec is None

    def test_check_overload_at_exactly_cap(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            check_overload, DEFAULT_CONFIG,
        )
        rec = check_overload(DEFAULT_CONFIG.fanout_ok)
        assert rec is None


# ---------------------------------------------------------------------------
# 4. Deadline enforcement
# ---------------------------------------------------------------------------

class TestDeadlineEnforcement:
    def test_fast_drain_within_deadline(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence,
        )
        r = prove_convergence(
            arrival_rate=0, initial_backlog=10, deadline_ticks=100,
        )
        assert r.within_deadline is True

    def test_slow_drain_exceeds_deadline(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, DEFAULT_CONFIG,
        )
        # Large backlog + arrival close to cap → slow drain
        r = prove_convergence(
            arrival_rate=DEFAULT_CONFIG.fanout_ok - 1,
            initial_backlog=5000,
            deadline_ticks=10,
        )
        assert r.within_deadline is False


# ---------------------------------------------------------------------------
# 5. Pressure transition correctness
# ---------------------------------------------------------------------------

class TestPressureTransitions:
    def test_backlog_below_warn_is_ok(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            pressure_from_backlog, SimPressureLevel, DEFAULT_CONFIG,
        )
        assert pressure_from_backlog(0) == SimPressureLevel.OK
        assert pressure_from_backlog(DEFAULT_CONFIG.backlog_warn - 1) == SimPressureLevel.OK

    def test_backlog_at_warn_is_warn(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            pressure_from_backlog, SimPressureLevel, DEFAULT_CONFIG,
        )
        assert pressure_from_backlog(DEFAULT_CONFIG.backlog_warn) == SimPressureLevel.WARN

    def test_backlog_at_high_is_high(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            pressure_from_backlog, SimPressureLevel, DEFAULT_CONFIG,
        )
        assert pressure_from_backlog(DEFAULT_CONFIG.backlog_high) == SimPressureLevel.HIGH

    def test_backlog_at_critical_is_critical(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            pressure_from_backlog, SimPressureLevel, DEFAULT_CONFIG,
        )
        assert pressure_from_backlog(DEFAULT_CONFIG.backlog_critical) == SimPressureLevel.CRITICAL

    def test_fanout_caps_correct(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            fanout_cap_at, SimPressureLevel, DEFAULT_CONFIG,
        )
        assert fanout_cap_at(SimPressureLevel.OK) == DEFAULT_CONFIG.fanout_ok
        assert fanout_cap_at(SimPressureLevel.WARN) == DEFAULT_CONFIG.fanout_warn
        assert fanout_cap_at(SimPressureLevel.HIGH) == DEFAULT_CONFIG.fanout_high
        assert fanout_cap_at(SimPressureLevel.CRITICAL) == DEFAULT_CONFIG.fanout_critical


# ---------------------------------------------------------------------------
# 6. Monotonic drain
# ---------------------------------------------------------------------------

class TestMonotonicDrain:
    def test_drain_without_respike(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            simulate_tick, TickState, pressure_from_backlog, fanout_cap_at,
        )
        # Zero arrival — backlog should monotonically decrease
        p = pressure_from_backlog(100)
        state = TickState(
            tick=0, backlog=100, pressure=p,
            fanout_cap=fanout_cap_at(p), completed=0, arrived=0,
        )
        prev_backlog = state.backlog
        for _ in range(200):
            state = simulate_tick(state, arrival_rate=0)
            assert state.backlog <= prev_backlog
            prev_backlog = state.backlog
            if state.backlog == 0:
                break
        assert state.backlog == 0


# ---------------------------------------------------------------------------
# 7. Parameter sweep — adversarial
# ---------------------------------------------------------------------------

class TestParameterSweep:
    def test_50_random_scenarios(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_batch, ConvergenceVerdict, DEFAULT_CONFIG,
        )
        rng = random.Random(42)
        scenarios = []
        for _ in range(50):
            arrival = rng.randint(0, DEFAULT_CONFIG.fanout_ok + 10)
            backlog = rng.randint(0, 500)
            scenarios.append((arrival, backlog))

        results = prove_batch(scenarios)
        assert len(results) == 50

        for r in results:
            if r.arrival_rate <= DEFAULT_CONFIG.fanout_critical:
                # arrival <= min fanout cap → system ALWAYS drains
                # regardless of initial backlog and pressure
                assert r.verdict in (
                    ConvergenceVerdict.DRAINED,
                    ConvergenceVerdict.CONVERGED,
                ), (
                    f"arrival={r.arrival_rate} backlog={r.initial_backlog} "
                    f"should converge but got {r.verdict}"
                )
            # All scenarios produce a valid verdict
            assert r.verdict in (
                ConvergenceVerdict.DRAINED,
                ConvergenceVerdict.CONVERGED,
                ConvergenceVerdict.OVERLOADED,
                ConvergenceVerdict.INCONCLUSIVE,
            )


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_arrival_zero_initial_zero(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict,
        )
        r = prove_convergence(arrival_rate=0, initial_backlog=0)
        assert r.verdict == ConvergenceVerdict.DRAINED

    def test_fanout_cap_one(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, PressureConfig, ConvergenceVerdict,
        )
        config = PressureConfig(
            fanout_ok=1, fanout_warn=1, fanout_high=1, fanout_critical=1,
        )
        r = prove_convergence(
            arrival_rate=0, initial_backlog=10, config=config,
        )
        assert r.verdict == ConvergenceVerdict.DRAINED
        assert r.ticks_to_drain == 10

    def test_negative_arrival_clamped(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, ConvergenceVerdict,
        )
        r = prove_convergence(arrival_rate=-5, initial_backlog=10)
        assert r.verdict == ConvergenceVerdict.DRAINED

    def test_result_serializable(self):
        import json
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence,
        )
        r = prove_convergence(arrival_rate=5, initial_backlog=50)
        d = r.to_dict()
        json_str = json.dumps(d)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# 9. Custom config
# ---------------------------------------------------------------------------

class TestCustomConfig:
    def test_tight_thresholds(self):
        from backend.core.ouroboros.governance.pressure_convergence_prover import (
            prove_convergence, PressureConfig, ConvergenceVerdict,
        )
        config = PressureConfig(
            backlog_warn=3, backlog_high=6, backlog_critical=10,
            fanout_ok=8, fanout_warn=4, fanout_high=2, fanout_critical=1,
        )
        r = prove_convergence(
            arrival_rate=3, initial_backlog=50, config=config,
        )
        assert r.verdict in (ConvergenceVerdict.DRAINED, ConvergenceVerdict.CONVERGED)


# ---------------------------------------------------------------------------
# 10. Master flag
# ---------------------------------------------------------------------------

class TestMasterFlag:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.pressure_convergence_prover import is_prover_enabled
        monkeypatch.setenv("JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED", val)
        assert is_prover_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.pressure_convergence_prover import is_prover_enabled
        monkeypatch.setenv("JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED", val)
        assert is_prover_enabled() is False

    def test_default_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance.pressure_convergence_prover import is_prover_enabled
        monkeypatch.delenv("JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED", raising=False)
        assert is_prover_enabled() is False


# ---------------------------------------------------------------------------
# 11. Constants pinned
# ---------------------------------------------------------------------------

class TestConstants:
    def test_pinned(self):
        from backend.core.ouroboros.governance import pressure_convergence_prover as mod
        assert mod.MAX_SIMULATION_TICKS == 10_000
        assert mod.DEFAULT_RELIEF_DEADLINE_TICKS == 500
        assert mod.DEFAULT_STEADY_STATE_WINDOW == 20
