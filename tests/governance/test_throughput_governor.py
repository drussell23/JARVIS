"""Measured throughput sizes the lane count — and says how it knows.

The defect this guards: `BackgroundAgentPool` ran a CONSTANT number of lanes
against one serving endpoint. A single local GPU serves concurrent requests
SERIALLY, so N lanes multiply each op's wall-clock instead of its output, and
the tail blows the route budget (`tool_loop_deadline_exceeded`).

These tests pin the two things that make the fix a fix rather than a differently
-spelled constant:

  1. the lane count is DERIVED from measured latency and moves with it, and
  2. every verdict states the PROVENANCE of the number it is derived from, so
     a clamp is never read as a measurement.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.core.ouroboros.governance import local_inference_director as lid
from backend.core.ouroboros.governance import throughput_governor as tg
from backend.core.ouroboros.governance import route_budgets as rb


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Every test gets its own physics ledger + a fresh governor.

    Without this the tests would read `.jarvis/latency_physics.json` — the
    real box's physics — and would pass or fail based on what the developer's
    laptop happened to have measured that morning.
    """
    d = tempfile.mkdtemp()
    monkeypatch.setenv("JARVIS_LATENCY_LEDGER_PATH", os.path.join(d, "l.json"))
    monkeypatch.delenv("JARVIS_THROUGHPUT_GOVERNOR_ENABLED", raising=False)
    tg.reset_for_tests()
    yield
    tg.reset_for_tests()


def _seed_ledger(*, total_ms: float, ttft_ms: float = 50.0,
                 out_tokens: int = 400, n: int = 8) -> None:
    """Write n real-shaped samples into the isolated ledger."""
    cfg = lid.LocalConfig.from_env()
    p = lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg))
    for _ in range(n):
        p.record(ttft_ms=ttft_ms, total_ms=total_ms, output_tokens=out_tokens)


class TestTheNumberIsDerived:
    def test_faster_physics_buys_more_lanes(self):
        """The whole point: lanes track measurement. If this can only ever
        return one value it is a hardcode wearing a governor's clothes."""
        _seed_ledger(total_ms=900.0)
        fast = tg.ThroughputGovernor().evaluate(budget_s=180)

        _seed_ledger(total_ms=40_000.0)
        slow = tg.ThroughputGovernor().evaluate(budget_s=180)

        assert fast.governed and slow.governed
        assert fast.lanes > slow.lanes, (
            f"lanes did not move with physics: fast={fast.lanes} "
            f"slow={slow.lanes}")

    def test_a_wider_budget_buys_more_lanes(self):
        _seed_ledger(total_ms=900.0)
        g = tg.ThroughputGovernor()
        narrow = g.evaluate(budget_s=30)
        g.invalidate()
        wide = g.evaluate(budget_s=600)
        assert wide.lanes >= narrow.lanes

    def test_it_never_returns_zero_lanes(self):
        """A zero-lane pool is a STALLED queue — a worse failure than a slow
        one. When even one op cannot fit, the honest answer is 'run one and
        let the existing deadline machinery record the breach'."""
        _seed_ledger(total_ms=600_000.0)
        v = tg.ThroughputGovernor().evaluate(budget_s=1)
        assert v.lanes == 1
        assert v.detail["raw_lanes"] < 1  # the math really did want zero


class TestItSaysHowItKnows:
    def test_no_samples_is_labelled_seeded_not_measured(self):
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.provenance == "seeded"
        assert v.measured is False

    def test_real_samples_are_labelled_measured(self):
        _seed_ledger(total_ms=900.0)
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.provenance == "measured"
        assert v.measured is True

    def test_a_saturated_estimate_is_labelled_a_lower_bound(self):
        """`adaptive_timeout_ms` CLAMPS to its ceiling. A clamp reported as a
        measurement is a fabricated reading — the same class the Advisor's
        blast-radius provenance work exists to prevent."""
        _seed_ledger(total_ms=400_000.0)
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.provenance == "ceiling_clamped"
        assert v.detail["saturated"] is True
        assert "LOWER BOUND" in v.reason

    def test_measured_provenance_survives_a_fresh_instance(self):
        """`LatencyProfiler.is_calibrated()` is a per-instance scout flag the
        ledger does NOT persist, so a fresh instance reports False while
        serving genuinely measured numbers. The verdict must key off sample
        count (`is_warm`), which does survive."""
        _seed_ledger(total_ms=900.0)
        cfg = lid.LocalConfig.from_env()
        fresh = lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg))
        assert fresh.is_calibrated() is False   # the trap
        assert fresh.is_warm() is True          # the truth
        assert tg.ThroughputGovernor().evaluate(budget_s=180).measured is True


class TestItComposesRatherThanCompetes:
    def test_the_memory_gate_can_only_lower_the_count(self, monkeypatch):
        _seed_ledger(total_ms=200.0)          # very fast -> many raw lanes
        ungated = tg.ThroughputGovernor().evaluate(budget_s=600)

        class _Decision:
            allowed, n_allowed, level = True, 2, "high"

        class _Gate:
            def can_fanout(self, n):
                return _Decision()

        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        monkeypatch.setattr(mpg, "get_default_gate", lambda: _Gate())
        monkeypatch.setattr(mpg, "is_enabled", lambda: True)
        gated = tg.ThroughputGovernor().evaluate(budget_s=600)

        assert gated.lanes == 2
        assert gated.lanes <= ungated.lanes

    def test_a_gate_answering_higher_than_asked_cannot_raise_lanes(
            self, monkeypatch):
        """Both are CEILING authorities. If the gate ever answered above what
        it was asked, taking its number verbatim would let a memory gate
        RAISE concurrency — the opposite of its job."""
        _seed_ledger(total_ms=40_000.0)

        class _Decision:
            allowed, n_allowed, level = True, 99, "ok"

        class _Gate:
            def can_fanout(self, n):
                return _Decision()

        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        monkeypatch.setattr(mpg, "get_default_gate", lambda: _Gate())
        monkeypatch.setattr(mpg, "is_enabled", lambda: True)
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.lanes <= v.detail["raw_lanes"] or v.lanes == 1


class TestItDegradesToTodaysBehaviour:
    def test_master_off_is_ungoverned(self, monkeypatch):
        monkeypatch.setenv("JARVIS_THROUGHPUT_GOVERNOR_ENABLED", "0")
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.governed is False and v.lanes == 0

    def test_ungoverned_is_distinguishable_from_one_lane(self):
        """`lanes=1` and 'we could not measure' must never be the same value:
        the first is a decision, the second is an absence of one."""
        assert tg._ungoverned("x").governed is False
        _seed_ledger(total_ms=600_000.0)
        assert tg.ThroughputGovernor().evaluate(budget_s=1).governed is True

    def test_a_broken_profiler_degrades_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(
            tg.ThroughputGovernor, "_profiler_and_config",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.governed is False

    def test_a_nonsense_budget_is_ungoverned(self):
        g = tg.ThroughputGovernor()
        assert g.evaluate(budget_s=0).governed is False
        g.invalidate()
        assert g.evaluate(budget_s=-5).governed is False

    def test_a_wedged_estimator_is_information_not_absence(self, monkeypatch):
        """A runaway-latency refusal MEANS something: the model is wedged, so
        the honest lane count is the minimum. Degrading to UNGOVERNED there
        would tell the pool to keep whatever it had."""
        class _Wedged:
            def adaptive_timeout_ms(self, **_):
                raise lid.UnrecoverableInferenceLatency("wedged")

            def is_warm(self):
                return True

        monkeypatch.setattr(
            tg.ThroughputGovernor, "_profiler_and_config",
            staticmethod(lambda: (_Wedged(), lid.LocalConfig.from_env())))
        v = tg.ThroughputGovernor().evaluate(budget_s=180)
        assert v.governed is True and v.lanes == 1
        assert v.provenance == "wedged"


class TestCaching:
    def test_the_verdict_is_cached_then_invalidatable(self):
        _seed_ledger(total_ms=900.0)
        g = tg.ThroughputGovernor()
        first = g.evaluate(budget_s=180)
        assert g.evaluate(budget_s=999999) is first     # served from cache
        g.invalidate()
        assert g.evaluate(budget_s=999999) is not first

    def test_ttl_zero_disables_caching(self, monkeypatch):
        monkeypatch.setenv("JARVIS_THROUGHPUT_VERDICT_TTL_S", "0")
        _seed_ledger(total_ms=900.0)
        g = tg.ThroughputGovernor()
        assert g.evaluate(budget_s=180) is not g.evaluate(budget_s=180)


class TestRouteBudgets:
    def test_the_table_has_exactly_one_home(self):
        """It was written twice, byte-identical, in orchestrator and
        generate_runner. Both must now READ the canonical resolver — a
        re-inlined private copy is how the drift comes back."""
        import inspect
        from backend.core.ouroboros.governance import orchestrator
        from backend.core.ouroboros.governance.phase_runners import generate_runner
        for mod in (orchestrator, generate_runner):
            src = inspect.getsource(mod)
            assert "route_generation_budgets" in src, mod.__name__
            assert "JARVIS_GEN_TIMEOUT_STANDARD_S" not in src, (
                f"{mod.__name__} re-inlined its own copy of the route table")

    def test_unknown_route_returns_the_callers_default(self):
        """Callers already hold a meaningful fallback
        (`config.generation_timeout_s`); inventing one here would silently
        overrule it."""
        assert rb.route_generation_budget_s("not-a-route", 42.0) == 42.0

    def test_a_malformed_env_value_falls_back_to_its_own_default(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_GEN_TIMEOUT_BACKGROUND_S", "not-a-number")
        assert rb.route_generation_budget_s("background") == 180.0

    def test_the_fast_lane_ceiling_is_genuinely_tighter(self):
        b = rb.route_generation_budgets()
        assert b["immediate"] < b["background"]
