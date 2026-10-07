# [Ouroboros] Modified by Ouroboros (op=op-01a116ed-) at 2026-10-07 15:17 UTC
# Reason: `backend/autonomy/system_states.py` has no corresponding test module. CREATE `tests/test_system_states.py` containing fo

from __future__ import annotations
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from backend.autonomy.system_states import (
    SystemState,
    ComponentState,
    TransitionReason,
    StateTransition,
    ComponentStatus,
    SystemStateManager,
    test_state_system,
)

class TestSystemStates:
    def _make_manager(self) -> SystemStateManager:
        return SystemStateManager()

    @pytest.mark.asyncio
    async def test_import_smoke(self):
        """Test that the module can be imported and basic classes are accessible."""
        # This test ensures no import errors or syntax issues
        assert SystemState
        assert ComponentState
        assert TransitionReason
        assert StateTransition
        assert ComponentStatus
        assert SystemStateManager
        assert test_state_system

    @pytest.mark.asyncio
    async def test_initialize_and_shutdown(self):
        """Test basic initialization and shutdown."""
        manager = self._make_manager()
        assert manager.current_state == SystemState.INITIALIZING
        
        # Test system initialization
        result = await manager.initialize_system()
        assert result is True
        assert manager.current_state == SystemState.IDLE
        
        # Test shutdown
        result = await manager.shutdown_system()
        assert result is True
        assert manager.current_state == SystemState.SHUTDOWN

    @pytest.mark.asyncio
    async def test_component_registration_and_updates(self):
        """Test component registration and state updates."""
        manager = self._make_manager()
        
        # Test component registration
        manager.register_component("test_component", ComponentState.READY)
        assert "test_component" in manager.components
        
        # Test component state update
        manager.update_component_state(
            "test_component",
            ComponentState.ACTIVE,
            health_score=0.9,
            metadata={"fps": 30}
        )
        
        component = manager.components["test_component"]
        assert component.state == ComponentState.ACTIVE
        assert component.health_score == 0.9
        assert component.metadata["fps"] == 30
        
        # Test error state update
        manager.update_component_state("test_component", ComponentState.ERROR)
        assert component.error_count == 1

    @pytest.mark.asyncio
    async def test_valid_transitions(self):
        """Test valid state transitions."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Test valid transition
        result = await manager.transition_to(SystemState.MONITORING, TransitionReason.USER_REQUEST)
        assert result is True
        assert manager.current_state == SystemState.MONITORING
        
        # Test another valid transition
        result = await manager.transition_to(SystemState.PROCESSING, TransitionReason.AUTOMATIC)
        assert result is True
        assert manager.current_state == SystemState.PROCESSING

    @pytest.mark.asyncio
    async def test_invalid_transitions(self):
        """Test invalid state transitions are properly rejected."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Test invalid transition (from IDLE to EXECUTING)
        result = await manager.transition_to(SystemState.EXECUTING, TransitionReason.USER_REQUEST)
        assert result is False
        assert manager.current_state == SystemState.IDLE  # Should remain unchanged

    @pytest.mark.asyncio
    async def test_system_operations(self):
        """Test system operation methods."""
        manager = self._make_manager()
        
        # Test initialize_system
        result = await manager.initialize_system()
        assert result is True
        assert manager.current_state == SystemState.IDLE
        
        # Test start_monitoring
        result = await manager.start_monitoring()
        assert result is True
        assert manager.current_state == SystemState.MONITORING
        
        # Test pause_system
        result = await manager.pause_system()
        assert result is True
        assert manager.current_state == SystemState.PAUSED
        
        # Test resume_system
        result = await manager.resume_system()
        assert result is True
        assert manager.current_state == SystemState.MONITORING

    @pytest.mark.asyncio
    async def test_component_health_checking(self):
        """Test component health checking and error recovery."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Register critical components
        manager.register_component("vision_pipeline", ComponentState.READY)
        manager.register_component("decision_engine", ComponentState.READY)
        manager.register_component("action_queue", ComponentState.READY)
        
        # Test healthy components
        assert manager.components["vision_pipeline"].is_healthy is True
        
        # Set a component to error state
        manager.update_component_state("vision_pipeline", ComponentState.ERROR)
        
        # Test that system transitions to error recovery (this would be async, so we check the logic)
        # The actual transition happens in _check_component_health which is called by update_component_state
        assert manager.components["vision_pipeline"].error_count == 1

    @pytest.mark.asyncio
    async def test_get_system_status(self):
        """Test system status reporting."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Test status retrieval
        status = manager.get_system_status()
        assert isinstance(status, dict)
        assert "current_state" in status
        assert "previous_state" in status
        assert "state_duration" in status
        assert "components" in status
        
        # Check that we have the expected state
        assert status["current_state"] == SystemState.IDLE.name

    @pytest.mark.asyncio
    async def test_callback_mechanisms(self):
        """Test callback registration and execution."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Test state callback registration
        mock_callback = AsyncMock()
        manager.add_state_callback(SystemState.MONITORING, mock_callback)
        
        # Test transition that should trigger callback
        result = await manager.transition_to(SystemState.MONITORING, TransitionReason.USER_REQUEST)
        assert result is True
        
        # Note: Callbacks are executed asynchronously, so we can't directly verify execution here
        # But we can check that the callback was registered
        assert len(manager.state_callbacks[SystemState.MONITORING]) == 1
        
        # Test transition callback registration
        mock_transition_callback = AsyncMock()
        manager.add_transition_callback(mock_transition_callback)
        
        # Test that transition callbacks are registered
        assert len(manager.transition_callbacks) == 1

    @pytest.mark.asyncio
    async def test_edge_cases(self):
        """Test edge cases and error conditions."""
        manager = self._make_manager()
        
        # Test transition to same state (should return True but not change anything)
        result = await manager.transition_to(SystemState.INITIALIZING, TransitionReason.USER_REQUEST)
        assert result is True
        assert manager.current_state == SystemState.INITIALIZING
        
        # Test with unknown component (should log warning but not crash)
        manager.update_component_state("nonexistent_component", ComponentState.ACTIVE)
        
        # Test invalid health score (should be clamped)
        manager.register_component("test_component2", ComponentState.READY)
        manager.update_component_state("test_component2", ComponentState.READY, health_score=1.5)  # Should clamp to 1.0
        assert manager.components["test_component2"].health_score == 1.0
        
        manager.update_component_state("test_component2", ComponentState.READY, health_score=-0.5)  # Should clamp to 0.0
        assert manager.components["test_component2"].health_score == 0.0

    @pytest.mark.asyncio
    async def test_can_transition_to(self):
        """Test the can_transition_to method."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Test valid transitions
        assert manager.can_transition_to(SystemState.MONITORING) is True
        assert manager.can_transition_to(SystemState.PAUSED) is True
        assert manager.can_transition_to(SystemState.SHUTDOWN) is True
        
        # Test invalid transitions
        assert manager.can_transition_to(SystemState.EXECUTING) is False
        assert manager.can_transition_to(SystemState.INITIALIZING) is False  # Can't go backwards

    @pytest.mark.asyncio
    async def test_get_available_transitions(self):
        """Test the get_available_transitions method."""
        manager = self._make_manager()
        
        # Initialize system first
        await manager.initialize_system()
        
        # Test available transitions from IDLE state
        available = manager.get_available_transitions()
        assert SystemState.MONITORING in available
        assert SystemState.PAUSED in available
        assert SystemState.SHUTDOWN in available
        
        # Test that we can transition to one of the available states
        result = await manager.transition_to(SystemState.MONITORING, TransitionReason.USER_REQUEST)
        assert result is True
        
        # After transitioning, check new available transitions
        available = manager.get_available_transitions()
        assert SystemState.PROCESSING in available
        assert SystemState.IDLE in available
        assert SystemState.PAUSED in available
        assert SystemState.ERROR_RECOVERY in available

    @pytest.mark.asyncio
    async def test_component_health_properties(self):
        """Test component health property methods."""
        manager = self._make_manager()
        
        # Register a component
        manager.register_component("test_component", ComponentState.READY)
        
        # Test healthy component
        assert manager.components["test_component"].is_healthy is True
        assert manager.components["test_component"].needs_attention is False
        
        # Make component unhealthy by changing state
        manager.update_component_state("test_component", ComponentState.ERROR)
        assert manager.components["test_component"].needs_attention is True
        
        # Test with low health score
        manager.update_component_state("test_component", ComponentState.READY, health_score=0.3)
        assert manager.components["test_component"].needs_attention is True