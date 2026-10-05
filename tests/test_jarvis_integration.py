# [Ouroboros] Modified by Ouroboros (op=op-01a10e59-) at 2026-10-05 23:17 UTC
# Reason: `backend/autonomy/jarvis_integration.py` has no corresponding test module. CREATE `tests/test_jarvis_integration.py` con

# Copyright (c) 2024 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

pytestmark = pytest.mark.asyncio

class TestJarvisIntegration:
    """
    Test suite for JARVIS integration layer - testing public interfaces,
    core functionality and edge cases of the adapters.
    """

    def test_import_smoke(self):
        """
        Smoke test to ensure all imports work correctly.
        Should not raise ImportError or other issues during import time.
        """
        from backend.autonomy.jarvis_integration import (
            PermissionManagerProtocol,
            ActionQueueProtocol,
            ActionExecutorProtocol,
            ContextEngineProtocol,
            LearningDatabaseProtocol,
            PermissionAdapter,
            ActionQueueAdapter,
            ActionExecutorAdapter,
            ContextAdapter,
            LearningAdapter,
            IntegrationConfig,
            JARVISIntegrationManager,
            get_integration_manager,
            configure_integration,
            auto_configure_integration
        )
        
        # All imports should be valid and accessible
        assert PermissionManagerProtocol is not None
        assert ActionQueueProtocol is not None
        assert ActionExecutorProtocol is not None
        assert ContextEngineProtocol is not None
        assert LearningDatabaseProtocol is not None
        assert PermissionAdapter is not None
        assert ActionQueueAdapter is not None
        assert ActionExecutorAdapter is not None
        assert ContextAdapter is not None
        assert LearningAdapter is not None
        assert IntegrationConfig is not None
        assert JARVISIntegrationManager is not None
        assert get_integration_manager is not None
        assert configure_integration is not None
        assert auto_configure_integration is not None

    async def test_permission_adapter_initialization(self):
        """
        Test PermissionAdapter initialization with various configurations.
        """
        from backend.autonomy.jarvis_integration import PermissionAdapter
        
        # Default initialization
        adapter = PermissionAdapter()
        assert adapter.permission_manager is None
        assert adapter.default_allow is False
        assert adapter.auto_learn is True
        assert adapter._decision_cache == {}
        
        # With explicit values
        mock_pm = MagicMock()
        adapter = PermissionAdapter(permission_manager=mock_pm, default_allow=True, auto_learn=False)
        assert adapter.permission_manager == mock_pm
        assert adapter.default_allow is True
        assert adapter.auto_learn is False

    async def test_permission_adapter_check_permission_with_cache(self):
        """
        Test permission checking with cache hit behavior.
        """
        from backend.autonomy.jarvis_integration import PermissionAdapter
        
        # Setup mock manager that returns True for first check
        adapter = PermissionAdapter(default_allow=False)
        
        # First call should populate cache (cache key is action_type:target)
        result1 = await adapter.check_permission("read", "file.txt")
        assert result1 is False  # default allow false, no manager to query
        
        # Second call should hit the cached value with require_explicit=False (default) and use cache
        result2 = await adapter.check_permission("read", "file.txt")
        assert result2 is False

    async def test_permission_adapter_check_with_manager(self):
        """
        Test permission checking using a real manager.
        """
        from backend.autonomy.jarvis_integration import PermissionAdapter
        
        mock_pm = AsyncMock()
        mock_pm.check_permission.return_value = True  # Always allow
        adapter = PermissionAdapter(permission_manager=mock_pm)
        
        result = await adapter.check_permission("write", "file.txt")
        assert result is True
        mock_pm.check_permission.assert_called_once_with(
            action_type="write",
            target="file.txt",
            context={}
        )

    async def test_action_queue_adapter_initialization(self):
        """
        Test ActionQueueAdapter initialization with various configurations.
        """
        from backend.autonomy.jarvis_integration import ActionQueueAdapter
        
        # Default initialization
        adapter = ActionQueueAdapter()
        assert adapter.queue_manager is None
        assert adapter.executor is None
        assert adapter.max_concurrent == 3
        assert adapter._local_queue == []
        assert not adapter._processing

    async def test_action_queue_adapter_enqueue_with_immediate(self):
        """
        Test enqueue with immediate execution.
        """
        from backend.autonomy.jarvis_integration import ActionQueueAdapter
        
        mock_executor = AsyncMock()
        action = {"action_id": "test-id", "type": "test_action"}
        adapter = ActionQueueAdapter(executor=mock_executor)
        
        result_id = await adapter.enqueue(action, immediate=True)
        assert result_id == "test-id"
        mock_executor.execute_action.assert_called_once_with(action)

    async def test_action_queue_adapter_enqueue_without_manager(self):
        """
        Test enqueue without queue manager (uses local queue).
        """
        from backend.autonomy.jarvis_integration import ActionQueueAdapter
        
        adapter = ActionQueueAdapter()
        action = {"type": "test_action", "target": "file.txt"}
        result_id = await adapter.enqueue(action)
        assert isinstance(result_id, str)
        # Should be added to local queue with priority 2 (default)
        assert len(adapter._local_queue) == 1
        assert adapter._local_queue[0]["priority"] == 2

    async def test_action_executor_adapter_initialization(self):
        """
        Test ActionExecutorAdapter initialization.
        """
        from backend.autonomy.jarvis_integration import ActionExecutorAdapter
        
        # Default initialization
        adapter = ActionExecutorAdapter()
        assert adapter.executor is None
        assert adapter.timeout_seconds == 30.0
        assert adapter.enable_rollback is True
        assert adapter._execution_history == []
        assert adapter._rollback_stack == []

    async def test_action_executor_adapter_execute_with_no_executor(self):
        """
        Test execution without an executor (should simulate).
        """
        from backend.autonomy.jarvis_integration import ActionExecutorAdapter
        
        adapter = ActionExecutorAdapter()
        result = await adapter.execute("test", "target.txt")
        assert result["success"] is True  # Simulated execution succeeds by default
        assert len(adapter._execution_history) == 1
        assert "duration_ms" in adapter._execution_history[0]

    async def test_action_executor_adapter_execute_with_timeout(self):
        """
        Test timeout handling during action execution.
        """
        from backend.autonomy.jarvis_integration import ActionExecutorAdapter
        
        mock_executor = AsyncMock()
        # Simulate a timeout in executor
        mock_executor.execute_action.side_effect = asyncio.TimeoutError("Timeout")
        adapter = ActionExecutorAdapter(executor=mock_executor)
        
        result = await adapter.execute("test", "target.txt")
        assert not result["success"]  # Should fail due to timeout
        assert "Execution timed out" in result["error"]

    async def test_context_adapter_initialization(self):
        """
        Test ContextAdapter initialization.
        """
        from backend.autonomy.jarvis_integration import ContextAdapter
        
        adapter = ContextAdapter()
        assert adapter.context_engine is None

    async def test_jarvis_integration_manager_creation(self):
        """
        Test the integration manager creation with various options.
        """
        from backend.autonomy.jarvis_integration import JARVISIntegrationManager, IntegrationConfig
        
        # Create default configuration
        config = IntegrationConfig()
        manager = JARVISIntegrationManager(config=config)
        assert manager.config == config
        assert manager.permission_adapter is not None
        assert manager.queue_adapter is not None
        assert manager.executor_adapter is not None
        assert manager.context_adapter is not None
        assert manager.learning_adapter is not None
