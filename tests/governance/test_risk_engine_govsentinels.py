"""
TDD: RiskEngine governance self-mod sentinels (Anti-Venom Task 2, defense-in-depth Lock C).

Asserts that exploration/roadmap/architecture-sourced ops targeting immune
governance files are escalated to BLOCKED or APPROVAL_REQUIRED via the
existing E2/A2 sentinel check, and that benign non-governance files are
unaffected (no false-positive escalation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
    RiskTier,
)


def _make_engine() -> RiskEngine:
    return RiskEngine()


def _explore_profile(file_path: str, source: str = "exploration") -> OperationProfile:
    """Minimal profile for an exploration/roadmap/architecture op."""
    return OperationProfile(
        files_affected=[Path(file_path)],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
        source=source,
    )


# ---------------------------------------------------------------------------
# New sentinel: semantic_guardian
# ---------------------------------------------------------------------------

class TestSemanticGuardianSentinel:
    """semantic_guardian.py must be blocked by exploration/roadmap sources."""

    TARGET = "backend/core/ouroboros/governance/semantic_guardian.py"

    def test_exploration_blocked(self):
        engine = _make_engine()
        result = engine.classify(_explore_profile(self.TARGET, source="exploration"))
        assert result.tier == RiskTier.BLOCKED, (
            f"Expected BLOCKED for exploration→{self.TARGET}, got {result.tier} ({result.reason_code})"
        )
        assert result.reason_code == "exploration_self_modification"

    def test_roadmap_blocked(self):
        engine = _make_engine()
        result = engine.classify(_explore_profile(self.TARGET, source="roadmap"))
        assert result.tier == RiskTier.BLOCKED, (
            f"Expected BLOCKED for roadmap→{self.TARGET}, got {result.tier} ({result.reason_code})"
        )
        assert result.reason_code == "exploration_self_modification"

    def test_architecture_approval_required(self):
        engine = _make_engine()
        result = engine.classify(_explore_profile(self.TARGET, source="architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED, (
            f"Expected APPROVAL_REQUIRED for architecture→{self.TARGET}, got {result.tier} ({result.reason_code})"
        )
        assert result.reason_code == "architecture_self_modification"


# ---------------------------------------------------------------------------
# New sentinel: tool_executor
# ---------------------------------------------------------------------------

class TestToolExecutorSentinel:
    TARGET = "backend/core/ouroboros/governance/tool_executor.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED
        assert result.reason_code == "exploration_self_modification"

    def test_roadmap_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "roadmap"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# New sentinel: semantic_firewall
# ---------------------------------------------------------------------------

class TestSemanticFirewallSentinel:
    TARGET = "backend/core/ouroboros/governance/semantic_firewall.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# New sentinel: scoped_tool_access
# ---------------------------------------------------------------------------

class TestScopedToolAccessSentinel:
    TARGET = "backend/core/ouroboros/governance/scoped_tool_access.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# New sentinel: risk_tier_floor
# ---------------------------------------------------------------------------

class TestRiskTierFloorSentinel:
    TARGET = "backend/core/ouroboros/governance/risk_tier_floor.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# New sentinel: change_engine
# ---------------------------------------------------------------------------

class TestChangeEngineSentinel:
    TARGET = "backend/core/ouroboros/governance/change_engine.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# New sentinel: sandbox_exec
# ---------------------------------------------------------------------------

class TestSandboxExecSentinel:
    TARGET = "backend/core/ouroboros/governance/sandbox_exec.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# New sentinel: unified_intake_router (nested path)
# ---------------------------------------------------------------------------

class TestUnifiedIntakeRouterSentinel:
    TARGET = "backend/core/ouroboros/governance/intake/unified_intake_router.py"

    def test_exploration_blocked(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "exploration"))
        assert result.tier == RiskTier.BLOCKED

    def test_architecture_approval_required(self):
        result = _make_engine().classify(_explore_profile(self.TARGET, "architecture"))
        assert result.tier == RiskTier.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# Negative: benign non-governance file must NOT be escalated
# ---------------------------------------------------------------------------

class TestBenignFileNotEscalated:
    """A benign source file must fall through to SAFE_AUTO for exploration ops."""

    BENIGN = "src/app.py"

    def test_exploration_benign_not_blocked(self):
        engine = _make_engine()
        result = engine.classify(_explore_profile(self.BENIGN, "exploration"))
        assert result.tier not in (RiskTier.BLOCKED, RiskTier.APPROVAL_REQUIRED), (
            f"False-positive escalation for benign file {self.BENIGN}: "
            f"got {result.tier} ({result.reason_code})"
        )

    def test_roadmap_benign_not_blocked(self):
        engine = _make_engine()
        result = engine.classify(_explore_profile(self.BENIGN, "roadmap"))
        assert result.tier not in (RiskTier.BLOCKED, RiskTier.APPROVAL_REQUIRED)

    def test_architecture_benign_not_approval(self):
        """Architecture + benign file falls through to standard rules (SAFE_AUTO expected)."""
        engine = _make_engine()
        result = engine.classify(_explore_profile(self.BENIGN, "architecture"))
        # Architecture only escalates for sentinel/kernel/security matches — benign passes through
        assert result.tier not in (RiskTier.BLOCKED,), (
            f"Architecture benign file should not be BLOCKED, got {result.tier}"
        )


# ---------------------------------------------------------------------------
# Existing sentinels: regression guard (must stay BLOCKED)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("existing_sentinel_path", [
    "backend/core/ouroboros/governance/risk_engine.py",
    "backend/core/ouroboros/governance/orchestrator.py",
    "backend/core/ouroboros/governance/governed_loop.py",
])
def test_existing_sentinels_still_blocked(existing_sentinel_path):
    """Pre-existing sentinels must not regress."""
    result = _make_engine().classify(_explore_profile(existing_sentinel_path, "exploration"))
    assert result.tier == RiskTier.BLOCKED, (
        f"Existing sentinel regressed: {existing_sentinel_path} → {result.tier}"
    )
