"""Tests for GovernedLoopConfig L2 repair_budget field and _build_components wiring."""
from __future__ import annotations

import pytest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig, GovernedLoopService
from backend.core.ouroboros.governance.repair_engine import RepairBudget, RepairEngine

_GLS = "backend.core.ouroboros.governance.governed_loop_service"
_PROVIDERS = "backend.core.ouroboros.governance.providers"
_TR = "backend.core.ouroboros.governance.test_runner"


class TestGovernedLoopConfigRepairBudget:
    def test_default_repair_budget_is_enabled(self):
        """L2 is enabled by default as of the Iron Gate push (Manifesto §6).

        The self-repair loop is load-bearing for the Ouroboros cycle and
        must engage automatically when VALIDATE exhausts retries.
        """
        cfg = GovernedLoopConfig()
        budget = cfg.repair_budget
        assert budget is not None
        assert budget.enabled is True

    def test_from_env_defaults_repair_budget_enabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_L2_ENABLED", raising=False)
        cfg = GovernedLoopConfig.from_env()
        assert cfg.repair_budget.enabled is True

    def test_from_env_explicit_false_opts_out(self, monkeypatch):
        monkeypatch.setenv("JARVIS_L2_ENABLED", "false")
        cfg = GovernedLoopConfig.from_env()
        assert cfg.repair_budget.enabled is False

    def test_from_env_reads_l2_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_L2_ENABLED", "true")
        cfg = GovernedLoopConfig.from_env()
        assert cfg.repair_budget.enabled is True

    def test_from_env_reads_l2_max_iters(self, monkeypatch):
        monkeypatch.setenv("JARVIS_L2_MAX_ITERS", "3")
        cfg = GovernedLoopConfig.from_env()
        assert cfg.repair_budget.max_iterations == 3

    def test_repair_budget_field_is_repair_budget_type(self):
        cfg = GovernedLoopConfig()
        assert isinstance(cfg.repair_budget, RepairBudget)

    def test_custom_repair_budget_can_be_injected(self):
        budget = RepairBudget(enabled=True, max_iterations=2)
        cfg = GovernedLoopConfig(repair_budget=budget)
        assert cfg.repair_budget.enabled is True
        assert cfg.repair_budget.max_iterations == 2


def _mock_build_components_context(mock_primary):
    """Return an ExitStack that stubs the heavy imports in _build_components()."""
    mock_rr = MagicMock()
    mock_rr.list_enabled.return_value = []
    es = ExitStack()
    es.enter_context(patch(f"{_PROVIDERS}.PrimeProvider", return_value=mock_primary))
    mock_rr_cls = es.enter_context(patch(f"{_GLS}.RepoRegistry"))
    mock_rr_cls.from_env.return_value = mock_rr
    es.enter_context(patch(f"{_GLS}.CandidateGenerator"))
    es.enter_context(patch(f"{_GLS}.CLIApprovalProvider"))
    es.enter_context(patch(f"{_TR}.LanguageRouter"))
    es.enter_context(patch(f"{_TR}.PythonAdapter"))
    es.enter_context(patch(f"{_TR}.CppAdapter"))
    return es


class TestBuildComponentsRepairEngineWiring:
    @pytest.mark.asyncio
    async def test_repair_engine_wired_when_budget_enabled(self, tmp_path):
        """_build_components creates RepairEngine and wires it into OrchestratorConfig."""
        budget = RepairBudget(enabled=True)
        cfg = GovernedLoopConfig(project_root=tmp_path, repair_budget=budget)
        mock_primary = MagicMock()
        mock_primary.health_probe = AsyncMock(return_value=True)

        with _mock_build_components_context(mock_primary):
            svc = GovernedLoopService(prime_client=MagicMock(), config=cfg)
            await svc._build_components()

        engine = svc._orchestrator._config.repair_engine
        assert engine is not None
        assert isinstance(engine, RepairEngine)

    @pytest.mark.asyncio
    async def test_repair_engine_none_when_budget_disabled(self, tmp_path):
        """_build_components leaves repair_engine=None when repair_budget.enabled=False."""
        cfg = GovernedLoopConfig(project_root=tmp_path)  # defaults to disabled
        mock_primary = MagicMock()
        mock_primary.health_probe = AsyncMock(return_value=True)

        with _mock_build_components_context(mock_primary):
            svc = GovernedLoopService(prime_client=MagicMock(), config=cfg)
            await svc._build_components()

        assert svc._orchestrator._config.repair_engine is None

    @pytest.mark.asyncio
    async def test_repair_engine_none_when_no_primary_provider(self, tmp_path):
        """_build_components leaves repair_engine=None when primary provider is unavailable."""
        budget = RepairBudget(enabled=True)
        cfg = GovernedLoopConfig(project_root=tmp_path, repair_budget=budget)

        mock_rr = MagicMock()
        mock_rr.list_enabled.return_value = []
        # No prime_client → primary stays None → repair engine not created
        with ExitStack() as es:
            mock_rr_cls = es.enter_context(patch(f"{_GLS}.RepoRegistry"))
            mock_rr_cls.from_env.return_value = mock_rr
            es.enter_context(patch(f"{_GLS}.CandidateGenerator"))
            es.enter_context(patch(f"{_GLS}.CLIApprovalProvider"))
            es.enter_context(patch(f"{_TR}.LanguageRouter"))
            es.enter_context(patch(f"{_TR}.PythonAdapter"))
            es.enter_context(patch(f"{_TR}.CppAdapter"))
            svc = GovernedLoopService(prime_client=None, config=cfg)
            await svc._build_components()

        assert svc._orchestrator._config.repair_engine is None
