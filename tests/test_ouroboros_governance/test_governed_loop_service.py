"""Tests for GovernedLoopService — lifecycle, submit, health, drain."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Add edge case tests",
    target_files: Tuple[str, ...] = ("tests/test_utils.py",),
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )


def _mock_stack(can_write_result: Tuple[bool, str] = (True, "ok")) -> MagicMock:
    """Build a mock GovernanceStack."""
    stack = MagicMock()
    stack.can_write.return_value = can_write_result
    stack._started = True
    stack.canary = MagicMock()
    stack.canary.register_slice = MagicMock()
    stack.canary.is_file_allowed = MagicMock(return_value=True)
    stack.risk_engine = MagicMock()
    stack.risk_engine.classify = MagicMock(return_value=MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="default_safe"
    ))
    stack.ledger = MagicMock()
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.change_engine = AsyncMock()
    stack.change_engine.execute = AsyncMock(return_value=MagicMock(
        success=True, rolled_back=False, op_id="op-test-001"
    ))
    stack.policy_version = "test-v1"
    return stack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGovernedLoopConfig:
    """Tests for GovernedLoopConfig."""

    def test_defaults(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
        )

        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        assert config.generation_timeout_s == 120.0
        assert config.approval_timeout_s == 600.0
        assert config.max_concurrent_ops == 2
        assert config.initial_canary_slices == ("tests/",)
        assert config.claude_daily_budget == 10.00

    def test_frozen(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
        )

        config = GovernedLoopConfig(project_root=Path("/tmp"))
        with pytest.raises(AttributeError):
            config.generation_timeout_s = 999.0  # type: ignore[misc]


@pytest.mark.asyncio
class TestGovernedLoopServiceLifecycle:
    """Tests for service start/stop lifecycle."""

    async def test_starts_active_with_mocked_providers(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))

        service = GovernedLoopService(
            stack=stack,
            prime_client=None,
            config=config,
        )
        assert service.state is ServiceState.INACTIVE

        await service.start()
        assert service.state in (ServiceState.ACTIVE, ServiceState.DEGRADED)

    async def test_start_is_idempotent(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        state_after_first = service.state
        await service.start()  # Second call — should be no-op
        assert service.state is state_after_first

    async def test_stop_transitions_to_inactive(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        await service.stop()
        assert service.state is ServiceState.INACTIVE

    async def test_registers_initial_canary_slices(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(
            project_root=Path("/tmp/test"),
            initial_canary_slices=("tests/", "backend/core/utils/"),
        )
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        assert stack.canary.register_slice.call_count == 2

    async def test_health_returns_state(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        health = service.health()
        assert "state" in health
        assert "active_ops" in health
        assert "canary_slices" in health


@pytest.mark.asyncio
class TestGovernedLoopServiceSubmit:
    """Tests for the submit() entrypoint."""

    async def test_submit_rejects_when_inactive(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        # Don't start — state is INACTIVE

        ctx = _make_context()
        result = await service.submit(ctx, trigger_source="cli")
        assert result.terminal_phase is OperationPhase.CANCELLED
        assert "not_active" in result.reason_code

    async def test_submit_rejects_at_capacity(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(
            project_root=Path("/tmp/test"),
            max_concurrent_ops=0,  # Zero capacity — always BUSY
        )
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        ctx = _make_context()
        result = await service.submit(ctx, trigger_source="cli")
        assert result.terminal_phase is OperationPhase.CANCELLED
        assert "busy" in result.reason_code

    async def test_submit_deduplicates(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        # Mock the orchestrator's run method to return a terminal context
        terminal_ctx = _make_context(op_id="op-dedup-001")
        # Advance through the pipeline to a terminal phase
        terminal_ctx = terminal_ctx.advance(OperationPhase.CANCELLED)
        service._orchestrator.run = AsyncMock(return_value=terminal_ctx)

        ctx = _make_context(op_id="op-dedup-001")
        result1 = await service.submit(ctx, trigger_source="cli")

        # Second submit with same op_id should be deduplicated
        result2 = await service.submit(ctx, trigger_source="cli")
        assert "duplicate" in result2.reason_code


class TestGovernedLoopRegistryWiring:
    async def test_build_components_wires_registry(self, tmp_path, monkeypatch):
        """_build_components() passes RepoRegistry to OrchestratorConfig."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        prime_path = tmp_path / "prime"
        prime_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("JARVIS_PRIME_REPO_PATH", str(prime_path))

        config = GovernedLoopConfig(project_root=tmp_path)
        stack = _mock_stack()
        svc = GovernedLoopService(stack=stack, prime_client=None, config=config)

        try:
            await svc.start()

            assert svc._orchestrator is not None
            registry = svc._orchestrator._config.repo_registry
            assert registry is not None
            names = {r.name for r in registry.list_enabled()}
            assert "jarvis" in names
            assert "prime" in names
        finally:
            await svc.stop()

    def test_reactor_canonical_wins_over_legacy(self, tmp_path, monkeypatch):
        """JARVIS_REACTOR_REPO_PATH takes priority over REACTOR_CORE_REPO_PATH."""
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
        canonical_path = tmp_path / "canonical"
        legacy_path = tmp_path / "legacy"
        canonical_path.mkdir()
        legacy_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("JARVIS_REACTOR_REPO_PATH", str(canonical_path))
        monkeypatch.setenv("REACTOR_CORE_REPO_PATH", str(legacy_path))

        registry = RepoRegistry.from_env()
        rc = registry.get("reactor-core")
        assert rc.local_path == canonical_path

    def test_reactor_legacy_env_var_wired(self, tmp_path, monkeypatch):
        """REACTOR_CORE_REPO_PATH is accepted as legacy alias for reactor-core."""
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
        reactor_path = tmp_path / "reactor-core"
        reactor_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.delenv("JARVIS_REACTOR_REPO_PATH", raising=False)
        monkeypatch.setenv("REACTOR_CORE_REPO_PATH", str(reactor_path))

        registry = RepoRegistry.from_env()
        names = {r.name for r in registry.list_all()}
        assert "reactor-core" in names
        rc = registry.get("reactor-core")
        assert rc.local_path == reactor_path


class TestGovernedLoopIntakeRegistryWiring:
    """GovernedLoopService resolves RepoRegistry and exposes it on _repo_registry."""

    async def test_repo_registry_exposed_on_gls_after_start(self, tmp_path, monkeypatch):
        """After start(), GovernedLoopService._repo_registry must contain all enabled repos
        so that supervisor Zone 6.9 can reuse it when building IntakeLayerService."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        prime_path = tmp_path / "prime"
        prime_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("JARVIS_PRIME_REPO_PATH", str(prime_path))

        config = GovernedLoopConfig(project_root=tmp_path)
        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        stack._started = True
        stack.canary = MagicMock()
        stack.canary.register_slice = MagicMock()
        stack.ledger = MagicMock()
        stack.ledger.append = AsyncMock(return_value=True)
        svc = GovernedLoopService(stack=stack, prime_client=None, config=config)

        try:
            await svc.start()
        except Exception:
            pass  # may fail without real infra; we only care about _repo_registry

        assert svc._repo_registry is not None, "_repo_registry was not set by _build_components"
        names = {r.name for r in svc._repo_registry.list_enabled()}
        assert "jarvis" in names
        assert "prime" in names
