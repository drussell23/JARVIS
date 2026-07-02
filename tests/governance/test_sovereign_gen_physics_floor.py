"""Sovereign-heavy physics floor for the route GENERATE budget.

Live evidence (bt-iso-1782977669): with EWMA plans + dilation live, every
attempt still died at exactly the route budget -- `adaptive gen budget: 330s
-> 375s` then `Generation attempt 1/2 failed ... :` (asyncio.TimeoutError's
empty str) ~380s later. The route-table timeout is the LAST static clock: it
wraps the whole tool loop in an outer wait_for sized for DW API rounds. When
the failover lifecycle is engaged (the op routes to the awakened 32B), the
budget must floor at rounds x expected-round wall -- the SAME physics formula
the BudgetPlan hint, the dilation, and the arm-time walls already share.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.phase_runners.generate_runner as gr


class TestExpectedAgenticCycle:
    def test_cold_physics_formula(self, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_TIMEOUT_SEED_MS", "30000")
        monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "4.0")
        monkeypatch.setenv("JARVIS_LOCAL_SEED_CTX_BASELINE", "8192")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        # 5 x 30s x 4 x (16384/8192=2) = 1200s
        assert lid.expected_agentic_cycle_s() == pytest.approx(1200.0, rel=0.05)

    def test_ewma_coupled_when_profiler_supplied(self, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "4")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 250_000.0)
        assert lid.expected_agentic_cycle_s(prof, num_ctx=16640) == pytest.approx(1000.0)

    def test_rounds_monotonic(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "3")
        three = lid.expected_agentic_cycle_s()
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "6")
        assert lid.expected_agentic_cycle_s() > three

    def test_profiler_error_falls_back_to_cold(self, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")

        def _boom(*, prompt_tokens):
            raise RuntimeError("ceiling")

        prof = SimpleNamespace(adaptive_timeout_ms=_boom)
        assert lid.expected_agentic_cycle_s(prof) == pytest.approx(1200.0, rel=0.05)


class _StubController:
    def __init__(self, state):
        self.state = state


@pytest.fixture
def fsm_state(monkeypatch):
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    holder = {"ctrl": _StubController(fl.FailoverState.DORMANT)}
    monkeypatch.setattr(fl, "get_failover_controller", lambda: holder["ctrl"])
    monkeypatch.setattr(fl, "lifecycle_enabled", lambda: True)

    def _set(name):
        holder["ctrl"] = _StubController(getattr(fl.FailoverState, name))

    return _set


class TestRunnerFloor:
    def test_engaged_lifecycle_floors_to_cycle(self, fsm_state, monkeypatch):
        fsm_state("SERVING")
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        assert gr._sovereign_physics_floor_s(375.0) == pytest.approx(1200.0, rel=0.05)

    def test_awakening_also_floors(self, fsm_state, monkeypatch):
        fsm_state("AWAKENING")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        assert gr._sovereign_physics_floor_s(375.0) > 375.0

    def test_dormant_is_identity(self, fsm_state):
        fsm_state("DORMANT")
        assert gr._sovereign_physics_floor_s(375.0) == 375.0

    def test_only_ever_raises(self, fsm_state, monkeypatch):
        fsm_state("SERVING")
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "1")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "1024")
        assert gr._sovereign_physics_floor_s(5000.0) == 5000.0

    def test_master_disable_is_identity(self, fsm_state, monkeypatch):
        fsm_state("SERVING")
        monkeypatch.setenv("JARVIS_SOVEREIGN_GEN_PHYSICS_FLOOR_ENABLED", "false")
        assert gr._sovereign_physics_floor_s(375.0) == 375.0


def test_runner_applies_floor_before_deadline_mint():
    """Source pin: the floor runs after the adaptive scale, before the
    deadline mint, so ONE floored value propagates to deadline + outer
    wait_for + tool-loop budget (the runner's own propagation contract)."""
    import pathlib
    src = pathlib.Path(gr.__file__).read_text()
    a = src.find("_gen_timeout = _adaptive_gt")
    b = src.find("deadline = datetime.now(tz=timezone.utc) + timedelta(\n                    seconds=_gen_timeout")
    assert a != -1 and b != -1 and a < b
    assert "_sovereign_physics_floor_s" in src[a:b], (
        "sovereign physics floor must run between adaptive scale and deadline mint"
    )
