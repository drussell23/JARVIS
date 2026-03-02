"""Test AGI OS cross-registers coordinator with integration module."""
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestCrossRegistration:
    """Verify AGI OS path sets integration.py's coordinator."""

    def setup_method(self):
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False

    @pytest.mark.asyncio
    async def test_cross_registration_sets_integration_coordinator(self):
        """After AGI OS inits mesh, integration module should resolve it."""
        from backend.neural_mesh.integration import get_neural_mesh_coordinator

        mock_coordinator = MagicMock()
        mock_coordinator._running = True

        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            mark_neural_mesh_initialized,
        )
        set_neural_mesh_coordinator(mock_coordinator)
        mark_neural_mesh_initialized(True)

        result = get_neural_mesh_coordinator()
        assert result is mock_coordinator

    @pytest.mark.asyncio
    async def test_cross_registration_agent_visible(self):
        """GoogleWorkspaceAgent should be findable after cross-registration."""
        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        mock_coordinator = MagicMock()
        mock_coordinator._running = True
        mock_agent = MagicMock()
        mock_coordinator.get_agent.return_value = mock_agent

        set_neural_mesh_coordinator(mock_coordinator)

        coord = get_neural_mesh_coordinator()
        agent = coord.get_agent("google_workspace_agent")
        assert agent is mock_agent
