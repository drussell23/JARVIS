# [Ouroboros] Modified by Ouroboros (op=op-01a10af5-) at 2026-10-05 22:46 UTC
# Reason: `backend/autonomy/unified_tool_registry.py` has no corresponding test module. CREATE `tests/test_unified_tool_registry.p

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Import the module under test
from backend.autonomy.unified_tool_registry import (
    ToolCategory,
    ToolTier,
    ToolCapability,
    ToolMetadata,
    ToolRegistration,
    ToolMatch,
    UnifiedToolRegistry,
    ToolRegistryConfig,
    get_tool_registry,
    set_tool_registry,
    start_tool_registry,
    stop_tool_registry,
    jarvis_tool,
)


@pytest.fixture
async def registry():
    """Return a fresh initialized UnifiedToolRegistry instance."""
    reg = UnifiedToolRegistry()
    await reg.initialize()
    return reg


@pytest.fixture
async def mock_registry():
    """Return a registry with mocked dependencies."""
    reg = UnifiedToolRegistry()
    # Mock the logger to avoid output during tests
    with patch.object(reg, 'logger', MagicMock()):
        await reg.initialize()
    return reg


class TestUnifiedToolRegistry:
    """Tests for UnifiedToolRegistry core functionality."""

    def test_initialization(self, mock_registry):
        """Test registry initialization and default state."""
        assert mock_registry._initialized is True
        assert len(mock_registry._tools) >= 5  # Built-in tools
        assert mock_registry._stats['total_registrations'] >= 5

    @pytest.mark.asyncio
    async def test_register_tool(self, mock_registry):
        """Test tool registration."""
        mock_handler = AsyncMock()
        result = await mock_registry.register_tool(
            tool_id="test.tool",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
            category=ToolCategory.UTILITY,
            tier=ToolTier.TIER1,
            capabilities=ToolCapability(
                keywords=["test"],
                actions=["test"],
                domains=["testing"]
            )
        )
        assert result is True
        assert "test.tool" in mock_registry._tools

    @pytest.mark.asyncio
    async def test_register_duplicate_tool(self, mock_registry):
        """Test registering a tool with existing ID."""
        mock_handler = AsyncMock()
        # Register first time
        await mock_registry.register_tool(
            tool_id="duplicate.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
        )
        # Try to register again
        result = await mock_registry.register_tool(
            tool_id="duplicate.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_unregister_tool(self, mock_registry):
        """Test tool unregistration."""
        mock_handler = AsyncMock()
        await mock_registry.register_tool(
            tool_id="unregister.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
        )
        result = await mock_registry.unregister_tool("unregister.test")
        assert result is True
        assert "unregister.test" not in mock_registry._tools

    @pytest.mark.asyncio
    async def test_get_tool(self, mock_registry):
        """Test getting a registered tool."""
        mock_handler = AsyncMock()
        await mock_registry.register_tool(
            tool_id="get.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
        )
        tool = mock_registry.get_tool("get.test")
        assert tool is not None
        assert tool.metadata.tool_id == "get.test"

    @pytest.mark.asyncio
    async def test_get_nonexistent_tool(self, mock_registry):
        """Test getting a non-existent tool."""
        tool = mock_registry.get_tool("nonexistent")
        assert tool is None

    @pytest.mark.asyncio
    async def test_get_tools_by_category(self, mock_registry):
        """Test getting tools by category."""
        mock_handler = AsyncMock()
        await mock_registry.register_tool(
            tool_id="category.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
            category=ToolCategory.UTILITY,
        )
        tools = mock_registry.get_tools_by_category(ToolCategory.UTILITY)
        assert len(tools) >= 1

    @pytest.mark.asyncio
    async def test_get_tools_by_tier(self, mock_registry):
        """Test getting tools by tier."""
        mock_handler = AsyncMock()
        await mock_registry.register_tool(
            tool_id="tier.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
            tier=ToolTier.TIER1,
        )
        tools = mock_registry.get_tools_by_tier(ToolTier.TIER1)
        assert len(tools) >= 1

    @pytest.mark.asyncio
    async def test_list_tools(self, mock_registry):
        """Test listing all registered tools."""
        tools = mock_registry.list_tools()
        assert isinstance(tools, list)
        assert len(tools) >= 5  # Built-in tools

    @pytest.mark.asyncio
    async def test_invoke_tool_success(self, mock_registry):
        """Test successful tool invocation."""
        mock_handler = AsyncMock(return_value="success")
        await mock_registry.register_tool(
            tool_id="invoke.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
            tier=ToolTier.TIER1,
        )
        success, result = await mock_registry.invoke_tool("invoke.test")
        assert success is True
        assert result == "success"

    @pytest.mark.asyncio
    async def test_invoke_tool_not_found(self, mock_registry):
        """Test invoking a non-existent tool."""
        success, result = await mock_registry.invoke_tool("nonexistent")
        assert success is False
        assert "Tool not found" in result

    @pytest.mark.asyncio
    async def test_invoke_tool_insufficient_tier(self, mock_registry):
        """Test invoking a tool with insufficient access tier."""
        mock_handler = AsyncMock()
        await mock_registry.register_tool(
            tool_id="tier.test",
            name="Test Tool",
            description="A test tool",
            handler=mock_handler,
            tier=ToolTier.TIER2,
        )
        success, result = await mock_registry.invoke_tool("tier.test", tier_level=ToolTier.TIER1)
        assert success is False
        assert "requires TIER2 access" in result

    @pytest.mark.asyncio
    async def test_match_tools_for_goal(self, mock_registry):
        """Test tool matching for a goal."""
        mock_handler = AsyncMock()
        await mock_registry.register_tool(
            tool_id="match.test",
            name="Test Tool",
            description="A test tool that matches goals",
            handler=mock_handler,
            capabilities=ToolCapability(
                keywords=["test", "goal"],
                actions=["match"],
                domains=["testing"]
            )
        )
        matches = await mock_registry.match_tools_for_goal("test goal matching")
        assert isinstance(matches, list)

    @pytest.mark.asyncio
    async def test_get_stats(self, mock_registry):
        """Test getting registry statistics."""
        stats = mock_registry.get_stats()
        assert isinstance(stats, dict)
        assert "total_registrations" in stats

    def test_is_ready(self, mock_registry):
        """Test checking if registry is ready."""
        assert mock_registry.is_ready is True  # is_ready is a property, not a method

    @pytest.mark.asyncio
    async def test_initialize_twice(self, mock_registry):
        """Test initializing an already initialized registry."""
        result = await mock_registry.initialize()
        assert result is True

    @pytest.mark.asyncio
    async def test_shutdown(self, mock_registry):
        """Test shutting down the registry."""
        await mock_registry.shutdown()
        assert mock_registry._initialized is False


class TestToolRegistryConfig:
    """Tests for ToolRegistryConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = ToolRegistryConfig()
        assert config.auto_discover is True
        assert config.hot_reload_enabled is False
        assert config.match_threshold == 0.6


class TestJarvisToolDecorator:
    """Tests for the jarvis_tool decorator."""

    def test_jarvis_tool_decorator(self):
        """Test the jarvis_tool decorator creates proper metadata."""
        @jarvis_tool(
            tool_id="decorator.test",
            name="Decorator Test",
            description="A test for decorator",
            category=ToolCategory.UTILITY,
            keywords=["test", "decorator"],
            actions=["test"]
        )
        async def test_function():
            return "result"

        assert hasattr(test_function, '_jarvis_tool')
        tool_def = test_function._jarvis_tool
        assert tool_def["id"] == "decorator.test"  # The key is 'id', not 'tool_id'
        assert tool_def["name"] == "Decorator Test"


class TestGlobalFunctions:
    """Tests for global functions."""

    def test_get_tool_registry(self):
        """Test get_tool_registry function."""
        registry = get_tool_registry()
        assert registry is None  # Should be None before initialization

    @pytest.mark.asyncio
    async def test_set_tool_registry(self):
        """Test set_tool_registry function."""
        mock_reg = MagicMock()
        set_tool_registry(mock_reg)
        # Verify that the global registry was actually set by getting it back
        retrieved_reg = get_tool_registry()
        assert retrieved_reg is mock_reg

    @pytest.mark.asyncio
    async def test_start_stop_tool_registry(self):
        """Test start and stop tool registry functions."""
        # Test start - patch the global _registry_instance to avoid conflicts
        with patch('backend.autonomy.unified_tool_registry._registry_instance', None):
            registry = await start_tool_registry()
            assert isinstance(registry, UnifiedToolRegistry)
            assert registry._initialized is True
            
            # Test stop
            await stop_tool_registry()
            # Verify that the global registry was cleared
            assert get_tool_registry() is None
