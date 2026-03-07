"""Tests for the Ouroboros governance integration module."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest


# -- GovernanceMode ----------------------------------------------------------


class TestGovernanceMode:
    """GovernanceMode enum must have exactly 5 members with string values."""

    def test_all_members_exist(self):
        from backend.core.ouroboros.governance.integration import GovernanceMode

        assert GovernanceMode.PENDING.value == "pending"
        assert GovernanceMode.SANDBOX.value == "sandbox"
        assert GovernanceMode.READ_ONLY_PLANNING.value == "read_only_planning"
        assert GovernanceMode.GOVERNED.value == "governed"
        assert GovernanceMode.EMERGENCY_STOP.value == "emergency_stop"

    def test_member_count(self):
        from backend.core.ouroboros.governance.integration import GovernanceMode

        assert len(GovernanceMode) == 5

    def test_roundtrip_from_string(self):
        from backend.core.ouroboros.governance.integration import GovernanceMode

        for member in GovernanceMode:
            assert GovernanceMode(member.value) is member


# -- CapabilityStatus --------------------------------------------------------


class TestCapabilityStatus:
    """CapabilityStatus must be frozen and carry reason string."""

    def test_creation(self):
        from backend.core.ouroboros.governance.integration import CapabilityStatus

        cs = CapabilityStatus(enabled=True, reason="ok")
        assert cs.enabled is True
        assert cs.reason == "ok"

    def test_frozen(self):
        from backend.core.ouroboros.governance.integration import CapabilityStatus

        cs = CapabilityStatus(enabled=False, reason="dep_missing")
        with pytest.raises(AttributeError):
            cs.enabled = True  # type: ignore[misc]

    def test_disabled_with_reason(self):
        from backend.core.ouroboros.governance.integration import CapabilityStatus

        cs = CapabilityStatus(enabled=False, reason="init_timeout")
        assert cs.enabled is False
        assert cs.reason == "init_timeout"


# -- GovernanceInitError -----------------------------------------------------


class TestGovernanceInitError:
    """GovernanceInitError must carry reason_code and format message."""

    def test_creation(self):
        from backend.core.ouroboros.governance.integration import GovernanceInitError

        err = GovernanceInitError("governance_init_timeout", "Factory exceeded 30s")
        assert err.reason_code == "governance_init_timeout"
        assert "governance_init_timeout" in str(err)
        assert "Factory exceeded 30s" in str(err)

    def test_is_exception(self):
        from backend.core.ouroboros.governance.integration import GovernanceInitError

        err = GovernanceInitError("test", "msg")
        assert isinstance(err, Exception)

    def test_catchable(self):
        from backend.core.ouroboros.governance.integration import GovernanceInitError

        with pytest.raises(GovernanceInitError) as exc_info:
            raise GovernanceInitError("governance_init_ledger_error", "Disk full")
        assert exc_info.value.reason_code == "governance_init_ledger_error"


# -- GovernanceConfig --------------------------------------------------------


class TestGovernanceConfig:
    """GovernanceConfig must be frozen, build from args+env, and validate."""

    def _make_args(self, **overrides):
        """Create a minimal argparse.Namespace for testing."""
        defaults = {
            "skip_governance": False,
            "governance_mode": "sandbox",
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_from_env_and_args_defaults(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.initial_mode == GovernanceMode.SANDBOX
        assert config.skip_governance is False
        assert config.ledger_dir == Path.home() / ".jarvis" / "ouroboros" / "ledger"
        assert config.gcp_daily_budget == 10.0
        assert config.startup_timeout_s == 30.0
        assert config.component_budget_s == 5.0
        assert config.canary_slices == ("backend/core/ouroboros/",)

    def test_from_env_and_args_governed_mode(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = self._make_args(governance_mode="governed")
        config = GovernanceConfig.from_env_and_args(args)
        assert config.initial_mode == GovernanceMode.GOVERNED

    def test_from_env_and_args_skip_governance(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = self._make_args(skip_governance=True)
        config = GovernanceConfig.from_env_and_args(args)
        assert config.skip_governance is True
        assert config.initial_mode == GovernanceMode.READ_ONLY_PLANNING

    def test_frozen(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        with pytest.raises(AttributeError):
            config.gcp_daily_budget = 999.0  # type: ignore[misc]

    def test_policy_version_populated(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.policy_version == "v0.1.0"

    def test_hashes_are_sha256(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        # SHA-256 hex digests are 64 chars
        assert len(config.policy_hash) == 64
        assert len(config.contract_hash) == 64
        assert len(config.config_digest) == 64

    def test_env_var_budget_override(self, monkeypatch):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        monkeypatch.setenv("OUROBOROS_GCP_DAILY_BUDGET", "25.0")
        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.gcp_daily_budget == 25.0

    def test_env_var_startup_timeout_override(self, monkeypatch):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        monkeypatch.setenv("OUROBOROS_STARTUP_TIMEOUT", "60")
        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.startup_timeout_s == 60.0

    def test_invalid_mode_raises(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args(governance_mode="invalid_mode")
        with pytest.raises(ValueError):
            GovernanceConfig.from_env_and_args(args)

    def test_contract_version_populated(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.contract_version.major == 2
        assert config.contract_version.minor == 1
        assert config.contract_version.patch == 0
