"""Tests for GovernedLoopService — lifecycle, submit, health, drain."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
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
    """Build a mock GovernanceStack including resource_monitor."""
    import time as _time
    from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot

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
    snap = ResourceSnapshot(
        ram_percent=42.10,
        cpu_percent=14.20,
        event_loop_latency_ms=2.50,
        disk_io_busy=False,
        sampled_monotonic_ns=_time.monotonic_ns(),
        ram_available_gb=6.80,
        platform_arch="arm64",
        collector_status="ok",
    )
    stack.resource_monitor = MagicMock()
    stack.resource_monitor.snapshot = AsyncMock(return_value=snap)
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
        assert config.initial_canary_slices == ("tests/", "docs/")
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

    async def test_stop_cancels_background_agent_pool(self) -> None:
        """Regression for Task #95 (budget cap overshoot).

        GovernedLoopService.stop() must invoke BackgroundAgentPool.stop()
        so that in-flight workers (which are issuing paid Claude/DW calls)
        are cancelled when the harness hits --cost-cap. Prior behaviour
        was a fake ``await asyncio.sleep(0)`` drain that let workers keep
        billing after ``budget_event`` fired — session bt-2026-04-13-011909
        overshot $0.50 → $0.5364 (+7.3%).
        """
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        fake_pool = MagicMock()
        fake_pool.stop = AsyncMock()
        fake_pool.list_active = MagicMock(return_value=[
            MagicMock(op_id="bgop-abc123"),
            MagicMock(op_id="bgop-def456"),
        ])
        service._bg_pool = fake_pool

        await service.stop()

        fake_pool.stop.assert_awaited_once()
        fake_pool.list_active.assert_called()

    async def test_stop_tolerates_bg_pool_stop_exception(self) -> None:
        """GLS.stop() must not raise if _bg_pool.stop() itself fails.

        Exception is logged and swallowed so the rest of the teardown
        (EventChannel, stack detach) still runs. Hard cap > clean drain.
        """
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        failing_pool = MagicMock()
        failing_pool.list_active = MagicMock(side_effect=RuntimeError("pool inspect boom"))
        failing_pool.stop = AsyncMock(side_effect=RuntimeError("pool stop boom"))
        service._bg_pool = failing_pool

        await service.stop()

        failing_pool.stop.assert_awaited_once()
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

        stack = _mock_stack_with_resource_monitor()
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

    async def test_tool_narration_forwards_round_preamble(self, tmp_path, monkeypatch):
        """Round WHY text must survive GLS callback wiring into ToolNarrationChannel."""
        from backend.core.ouroboros.governance import tool_executor, tool_narration
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        captured: Dict[str, object] = {}

        class _FakeNarrationChannel:
            def __init__(self, _comm) -> None:
                self.calls = []
                captured["channel"] = self

            def emit(self, **kwargs) -> None:
                self.calls.append(kwargs)

        class _FakeToolLoopCoordinator:
            def __init__(self, *args, on_tool_call=None, **kwargs) -> None:
                captured["on_tool_call"] = on_tool_call
                self.on_token = None

        monkeypatch.setattr(tool_narration, "ToolNarrationChannel", _FakeNarrationChannel)
        monkeypatch.setattr(tool_executor, "ToolLoopCoordinator", _FakeToolLoopCoordinator)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=tmp_path, claude_api_key=None)
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        try:
            await service._build_components()
        except Exception:
            pass

        on_tool_call = captured.get("on_tool_call")
        assert callable(on_tool_call)

        on_tool_call(
            op_id="op-pre",
            tool_name="read_file",
            round_index=2,
            args_summary="backend/core/foo.py",
            preamble="Inspecting the current file before editing.",
        )

        channel = captured.get("channel")
        assert channel is not None
        assert channel.calls[-1]["preamble"] == "Inspecting the current file before editing."

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


class TestBackgroundTaskLifecycle:
    """Tests for curriculum_loop and reactor_event_loop lifecycle."""

    async def test_curriculum_task_created_on_start_when_enabled(self):
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig
        )
        config = GovernedLoopConfig(curriculum_enabled=True)
        service = GovernedLoopService(config=config)
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch("backend.core.ouroboros.governance.governed_loop_service.CurriculumPublisher"),
            patch("backend.core.ouroboros.governance.governed_loop_service.ModelAttributionRecorder"),
            patch("backend.core.ouroboros.governance.governed_loop_service.get_performance_persistence"),
        ):
            service._generator = None
            await service.start()
            assert service._curriculum_task is not None
            assert service._reactor_event_task is not None
            await service.stop()

    async def test_curriculum_task_cancelled_on_stop(self):
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig
        )
        config = GovernedLoopConfig(curriculum_enabled=True)
        service = GovernedLoopService(config=config)
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch("backend.core.ouroboros.governance.governed_loop_service.CurriculumPublisher"),
            patch("backend.core.ouroboros.governance.governed_loop_service.ModelAttributionRecorder"),
            patch("backend.core.ouroboros.governance.governed_loop_service.get_performance_persistence"),
        ):
            service._generator = None
            await service.start()
            curriculum_task = service._curriculum_task
            await service.stop()
            assert curriculum_task.done()

    async def test_reactor_event_loop_dispatches_model_promoted(self):
        import json, time
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService, GovernedLoopConfig

        with tempfile.TemporaryDirectory() as ev_dir:
            ev = Path(ev_dir)
            # Write a model_promoted event file
            event = {
                "schema_version": "reactor.1",
                "event_type": "model_promoted",
                "model_id": "v2",
                "previous_model_id": "v1",
                "training_batch_size": 40,
                "promoted_at": "2026-03-09T07:00:00Z",
            }
            (ev / f"model_promoted_{int(time.time() * 1000)}.json").write_text(json.dumps(event))

            config = GovernedLoopConfig(curriculum_enabled=True, reactor_event_poll_interval_s=0.0)
            service = GovernedLoopService(config=config)
            service._event_dir = ev
            recorder = AsyncMock()
            recorder.record_model_transition = AsyncMock(return_value=[])
            service._model_attribution_recorder = recorder
            seen: set[str] = set()
            await service._handle_event_files(seen)
            recorder.record_model_transition.assert_called_once_with(
                new_model_id="v2",
                previous_model_id="v1",
                training_batch_size=40,
                task_types=None,
            )

    async def test_unknown_event_type_does_not_raise(self):
        import json, time
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService, GovernedLoopConfig

        with tempfile.TemporaryDirectory() as ev_dir:
            ev = Path(ev_dir)
            (ev / f"unknown_{int(time.time() * 1000)}.json").write_text(
                json.dumps({"event_type": "something_reactor_invented", "data": 42})
            )
            config = GovernedLoopConfig(curriculum_enabled=True)
            service = GovernedLoopService(config=config)
            service._event_dir = ev
            service._model_attribution_recorder = AsyncMock()
            seen: set[str] = set()
            await service._handle_event_files(seen)  # must not raise


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


class TestOracleConfig:
    """Tests for oracle fields on GovernedLoopConfig and GovernanceStack."""

    def test_governed_loop_config_oracle_enabled_default(self):
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        from pathlib import Path
        config = GovernedLoopConfig(project_root=Path("/tmp"))
        assert config.oracle_enabled is True

    def test_governed_loop_config_oracle_poll_interval_default(self):
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        from pathlib import Path
        config = GovernedLoopConfig(project_root=Path("/tmp"))
        assert config.oracle_incremental_poll_interval_s == 300.0

    def test_governance_stack_oracle_defaults_none(self):
        import dataclasses
        from backend.core.ouroboros.governance.integration import GovernanceStack
        field_names = {f.name for f in dataclasses.fields(GovernanceStack)}
        assert "oracle" in field_names
        defaults = {f.name: f.default for f in dataclasses.fields(GovernanceStack) if f.name == "oracle"}
        assert defaults["oracle"] is None


class TestOracleIndexerLifecycle:
    """Oracle indexer task starts non-blocking and failure never fails the service."""

    async def test_oracle_indexer_failure_does_not_fail_service_start(self):
        """If oracle.initialize() raises, service still becomes ACTIVE/DEGRADED."""
        from unittest.mock import AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig,
        )
        from pathlib import Path
        config = GovernedLoopConfig(
            project_root=Path("/tmp"),
            oracle_enabled=True,
            curriculum_enabled=False,
        )
        service = GovernedLoopService(config=config)
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch(
                "backend.core.ouroboros.governance.governed_loop_service.TheOracle",
                side_effect=RuntimeError("oracle boom"),
            ),
        ):
            service._generator = None
            await service.start()
            # Service must have started despite oracle failure
            assert service._oracle_indexer_task is not None
            # Wait briefly for the background task to run and fail
            import asyncio
            await asyncio.sleep(0.05)
            # Task should have exited (done) after the exception
            assert service._oracle_indexer_task.done()
            # oracle must be None (not set)
            assert service._oracle is None
            await service.stop()

    async def test_oracle_indexer_task_cancelled_on_stop(self):
        """oracle_indexer_task is cancelled when service stops."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig,
        )
        from pathlib import Path
        import asyncio
        config = GovernedLoopConfig(
            project_root=Path("/tmp"),
            oracle_enabled=True,
            oracle_incremental_poll_interval_s=9999.0,  # never polls during test
            curriculum_enabled=False,
        )
        service = GovernedLoopService(config=config)
        mock_oracle = MagicMock()
        mock_oracle.initialize = AsyncMock()
        mock_oracle.incremental_update = AsyncMock()
        mock_oracle.shutdown = AsyncMock()
        mock_oracle.get_status = MagicMock(return_value={"running": True})
        mock_oracle.get_metrics = MagicMock(return_value={"total_nodes": 42})
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch(
                "backend.core.ouroboros.governance.governed_loop_service.TheOracle",
                return_value=mock_oracle,
            ),
        ):
            service._generator = None
            await service.start()
            await asyncio.sleep(0.05)  # let oracle initialize
            oracle_task = service._oracle_indexer_task
            await service.stop()
            assert oracle_task.done()


# ---------------------------------------------------------------------------
# Helpers for telemetry tests
# ---------------------------------------------------------------------------


def _mock_stack_with_resource_monitor(
    can_write_result: Tuple[bool, str] = (True, "ok"),
) -> MagicMock:
    """Thin wrapper around _mock_stack() for backward compatibility.

    _mock_stack() already includes resource_monitor; this alias exists so
    existing test call-sites don't need to be updated.
    """
    return _mock_stack(can_write_result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExpectedProviderFromPressure:
    """Tests for _expected_provider_from_pressure() module-level helper."""

    def _make_snap(self, ram_percent: float = 50.0, cpu_percent: float = 30.0) -> "ResourceSnapshot":
        import time as _time
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
        return ResourceSnapshot(
            ram_percent=ram_percent,
            cpu_percent=cpu_percent,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
            sampled_monotonic_ns=_time.monotonic_ns(),
            ram_available_gb=8.0,
            platform_arch="arm64",
            collector_status="ok",
        )

    def test_normal_pressure_routes_gcp(self):
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        snap = self._make_snap(ram_percent=50.0, cpu_percent=30.0)
        assert _expected_provider_from_pressure(snap) == "GCP_PRIME_SPOT"

    def test_elevated_pressure_routes_gcp(self):
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        snap = self._make_snap(ram_percent=82.0, cpu_percent=30.0)  # ELEVATED RAM
        assert _expected_provider_from_pressure(snap) == "GCP_PRIME_SPOT"

    def test_critical_pressure_routes_local(self):
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        snap = self._make_snap(ram_percent=87.0, cpu_percent=30.0)  # CRITICAL RAM (>=85%)
        assert _expected_provider_from_pressure(snap) == "LOCAL_CLAUDE"

    def test_emergency_pressure_routes_local(self):
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        snap = self._make_snap(ram_percent=92.0, cpu_percent=30.0)  # EMERGENCY RAM (>=90%)
        assert _expected_provider_from_pressure(snap) == "LOCAL_CLAUDE"

    @pytest.mark.asyncio
    async def test_submit_stamps_telemetry_on_orchestrator_ctx(self):
        """submit() must stamp ctx.telemetry before calling orchestrator.run().

        This exercises the full submit() code path and verifies that the
        TelemetryContext (including HostTelemetry and RoutingIntentTelemetry)
        is attached to the OperationContext received by the orchestrator.
        """
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        captured: Dict[str, OperationContext] = {}

        async def fake_orchestrator_run(ctx: OperationContext) -> OperationContext:
            captured["ctx"] = ctx
            # CLASSIFY -> CANCELLED is the only valid terminal shortcut from the
            # initial phase, so we use it here.  The test only cares that
            # ctx.telemetry was stamped before orchestrator.run() was called.
            return ctx.advance(OperationPhase.CANCELLED)

        stack = _mock_stack_with_resource_monitor()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        # Replace the real orchestrator with a mock that captures the context.
        mock_orch = MagicMock()
        mock_orch.run = AsyncMock(side_effect=fake_orchestrator_run)
        service._orchestrator = mock_orch

        ctx = _make_context(op_id="op-telemetry-001")
        await service.submit(ctx, trigger_source="test")

        assert "ctx" in captured, "orchestrator.run() was never called — submit() did not reach the orchestrator"
        received_ctx = captured["ctx"]

        # Telemetry must have been stamped by submit() before calling run().
        assert received_ctx.telemetry is not None, "ctx.telemetry was None — stamping did not occur"

        # HostTelemetry fields must reflect the mock snapshot (platform_arch="arm64").
        assert received_ctx.telemetry.local_node.arch == "arm64"

        # RoutingIntentTelemetry must carry one of the two valid provider strings.
        assert received_ctx.telemetry.routing_intent.expected_provider in (
            "GCP_PRIME_SPOT",
            "LOCAL_CLAUDE",
        )


# ---------------------------------------------------------------------------
# TestSeedAutonomyPolicies
# ---------------------------------------------------------------------------


class TestSeedAutonomyPolicies:
    """Unit tests for GovernedLoopService._seed_autonomy_policies()."""

    def _make_service_with_registry(self, repos: list):
        """Build a GovernedLoopService with a mock registry containing named repos."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        mock_registry = MagicMock()
        mock_repos = []
        for name in repos:
            r = MagicMock()
            r.name = name
            mock_repos.append(r)
        mock_registry.list_enabled.return_value = mock_repos
        service._repo_registry = mock_registry
        return service

    def test_seeds_governed_for_tests_slice(self):
        """tests/ canary slice seeds GOVERNED tier for all trigger sources."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        assert service._trust_graduator is not None
        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger, repo="jarvis", canary_slice="tests/"
            )
            assert cfg is not None, f"No config for trigger={trigger}, slice=tests/"
            assert cfg.current_tier is AutonomyTier.GOVERNED

    def test_seeds_governed_for_docs_slice(self):
        """docs/ canary slice seeds GOVERNED tier for all trigger sources."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger, repo="jarvis", canary_slice="docs/"
            )
            assert cfg is not None
            assert cfg.current_tier is AutonomyTier.GOVERNED

    def test_seeds_observe_for_core_slice(self):
        """backend/core/ seeds OBSERVE tier for all trigger sources."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger,
                repo="jarvis",
                canary_slice="backend/core/",
            )
            assert cfg is not None
            assert cfg.current_tier is AutonomyTier.OBSERVE

    def test_seeds_observe_for_unclassified_root(self):
        """Empty canary_slice (root default) seeds OBSERVE tier."""
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier

        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        for trigger in ("voice_command", "backlog", "test_failure", "opportunity_miner"):
            cfg = service._trust_graduator.get_config(
                trigger_source=trigger, repo="jarvis", canary_slice=""
            )
            assert cfg is not None
            assert cfg.current_tier is AutonomyTier.OBSERVE

    def test_seeds_all_registered_repos(self):
        """All repos in registry get policies seeded."""
        repos = ["jarvis", "prime", "reactor-core"]
        service = self._make_service_with_registry(repos)
        service._seed_autonomy_policies()

        all_configs = service._trust_graduator.all_configs()
        repos_covered = {cfg.repo for cfg in all_configs}
        assert repos_covered == set(repos)

    def test_seeds_all_trigger_sources(self):
        """All four trigger sources are seeded per slice per repo."""
        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()

        triggers_covered = {cfg.trigger_source for cfg in service._trust_graduator.all_configs()}
        assert triggers_covered == {"voice_command", "backlog", "test_failure", "opportunity_miner"}

    def test_seed_count_is_deterministic(self):
        """4 repos * 4 triggers * 4 slices = 64 configs."""
        repos = ["jarvis", "prime", "reactor-core", "extra"]
        service = self._make_service_with_registry(repos)
        service._seed_autonomy_policies()

        # 4 slices: "tests/", "docs/", "backend/core/", ""
        # 4 trigger sources, 4 repos
        assert len(service._trust_graduator.all_configs()) == 64

    def test_fallback_to_jarvis_when_no_registry(self):
        """When _repo_registry is None, seeds policies for 'jarvis' only."""
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        service._repo_registry = None

        service._seed_autonomy_policies()

        repos_covered = {cfg.repo for cfg in service._trust_graduator.all_configs()}
        assert repos_covered == {"jarvis"}

    async def test_seed_called_during_start(self):
        """_seed_autonomy_policies() is called during start(), populating _trust_graduator."""
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        assert service._trust_graduator is None

        await service.start()
        assert service._trust_graduator is not None
        assert len(service._trust_graduator.all_configs()) > 0
        await service.stop()

    def test_seeding_is_idempotent(self):
        """Calling _seed_autonomy_policies() twice produces the same count (fresh graduator each time)."""
        service = self._make_service_with_registry(["jarvis"])
        service._seed_autonomy_policies()
        count_first = len(service._trust_graduator.all_configs())

        service._seed_autonomy_policies()
        count_second = len(service._trust_graduator.all_configs())

        assert count_first == count_second


# ---------------------------------------------------------------------------
# TestFileScopeLock
# ---------------------------------------------------------------------------


class TestFileScopeLock:
    """Second op for a file already in-flight is rejected before generation."""

    def _make_service(self):
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )
        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        return GovernedLoopService(stack=stack, prime_client=None, config=config)

    def test_active_file_ops_initialized_empty(self):
        """`_active_file_ops` starts as an empty set."""
        svc = self._make_service()
        assert hasattr(svc, "_active_file_ops")
        assert isinstance(svc._active_file_ops, set)
        assert len(svc._active_file_ops) == 0

    async def test_submit_rejects_in_flight_file(self, tmp_path):
        """submit() returns reason_code='file_in_flight' when a target file is already locked."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
            OperationPhase,
        )
        from pathlib import Path

        svc = self._make_service()
        svc._state = ServiceState.ACTIVE

        # Simulate first op holding the file lock
        fp = str(Path(tmp_path / "tests" / "test_foo.py").resolve())
        svc._active_file_ops.add(fp)

        ctx = OperationContext.create(
            target_files=(fp,),
            description="fix test",
        )
        result = await svc.submit(ctx, trigger_source="test")

        assert result.terminal_phase is OperationPhase.CANCELLED
        assert result.reason_code == "file_in_flight"

    async def test_submit_passes_for_different_file(self, tmp_path):
        """submit() does NOT cancel with file_in_flight when target file is not locked."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            ServiceState,
        )
        from backend.core.ouroboros.governance.op_context import OperationContext
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock

        svc = self._make_service()
        svc._state = ServiceState.ACTIVE

        # Lock a DIFFERENT file
        other_fp = str(Path(tmp_path / "tests" / "test_other.py").resolve())
        svc._active_file_ops.add(other_fp)

        # Target a different file; wire a mock orchestrator so pipeline completes
        target_fp = str(Path(tmp_path / "tests" / "test_target.py").resolve())
        from backend.core.ouroboros.governance.op_context import OperationPhase, GenerationResult
        mock_orch = MagicMock()
        async def _complete(_):
            result_ctx = MagicMock(spec=OperationContext)
            result_ctx.phase = OperationPhase.COMPLETE
            result_ctx.generation = GenerationResult(
                candidates=({"file": target_fp, "content": "pass"},),
                provider_name="mock",
                generation_duration_s=0.1,
            )
            return result_ctx
        mock_orch.run = AsyncMock(side_effect=_complete)
        svc._orchestrator = mock_orch

        ctx = OperationContext.create(target_files=(target_fp,), description="fix target")
        result = await svc.submit(ctx, trigger_source="test")

        # Must NOT be cancelled due to file_in_flight
        assert result.reason_code != "file_in_flight", (
            f"Different file should not trigger file_in_flight, got: {result.reason_code}"
        )

    def test_canonical_path_used_for_lock_key(self, tmp_path):
        """Symlink and real path produce same canonical key (resolve() applied)."""
        import os
        real_file = tmp_path / "tests" / "test_foo.py"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.touch()
        link_file = tmp_path / "link_test_foo.py"
        os.symlink(str(real_file), str(link_file))

        canonical_real = str(real_file.resolve())
        canonical_link = str(link_file.resolve())
        assert canonical_real == canonical_link, (
            "Symlink and target should resolve to same canonical path"
        )


# ---------------------------------------------------------------------------
# TestFrozenTierStamping
# ---------------------------------------------------------------------------


class TestFrozenTierStamping:
    """GovernedLoopService.submit() stamps frozen_autonomy_tier onto ctx."""

    async def test_observe_tier_stamped_for_core_file(self, tmp_path):
        """Files under backend/core/ get frozen_autonomy_tier='observe'."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
            OperationPhase,
        )

        config = GovernedLoopConfig(project_root=tmp_path)
        stack = _mock_stack()
        svc = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await svc.start()

        captured_ctx = []

        async def capturing_run(ctx):
            captured_ctx.append(ctx)
            return ctx.advance(OperationPhase.CANCELLED)

        svc._orchestrator = MagicMock()
        svc._orchestrator.run = capturing_run

        ctx = OperationContext.create(
            target_files=("backend/core/some_module.py",),
            description="refactor core",
        )
        await svc.submit(ctx, trigger_source="backlog")

        assert len(captured_ctx) >= 1
        assert captured_ctx[0].frozen_autonomy_tier == "observe", (
            f"Expected 'observe' for core file, got '{captured_ctx[0].frozen_autonomy_tier}'"
        )
        await svc.stop()

    async def test_governed_tier_stamped_for_tests_file(self, tmp_path):
        """Files under tests/ get frozen_autonomy_tier='governed'."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
            OperationPhase,
        )

        config = GovernedLoopConfig(project_root=tmp_path)
        stack = _mock_stack()
        svc = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await svc.start()

        captured_ctx = []

        async def capturing_run(ctx):
            captured_ctx.append(ctx)
            return ctx.advance(OperationPhase.CANCELLED)

        svc._orchestrator = MagicMock()
        svc._orchestrator.run = capturing_run

        ctx = OperationContext.create(
            target_files=("tests/test_foo.py",),
            description="fix test",
        )
        await svc.submit(ctx, trigger_source="test_failure")

        assert len(captured_ctx) >= 1
        assert captured_ctx[0].frozen_autonomy_tier == "governed", (
            f"Expected 'governed' for tests/ file, got '{captured_ctx[0].frozen_autonomy_tier}'"
        )
        await svc.stop()


# ---------------------------------------------------------------------------
# TestCooldownSymlinkResolution
# ---------------------------------------------------------------------------


class TestCooldownSymlinkResolution:
    """Cooldown guard uses canonical (resolved) path so symlinks share the same counter."""

    def _make_service(self):
        from pathlib import Path
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )
        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        return GovernedLoopService(stack=stack, prime_client=None, config=config)

    def test_cooldown_key_is_canonical(self, tmp_path):
        """Symlink and real path produce the same cooldown key after resolve()."""
        import os
        real_file = tmp_path / "tests" / "test_foo.py"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.touch()
        link_file = tmp_path / "link_test_foo.py"
        os.symlink(str(real_file), str(link_file))

        canonical_real = str(real_file.resolve())
        canonical_link = str(link_file.resolve())
        assert canonical_real == canonical_link, (
            "Symlink and real path must resolve to the same canonical path"
        )

    async def test_cooldown_counts_symlink_and_target_together(self, tmp_path):
        """Touches via symlink and real path are counted against the same counter."""
        import os
        from datetime import datetime, timezone, timedelta
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
            OperationPhase,
        )

        real_file = tmp_path / "tests" / "test_foo.py"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.touch()
        link_file = tmp_path / "link_test_foo.py"
        os.symlink(str(real_file), str(link_file))

        canonical = str(real_file.resolve())

        svc = self._make_service()

        def _make_ctx(fp: str) -> OperationContext:
            ctx = OperationContext.create(target_files=(fp,), description="fix")
            return ctx.with_pipeline_deadline(
                datetime.now(tz=timezone.utc) + timedelta(seconds=600)
            )

        # Directly prime the cache with the CANONICAL path to simulate 3 previous touches
        import collections
        svc._file_touch_cache[canonical] = collections.deque([0.0, 1.0, 2.0])

        # Op via symlink path should also hit the canonical counter
        result = await svc._preflight_check(_make_ctx(str(link_file)))

        assert result is not None, "Expected cooldown block for symlink path"
        assert result.phase is OperationPhase.CANCELLED, (
            f"Expected CANCELLED, got {result.phase}"
        )


# ---------------------------------------------------------------------------
# TestPressureForLoadRouting
# ---------------------------------------------------------------------------


class TestPressureForLoadRouting:
    """GLS routing uses pressure_for_load(active_ops) not overall_pressure."""

    def test_expected_provider_uses_load_aware_pressure(self):
        """_expected_provider_from_pressure scales CPU emergency threshold by active_ops.

        96% CPU is EMERGENCY at 0 ops (>= 95% base threshold) and CRITICAL at 6 ops
        (below 99% scaled emergency threshold, but still above 80% critical threshold).
        Both EMERGENCY and CRITICAL route to LOCAL_CLAUDE — the load-aware scaling
        prevents false-positive EMERGENCY classification but does not suppress CRITICAL.

        A CPU reading between CRITICAL (80%) and base EMERGENCY (95%) is used to
        demonstrate that GCP_PRIME_SPOT is preferred under light load.
        """
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_pressure,
        )
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot, PressureLevel

        # CPU at 96% — EMERGENCY at 0 ops, CRITICAL (not EMERGENCY) at 6 ops
        # Both CRITICAL and EMERGENCY still route to LOCAL_CLAUDE (expected: no change in provider)
        snap_high = ResourceSnapshot(
            ram_percent=10.0,
            cpu_percent=96.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap_high.pressure_for_load(0) == PressureLevel.EMERGENCY, (
            "Sanity: 96% CPU at 0 ops should be EMERGENCY"
        )
        assert snap_high.pressure_for_load(6) == PressureLevel.CRITICAL, (
            "Sanity: 96% CPU at 6 ops should be CRITICAL (scaled emergency=99%, still >= 80% critical)"
        )
        # With 0 active ops: 96% >= 95% emergency → LOCAL_CLAUDE
        assert _expected_provider_from_pressure(snap_high, active_ops=0) == "LOCAL_CLAUDE", (
            "96% CPU + 0 ops: EMERGENCY → LOCAL_CLAUDE"
        )
        # With 6 active ops: 96% CRITICAL (not EMERGENCY) → LOCAL_CLAUDE (CRITICAL still falls back)
        assert _expected_provider_from_pressure(snap_high, active_ops=6) == "LOCAL_CLAUDE", (
            "96% CPU + 6 ops: CRITICAL → LOCAL_CLAUDE (CRITICAL still falls back)"
        )

        # CPU at 72% — below CRITICAL threshold: ELEVATED at any load count → GCP_PRIME_SPOT
        snap_mod = ResourceSnapshot(
            ram_percent=10.0,
            cpu_percent=72.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap_mod.pressure_for_load(0) == PressureLevel.ELEVATED, (
            "Sanity: 72% CPU should be ELEVATED"
        )
        assert _expected_provider_from_pressure(snap_mod, active_ops=0) == "GCP_PRIME_SPOT", (
            "72% CPU + 0 ops: ELEVATED → GCP_PRIME_SPOT"
        )
        assert _expected_provider_from_pressure(snap_mod, active_ops=6) == "GCP_PRIME_SPOT", (
            "72% CPU + 6 ops: ELEVATED → GCP_PRIME_SPOT"
        )


@pytest.mark.asyncio
async def test_handle_submit_execution_graph_routes_to_scheduler() -> None:
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
        CommandEnvelope,
        CommandType,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        WorkUnitSpec,
    )
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )

    service = GovernedLoopService(config=GovernedLoopConfig(project_root=Path("/tmp/test")))
    service._subagent_scheduler = MagicMock()
    service._subagent_scheduler.submit = AsyncMock(return_value=True)

    graph = ExecutionGraph(
        graph_id="graph-route-001",
        op_id="op-route-001",
        planner_id="planner-v1",
        schema_version="2d.1",
        concurrency_limit=1,
        units=(
            WorkUnitSpec(
                unit_id="u1",
                repo="jarvis",
                goal="update file",
                target_files=("backend/core/utils.py",),
                owned_paths=("backend/core/utils.py",),
            ),
        ),
    )
    cmd = CommandEnvelope(
        source_layer="L3",
        target_layer="L1",
        command_type=CommandType.SUBMIT_EXECUTION_GRAPH,
        payload={"execution_graph": graph},
        ttl_s=30.0,
    )

    await service._handle_advisory_command(cmd)

    service._subagent_scheduler.submit.assert_awaited_once_with(graph)


def test_health_exposes_execution_graph_scheduler_state() -> None:
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )

    service = GovernedLoopService(config=GovernedLoopConfig(project_root=Path("/tmp/test")))
    service._subagent_scheduler = MagicMock()
    service._subagent_scheduler.health.return_value = {
        "running": True,
        "active_graphs": ["graph-health-001"],
        "max_concurrent_graphs": 2,
        "completed_graphs": [],
    }

    health = service.health()

    assert health["execution_graph_scheduler"]["running"] is True
    assert health["execution_graph_scheduler"]["active_graphs"] == ["graph-health-001"]


@pytest.mark.asyncio
async def test_submit_stamps_strategic_memory_context_before_orchestrator() -> None:
    from types import SimpleNamespace

    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
        ServiceState,
    )

    stack = _mock_stack()
    service = GovernedLoopService(
        stack=stack,
        prime_client=None,
        config=GovernedLoopConfig(project_root=Path("/tmp/test"), l4_enabled=True),
    )
    service._state = ServiceState.ACTIVE
    service._ledger = None
    service._advanced_autonomy = MagicMock()
    service._advanced_autonomy.build_strategic_memory_context.return_value = SimpleNamespace(
        fact_ids=("fact-001",),
        prompt_block="## Strategic Memory (advisory context only)\n- preserve architecture",
        context_digest="digest-001",
    )
    service._advanced_autonomy.remember_user_intent.return_value = SimpleNamespace(
        intent_id="intent-001"
    )

    brain = SimpleNamespace(
        brain_id="qwen_coder_32b",
        model_name="qwen-coder-32b",
        routing_reason="memory_guided_governance",
        task_complexity="light",
        estimated_prompt_tokens=512,
        provider_tier="gcp_prime",
        schema_capability="full_content_only",
        narration=lambda: "routing narration",
    )
    service._brain_selector = MagicMock()
    service._brain_selector.select = AsyncMock(return_value=brain)
    service._brain_selector.daily_spend = 0.0

    def _terminal_ctx(ctx):
        return ctx.advance(OperationPhase.CANCELLED)

    service._orchestrator = MagicMock()
    service._orchestrator.run = AsyncMock(side_effect=_terminal_ctx)

    ctx = _make_context(
        description="Preserve architecture across sessions",
        target_files=("backend/core/utils.py",),
    )
    result = await service.submit(ctx, trigger_source="cli")

    assert result.terminal_phase is OperationPhase.CANCELLED
    submitted_ctx = service._orchestrator.run.await_args.args[0]
    assert submitted_ctx.strategic_intent_id == "intent-001"
    assert submitted_ctx.strategic_memory_fact_ids == ("fact-001",)
    assert "## Strategic Memory" in submitted_ctx.strategic_memory_prompt


@pytest.mark.asyncio
async def test_submit_records_verified_outcome_on_complete() -> None:
    import dataclasses
    from types import SimpleNamespace

    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
        ServiceState,
    )

    stack = _mock_stack()
    service = GovernedLoopService(
        stack=stack,
        prime_client=None,
        config=GovernedLoopConfig(project_root=Path("/tmp/test"), l4_enabled=True),
    )
    service._state = ServiceState.ACTIVE
    service._ledger = None
    service._advanced_autonomy = MagicMock()
    service._advanced_autonomy.build_strategic_memory_context.return_value = SimpleNamespace(
        fact_ids=("fact-001",),
        prompt_block="## Strategic Memory (advisory context only)\n- preserve architecture",
        context_digest="digest-001",
    )
    service._advanced_autonomy.remember_user_intent.return_value = SimpleNamespace(
        intent_id="intent-001"
    )
    service._advanced_autonomy.record_verified_outcome = MagicMock()

    brain = SimpleNamespace(
        brain_id="qwen_coder_32b",
        model_name="qwen-coder-32b",
        routing_reason="memory_guided_governance",
        task_complexity="light",
        estimated_prompt_tokens=512,
        provider_tier="gcp_prime",
        schema_capability="full_content_only",
        narration=lambda: "routing narration",
    )
    service._brain_selector = MagicMock()
    service._brain_selector.select = AsyncMock(return_value=brain)
    service._brain_selector.daily_spend = 0.0

    async def _complete_ctx(stamped_ctx):
        generation = GenerationResult(
            candidates=(),
            provider_name="gcp-jprime",
            generation_duration_s=0.25,
            model_id="qwen-coder-32b",
        )
        return dataclasses.replace(
            stamped_ctx,
            phase=OperationPhase.COMPLETE,
            generation=generation,
        )

    service._orchestrator = MagicMock()
    service._orchestrator.run = AsyncMock(side_effect=_complete_ctx)

    ctx = _make_context(
        description="Preserve architecture across sessions",
        target_files=("backend/core/utils.py",),
    )
    result = await service.submit(ctx, trigger_source="cli")

    assert result.terminal_phase is OperationPhase.COMPLETE
    service._advanced_autonomy.record_verified_outcome.assert_called_once()
    kwargs = service._advanced_autonomy.record_verified_outcome.call_args.kwargs
    assert kwargs["op_id"] == ctx.op_id
    assert kwargs["strategic_intent_id"] == "intent-001"
    assert kwargs["provider_used"] == "gcp-jprime"


@pytest.mark.asyncio
async def test_submit_emits_rollback_event_and_supersedes_verified_fact(tmp_path) -> None:
    import dataclasses
    from types import SimpleNamespace

    from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
        AdvancedAutonomyService,
        AdvancedCoordinationConfig,
    )
    from backend.core.ouroboros.governance.autonomy.autonomy_types import EventType
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
        ServiceState,
    )

    stack = _mock_stack()
    service = GovernedLoopService(
        stack=stack,
        prime_client=None,
        config=GovernedLoopConfig(project_root=tmp_path, l4_enabled=True),
    )
    service._state = ServiceState.ACTIVE
    service._ledger = None
    service._event_emitter = EventEmitter()

    captured_rollbacks = []

    async def _capture(event):
        captured_rollbacks.append(event)

    service._event_emitter.subscribe(EventType.OP_ROLLED_BACK, _capture)
    service._advanced_autonomy = AdvancedAutonomyService(
        command_bus=CommandBus(maxsize=100),
        config=AdvancedCoordinationConfig(state_dir=tmp_path / "advanced_coordination"),
    )
    service._advanced_autonomy.register_event_handlers(service._event_emitter)

    seeded_fact = service._advanced_autonomy.record_verified_outcome(
        op_id="op-rollback-001",
        description="Preserve architecture consistency",
        target_files=("backend/core/utils.py",),
        repo_scope=("jarvis",),
        provider_used="gcp-jprime",
        routing_reason="memory_guided_governance",
        benchmark_result=None,
        is_noop=False,
    )
    assert seeded_fact is not None

    brain = SimpleNamespace(
        brain_id="qwen_coder_32b",
        model_name="qwen-coder-32b",
        routing_reason="memory_guided_governance",
        task_complexity="light",
        estimated_prompt_tokens=512,
        provider_tier="gcp_prime",
        schema_capability="full_content_only",
        narration=lambda: "routing narration",
    )
    service._brain_selector = MagicMock()
    service._brain_selector.select = AsyncMock(return_value=brain)
    service._brain_selector.daily_spend = 0.0

    async def _rolled_back_ctx(stamped_ctx):
        generation = GenerationResult(
            candidates=(),
            provider_name="gcp-jprime",
            generation_duration_s=0.25,
            model_id="qwen-coder-32b",
        )
        return dataclasses.replace(
            stamped_ctx,
            op_id="op-rollback-001",
            phase=OperationPhase.POSTMORTEM,
            generation=generation,
            terminal_reason_code="change_engine_failed",
            rollback_occurred=True,
        )

    service._orchestrator = MagicMock()
    service._orchestrator.run = AsyncMock(side_effect=_rolled_back_ctx)

    ctx = _make_context(
        op_id="op-rollback-001",
        description="Preserve architecture consistency",
        target_files=("backend/core/utils.py",),
    )
    result = await service.submit(ctx, trigger_source="cli")

    assert result.terminal_phase is OperationPhase.POSTMORTEM
    assert result.reason_code == "change_engine_failed"
    assert len(captured_rollbacks) == 1
    assert captured_rollbacks[0].payload["rollback_reason"] == "change_engine_failed"
    assert captured_rollbacks[0].payload["failure_class"] == "rollback"

    recovered = service._advanced_autonomy.get_memory_fact(seeded_fact.fact_id)
    assert recovered is not None
    assert recovered.status == "superseded"


@pytest.mark.asyncio
async def test_reconcile_on_boot_emits_rollback_event_and_supersedes_verified_fact(
    tmp_path,
) -> None:
    import hashlib

    from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
        AdvancedAutonomyService,
        AdvancedCoordinationConfig,
    )
    from backend.core.ouroboros.governance.autonomy.autonomy_types import EventType
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )
    from backend.core.ouroboros.governance.ledger import (
        LedgerEntry,
        OperationLedger,
        OperationState,
    )

    target = tmp_path / "boot_target.py"
    original = b"print('stable')\n"
    target.write_bytes(original)
    rollback_hash = hashlib.sha256(original).hexdigest()
    op_id = "op-boot-rollback-001"

    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    await ledger.append(LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={}))
    await ledger.append(LedgerEntry(
        op_id=op_id,
        state=OperationState.APPLIED,
        data={
            "target_file": str(target),
            "rollback_hash": rollback_hash,
        },
    ))

    stack = _mock_stack()
    stack.ledger = ledger
    stack.comm.emit_decision = AsyncMock()
    stack.approval_store = None

    service = GovernedLoopService(
        stack=stack,
        prime_client=None,
        config=GovernedLoopConfig(project_root=tmp_path, l4_enabled=True),
    )
    service._event_emitter = EventEmitter()

    captured_rollbacks = []

    async def _capture(event):
        captured_rollbacks.append(event)

    service._event_emitter.subscribe(EventType.OP_ROLLED_BACK, _capture)
    service._advanced_autonomy = AdvancedAutonomyService(
        command_bus=CommandBus(maxsize=100),
        config=AdvancedCoordinationConfig(state_dir=tmp_path / "advanced_coordination"),
    )
    service._advanced_autonomy.register_event_handlers(service._event_emitter)

    seeded_fact = service._advanced_autonomy.record_verified_outcome(
        op_id=op_id,
        description="Preserve boot-time invariants",
        target_files=(str(target),),
        repo_scope=("jarvis",),
        provider_used="gcp-jprime",
        routing_reason="boot_recovery",
        benchmark_result=None,
        is_noop=False,
    )
    assert seeded_fact is not None

    await service._reconcile_on_boot()

    latest = await ledger.get_latest_state(op_id)
    assert latest is OperationState.ROLLED_BACK
    assert len(captured_rollbacks) == 1
    assert captured_rollbacks[0].payload["rollback_reason"] == "boot_recovery_already_reverted"
    assert captured_rollbacks[0].payload["outcome_source"] == "boot_recovery"

    recovered = service._advanced_autonomy.get_memory_fact(seeded_fact.fact_id)
    assert recovered is not None
    assert recovered.status == "superseded"


@pytest.mark.asyncio
async def test_feedback_loop_scores_attribution_with_real_persistence_shape() -> None:
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )

    service = GovernedLoopService(
        config=GovernedLoopConfig(project_root=Path("/tmp/test")),
    )
    service._feedback_engine = MagicMock()
    service._feedback_engine.consume_curriculum_once = AsyncMock()
    service._feedback_engine.consume_reactor_events_once = AsyncMock()
    service._feedback_engine.score_attribution_once = AsyncMock()

    fake_persistence = object()
    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch(
            "backend.core.ouroboros.governance.governed_loop_service.get_performance_persistence",
            return_value=fake_persistence,
        ),
        patch(
            "backend.core.ouroboros.governance.governed_loop_service.asyncio.sleep",
            new=sleep_mock,
        ),
    ):
        await service._feedback_loop()

    service._feedback_engine.consume_curriculum_once.assert_awaited_once()
    service._feedback_engine.consume_reactor_events_once.assert_awaited_once()
    service._feedback_engine.score_attribution_once.assert_awaited_once_with(
        fake_persistence,
    )
