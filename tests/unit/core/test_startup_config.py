# tests/unit/core/test_startup_config.py
"""Tests for backend.core.startup_config — declarative startup configuration.

Covers gate topology, budget policy, DAG validation, FSM transition
timeouts, and env-var override mechanics with range enforcement.
"""

from __future__ import annotations

import pytest

from backend.core.startup_config import (
    BudgetConfig,
    ConfigValidationError,
    GateConfig,
    SoftGatePrecondition,
    StartupConfig,
    load_startup_config,
)


# ---------------------------------------------------------------------------
# TestGateConfig
# ---------------------------------------------------------------------------


class TestGateConfig:
    """Gate topology and env-var overrides for gate timeouts."""

    def test_default_gate_config(self) -> None:
        """load_startup_config() returns 4 gates; PREWARM_GCP has correct defaults."""
        cfg = load_startup_config()
        assert len(cfg.gates) == 4
        pw = cfg.gates["PREWARM_GCP"]
        assert pw.dependencies == []
        assert pw.timeout_s == 45.0
        assert pw.on_timeout == "skip"

    def test_core_services_depends_on_prewarm(self) -> None:
        """CORE_SERVICES depends on PREWARM_GCP and fails on timeout."""
        cfg = load_startup_config()
        cs = cfg.gates["CORE_SERVICES"]
        assert cs.dependencies == ["PREWARM_GCP"]
        assert cs.on_timeout == "fail"

    def test_full_dependency_chain(self) -> None:
        """CORE_READY depends on CORE_SERVICES; DEFERRED depends on CORE_READY."""
        cfg = load_startup_config()
        assert cfg.gates["CORE_READY"].dependencies == ["CORE_SERVICES"]
        assert cfg.gates["DEFERRED_COMPONENTS"].dependencies == ["CORE_READY"]

    def test_env_override_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JARVIS_GATE_PREWARM_TIMEOUT overrides the default 45 -> 99."""
        monkeypatch.setenv("JARVIS_GATE_PREWARM_TIMEOUT", "99.0")
        cfg = load_startup_config()
        assert cfg.gates["PREWARM_GCP"].timeout_s == 99.0

    def test_env_override_bounds_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An env-var value below the minimum raises ConfigValidationError."""
        monkeypatch.setenv("JARVIS_GATE_PREWARM_TIMEOUT", "0.001")
        with pytest.raises(ConfigValidationError, match="below minimum"):
            load_startup_config()

    def test_env_override_upper_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An env-var value above the maximum raises ConfigValidationError."""
        monkeypatch.setenv("JARVIS_GATE_PREWARM_TIMEOUT", "99999")
        with pytest.raises(ConfigValidationError, match="above maximum"):
            load_startup_config()


# ---------------------------------------------------------------------------
# TestBudgetConfig
# ---------------------------------------------------------------------------


class TestBudgetConfig:
    """Budget policy defaults and env-var overrides."""

    def test_default_budget_config(self) -> None:
        """Default budget: max_hard=1, max_total=3, correct hard/soft categories."""
        cfg = load_startup_config()
        b = cfg.budget
        assert b.max_hard_concurrent == 1
        assert b.max_total_concurrent == 3
        assert set(b.hard_gate_categories) == {
            "MODEL_LOAD",
            "REACTOR_LAUNCH",
            "SUBPROCESS_SPAWN",
        }
        assert set(b.soft_gate_categories) == {"ML_INIT", "GCP_PROVISION"}

    def test_soft_gate_preconditions(self) -> None:
        """ML_INIT precondition requires CORE_READY phase and memory stability."""
        cfg = load_startup_config()
        pre = cfg.budget.soft_gate_preconditions["ML_INIT"]
        assert pre.require_phase == "CORE_READY"
        assert pre.require_memory_stable_s == 10.0

    def test_gcp_parallel_allowed(self) -> None:
        """GCP parallel flag defaults to True."""
        cfg = load_startup_config()
        assert cfg.budget.gcp_parallel_allowed is True

    def test_env_override_max_hard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JARVIS_BUDGET_MAX_HARD=2 overrides default of 1."""
        monkeypatch.setenv("JARVIS_BUDGET_MAX_HARD", "2")
        cfg = load_startup_config()
        assert cfg.budget.max_hard_concurrent == 2

    def test_budget_max_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JARVIS_BUDGET_MAX_WAIT_S=30.0 overrides default of 60."""
        monkeypatch.setenv("JARVIS_BUDGET_MAX_WAIT_S", "30.0")
        cfg = load_startup_config()
        assert cfg.budget.max_wait_s == 30.0


# ---------------------------------------------------------------------------
# TestDAGValidation
# ---------------------------------------------------------------------------


class TestDAGValidation:
    """DAG cycle detection and unknown-dependency checks."""

    def test_default_dag_is_sound(self) -> None:
        """The default gate graph passes validate_dag() without error."""
        cfg = load_startup_config()
        cfg.validate_dag()  # should not raise

    def test_cycle_detection(self) -> None:
        """A cycle in the gate graph is detected and raises ConfigValidationError."""
        cfg = load_startup_config()
        # Introduce a cycle: PREWARM_GCP -> DEFERRED_COMPONENTS -> CORE_READY -> CORE_SERVICES -> PREWARM_GCP
        cfg.gates["PREWARM_GCP"] = GateConfig(
            dependencies=["DEFERRED_COMPONENTS"],
            timeout_s=45.0,
            on_timeout="skip",
        )
        with pytest.raises(ConfigValidationError, match="cycle"):
            cfg.validate_dag()

    def test_unknown_dependency_target(self) -> None:
        """A dependency on a non-existent gate raises ConfigValidationError."""
        cfg = load_startup_config()
        cfg.gates["PREWARM_GCP"] = GateConfig(
            dependencies=["NONEXISTENT"],
            timeout_s=45.0,
            on_timeout="skip",
        )
        with pytest.raises(ConfigValidationError, match="unknown"):
            cfg.validate_dag()

    def test_unreachable_phase(self) -> None:
        """An isolated phase (no incoming or outgoing deps) is allowed."""
        cfg = load_startup_config()
        cfg.gates["ISOLATED"] = GateConfig(
            dependencies=[],
            timeout_s=10.0,
            on_timeout="skip",
        )
        cfg.validate_dag()  # should not raise

    def test_duplicate_phase(self) -> None:
        """Dict-keyed gates naturally prevent duplicate phase names."""
        cfg = load_startup_config()
        # Overwriting same key just replaces, no error
        cfg.gates["PREWARM_GCP"] = GateConfig(
            dependencies=[],
            timeout_s=30.0,
            on_timeout="skip",
        )
        assert cfg.gates["PREWARM_GCP"].timeout_s == 30.0
        cfg.validate_dag()  # should not raise


# ---------------------------------------------------------------------------
# TestTransitionTimeouts
# ---------------------------------------------------------------------------


class TestTransitionTimeouts:
    """FSM transition timeouts and lease configuration."""

    def test_default_handoff_timeout(self) -> None:
        """Default handoff_timeout_s is 10.0."""
        cfg = load_startup_config()
        assert cfg.handoff_timeout_s == 10.0

    def test_default_drain_window(self) -> None:
        """Default drain_window_s is 5.0."""
        cfg = load_startup_config()
        assert cfg.drain_window_s == 5.0

    def test_lease_config(self) -> None:
        """Default lease parameters: ttl=120, probe=15, cache_ttl=3, hysteresis=3."""
        cfg = load_startup_config()
        assert cfg.lease_ttl_s == 120.0
        assert cfg.probe_timeout_s == 15.0
        assert cfg.probe_cache_ttl_s == 3.0
        assert cfg.lease_hysteresis_count == 3

    def test_env_override_handoff_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JARVIS_HANDOFF_TIMEOUT_S=20.0 overrides the default 10."""
        monkeypatch.setenv("JARVIS_HANDOFF_TIMEOUT_S", "20.0")
        cfg = load_startup_config()
        assert cfg.handoff_timeout_s == 20.0
