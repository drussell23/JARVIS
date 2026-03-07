"""Tests for the Ouroboros governance integration module."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


# -- GovernanceStack ---------------------------------------------------------


def _make_mock_stack_components():
    """Create mock governance components for testing GovernanceStack."""
    controller = MagicMock()
    controller.start = AsyncMock()
    controller.stop = AsyncMock()
    controller.mode = MagicMock()
    controller.mode.value = "sandbox"
    controller.writes_allowed = True

    risk_engine = MagicMock()
    ledger = MagicMock()
    ledger.get_history = AsyncMock(return_value=[])

    comm = MagicMock()
    lock_manager = MagicMock()
    break_glass = MagicMock()
    change_engine = MagicMock()
    resource_monitor = MagicMock()

    degradation = MagicMock()
    degradation.mode = MagicMock()
    degradation.mode.value = 0  # FULL_AUTONOMY
    degradation.mode.name = "FULL_AUTONOMY"

    routing = MagicMock()
    routing.cost_guardrail = MagicMock()
    routing.cost_guardrail.over_budget = False
    routing.cost_guardrail.daily_usage = 0.0

    canary = MagicMock()
    canary.slices = {}
    canary.is_file_allowed = MagicMock(return_value=True)

    contract_checker = MagicMock()
    contract_checker.check_before_write = MagicMock(return_value=True)

    return {
        "controller": controller,
        "risk_engine": risk_engine,
        "ledger": ledger,
        "comm": comm,
        "lock_manager": lock_manager,
        "break_glass": break_glass,
        "change_engine": change_engine,
        "resource_monitor": resource_monitor,
        "degradation": degradation,
        "routing": routing,
        "canary": canary,
        "contract_checker": contract_checker,
        "event_bridge": None,
        "blast_adapter": None,
        "learning_bridge": None,
        "policy_version": "v0.1.0",
        "capabilities": {},
    }


class TestGovernanceStack:
    """GovernanceStack lifecycle, write gate, and health."""

    def _make_stack(self, **overrides):
        from backend.core.ouroboros.governance.integration import GovernanceStack

        components = _make_mock_stack_components()
        components.update(overrides)
        return GovernanceStack(**components)

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        stack = self._make_stack()
        await stack.start()
        await stack.start()  # second call is no-op
        stack.controller.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        stack = self._make_stack()
        await stack.start()
        await stack.stop()
        await stack.stop()  # second call is no-op
        stack.controller.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        stack = self._make_stack()
        await stack.stop()  # no error
        stack.controller.stop.assert_not_awaited()

    def test_health_returns_structured_dict(self):
        stack = self._make_stack()
        stack._started = True
        health = stack.health()
        assert "mode" in health
        assert "policy_version" in health
        assert "capabilities" in health
        assert "degradation_mode" in health
        assert "canary_slices" in health

    def test_can_write_before_start_denied(self):
        stack = self._make_stack()
        allowed, reason = stack.can_write({"files": []})
        assert allowed is False
        assert reason == "governance_not_started"

    @pytest.mark.asyncio
    async def test_can_write_when_writes_allowed(self):
        stack = self._make_stack()
        await stack.start()
        allowed, reason = stack.can_write({"files": ["foo.py"]})
        assert allowed is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_can_write_denied_by_controller(self):
        stack = self._make_stack()
        stack.controller.writes_allowed = False
        await stack.start()
        allowed, reason = stack.can_write({"files": []})
        assert allowed is False
        assert "mode_" in reason

    @pytest.mark.asyncio
    async def test_can_write_denied_by_degradation(self):
        stack = self._make_stack()
        stack.degradation.mode.value = 2  # READ_ONLY_PLANNING
        stack.degradation.mode.name = "READ_ONLY_PLANNING"
        await stack.start()
        allowed, reason = stack.can_write({"files": []})
        assert allowed is False
        assert "degradation_" in reason

    @pytest.mark.asyncio
    async def test_can_write_denied_by_canary(self):
        stack = self._make_stack()
        stack.canary.is_file_allowed = MagicMock(return_value=False)
        await stack.start()
        allowed, reason = stack.can_write({"files": ["blocked.py"]})
        assert allowed is False
        assert "canary_not_promoted" in reason

    @pytest.mark.asyncio
    async def test_can_write_denied_by_contract(self):
        from backend.core.ouroboros.governance.contract_gate import ContractVersion

        stack = self._make_stack()
        stack.contract_checker.check_before_write = MagicMock(return_value=False)
        await stack.start()
        allowed, reason = stack.can_write({
            "files": [],
            "proposed_contract_version": ContractVersion(3, 0, 0),
        })
        assert allowed is False
        assert reason == "contract_incompatible"

    @pytest.mark.asyncio
    async def test_replay_decision_no_entry(self):
        stack = self._make_stack()
        stack.ledger.get_history = AsyncMock(return_value=[])
        result = await stack.replay_decision("nonexistent-op-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_replay_decision_with_entry(self):
        from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
        from backend.core.ouroboros.governance.risk_engine import (
            RiskClassification,
            RiskTier,
        )

        entry = LedgerEntry(
            op_id="test-op-1",
            state=OperationState.PLANNED,
            data={
                "profile": {
                    "files_affected": ["test.py"],
                    "change_type": "MODIFY",
                    "blast_radius": 1,
                    "crosses_repo_boundary": False,
                    "touches_security_surface": False,
                    "touches_supervisor": False,
                    "test_scope_confidence": 0.9,
                },
                "risk_tier": "SAFE_AUTO",
            },
        )
        stack = self._make_stack()
        stack.ledger.get_history = AsyncMock(return_value=[entry])
        stack.risk_engine.classify = MagicMock(
            return_value=RiskClassification(
                tier=RiskTier.SAFE_AUTO,
                reason_code="safe_single_file",
                policy_version="v0.1.0",
            )
        )

        result = await stack.replay_decision("test-op-1")
        assert result is not None
        assert result["op_id"] == "test-op-1"
        assert result["replayed_tier"] == "SAFE_AUTO"
        assert result["match"] is True

    @pytest.mark.asyncio
    async def test_drain_is_callable(self):
        stack = self._make_stack()
        await stack.drain()  # should not raise


# -- create_governance_stack -------------------------------------------------


class TestCreateGovernanceStack:
    """Factory function tests."""

    def _make_config(self, tmp_path):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = argparse.Namespace(skip_governance=False, governance_mode="sandbox")
        base = GovernanceConfig.from_env_and_args(args)
        return GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=base.policy_version,
            policy_hash=base.policy_hash,
            contract_version=base.contract_version,
            contract_hash=base.contract_hash,
            config_digest=base.config_digest,
            initial_mode=base.initial_mode,
            skip_governance=base.skip_governance,
            canary_slices=base.canary_slices,
            gcp_daily_budget=base.gcp_daily_budget,
            startup_timeout_s=base.startup_timeout_s,
            component_budget_s=base.component_budget_s,
        )

    @pytest.mark.asyncio
    async def test_creates_stack_successfully(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceStack,
            create_governance_stack,
        )

        config = self._make_config(tmp_path)
        stack = await create_governance_stack(config)
        assert isinstance(stack, GovernanceStack)
        assert stack.policy_version == "v0.1.0"

    @pytest.mark.asyncio
    async def test_optional_bridges_missing(self, tmp_path):
        from backend.core.ouroboros.governance.integration import create_governance_stack

        config = self._make_config(tmp_path)
        stack = await create_governance_stack(config)
        assert stack.event_bridge is None
        assert stack.blast_adapter is None
        assert stack.learning_bridge is None
        assert stack.capabilities["event_bridge"].enabled is False
        assert stack.capabilities["event_bridge"].reason == "dep_missing"

    @pytest.mark.asyncio
    async def test_optional_bridges_present(self, tmp_path):
        from backend.core.ouroboros.governance.integration import create_governance_stack

        config = self._make_config(tmp_path)
        stack = await create_governance_stack(
            config,
            event_bus=MagicMock(),
            oracle=MagicMock(),
            learning_memory=MagicMock(),
        )
        assert stack.event_bridge is not None
        assert stack.blast_adapter is not None
        assert stack.learning_bridge is not None
        assert stack.capabilities["event_bridge"].enabled is True

    @pytest.mark.asyncio
    async def test_capabilities_reason_map(self, tmp_path):
        from backend.core.ouroboros.governance.integration import create_governance_stack

        config = self._make_config(tmp_path)
        stack = await create_governance_stack(config)
        for name, status in stack.capabilities.items():
            assert isinstance(status.reason, str)
            assert len(status.reason) > 0


# -- register_governance_argparse --------------------------------------------


class TestRegisterGovernanceArgparse:
    """Argparse registration adds governance flags."""

    def test_adds_skip_governance(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args(["--skip-governance"])
        assert args.skip_governance is True

    def test_adds_governance_mode(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args(["--governance-mode", "governed"])
        assert args.governance_mode == "governed"

    def test_default_governance_mode_is_sandbox(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args([])
        assert args.governance_mode == "sandbox"

    def test_adds_break_glass_flags(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args(["--break-glass", "list"])
        assert args.break_glass_action == "list"

    def test_break_glass_with_op_id(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args([
            "--break-glass", "issue",
            "--break-glass-op-id", "op-123",
            "--break-glass-reason", "emergency fix",
        ])
        assert args.break_glass_action == "issue"
        assert args.break_glass_op_id == "op-123"
        assert args.break_glass_reason == "emergency fix"


# -- handle_break_glass_command ----------------------------------------------


class TestHandleBreakGlassCommand:
    """Break-glass CLI dispatch handles all cases."""

    @pytest.mark.asyncio
    async def test_list_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(break_glass_action="list")
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_audit_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(break_glass_action="audit")
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_issue_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(
            break_glass_action="issue",
            break_glass_op_id="op-1",
            break_glass_reason="test",
            break_glass_ttl=300,
        )
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_issue_with_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        mock_token = MagicMock()
        mock_token.token_id = "t1"

        mock_stack = MagicMock()
        mock_stack.break_glass = MagicMock()

        args = argparse.Namespace(
            break_glass_action="issue",
            break_glass_op_id="op-1",
            break_glass_reason="emergency",
            break_glass_ttl=300,
        )

        with pytest.MonkeyPatch.context() as mp:
            mock_issue = AsyncMock(return_value=mock_token)
            mp.setattr(
                "backend.core.ouroboros.governance.cli_commands.issue_break_glass",
                mock_issue,
            )
            exit_code = await handle_break_glass_command(args, mock_stack)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_revoke_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(
            break_glass_action="revoke",
            break_glass_op_id="op-1",
            break_glass_reason="done",
        )
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(break_glass_action="unknown_action")
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 1


# -- Package exports ---------------------------------------------------------


class TestPackageExports:
    """Integration module exports must be accessible from the package."""

    def test_governance_mode_importable(self):
        from backend.core.ouroboros.governance import GovernanceMode
        assert GovernanceMode.SANDBOX.value == "sandbox"

    def test_governance_config_importable(self):
        from backend.core.ouroboros.governance import GovernanceConfig
        assert GovernanceConfig is not None

    def test_governance_stack_importable(self):
        from backend.core.ouroboros.governance import GovernanceStack
        assert GovernanceStack is not None

    def test_governance_init_error_importable(self):
        from backend.core.ouroboros.governance import GovernanceInitError
        assert GovernanceInitError is not None

    def test_capability_status_importable(self):
        from backend.core.ouroboros.governance import CapabilityStatus
        assert CapabilityStatus is not None

    def test_create_governance_stack_importable(self):
        from backend.core.ouroboros.governance import create_governance_stack
        assert callable(create_governance_stack)

    def test_register_governance_argparse_importable(self):
        from backend.core.ouroboros.governance import register_governance_argparse
        assert callable(register_governance_argparse)

    def test_handle_break_glass_command_importable(self):
        from backend.core.ouroboros.governance import handle_break_glass_command
        assert callable(handle_break_glass_command)


# -- End-to-End Integration Tests --------------------------------------------


class TestEndToEnd:
    """Full flow: config -> factory -> start -> write gate -> stop."""

    def _make_config(self, tmp_path):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = argparse.Namespace(skip_governance=False, governance_mode="sandbox")
        base = GovernanceConfig.from_env_and_args(args)
        return GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=base.policy_version,
            policy_hash=base.policy_hash,
            contract_version=base.contract_version,
            contract_hash=base.contract_hash,
            config_digest=base.config_digest,
            initial_mode=base.initial_mode,
            skip_governance=base.skip_governance,
            canary_slices=base.canary_slices,
            gcp_daily_budget=base.gcp_daily_budget,
            startup_timeout_s=base.startup_timeout_s,
            component_budget_s=base.component_budget_s,
        )

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceStack,
            create_governance_stack,
        )

        config = self._make_config(tmp_path)

        # Create
        stack = await create_governance_stack(config)
        assert isinstance(stack, GovernanceStack)
        assert stack._started is False

        # Write gate before start = denied
        allowed, reason = stack.can_write({"files": ["test.py"]})
        assert allowed is False
        assert reason == "governance_not_started"

        # Start
        await stack.start()
        assert stack._started is True

        # Health
        health = stack.health()
        assert "mode" in health
        assert "policy_version" in health
        assert health["policy_version"] == "v0.1.0"

        # Stop
        await stack.stop()
        assert stack._started is False

        # Double-stop is safe
        await stack.stop()

    @pytest.mark.asyncio
    async def test_skip_governance_forces_read_only(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = argparse.Namespace(skip_governance=True, governance_mode="governed")
        config = GovernanceConfig.from_env_and_args(args)
        assert config.initial_mode == GovernanceMode.READ_ONLY_PLANNING

    @pytest.mark.asyncio
    async def test_break_glass_cli_round_trip(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            create_governance_stack,
            handle_break_glass_command,
        )

        config = self._make_config(tmp_path)
        stack = await create_governance_stack(config)
        await stack.start()

        # List should work (empty)
        code = await handle_break_glass_command(
            argparse.Namespace(break_glass_action="list"), stack
        )
        assert code == 0

        # Audit should work (empty)
        code = await handle_break_glass_command(
            argparse.Namespace(break_glass_action="audit"), stack
        )
        assert code == 0

        await stack.stop()

    def test_no_circular_imports(self):
        """Import the package to ensure no circular imports."""
        import backend.core.ouroboros.governance
        assert hasattr(backend.core.ouroboros.governance, "GovernanceMode")
        assert hasattr(backend.core.ouroboros.governance, "GovernanceStack")
