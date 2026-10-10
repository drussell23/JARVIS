# [Ouroboros] Modified by Ouroboros (op=op-01a121a6-) at 2026-10-09 17:14 UTC
# Reason: `backend/api/unified_vision_handler.py` has no corresponding test module. CREATE `tests/test_unified_vision_handler.py` 

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from backend.api.unified_vision_handler import (
    VisionContext,
    MessageRouter,
    UnifiedVisionHandler,
    handle_monitoring_interval,
    handle_workspace_analysis,
    handle_action_execution,
    handle_vision_command,
    handle_claude_vision,
    handle_autonomous_action,
    handle_pattern_learning
)
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime
import json


class TestUnifiedVisionHandler:
    def test_init(self):
        handler = UnifiedVisionHandler()
        assert isinstance(handler, UnifiedVisionHandler)
        assert hasattr(handler, 'router')
        assert hasattr(handler, 'contexts')
        assert hasattr(handler, 'monitoring_tasks')

    @pytest.mark.asyncio
    async def test_handle_monitoring_interval(self):
        handler = UnifiedVisionHandler()
        context = VisionContext(
            client_id="test_client",
            capabilities=[],
            metadata={}
        )
        
        # Test normal interval update
        message = {"type": "set_monitoring_interval", "interval": 3.0}
        result = await handler.handle_monitoring_interval(message, context)
        assert result["type"] == "config_updated"
        assert result["monitoring_interval"] == 3.0
        
        # Test interval clamping
        message = {"type": "set_monitoring_interval", "interval": 15.0}
        result = await handler.handle_monitoring_interval(message, context)
        assert result["monitoring_interval"] == 10.0  # Max limit
        
        message = {"type": "set_monitoring_interval", "interval": 0.1}
        result = await handler.handle_monitoring_interval(message, context)
        assert result["monitoring_interval"] == 0.5  # Min limit

    @pytest.mark.asyncio
    async def test_handle_autonomous_mode(self):
        handler = UnifiedVisionHandler()
        context = VisionContext(
            client_id="test_client",
            capabilities=[],
            metadata={}
        )
        
        # Test enabling autonomous mode
        message = {"type": "set_autonomous_mode", "enabled": True}
        result = await handler.handle_autonomous_mode(message, context)
        assert result["type"] == "config_updated"
        assert result["autonomous_mode"] is True
        
        # Test disabling autonomous mode
        message = {"type": "set_autonomous_mode", "enabled": False}
        result = await handler.handle_autonomous_mode(message, context)
        assert result["type"] == "config_updated"
        assert result["autonomous_mode"] is False


    @pytest.mark.asyncio
    async def test_handle_workspace_analysis_error(self):
        handler = UnifiedVisionHandler()
        
        # Mock the vision system to raise an exception
        with patch.object(handler, 'vision_system') as mock_vision:
            mock_vision.capture_screenshot = AsyncMock(side_effect=Exception("Test error"))
            
            context = VisionContext(
                client_id="test_client",
                capabilities=[],
                metadata={}
            )
            
            message = {"type": "request_workspace_analysis"}
            result = await handler.handle_workspace_analysis(message, context)
            
            assert result["type"] == "error"
            assert "Failed to analyze workspace" in result["message"]

    @pytest.mark.asyncio
    async def test_handle_vision_command_success(self):
        handler = UnifiedVisionHandler()
        
        # Mock the vision system
        with patch.object(handler, 'vision_system') as mock_vision:
            mock_vision.process_command = AsyncMock(return_value=MagicMock(
                intent_type="test_intent",
                confidence=0.9,
                message="test_message"
            ))
            
            context = VisionContext(
                client_id="test_client",
                capabilities=[],
                metadata={}
            )
            
            message = {"type": "vision_command", "command": "test command"}
            result = await handler.handle_vision_command(message, context)
            
            assert result["type"] == "vision_result"
            assert result["command"] == "test command"

    @pytest.mark.asyncio
    async def test_handle_vision_command_fallback(self):
        handler = UnifiedVisionHandler()
        
        # Mock the vision system to be None (fallback scenario)
        with patch.object(handler, 'vision_system', None), \
             patch('vision.screen_capture_fallback.capture_with_intelligence') as mock_capture:
            
            mock_capture.return_value = {
                "success": True,
                "analysis": "fallback analysis"
            }
            
            context = VisionContext(
                client_id="test_client",
                capabilities=[],
                metadata={}
            )
            
            message = {"type": "vision_command", "command": "test command"}
            result = await handler.handle_vision_command(message, context)
            
            assert result["type"] == "vision_result"
            assert result["fallback_used"] is True


    @pytest.mark.asyncio
    async def test_handle_claude_vision_success(self):
        handler = UnifiedVisionHandler()
        
        # Mock the claude_ai and vision system
        with patch.object(handler, 'claude_ai') as mock_claude, \
             patch.object(handler, 'vision_system') as mock_vision:
            
            mock_claude.is_available = MagicMock(return_value=True)
            mock_vision.capture_screenshot = AsyncMock(return_value="screenshot_data")
            mock_claude.analyze_with_query = AsyncMock(return_value="claude_analysis")
            
            context = VisionContext(
                client_id="test_client",
                capabilities=[],
                metadata={}
            )
            
            message = {"type": "claude_vision", "query": "test query"}
            result = await handler.handle_claude_vision(message, context)
            
            assert result["type"] == "claude_vision_result"

    @pytest.mark.asyncio
    async def test_handle_pattern_learning(self):
        handler = UnifiedVisionHandler()
        
        # Mock the pattern learner
        with patch.object(handler, 'pattern_learner') as mock_learner:
            mock_learner.learn_pattern = AsyncMock(return_value="learned_pattern")
            
            context = VisionContext(
                client_id="test_client",
                capabilities=[],
                metadata={}
            )
            
            message = {"type": "learn_pattern", "pattern": {"key": "value"}}
            result = await handler.handle_pattern_learning(message, context)
            
            assert result["type"] == "pattern_learned"


    def test_create_context(self):
        handler = UnifiedVisionHandler()
        context = handler.create_context("test_client", [], {})
        assert isinstance(context, VisionContext)
        assert context.client_id == "test_client"

    def test_remove_context(self):
        handler = UnifiedVisionHandler()
        handler.contexts["test_client"] = VisionContext(
            client_id="test_client",
            capabilities=[],
            metadata={}
        )
        
        handler.remove_context("test_client")
        assert "test_client" not in handler.contexts

    @pytest.mark.asyncio
    async def test_message_router(self):
        router = MessageRouter()
        
        # Test registration
        async def dummy_handler(message, context):
            return {"type": "dummy_result"}
        
        router.register("test_type", dummy_handler)
        assert "test_type" in router.handlers
        
        # Test routing
        context = VisionContext(
            client_id="test_client",
            capabilities=[],
            metadata={}
        )
        
        result = await router.route({"type": "test_type"}, context)
        assert result["type"] == "dummy_result"

    @pytest.mark.asyncio
    async def test_message_router_fallback(self):
        router = MessageRouter()
        
        # Test fallback handler
        async def fallback_handler(message, context):
            return {"type": "fallback_result"}
        
        router.set_fallback(fallback_handler)
        context = VisionContext(
            client_id="test_client",
            capabilities=[],
            metadata={}
        )
        
        result = await router.route({"type": "unknown_type"}, context)
        assert result["type"] == "fallback_result"

    @pytest.mark.asyncio
    async def test_message_router_middleware(self):
        router = MessageRouter()
        
        # Test middleware
        async def test_middleware(message, context):
            message["middleware_applied"] = True
            return message
        
        router.use(test_middleware)
        
        async def dummy_handler(message, context):
            return {"type": "dummy_result", "middleware_applied": message.get("middleware_applied", False)}
        
        router.register("test_type", dummy_handler)
        
        context = VisionContext(
            client_id="test_client",
            capabilities=[],
            metadata={}
        )
        
        result = await router.route({"type": "test_type"}, context)
        assert result["middleware_applied"] is True
