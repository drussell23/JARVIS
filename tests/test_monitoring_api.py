# [Ouroboros] Modified by Ouroboros (op=op-01a121b4-) at 2026-10-09 17:29 UTC
# Reason: `backend/api/monitoring_api.py` has no corresponding test module. CREATE `tests/test_monitoring_api.py` containing focus

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.monitoring_api import (
    get_system_metrics,
    get_api_performance,
    get_resource_usage,
    get_health_history,
    get_component_health,
    record_api_call,
    get_uptime,
    track_api_call,
    get_optimization_stats,
    get_engine_breakdown,
    get_proxy_status
)

# Mock data for testing
MOCK_METRICS = {
    'cpu': {'percent': 50.0, 'count': 8, 'per_cpu': [45.0, 55.0]},
    'memory': {'total_gb': 16.0, 'available_gb': 8.0, 'used_gb': 8.0, 'percent': 50.0},
    'disk': {'total_gb': 500.0, 'used_gb': 250.0, 'free_gb': 250.0, 'percent': 50.0},
    'process': {
        'memory_mb': 100.0,
        'cpu_percent': 10.0,
        'threads': 10,
        'open_files': 5
    },
    'timestamp': datetime.now().isoformat(),
    'uptime_seconds': 3600.0
}

MOCK_PERFORMANCE = {
    '/test/endpoint': {
        'request_count': 10,
        'total_time_ms': 500.0,
        'min_time_ms': 20.0,
        'max_time_ms': 100.0,
        'error_count': 1
    }
}

MOCK_RESOURCE_USAGE = {
    'memory': {
        'rss_mb': 100.0,
        'vms_mb': 200.0,
        'shared_mb': 50.0,
        'top_memory_maps': []
    },
    'files': {
        'open_count': 5,
        'open_files': ['/tmp/test.txt']
    },
    'network': {
        'connection_count': 3,
        'connections': [{'local': '127.0.0.1:8000', 'remote': '127.0.0.1:54321', 'status': 'ESTABLISHED'}]
    },
    'threads': {
        'count': 10
    },
    'timestamp': datetime.now().isoformat()
}

MOCK_HEALTH_HISTORY = {
    'history': [MOCK_METRICS],
    'summary': {
        'data_points': 1,
        'period_minutes': 60,
        'avg_cpu_percent': 50.0,
        'avg_memory_percent': 50.0,
        'peak_cpu_percent': 50.0,
        'peak_memory_percent': 50.0
    }
}

MOCK_COMPONENT_HEALTH = {
    'components': {
        'voice': {'status': 'healthy', 'available': True},
        'vision': {'status': 'unavailable', 'available': False}
    },
    'summary': {
        'healthy': 1,
        'total': 2,
        'health_percentage': 50.0
    },
    'timestamp': datetime.now().isoformat()
}

MOCK_UPTIME = {
    'start_time': datetime.now().isoformat(),
    'current_time': datetime.now().isoformat(),
    'uptime': {
        'total_seconds': 3600.0,
        'days': 0,
        'hours': 1,
        'minutes': 0,
        'seconds': 0
    },
    'formatted': '1:00:00'
}

MOCK_OPT_STATS = {
    'status': 'available',
    'timestamp': datetime.now().isoformat()
}

MOCK_ENGINE_BREAKDOWN = {
    'RUST_INT8': {'count': 5, 'models': ['model1', 'model2']},
    'timestamp': datetime.now().isoformat()
}

MOCK_PROXY_STATUS = {
    'proxies': [{'name': 'test_proxy', 'status': 'READY'}],
    'timestamp': datetime.now().isoformat()
}

# Mock global state for testing
MOCK_START_TIME = datetime.now() - timedelta(hours=1)

async def _mock_get_system_metrics() -> Dict[str, Any]:
    return MOCK_METRICS

async def _mock_get_api_performance() -> Dict[str, Any]:
    return {
        'endpoints': {},
        'total_requests': 0,
        'total_errors': 0,
        'measurement_period': {
            'start': MOCK_START_TIME.isoformat(),
            'duration_seconds': 3600.0
        }
    }

async def _mock_get_resource_usage() -> Dict[str, Any]:
    return MOCK_RESOURCE_USAGE

async def _mock_get_health_history(minutes: int = 60) -> Dict[str, Any]:
    return MOCK_HEALTH_HISTORY

async def _mock_get_component_health() -> Dict[str, Any]:
    return MOCK_COMPONENT_HEALTH

async def _mock_get_uptime() -> Dict[str, Any]:
    return MOCK_UPTIME

async def _mock_get_optimization_stats(request) -> Dict[str, Any]:
    return MOCK_OPT_STATS

async def _mock_get_engine_breakdown(request) -> Dict[str, Any]:
    return MOCK_ENGINE_BREAKDOWN

async def _mock_get_proxy_status(request) -> Dict[str, Any]:
    return MOCK_PROXY_STATUS

async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert callable(get_system_metrics)
    assert callable(get_api_performance)
    assert callable(get_resource_usage)
    assert callable(get_health_history)
    assert callable(get_component_health)
    assert callable(record_api_call)
    assert callable(get_uptime)
    assert callable(track_api_call)
    assert callable(get_optimization_stats)
    assert callable(get_engine_breakdown)
    assert callable(get_proxy_status)


async def test_get_api_performance() -> None:
    """Test get_api_performance function."""
    with patch('backend.api.monitoring_api._api_performance', MOCK_PERFORMANCE):
        result = await get_api_performance()
        assert 'endpoints' in result
        assert 'total_requests' in result
        assert 'total_errors' in result

async def test_get_resource_usage() -> None:
    """Test get_resource_usage function."""
    with patch('backend.api.monitoring_api.psutil.Process') as mock_process:
        mock_process_instance = MagicMock()
        mock_process_instance.memory_info.return_value.rss = 100 * 1024 * 1024
        mock_process_instance.memory_info.return_value.vms = 200 * 1024 * 1024
        mock_process_instance.num_threads.return_value = 10
        mock_process.return_value = mock_process_instance
        
        result = await get_resource_usage()
        assert 'memory' in result
        assert 'files' in result
        assert 'network' in result
        assert 'threads' in result

async def test_get_health_history() -> None:
    """Test get_health_history function."""
    with patch('backend.api.monitoring_api._metrics_history', [MOCK_METRICS]):
        result = await get_health_history(minutes=60)
        assert 'history' in result
        assert 'summary' in result
        assert result['summary']['data_points'] == 1

async def test_get_component_health() -> None:
    """Test get_component_health function."""
    result = await get_component_health()
    assert 'components' in result
    assert 'summary' in result
    assert 'timestamp' in result

async def test_record_api_call_success() -> None:
    """Test record_api_call function with success case."""
    result = await record_api_call('/test', 100.0, True)
    assert result['status'] == 'recorded'

async def test_record_api_call_error() -> None:
    """Test record_api_call function with error case."""
    result = await record_api_call('/test', 100.0, False)
    assert result['status'] == 'recorded'

async def test_get_uptime() -> None:
    """Test get_uptime function."""
    with patch('backend.api.monitoring_api._start_time', MOCK_START_TIME):
        result = await get_uptime()
        assert 'start_time' in result
        assert 'current_time' in result
        assert 'uptime' in result
        assert 'formatted' in result

async def test_track_api_call() -> None:
    """Test track_api_call function."""
    # Test that it doesn't raise an exception
    track_api_call('/test', 100.0, True)
    # Verify it was recorded
    assert '/test' in track_api_call.__globals__['_api_performance']




async def test_get_health_history_empty_history() -> None:
    """Test get_health_history function with empty history."""
    with patch('backend.api.monitoring_api._metrics_history', []):
        result = await get_health_history(minutes=60)
        assert 'history' in result
        assert 'summary' in result
        assert result['summary']['data_points'] == 0

