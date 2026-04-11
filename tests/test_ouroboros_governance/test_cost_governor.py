"""Tests for CostGovernor (per-op cumulative cost cap)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    OpCostCapExceeded,
    _env_float,
)


# ---------------------------------------------------------------------------
# Env helper
# ---------------------------------------------------------------------------

class TestEnvFloat:
    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_EF_1", None)
            assert _env_float("TEST_EF_1", 3.14) == 3.14

    def test_reads_valid_value(self):
        with patch.dict(os.environ, {"TEST_EF_2": "2.71"}):
            assert _env_float("TEST_EF_2", 1.0) == 2.71

    def test_invalid_value_returns_default(self):
        with patch.dict(os.environ, {"TEST_EF_3": "not_a_number"}):
            assert _env_float("TEST_EF_3", 1.5) == 1.5

    def test_negative_value_returns_default(self):
        with patch.dict(os.environ, {"TEST_EF_4": "-5.0"}):
            assert _env_float("TEST_EF_4", 2.0) == 2.0


# ---------------------------------------------------------------------------
# Cap derivation — no hardcoding, dynamic from route + complexity
# ---------------------------------------------------------------------------

class TestCapDerivation:
    def _gov(self, **cfg_overrides):
        defaults = dict(
            baseline_usd=0.10,
            retry_headroom=3.0,
            route_factors={
                "immediate":   5.0,
                "standard":    1.5,
                "complex":     4.0,
                "background":  0.5,
                "speculative": 0.25,
            },
            complexity_factors={
                "trivial":    0.5,
                "simple":     0.8,
                "light":      1.0,
                "heavy_code": 2.0,
                "complex":    3.0,
            },
            min_cap_usd=0.05,
            max_cap_usd=5.00,
            default_route_factor=1.5,
            default_complexity_factor=1.0,
            ttl_s=3600.0,
            enabled=True,
        )
        defaults.update(cfg_overrides)
        return CostGovernor(config=CostGovernorConfig(**defaults))

    def test_standard_light_baseline(self):
        # cap = 0.10 * 1.5 * 1.0 * 3.0 = 0.45
        gov = self._gov()
        cap = gov.start("op-1", "standard", "light")
        assert cap == pytest.approx(0.45)

    def test_immediate_heavy(self):
        # cap = 0.10 * 5.0 * 2.0 * 3.0 = 3.00
        gov = self._gov()
        cap = gov.start("op-2", "immediate", "heavy_code")
        assert cap == pytest.approx(3.00)

    def test_speculative_trivial_clamps_to_min(self):
        # raw = 0.10 * 0.25 * 0.5 * 3.0 = 0.0375 → clamped to min 0.05
        gov = self._gov()
        cap = gov.start("op-3", "speculative", "trivial")
        assert cap == pytest.approx(0.05)

    def test_clamps_to_max(self):
        # force a cap that would blow past max
        gov = self._gov(max_cap_usd=0.50)
        cap = gov.start("op-4", "immediate", "complex")
        assert cap == pytest.approx(0.50)

    def test_unknown_route_uses_default_factor(self):
        gov = self._gov()
        # default_route_factor=1.5, default_complexity=1.0 → 0.45
        cap = gov.start("op-5", "enigma", "wizardry")
        assert cap == pytest.approx(0.45)

    def test_empty_route_falls_back_to_standard(self):
        gov = self._gov()
        cap = gov.start("op-6", "", "")
        # empty route -> "standard", empty complexity -> "light"
        # 0.10 * 1.5 * 1.0 * 3.0 = 0.45
        assert cap == pytest.approx(0.45)

    def test_case_insensitive_keys(self):
        gov = self._gov()
        cap = gov.start("op-7", "IMMEDIATE", "Heavy_Code")
        assert cap == pytest.approx(3.00)


# ---------------------------------------------------------------------------
# Charge + exceed semantics
# ---------------------------------------------------------------------------

class TestChargeAndExceed:
    def _gov(self):
        return CostGovernor(config=CostGovernorConfig(
            baseline_usd=0.10,
            retry_headroom=1.0,
            route_factors={"standard": 1.0},
            complexity_factors={"light": 1.0},
            min_cap_usd=0.05,
            max_cap_usd=5.00,
            enabled=True,
        ))

    def test_charge_accumulates(self):
        gov = self._gov()
        gov.start("op-a", "standard", "light")  # cap 0.10
        assert gov.charge("op-a", 0.03, "dw") == pytest.approx(0.03)
        assert gov.charge("op-a", 0.04, "claude") == pytest.approx(0.07)
        assert not gov.is_exceeded("op-a")

    def test_exceed_at_cap_boundary(self):
        gov = self._gov()
        gov.start("op-b", "standard", "light")  # cap 0.10
        gov.charge("op-b", 0.10, "dw")
        assert gov.is_exceeded("op-b")

    def test_exceed_past_cap(self):
        gov = self._gov()
        gov.start("op-c", "standard", "light")  # cap 0.10
        gov.charge("op-c", 0.25, "claude")
        assert gov.is_exceeded("op-c")

    def test_charge_zero_is_noop(self):
        gov = self._gov()
        gov.start("op-d", "standard", "light")
        gov.charge("op-d", 0.0, "dw")
        gov.charge("op-d", None, "dw")  # type: ignore[arg-type]
        assert not gov.is_exceeded("op-d")
        summary = gov.summary("op-d")
        assert summary["cumulative_usd"] == 0.0
        assert summary["call_count"] == 0

    def test_negative_charge_is_noop(self):
        gov = self._gov()
        gov.start("op-e", "standard", "light")
        gov.charge("op-e", -0.50, "dw")
        assert gov.summary("op-e")["cumulative_usd"] == 0.0

    def test_charge_tracks_per_provider_breakdown(self):
        gov = self._gov()
        gov.start("op-f", "standard", "light")
        gov.charge("op-f", 0.02, "dw")
        gov.charge("op-f", 0.03, "dw")
        gov.charge("op-f", 0.05, "claude")
        summary = gov.summary("op-f")
        assert summary["provider_totals"]["dw"] == pytest.approx(0.05)
        assert summary["provider_totals"]["claude"] == pytest.approx(0.05)

    def test_charge_for_unstarted_op_auto_registers(self):
        gov = self._gov()
        gov.charge("op-g", 0.01, "dw")
        # Should now have an entry
        assert gov.summary("op-g") is not None
        assert gov.active_op_count() == 1


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_finish_removes_entry_and_returns_summary(self):
        gov = CostGovernor()
        gov.start("op-h", "standard", "light")
        gov.charge("op-h", 0.05, "dw")
        summary = gov.finish("op-h")
        assert summary is not None
        assert summary["cumulative_usd"] == pytest.approx(0.05)
        assert gov.summary("op-h") is None
        assert gov.active_op_count() == 0

    def test_finish_unknown_op_returns_none(self):
        gov = CostGovernor()
        assert gov.finish("nonexistent") is None

    def test_start_twice_refreshes_cap_preserves_spend(self):
        gov = CostGovernor(config=CostGovernorConfig(
            baseline_usd=0.10,
            retry_headroom=1.0,
            route_factors={"standard": 1.0, "immediate": 5.0},
            complexity_factors={"light": 1.0, "heavy_code": 2.0},
            enabled=True,
        ))
        cap1 = gov.start("op-i", "standard", "light")
        gov.charge("op-i", 0.05, "dw")
        cap2 = gov.start("op-i", "immediate", "heavy_code")
        assert cap2 > cap1  # cap was refreshed
        summary = gov.summary("op-i")
        assert summary["cumulative_usd"] == pytest.approx(0.05)  # spend preserved

    def test_remaining_budget(self):
        gov = CostGovernor(config=CostGovernorConfig(
            baseline_usd=0.10,
            retry_headroom=1.0,
            route_factors={"standard": 1.0},
            complexity_factors={"light": 1.0},
            enabled=True,
        ))
        gov.start("op-j", "standard", "light")  # cap 0.10
        gov.charge("op-j", 0.03, "dw")
        assert gov.remaining("op-j") == pytest.approx(0.07)
        gov.charge("op-j", 0.10, "claude")
        assert gov.remaining("op-j") == 0.0  # floored at 0


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------

class TestTTLPruning:
    def test_stale_entries_pruned_on_charge(self):
        gov = CostGovernor(config=CostGovernorConfig(
            baseline_usd=0.10,
            retry_headroom=1.0,
            ttl_s=0.01,  # 10ms
            enabled=True,
        ))
        gov.start("op-k", "standard", "light")
        gov.start("op-l", "standard", "light")
        assert gov.active_op_count() == 2

        # Sleep past TTL, then trigger a charge on a new op — pruning happens
        # on start(). Use a 3rd op to trigger pruning without interacting
        # with k or l.
        import time
        time.sleep(0.02)

        gov.start("op-m", "standard", "light")
        # k and l should have been pruned
        assert gov.summary("op-k") is None
        assert gov.summary("op-l") is None
        assert gov.summary("op-m") is not None


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------

class TestDisabled:
    def test_disabled_governor_never_exceeds(self):
        gov = CostGovernor(config=CostGovernorConfig(
            baseline_usd=0.01,
            retry_headroom=1.0,
            enabled=False,
        ))
        gov.start("op-n", "standard", "light")
        gov.charge("op-n", 100.00, "claude")
        assert not gov.is_exceeded("op-n")
        assert gov.remaining("op-n") == float("inf")

    def test_disabled_start_returns_inf(self):
        gov = CostGovernor(config=CostGovernorConfig(enabled=False))
        assert gov.start("op-o", "standard", "light") == float("inf")


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class TestOpCostCapExceeded:
    def test_carries_op_id_and_summary(self):
        summary = {"op_id": "op-p", "cumulative_usd": 0.5, "cap_usd": 0.3}
        exc = OpCostCapExceeded("op-p", summary)
        assert exc.op_id == "op-p"
        assert exc.summary["cap_usd"] == 0.3
        assert "op_cost_cap_exceeded" in str(exc)


# ---------------------------------------------------------------------------
# Orchestrator integration — the governor is wired onto GovernedOrchestrator
# and uses the phase-aware terminal picker for cost-cap aborts.
# ---------------------------------------------------------------------------

class TestOrchestratorWiring:
    def test_orchestrator_instantiates_cost_governor(self, tmp_path):
        """GovernedOrchestrator constructs a CostGovernor in __init__."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        cfg = OrchestratorConfig(project_root=tmp_path)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=cfg,
        )
        assert hasattr(orch, "_cost_governor")
        assert isinstance(orch._cost_governor, CostGovernor)

    def test_cost_cap_abort_uses_phase_aware_terminal(self):
        """Cost-cap abort uses _l2_escape_terminal semantics.

        Pre-apply phase (GENERATE) → CANCELLED.
        Post-apply phase (VERIFY)  → POSTMORTEM.
        """
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
        )
        from backend.core.ouroboros.governance.op_context import OperationPhase
        # GENERATE is pre-apply → CANCELLED
        assert GovernedOrchestrator._l2_escape_terminal(
            OperationPhase.GENERATE
        ) == OperationPhase.CANCELLED
        # VERIFY is post-apply → POSTMORTEM
        assert GovernedOrchestrator._l2_escape_terminal(
            OperationPhase.VERIFY
        ) == OperationPhase.POSTMORTEM

    def test_governor_respects_env_kill_switch(self, tmp_path, monkeypatch):
        """Setting JARVIS_OP_COST_GOVERNOR_ENABLED=false disables enforcement."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        monkeypatch.setenv("JARVIS_OP_COST_GOVERNOR_ENABLED", "false")
        cfg = OrchestratorConfig(project_root=tmp_path)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=cfg,
        )
        # Even charging $100 on an op that wasn't started yields no
        # is_exceeded=True when disabled.
        orch._cost_governor.charge("op-kill-switch", 100.0, "claude")
        assert not orch._cost_governor.is_exceeded("op-kill-switch")

    def test_governor_charge_and_exceed_flow(self, tmp_path):
        """End-to-end: start → charge → exceed → summary matches expectations."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        cfg = OrchestratorConfig(project_root=tmp_path)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=cfg,
        )
        # Use a tight in-process config
        orch._cost_governor = CostGovernor(config=CostGovernorConfig(
            baseline_usd=0.05,
            retry_headroom=1.0,
            route_factors={"standard": 1.0},
            complexity_factors={"light": 1.0},
            min_cap_usd=0.01,
            max_cap_usd=1.0,
            enabled=True,
        ))
        orch._cost_governor.start("op-wired", "standard", "light")  # cap 0.05
        assert not orch._cost_governor.is_exceeded("op-wired")
        orch._cost_governor.charge("op-wired", 0.02, "dw")
        orch._cost_governor.charge("op-wired", 0.04, "claude")
        assert orch._cost_governor.is_exceeded("op-wired")
        summary = orch._cost_governor.finish("op-wired")
        assert summary["exceeded"] is True
        assert summary["cumulative_usd"] == pytest.approx(0.06)
