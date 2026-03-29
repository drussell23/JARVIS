"""Tests for exploration-source stricter risk rules in the RiskEngine.

Exploration-source rules (evaluated before general rules when source=="exploration"):
  E1. Files touching unified_supervisor              -> BLOCKED (exploration_touches_kernel)
  E2. Files in ouroboros daemon/governance internals -> BLOCKED (exploration_self_modification)
  E3. Files touching security surface paths          -> BLOCKED (exploration_touches_security)
  E4. blast_radius > 3                               -> APPROVAL_REQUIRED (exploration_blast_radius_exceeded)

Non-exploration profiles fall through to the standard rule chain unchanged.
"""

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
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
    return RiskEngine()


def _safe_exploration_profile(**overrides) -> OperationProfile:
    """A minimal exploration profile that should be SAFE_AUTO under all rules."""
    kwargs = dict(
        files_affected=[Path("backend/core/some_module.py")],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
        source="exploration",
    )
    kwargs.update(overrides)
    return OperationProfile(**kwargs)


# ---------------------------------------------------------------------------
# E1: Kernel sentinel — unified_supervisor
# ---------------------------------------------------------------------------


class TestExplorationKernelBlock:
    def test_unified_supervisor_direct_path_is_blocked(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("unified_supervisor.py")],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "exploration_touches_kernel"

    def test_unified_supervisor_nested_path_is_blocked(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/unified_supervisor.py")],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "exploration_touches_kernel"

    def test_file_containing_supervisor_substring_is_blocked(self, engine: RiskEngine) -> None:
        """Any path containing 'unified_supervisor' is caught by the substring check."""
        profile = _safe_exploration_profile(
            files_affected=[Path("tests/test_unified_supervisor_boot.py")],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "exploration_touches_kernel"

    def test_non_supervisor_file_not_blocked_by_kernel_rule(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/utils.py")],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.SAFE_AUTO


# ---------------------------------------------------------------------------
# E2: Self-modification sentinels — ouroboros daemon / governance code
# ---------------------------------------------------------------------------


_SELF_MOD_PATHS = [
    "backend/core/ouroboros/daemon/controller.py",
    "backend/core/ouroboros/vital_scan/scanner.py",
    "backend/core/ouroboros/spinal_cord/nerve.py",
    "backend/core/ouroboros/rem_sleep/state_machine.py",
    "backend/core/ouroboros/rem_epoch/cycle.py",
    "backend/core/ouroboros/governance/risk_engine.py",
    "backend/core/ouroboros/governance/orchestrator.py",
    "backend/core/ouroboros/governance/governed_loop.py",
]


class TestExplorationSelfModificationBlock:
    @pytest.mark.parametrize("path_str", _SELF_MOD_PATHS)
    def test_self_mod_path_is_blocked(self, engine: RiskEngine, path_str: str) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path(path_str)],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED, (
            f"Expected BLOCKED for {path_str!r}, got {result.tier}"
        )
        assert result.reason_code == "exploration_self_modification"

    def test_mixed_files_one_self_mod_is_blocked(self, engine: RiskEngine) -> None:
        """Even one self-mod path in the list must trigger BLOCKED."""
        profile = _safe_exploration_profile(
            files_affected=[
                Path("backend/core/utils.py"),
                Path("backend/core/ouroboros/rem_sleep/scheduler.py"),
            ],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        assert result.reason_code == "exploration_self_modification"

    def test_ouroboros_non_daemon_paths_not_blocked_by_self_mod(self, engine: RiskEngine) -> None:
        """Paths under ouroboros/ that are NOT in the sentinel list are allowed."""
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/ouroboros/governance/providers.py")],
        )
        result = engine.classify(profile)
        # Should not be BLOCKED by self-mod rule (may still hit other rules)
        assert result.reason_code != "exploration_self_modification"


# ---------------------------------------------------------------------------
# E3: Security surface sentinels
# ---------------------------------------------------------------------------


_SECURITY_PATHS = [
    "backend/core/auth/verifier.py",
    "backend/core/auth/token_store.py",
    "config/credential.json",
    "utils/secret_manager.py",
    "backend/security/token_validator.py",
    "infra/.env",
    ".env",
]


class TestExplorationSecurityBlock:
    @pytest.mark.parametrize("path_str", _SECURITY_PATHS)
    def test_security_path_is_blocked(self, engine: RiskEngine, path_str: str) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path(path_str)],
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED, (
            f"Expected BLOCKED for {path_str!r}, got {result.tier}"
        )
        assert result.reason_code == "exploration_touches_security"

    def test_non_security_path_not_blocked(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/feature_flag.py")],
        )
        result = engine.classify(profile)
        assert result.reason_code != "exploration_touches_security"


# ---------------------------------------------------------------------------
# E4: Stricter blast-radius cap (threshold = 3)
# ---------------------------------------------------------------------------


class TestExplorationBlastRadius:
    def test_blast_radius_at_threshold_is_safe(self, engine: RiskEngine) -> None:
        """blast_radius == 3 should NOT trigger APPROVAL_REQUIRED."""
        profile = _safe_exploration_profile(blast_radius=3)
        result = engine.classify(profile)
        assert result.tier is RiskTier.SAFE_AUTO

    def test_blast_radius_above_threshold_is_approval_required(self, engine: RiskEngine) -> None:
        """blast_radius == 4 exceeds the exploration cap of 3."""
        profile = _safe_exploration_profile(blast_radius=4)
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "exploration_blast_radius_exceeded"

    def test_blast_radius_well_above_threshold(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(blast_radius=10)
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "exploration_blast_radius_exceeded"

    def test_non_exploration_uses_standard_threshold(self, engine: RiskEngine) -> None:
        """Non-exploration sources use the default blast_radius threshold (5).
        blast_radius=4 with a non-exploration source should still be SAFE_AUTO
        (below the default threshold of 5).
        """
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=4,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
            source="ai_miner",
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.SAFE_AUTO

    def test_non_exploration_blast_radius_above_5_triggers_standard_rule(self, engine: RiskEngine) -> None:
        """Non-exploration source: blast_radius=6 > default threshold 5 -> APPROVAL_REQUIRED."""
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=6,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
            source="backlog",
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "blast_radius_exceeded"


# ---------------------------------------------------------------------------
# Exploration rule ordering: earlier rules win over later ones
# ---------------------------------------------------------------------------


class TestExplorationRulePriority:
    def test_kernel_rule_beats_security_rule(self, engine: RiskEngine) -> None:
        """A file that matches both kernel and security sentinels should yield
        exploration_touches_kernel (E1 evaluated before E3)."""
        profile = _safe_exploration_profile(
            files_affected=[Path("unified_supervisor_auth_token.py")],
        )
        result = engine.classify(profile)
        assert result.reason_code == "exploration_touches_kernel"

    def test_kernel_rule_beats_blast_radius(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("unified_supervisor.py")],
            blast_radius=10,
        )
        result = engine.classify(profile)
        assert result.reason_code == "exploration_touches_kernel"

    def test_self_mod_rule_beats_blast_radius(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/ouroboros/daemon/core.py")],
            blast_radius=10,
        )
        result = engine.classify(profile)
        assert result.reason_code == "exploration_self_modification"

    def test_security_rule_beats_blast_radius(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/auth/verifier.py")],
            blast_radius=10,
        )
        result = engine.classify(profile)
        assert result.reason_code == "exploration_touches_security"


# ---------------------------------------------------------------------------
# Exploration fallthrough to standard rules
# ---------------------------------------------------------------------------


class TestExplorationFallthrough:
    def test_exploration_safe_profile_is_safe_auto(self, engine: RiskEngine) -> None:
        result = engine.classify(_safe_exploration_profile())
        assert result.tier is RiskTier.SAFE_AUTO
        assert result.reason_code == "all_checks_passed"

    def test_exploration_crosses_repo_boundary_triggers_standard_rule(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(crosses_repo_boundary=True)
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "crosses_repo_boundary"

    def test_exploration_delete_triggers_standard_rule(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(change_type=ChangeType.DELETE)
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "delete_operation"

    def test_exploration_low_test_confidence_triggers_standard_rule(self, engine: RiskEngine) -> None:
        profile = _safe_exploration_profile(test_scope_confidence=0.5)
        result = engine.classify(profile)
        assert result.tier is RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "low_test_confidence"

    def test_exploration_touches_supervisor_flag_still_blocked(self, engine: RiskEngine) -> None:
        """Even if the file path doesn't contain 'unified_supervisor', the
        touches_supervisor=True flag on the profile must be respected (general Rule 1).
        This confirms exploration rules + general rules are both active.
        """
        profile = _safe_exploration_profile(
            files_affected=[Path("backend/core/boot_controller.py")],
            touches_supervisor=True,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.BLOCKED
        # Could be exploration_touches_kernel OR touches_supervisor depending on
        # whether the filename triggers the sentinel — in this case it doesn't,
        # so general Rule 1 fires.
        assert result.reason_code in ("touches_supervisor", "exploration_touches_kernel")


# ---------------------------------------------------------------------------
# Backward compatibility: profiles without source still work
# ---------------------------------------------------------------------------


class TestSourceFieldBackwardCompatibility:
    def test_profile_without_source_defaults_to_empty_string(self) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        assert profile.source == ""

    def test_profile_without_source_classifies_as_safe_auto(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        result = engine.classify(profile)
        assert result.tier is RiskTier.SAFE_AUTO

    def test_policy_version_is_v0_2_0(self, engine: RiskEngine) -> None:
        profile = OperationProfile(
            files_affected=[Path("backend/core/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        result = engine.classify(profile)
        assert result.policy_version == "v0.2.0"
        assert result.policy_version == POLICY_VERSION
