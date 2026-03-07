"""Tests for the Ouroboros Deterministic Risk Engine.

The risk engine classifies every autonomous operation into one of three tiers
(SAFE_AUTO, APPROVAL_REQUIRED, BLOCKED) using purely deterministic rules.
No LLM calls.  No heuristics.  Every test is reproducible.
"""

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    HardInvariantViolation,
    OperationProfile,
    RiskClassification,
    RiskEngine,
    RiskTier,
    POLICY_VERSION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> RiskEngine:
    """Return a default RiskEngine with strict defaults."""
    return RiskEngine()


@pytest.fixture
def safe_profile() -> OperationProfile:
    """Return a profile that should classify as SAFE_AUTO.

    Single file, modify, blast_radius=1, no security, no supervisor,
    test confidence 0.9.
    """
    return OperationProfile(
        files_affected=[Path("backend/core/utils.py")],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
    )


# ---------------------------------------------------------------------------
# TestRiskTierClassification
# ---------------------------------------------------------------------------


class TestRiskTierClassification:
    """Rule-priority classification tests."""

    def test_touches_supervisor_is_blocked(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("unified_supervisor.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=True,
            test_scope_confidence=0.95,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED

    def test_touches_security_is_blocked(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/auth.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=True,
            touches_supervisor=False,
            test_scope_confidence=0.95,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED

    def test_crosses_repo_boundary_needs_approval(
        self, engine: RiskEngine
    ) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=True,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.95,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED

    def test_delete_needs_approval(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/old_module.py")],
            change_type=ChangeType.DELETE,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.95,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "delete_operation"

    def test_high_blast_radius_needs_approval(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=6,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.95,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "blast_radius_exceeded"

    def test_many_files_needs_approval(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[
                Path("a.py"),
                Path("b.py"),
                Path("c.py"),
            ],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.95,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "too_many_files"

    def test_low_test_confidence_needs_approval(
        self, engine: RiskEngine
    ) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.6,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "low_test_confidence"

    def test_safe_single_file_fix_is_safe_auto(
        self, engine: RiskEngine, safe_profile: OperationProfile
    ) -> None:
        result = engine.classify(safe_profile)
        assert result.tier is RiskTier.SAFE_AUTO
        assert result.reason_code == "all_checks_passed"

    def test_dependency_change_needs_approval(
        self, engine: RiskEngine
    ) -> None:
        profile = OperationProfile(
            files_affected=[Path("requirements.txt")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.95,
            is_dependency_change=True,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "dependency_change"


# ---------------------------------------------------------------------------
# TestDeterminism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Verify the engine is fully deterministic."""

    def test_same_input_1000x_same_result(
        self, engine: RiskEngine, safe_profile: OperationProfile
    ) -> None:
        first = engine.classify(safe_profile)
        for _ in range(999):
            result = engine.classify(safe_profile)
            assert result.tier is first.tier
            assert result.reason_code == first.reason_code
            assert result.policy_version == first.policy_version

    def test_classification_includes_policy_version(
        self, engine: RiskEngine, safe_profile: OperationProfile
    ) -> None:
        result = engine.classify(safe_profile)
        assert result.policy_version == POLICY_VERSION


# ---------------------------------------------------------------------------
# TestHardInvariants
# ---------------------------------------------------------------------------


class TestHardInvariants:
    """enforce_invariants() must raise on any hard-invariant violation."""

    def test_contract_regression_blocks(
        self, engine: RiskEngine, safe_profile: OperationProfile
    ) -> None:
        with pytest.raises(HardInvariantViolation):
            engine.enforce_invariants(
                profile=safe_profile,
                contract_regression_delta=1,
                security_risk_delta=0,
                operator_load_delta=0,
            )

    def test_security_risk_increase_blocks(
        self, engine: RiskEngine, safe_profile: OperationProfile
    ) -> None:
        with pytest.raises(HardInvariantViolation):
            engine.enforce_invariants(
                profile=safe_profile,
                contract_regression_delta=0,
                security_risk_delta=1,
                operator_load_delta=0,
            )

    def test_all_invariants_pass(
        self, engine: RiskEngine, safe_profile: OperationProfile
    ) -> None:
        # Should NOT raise
        engine.enforce_invariants(
            profile=safe_profile,
            contract_regression_delta=0,
            security_risk_delta=0,
            operator_load_delta=0,
        )


# ---------------------------------------------------------------------------
# TestCoreOrchestrationPaths
# ---------------------------------------------------------------------------


class TestCoreOrchestrationPaths:
    """Tests for is_core_orchestration_path classification."""

    def test_create_in_core_path_needs_approval(
        self, engine: RiskEngine
    ) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/orchestrator.py")],
            change_type=ChangeType.CREATE,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.95,
            is_core_orchestration_path=True,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "core_path_structural_change"
