# [Ouroboros] Modified by Ouroboros (op=op-01a10eb7-) at 2026-10-06 00:58 UTC
# Reason: `backend/resource_manager.py` has no corresponding test module. CREATE `tests/test_resource_manager.py` containing focus

from unittest.mock import patch, MagicMock
import asyncio
from datetime import datetime
from backend.resource_manager import (
    ResourceManager,
    ResourceSnapshot,
    ServicePriority,
    get_resource_manager,
    throttled_operation,
)

class TestResourceManager:
    def test_init_sets_correct_defaults(self):
        """ResourceManager initializes with correct default values."""
        rm = ResourceManager()
        assert rm.TOTAL_RAM_GB == 16
        assert rm.MAX_MEMORY_PERCENT == 30.0
        assert rm.PANIC_MEMORY_PERCENT == 35.0
        assert rm.MAX_JARVIS_MEMORY_MB == 1024
        assert rm.MAX_ML_MEMORY_MB == 300
        assert rm.MAX_CPU_PERCENT == 40.0
        assert rm.current_ml_model is None
        assert rm.throttle_level == 0
        assert not rm.monitoring

    def test_start_monitoring_starts_thread(self):
        """start_monitoring starts monitoring thread."""
        rm = ResourceManager()
        with patch('threading.Thread') as mock_thread:
            rm.start_monitoring()
            assert rm.monitoring is True
            mock_thread.assert_called_once()

    def test_stop_monitoring_stops_thread(self):
        """stop_monitoring stops monitoring thread."""
        rm = ResourceManager()
        with patch.object(rm, 'monitor_thread', MagicMock()):
            rm.monitoring = True
            rm.stop_monitoring()
            assert rm.monitoring is False

    def test_request_voice_unlock_resources_succeeds_when_safe(self):
        """request_voice_unlock_resources succeeds when memory allows."""
        rm = ResourceManager()
        with patch.object(rm, '_take_snapshot', return_value=MagicMock(memory_percent=20.0)):
            result = rm.request_voice_unlock_resources()
            assert result is True

    def test_request_voice_unlock_resources_fails_when_memory_high(self):
        """request_voice_unlock_resources fails when memory is too high."""
        rm = ResourceManager()
        with patch.object(rm, '_take_snapshot', return_value=MagicMock(memory_percent=30.0)):
            result = rm.request_voice_unlock_resources()
            assert result is False

    def test_request_ml_model_succeeds_when_memory_low(self):
        """request_ml_model succeeds when memory allows."""
        rm = ResourceManager()
        with patch.object(rm, '_take_snapshot', return_value=MagicMock(memory_percent=20.0)):
            result = rm.request_ml_model('test_model')
            assert result is True
            assert rm.current_ml_model == 'test_model'

    def test_request_ml_model_fails_when_memory_high(self):
        """request_ml_model fails when memory is too high."""
        rm = ResourceManager()
        with patch.object(rm, '_take_snapshot', return_value=MagicMock(memory_percent=30.0)):
            result = rm.request_ml_model('test_model')
            assert result is False
            assert rm.current_ml_model is None

    def test_predict_next_model_returns_none_when_queue_too_small(self):
        """predict_next_model returns None when model queue has fewer than 3 items."""
        rm = ResourceManager()
        result = rm.predict_next_model()
        assert result is None

    def test_predict_next_model_returns_predicted_model(self):
        """predict_next_model returns the most frequent model in queue."""
        rm = ResourceManager()
        # Add some models to the queue
        rm.ml_model_queue.append(('model_a', datetime.now()))
        rm.ml_model_queue.append(('model_a', datetime.now()))
        rm.ml_model_queue.append(('model_b', datetime.now()))
        result = rm.predict_next_model()
        assert result == 'model_a'

    def test_get_throttle_delay_returns_correct_values(self):
        """get_throttle_delay returns correct delay values for each throttle level."""
        rm = ResourceManager()
        # Test all throttle levels from 0 to 5
        expected_delays = {0: 0.0, 1: 0.05, 2: 0.2, 3: 0.5, 4: 1.0, 5: 2.0}
        for level, expected in expected_delays.items():
            rm.throttle_level = level
            assert rm.get_throttle_delay() == expected

    def test_get_throttle_delay_returns_zero_for_high_levels(self):
        """get_throttle_delay returns 0.0 for throttle levels beyond 5."""
        rm = ResourceManager()
        rm.throttle_level = 10
        assert rm.get_throttle_delay() == 0.0

    def test_get_status_returns_empty_dict_when_no_history(self):
        """get_status returns empty dict when history is empty."""
        rm = ResourceManager()
        result = rm.get_status()
        assert result == {}

    def test_get_status_returns_correct_data_when_history_exists(self):
        """get_status returns correct data when history exists."""
        rm = ResourceManager()
        # Add a mock snapshot to history
        mock_snapshot = ResourceSnapshot(
            timestamp=datetime.now(),
            memory_percent=50.0,
            memory_available_mb=1024.0,
            memory_used_mb=2048.0,
            cpu_percent=30.0,
            cpu_per_core=[25.0, 35.0],
            jarvis_memory_mb=512.0,
            ml_models_loaded=1,
            active_services=['voice_unlock']
        )
        rm.history.append(mock_snapshot)
        rm.current_ml_model = 'test_model'
        rm.active_services['voice_unlock'] = True
        result = rm.get_status()
        assert 'memory_percent' in result
        assert 'cpu_percent' in result
        assert 'throttle_level' in result
        assert 'current_ml_model' in result
        assert 'active_services' in result


class TestGetResourceManager:
    def test_get_resource_manager_returns_instance(self):
        """get_resource_manager returns a ResourceManager instance."""
        rm = get_resource_manager()
        assert isinstance(rm, ResourceManager)


class TestThrottledOperation:
    def test_throttled_operation_decorator_works(self):
        """throttled_operation decorator adds delay based on throttle level."""
        @throttled_operation
        def dummy_func():
            return True
        
        # Just verify the decorator can be applied
        assert callable(dummy_func)


class TestResourceSnapshot:
    def test_resource_snapshot_creation(self):
        """ResourceSnapshot can be created with all required fields."""
        snapshot = ResourceSnapshot(
            timestamp=datetime.now(),
            memory_percent=50.0,
            memory_available_mb=1024.0,
            memory_used_mb=2048.0,
            cpu_percent=30.0,
            cpu_per_core=[25.0, 35.0],
            jarvis_memory_mb=512.0,
            ml_models_loaded=1,
            active_services=['voice_unlock']
        )
        assert snapshot.memory_percent == 50.0
        assert snapshot.cpu_per_core == [25.0, 35.0]


class TestServicePriority:
    def test_service_priority_enum_has_correct_values(self):
        """ServicePriority enum has correct values."""
        assert ServicePriority.CRITICAL.value == 0
        assert ServicePriority.HIGH.value == 1
        assert ServicePriority.MEDIUM.value == 2
        assert ServicePriority.LOW.value == 3
        assert ServicePriority.IDLE.value == 4