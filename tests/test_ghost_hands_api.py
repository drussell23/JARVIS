# [Ouroboros] Modified by Ouroboros (op=op-01a121c2-) at 2026-10-09 17:44 UTC
# Reason: `backend/api/ghost_hands_api.py` has no corresponding test module. CREATE `tests/test_ghost_hands_api.py` containing foc

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.ghost_hands_api import (
    ActionModel,
    AutoRetryRequest,
    CreateTaskRequest,
    GhostHandsAPI,
    NotifyRequest,
    SpeakRequest,
    StatsResponse,
    TaskResponse,
    VerbosityRequest,
    WatchAndReactRequest,
    create_ghost_hands_router
)


class _FakeGhostHands:
    """Mock ghost hands instance for testing."""
    def __init__(self):
        self._is_running = False
        self.stats = {
            'components': {},
            'active_tasks': 0,
            'watching_tasks': 0,
            'total_tasks_created': 0,
            'total_triggers': 0,
            'total_actions_executed': 0,
            'successful_executions': 0,
            'failed_executions': 0,
            'start_time': None
        }
        
    async def start(self):
        self._is_running = True
        return True
        
    async def stop(self):
        self._is_running = False
        
    def get_stats(self):
        return self.stats
        
    async def create_task(self, **kwargs):
        class MockTask:
            def __init__(self):
                self.name = kwargs.get('name', 'test_task')
                self.state = MagicMock()
                self.state.name = 'running'
        return MockTask()
        
    def list_tasks(self):
        return []
        
    def get_task(self, name):
        class MockTask:
            def __init__(self):
                self.name = name
                self.state = MagicMock()
                self.state.name = 'running'
                self.watch_app = None
                self.trigger_text = None
                self.trigger_pattern = None
                self.one_shot = False
                self.enabled = True
                self.trigger_count = 0
                self.last_triggered = None
        return MockTask()
        
    async def _get_ghost_hands(self):
        return self


class _FakeActuator:
    """Mock actuator for testing."""
    def __init__(self):
        self.stats = {}
        
    async def get_stats(self):
        return self.stats


class _FakeVision:
    """Mock vision system for testing."""
    def __init__(self):
        self._n_optic = None
        
    async def get_all_windows(self):
        return []


async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert hasattr(GhostHandsAPI, '__init__')
    assert callable(create_ghost_hands_router)
    
    # Test that all Pydantic models are available
    assert hasattr(ActionModel, '__init__')
    assert hasattr(CreateTaskRequest, '__init__')
    assert hasattr(WatchAndReactRequest, '__init__')
    assert hasattr(AutoRetryRequest, '__init__')
    assert hasattr(NotifyRequest, '__init__')
    assert hasattr(SpeakRequest, '__init__')
    assert hasattr(VerbosityRequest, '__init__')
    assert hasattr(TaskResponse, '__init__')
    assert hasattr(StatsResponse, '__init__')


async def test_start_system() -> None:
    """Test start_system function."""
    with patch('backend.api.ghost_hands_api._get_ghost_hands') as mock_get_gh:
        fake_gh = _FakeGhostHands()
        fake_gh._is_running = False
        mock_get_gh.return_value = fake_gh
        
        api = GhostHandsAPI()
        result = await api.start_system()
        
        assert result['status'] == 'started'
        assert result['message'] == 'Ghost Hands system started'
        
        # Test already running case
        fake_gh._is_running = True
        result = await api.start_system()
        assert result['status'] == 'already_running'
        assert result['message'] == 'Ghost Hands is already running'




async def test_get_status() -> None:
    """Test get_status function."""
    with patch('backend.api.ghost_hands_api._get_ghost_hands') as mock_get_gh:
        fake_gh = _FakeGhostHands()
        fake_gh._is_running = True
        mock_get_gh.return_value = fake_gh
        
        api = GhostHandsAPI()
        result = await api.get_status()
        
        assert result['status'] == 'running'
        assert result['is_running'] is True
        assert result['components'] == {}
        assert result['active_tasks'] == 0
        assert result['watching_tasks'] == 0


async def test_get_stats() -> None:
    """Test get_stats function."""
    with patch('backend.api.ghost_hands_api._get_ghost_hands') as mock_get_gh:
        fake_gh = _FakeGhostHands()
        fake_gh._is_running = True
        mock_get_gh.return_value = fake_gh
        
        api = GhostHandsAPI()
        result = await api.get_stats()
        
        assert result['is_running'] is True
        assert result['components'] == {}
        assert result['active_tasks'] == 0
        assert result['watching_tasks'] == 0
        assert result['total_tasks_created'] == 0
        assert result['total_triggers'] == 0
        assert result['total_actions_executed'] == 0
        assert result['successful_executions'] == 0
        assert result['failed_executions'] == 0


async def test_create_task() -> None:
    """Test create_task function."""
    with patch('backend.api.ghost_hands_api._get_ghost_hands') as mock_get_gh:
        fake_gh = _FakeGhostHands()
        mock_get_gh.return_value = fake_gh
        
        api = GhostHandsAPI()
        request = CreateTaskRequest(
            name='test_task',
            actions=[ActionModel(type='click')]
        )
        
        result = await api.create_task(request)
        
        assert result['status'] == 'created'
        assert result['task_name'] == 'test_task'
        assert result['state'] == 'running'


async def test_list_tasks() -> None:
    """Test list_tasks function."""
    with patch('backend.api.ghost_hands_api._get_ghost_hands') as mock_get_gh:
        fake_gh = _FakeGhostHands()
        mock_get_gh.return_value = fake_gh
        
        api = GhostHandsAPI()
        result = await api.list_tasks()
        
        assert 'tasks' in result
        assert 'count' in result
















async def test_list_windows() -> None:
    """Test list_windows function."""
    with patch('backend.api.ghost_hands_api._get_ghost_hands') as mock_get_gh:
        fake_gh = _FakeGhostHands()
        fake_gh._n_optic = _FakeVision()
        mock_get_gh.return_value = fake_gh
        
        api = GhostHandsAPI()
        result = await api.list_windows()
        
        assert 'windows' in result
        assert 'count' in result








