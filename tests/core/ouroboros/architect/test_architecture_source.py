"""Tests for 'architecture' source support across intake, risk, and daemon config.

Covers:
- test_architecture_is_valid_source: "architecture" accepted by IntentEnvelope
- test_architecture_priority: _PRIORITY_MAP["architecture"] == 3
- test_architect_config_fields: all 6 new DaemonConfig fields with correct defaults
- Additional architecture risk-engine rules (A1/A2/A3)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    _PRIORITY_MAP,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
    RiskTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _arch_profile(**overrides) -> OperationProfile:
    """Minimal architecture profile that should be SAFE_AUTO under all rules."""
    kwargs = dict(
        files_affected=[Path("backend/core/some_module.py")],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
        source="architecture",
    )
    kwargs.update(overrides)
    return OperationProfile(**kwargs)


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


# ---------------------------------------------------------------------------
# Source validity
# ---------------------------------------------------------------------------


def test_architecture_is_valid_source() -> None:
    """'architecture' must be in the canonical _VALID_SOURCES frozenset."""
    assert "architecture" in _VALID_SOURCES


def test_architecture_envelope_accepted() -> None:
    """make_envelope() must not raise when source='architecture'."""
    env = make_envelope(
        source="architecture",
        description="Refactor module layout",
        target_files=("backend/core/some_module.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={},
        requires_human_ack=False,
    )
    assert env.source == "architecture"


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def test_architecture_priority() -> None:
    """'architecture' must have priority 3 in _PRIORITY_MAP (same as ai_miner,
    higher than exploration/roadmap at 4)."""
    assert _PRIORITY_MAP["architecture"] == 3


def test_architecture_priority_higher_than_exploration() -> None:
    assert _PRIORITY_MAP["architecture"] < _PRIORITY_MAP["exploration"]


def test_architecture_priority_higher_than_roadmap() -> None:
    assert _PRIORITY_MAP["architecture"] < _PRIORITY_MAP["roadmap"]


# ---------------------------------------------------------------------------
# DaemonConfig architect fields
# ---------------------------------------------------------------------------


class TestArchitectConfigFields:
    def test_architect_enabled_default(self) -> None:
        cfg = DaemonConfig()
        assert cfg.architect_enabled is True

    def test_architect_max_steps_default(self) -> None:
        cfg = DaemonConfig()
        assert cfg.architect_max_steps == 10

    def test_architect_max_sagas_per_epoch_default(self) -> None:
        cfg = DaemonConfig()
        assert cfg.architect_max_sagas_per_epoch == 2

    def test_saga_step_timeout_s_default(self) -> None:
        cfg = DaemonConfig()
        assert cfg.saga_step_timeout_s == 300.0

    def test_saga_total_timeout_s_default(self) -> None:
        cfg = DaemonConfig()
        assert cfg.saga_total_timeout_s == 3600.0

    def test_acceptance_timeout_s_default(self) -> None:
        cfg = DaemonConfig()
        assert cfg.acceptance_timeout_s == 120.0

    def test_all_six_fields_exist(self) -> None:
        """All 6 new fields are present with correct defaults in one assertion."""
        cfg = DaemonConfig()
        assert cfg.architect_enabled is True
        assert cfg.architect_max_steps == 10
        assert cfg.architect_max_sagas_per_epoch == 2
        assert cfg.saga_step_timeout_s == 300.0
        assert cfg.saga_total_timeout_s == 3600.0
        assert cfg.acceptance_timeout_s == 120.0

    def test_from_env_architect_fields(self, monkeypatch) -> None:
        """Environment variables override all 6 architect fields correctly."""
        monkeypatch.setenv("OUROBOROS_ARCHITECT_ENABLED", "false")
        monkeypatch.setenv("OUROBOROS_ARCHITECT_MAX_STEPS", "20")
        monkeypatch.setenv("OUROBOROS_ARCHITECT_MAX_SAGAS_PER_EPOCH", "5")
        monkeypatch.setenv("OUROBOROS_SAGA_STEP_TIMEOUT_S", "60.0")
        monkeypatch.setenv("OUROBOROS_SAGA_TOTAL_TIMEOUT_S", "7200.0")
        monkeypatch.setenv("OUROBOROS_ACCEPTANCE_TIMEOUT_S", "240.0")

        cfg = DaemonConfig.from_env()

        assert cfg.architect_enabled is False
        assert cfg.architect_max_steps == 20
        assert cfg.architect_max_sagas_per_epoch == 5
        assert cfg.saga_step_timeout_s == 60.0
        assert cfg.saga_total_timeout_s == 7200.0
        assert cfg.acceptance_timeout_s == 240.0


# ---------------------------------------------------------------------------
# Architecture risk-engine rules
# ---------------------------------------------------------------------------


class TestArchitectureRiskRules:
    # A1a: BLOCK kernel
    def test_a1_kernel_sentinel_is_blocked(self, engine: RiskEngine) -> None:
        profile = _arch_profile(files_affected=[Path("unified_supervisor.py")])
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "architecture_touches_kernel"

    def test_a1_kernel_nested_path_blocked(self, engine: RiskEngine) -> None:
        profile = _arch_profile(files_affected=[Path("backend/unified_supervisor_boot.py")])
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "architecture_touches_kernel"

    # A1b: BLOCK security
    def test_a1_security_sentinel_is_blocked(self, engine: RiskEngine) -> None:
        profile = _arch_profile(files_affected=[Path("backend/core/auth/verifier.py")])
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "architecture_touches_security"

    def test_a1_env_file_blocked(self, engine: RiskEngine) -> None:
        profile = _arch_profile(files_affected=[Path(".env")])
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "architecture_touches_security"

    # A2: APPROVAL_REQUIRED for self-modification
    def test_a2_self_mod_requires_approval(self, engine: RiskEngine) -> None:
        profile = _arch_profile(
            files_affected=[Path("backend/core/ouroboros/governance/orchestrator.py")]
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "architecture_self_modification"

    def test_a2_daemon_path_requires_approval(self, engine: RiskEngine) -> None:
        profile = _arch_profile(
            files_affected=[Path("backend/core/ouroboros/daemon/controller.py")]
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "architecture_self_modification"

    # A3: APPROVAL_REQUIRED for cross-repo
    def test_a3_cross_repo_requires_approval(self, engine: RiskEngine) -> None:
        profile = _arch_profile(crosses_repo_boundary=True)
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "architecture_cross_repo"

    # Safe path — falls through to standard rules
    def test_architecture_safe_profile_is_safe_auto(self, engine: RiskEngine) -> None:
        result = engine.classify(_arch_profile())
        assert result.tier is RiskTier.SAFE_AUTO
        assert result.reason_code == "all_checks_passed"

    # Rule ordering: kernel before security
    def test_a1_kernel_beats_security(self, engine: RiskEngine) -> None:
        profile = _arch_profile(
            files_affected=[Path("unified_supervisor_auth_token.py")]
        )
        result = engine.classify(profile)
        assert result.reason_code == "architecture_touches_kernel"

    # Architecture rules don't leak into other sources
    def test_non_architecture_source_unaffected(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/some_module.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
            source="backlog",
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.SAFE_AUTO
