# [Ouroboros] Modified by Ouroboros (op=op-01a109a6-) at 2026-10-05 01:29 UTC
# Reason: `backend/autonomy/macos_integration.py` has no corresponding test module. CREATE `tests/test_macos_integration.py` conta

from __future__ import annotations
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
import asyncio
from datetime import datetime

# Import the module under test
from backend.autonomy.macos_integration import (
    SystemResource,
    ControlAction,
    SystemState,
    ControlDecision,
    AdvancedMacOSIntegration,
    get_macos_integration
)


def test_import_smoke():
    """Test that the module can be imported without errors."""
    # This test ensures all imports work correctly
    assert SystemResource is not None
    assert ControlAction is not None
    assert SystemState is not None
    assert ControlDecision is not None
    assert AdvancedMacOSIntegration is not None
    assert get_macos_integration is not None


@pytest.mark.asyncio
async def test_get_macos_integration_creation():
    """Test that get_macos_integration creates an instance correctly."""
    api_key = "test_api_key_12345"
    
    with patch('backend.autonomy.macos_integration.anthropic'):
        integration = get_macos_integration(api_key)
        
        assert isinstance(integration, AdvancedMacOSIntegration)
        assert integration.claude is not None


@pytest.mark.asyncio
async def test_system_state_initialization():
    """Test that SystemState initializes with correct default values."""
    # Create a simple SystemState instance
    state = SystemState(
        cpu_usage=50.0,
        memory_usage=60.0,
        disk_usage=70.0,
        active_apps=['Finder', 'Safari'],
        network_status={'wifi_connected': True},
        display_config={'brightness': 0.8},
        power_status={'on_battery': False}
    )
    
    assert state.cpu_usage == 50.0
    assert state.memory_usage == 60.0
    assert state.disk_usage == 70.0
    assert state.active_apps == ['Finder', 'Safari']
    assert state.network_status['wifi_connected'] is True
    assert state.display_config['brightness'] == 0.8
    assert state.power_status['on_battery'] is False


@pytest.mark.asyncio
async def test_control_decision_initialization():
    """Test that ControlDecision initializes correctly with all required fields."""
    decision = ControlDecision(
        resource=SystemResource.CPU,
        action=ControlAction.OPTIMIZE,
        parameters={'test': 'value'},
        reasoning='test reasoning',
        confidence=0.9,
        impact_prediction={'impact': 0.8}
    )
    
    assert decision.resource == SystemResource.CPU
    assert decision.action == ControlAction.OPTIMIZE
    assert decision.parameters['test'] == 'value'
    assert decision.reasoning == 'test reasoning'
    assert decision.confidence == 0.9
    assert decision.impact_prediction['impact'] == 0.8
    assert decision.reversible is True  # Default value


@pytest.mark.asyncio
async def test_advanced_macos_integration_init():
    """Test that AdvancedMacOSIntegration initializes correctly."""
    api_key = 'test_api_key_12345'
    
    with patch('backend.autonomy.macos_integration.anthropic'):
        integration = AdvancedMacOSIntegration(api_key)
        
        assert integration.claude is not None
        assert integration.system_state is None
        assert integration.monitoring_active is False
        assert integration.control_history == []
        assert integration.safety_limits is not None


@pytest.mark.asyncio
async def test_start_stop_system_monitoring():
    """Test that start and stop system monitoring work correctly."""
    api_key = 'test_api_key_12345'
    
    with patch('backend.autonomy.macos_integration.anthropic'):
        integration = AdvancedMacOSIntegration(api_key)
        
        # Test starting monitoring
        await integration.start_system_monitoring()
        assert integration.monitoring_active is True
        
        # Test stopping monitoring
        await integration.stop_system_monitoring()
        assert integration.monitoring_active is False


@pytest.mark.asyncio
async def test_get_system_status_not_monitoring():
    """Test get_system_status when not monitoring."""
    api_key = 'test_api_key_12345'
    
    with patch('backend.autonomy.macos_integration.anthropic'):
        integration = AdvancedMacOSIntegration(api_key)
        
        status = integration.get_system_status()
        assert status['status'] == 'Not monitoring'


@pytest.mark.asyncio
async def test_get_system_status_monitoring_active():
    """Test get_system_status when monitoring is active."""
    api_key = 'test_api_key_12345'
    
    # Mock system state
    mock_state = MagicMock()
    mock_state.cpu_usage = 50.0
    mock_state.memory_usage = 60.0
    mock_state.disk_usage = 70.0
    mock_state.active_apps = ['Finder', 'Safari']
    mock_state.network_status = {'wifi_connected': True}
    mock_state.power_status = {'on_battery': False}
    mock_state.timestamp = datetime.now()
    
    with patch('backend.autonomy.macos_integration.anthropic'):
        integration = AdvancedMacOSIntegration(api_key)
        integration.system_state = mock_state
        integration.monitoring_active = True
        integration.control_history = [{}]  # Add some history
        
        status = integration.get_system_status()
        
        assert status['monitoring_active'] is True
        assert status['system_health']['cpu_usage'] == '50.0%'
        assert status['system_health']['memory_usage'] == '60.0%'
        assert status['system_health']['disk_usage'] == '70.0%'
        assert status['active_apps'] == 2
        assert status['network_connected'] is True
        assert status['on_battery'] is False
        assert status['optimizations_applied'] == 1
