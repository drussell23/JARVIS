# tests/unit/supervisor/test_policy_delegation.py
"""Tests for policy delegation when verdict_executor_mode is enabled."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class TestPolicyDelegation:
    def test_verdict_executor_mode_default_false(self):
        """ProcessOrchestrator defaults to verdict_executor_mode=False."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator
        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        assert orch._verdict_executor_mode is False

    def test_set_verdict_executor_mode(self):
        """set_verdict_executor_mode toggles the flag."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator
        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch._verdict_executor_mode = False
        orch.set_verdict_executor_mode(True)
        assert orch._verdict_executor_mode is True
        orch.set_verdict_executor_mode(False)
        assert orch._verdict_executor_mode is False

    def test_should_delegate_health_when_active(self):
        """When verdict_executor_mode=True, health decisions should be delegated."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator
        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch._verdict_executor_mode = True
        assert orch.should_delegate_health_decisions() is True

    def test_should_not_delegate_when_inactive(self):
        """When verdict_executor_mode=False, health decisions are NOT delegated."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator
        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch._verdict_executor_mode = False
        assert orch.should_delegate_health_decisions() is False

    def test_should_delegate_restart_when_active(self):
        """When verdict_executor_mode=True, restart decisions should be delegated."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator
        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch._verdict_executor_mode = True
        assert orch.should_delegate_restart_decisions() is True

    def test_should_not_delegate_restart_when_inactive(self):
        """When verdict_executor_mode=False, restart decisions are NOT delegated."""
        from backend.supervisor.cross_repo_startup_orchestrator import ProcessOrchestrator
        orch = ProcessOrchestrator.__new__(ProcessOrchestrator)
        orch._verdict_executor_mode = False
        assert orch.should_delegate_restart_decisions() is False
