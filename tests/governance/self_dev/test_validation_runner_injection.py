"""Tests for ValidationRunner DI into GovernedOrchestrator."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)


def test_orchestrator_accepts_validation_runner():
    """GovernedOrchestrator.__init__ accepts validation_runner kwarg."""
    mock_runner = MagicMock()
    orch = GovernedOrchestrator(
        stack=MagicMock(),
        generator=MagicMock(),
        approval_provider=MagicMock(),
        config=OrchestratorConfig(project_root=Path("/tmp")),
        validation_runner=mock_runner,
    )
    assert orch._validation_runner is mock_runner


def test_orchestrator_validation_runner_defaults_to_none():
    """validation_runner defaults to None if not supplied."""
    orch = GovernedOrchestrator(
        stack=MagicMock(),
        generator=MagicMock(),
        approval_provider=MagicMock(),
        config=OrchestratorConfig(project_root=Path("/tmp")),
    )
    assert orch._validation_runner is None


def test_build_components_wires_language_router(tmp_path):
    """_build_components() creates LanguageRouter and passes it to orchestrator."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )
    from backend.core.ouroboros.governance.test_runner import LanguageRouter

    config = GovernedLoopConfig(project_root=tmp_path)
    svc = GovernedLoopService(
        stack=MagicMock(),
        prime_client=None,
        config=config,
    )
    asyncio.get_event_loop().run_until_complete(svc._build_components())

    assert svc._orchestrator is not None
    assert isinstance(svc._orchestrator._validation_runner, LanguageRouter)
