# [Ouroboros] Modified by Ouroboros (op=op-01a121ad-) at 2026-10-09 17:21 UTC
# Reason: `backend/api/proactive_monitoring_handler.py` has no corresponding test module. CREATE `tests/test_proactive_monitoring_

from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.api.proactive_monitoring_handler import ProactiveMonitoringHandler, get_monitoring_handler

class _FakeVisionHandler:
    def __init__(self):
        self.intelligence = MagicMock()
        self.jarvis_api = AsyncMock()

class _FakeIntelligence:
    def __init__(self, has_workspace_insights=True, has_multi_space_monitoring=True):
        self.has_workspace_insights = has_workspace_insights
        self.has_multi_space_monitoring = has_multi_space_monitoring
        self.multi_space_detector = MagicMock()
        self.multi_space_detector.get_all_windows_across_spaces.return_value = {
            'spaces': [],
            'windows': [],
            'current_space': {'id': 1}
        }

    async def get_workspace_insights(self):
        if self.has_workspace_insights:
            return "Workspace insights available"
        else:
            raise RuntimeError("No insights")

    async def start_multi_space_monitoring(self):
        return self.has_multi_space_monitoring

    async def stop_multi_space_monitoring(self):
        pass

class _FakeJarvisAPI:
    def __init__(self):
        self.speak_proactive = AsyncMock()





@pytest.mark.asyncio
async def test_handle_monitoring_request_report_changes():
    vision_handler = _FakeVisionHandler()
    vision_handler.intelligence = _FakeIntelligence()
    handler = ProactiveMonitoringHandler(vision_handler)
    
    result = await handler.handle_monitoring_request("report any changes")
    assert result["handled"] is True
    assert result["monitoring_active"] is True
    assert result["report_changes"] is True


@pytest.mark.asyncio
async def test_handle_monitoring_request_desktop_space_query():
    handler = ProactiveMonitoringHandler(_FakeVisionHandler())
    result = await handler.handle_monitoring_request("tell me about my desktop space")
    assert result["handled"] is False
    assert "should be handled by vision pipeline" in result["reason"]


@pytest.mark.asyncio
async def test_handle_monitoring_request_workspace_insights():
    vision_handler = _FakeVisionHandler()
    vision_handler.intelligence = _FakeIntelligence()
    handler = ProactiveMonitoringHandler(vision_handler)
    
    result = await handler.handle_monitoring_request("show me changes")
    assert result["handled"] is True
    assert "Workspace insights available" in result["response"]


@pytest.mark.asyncio
async def test_handle_monitoring_request_invalid_command():
    handler = ProactiveMonitoringHandler(_FakeVisionHandler())
    result = await handler.handle_monitoring_request("random command")
    assert result["handled"] is False
    assert "Not a monitoring command" in result["reason"]


@pytest.mark.asyncio
async def test_enable_change_reporting_success():
    vision_handler = _FakeVisionHandler()
    vision_handler.intelligence = _FakeIntelligence()
    handler = ProactiveMonitoringHandler(vision_handler)
    
    result = await handler.enable_change_reporting()
    assert result["handled"] is True
    assert result["monitoring_active"] is True
    assert result["report_changes"] is True




@pytest.mark.asyncio
async def test_get_workspace_insights_success():
    vision_handler = _FakeVisionHandler()
    vision_handler.intelligence = _FakeIntelligence()
    handler = ProactiveMonitoringHandler(vision_handler)
    
    result = await handler.get_workspace_insights()
    assert result["handled"] is True
    assert "Workspace insights available" in result["response"]


@pytest.mark.asyncio
async def test_get_workspace_insights_no_intelligence():
    vision_handler = _FakeVisionHandler()
    vision_handler.intelligence = None
    handler = ProactiveMonitoringHandler(vision_handler)
    
    result = await handler.get_workspace_insights()
    assert result["handled"] is True
    assert "initialize my vision systems" in result["response"]


@pytest.mark.asyncio
async def test_stop_monitoring():
    vision_handler = _FakeVisionHandler()
    vision_handler.intelligence = _FakeIntelligence()
    handler = ProactiveMonitoringHandler(vision_handler)
    
    # Start monitoring first
    await handler.enable_change_reporting()
    assert handler.monitoring_active is True
    
    # Stop monitoring
    await handler.stop_monitoring()
    assert handler.monitoring_active is False
    assert handler.report_changes is False


def test_get_monitoring_handler_singleton():
    vision_handler = _FakeVisionHandler()
    handler1 = get_monitoring_handler(vision_handler)
    handler2 = get_monitoring_handler(vision_handler)
    assert handler1 is handler2
