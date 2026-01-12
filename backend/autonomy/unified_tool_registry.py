"""
Unified Tool Registry
=====================

Centralizes tool management for autonomous agents, providing:
- Dynamic tool discovery and registration
- Goal-based tool capability matching
- Tool versioning and hot-reload support
- Computer Use Tool integration
- Multi-agent tool access control

v1.0: Initial implementation with dynamic discovery and capability matching.

Author: JARVIS AI System
"""

import asyncio
import hashlib
import importlib
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class ToolRegistryConfig:
    """Configuration for the Unified Tool Registry."""

    # Discovery settings
    auto_discover: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TOOL_AUTO_DISCOVER", "true").lower() == "true"
    )
    discovery_paths: List[str] = field(
        default_factory=lambda: os.getenv("JARVIS_TOOL_PATHS", "").split(",") if os.getenv("JARVIS_TOOL_PATHS") else []
    )

    # Hot reload settings
    hot_reload_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TOOL_HOT_RELOAD", "false").lower() == "true"
    )
    reload_check_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TOOL_RELOAD_INTERVAL", "30"))
    )

    # Access control
    require_tier2_for_system: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TIER2_SYSTEM_TOOLS", "true").lower() == "true"
    )

    # Versioning
    version_tracking_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TOOL_VERSIONING", "true").lower() == "true"
    )

    # Capability matching
    match_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TOOL_MATCH_THRESHOLD", "0.6"))
    )


# =============================================================================
# Tool Definitions
# =============================================================================


class ToolCategory(Enum):
    """Categories for tool classification."""

    SYSTEM = auto()  # OS/system control tools
    FILE = auto()  # File system operations
    BROWSER = auto()  # Web browser automation
    COMMUNICATION = auto()  # Email, messaging, etc.
    PRODUCTIVITY = auto()  # Notes, calendar, etc.
    DEVELOPMENT = auto()  # Code, git, terminal
    MEDIA = auto()  # Audio, video, images
    AI = auto()  # AI/ML tools
    SECURITY = auto()  # Authentication, encryption
    UTILITY = auto()  # General utilities
    COMPUTER_USE = auto()  # Claude Computer Use tools


class ToolTier(Enum):
    """Access tier requirements for tools."""

    TIER1 = 1  # Safe, low-authentication
    TIER2 = 2  # Requires strict VBIA
    INTERNAL = 0  # Internal tools only


@dataclass
class ToolCapability:
    """Describes what a tool can do."""

    keywords: List[str]
    actions: List[str]
    domains: List[str]
    requires_screen: bool = False
    requires_keyboard: bool = False
    requires_mouse: bool = False
    requires_network: bool = False
    requires_filesystem: bool = False


@dataclass
class ToolMetadata:
    """Metadata about a registered tool."""

    tool_id: str
    name: str
    description: str
    version: str
    category: ToolCategory
    tier: ToolTier
    capabilities: ToolCapability
    parameters: Dict[str, Any]
    returns: str
    examples: List[str] = field(default_factory=list)
    deprecated: bool = False
    registered_at: float = field(default_factory=time.time)
    last_used: Optional[float] = None
    usage_count: int = 0


@dataclass
class ToolRegistration:
    """A registered tool with its implementation."""

    metadata: ToolMetadata
    handler: Callable
    source_path: Optional[str] = None
    source_hash: Optional[str] = None


@dataclass
class ToolMatch:
    """Result of matching a goal to available tools."""

    tool_id: str
    metadata: ToolMetadata
    confidence: float
    matched_keywords: List[str]
    matched_actions: List[str]


# =============================================================================
# Unified Tool Registry
# =============================================================================


class UnifiedToolRegistry:
    """
    Centralized registry for all autonomous agent tools.

    Features:
    - Dynamic tool discovery from modules
    - Goal-based tool matching
    - Version tracking and hot-reload
    - Access tier enforcement
    - Computer Use Tool integration
    """

    def __init__(
        self,
        config: Optional[ToolRegistryConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the tool registry."""
        self.config = config or ToolRegistryConfig()
        self.logger = logger or logging.getLogger(__name__)

        # Tool storage
        self._tools: Dict[str, ToolRegistration] = {}
        self._categories: Dict[ToolCategory, Set[str]] = {cat: set() for cat in ToolCategory}
        self._tiers: Dict[ToolTier, Set[str]] = {tier: set() for tier in ToolTier}

        # Discovery tracking
        self._discovered_modules: Set[str] = set()
        self._reload_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = {
            "total_registrations": 0,
            "total_invocations": 0,
            "discovery_runs": 0,
            "hot_reloads": 0,
            "match_queries": 0,
        }

        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the tool registry."""
        if self._initialized:
            return True

        try:
            self.logger.info("[ToolRegistry] Initializing Unified Tool Registry...")

            # Register built-in tools
            await self._register_builtin_tools()

            # Auto-discover tools if enabled
            if self.config.auto_discover:
                await self.discover_tools()

            # Start hot-reload watcher if enabled
            if self.config.hot_reload_enabled:
                self._reload_task = asyncio.create_task(self._hot_reload_watcher())

            self._initialized = True
            self.logger.info(
                f"[ToolRegistry] ✓ Initialized with {len(self._tools)} tools"
            )
            return True

        except Exception as e:
            self.logger.error(f"[ToolRegistry] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the tool registry."""
        if not self._initialized:
            return

        self.logger.info("[ToolRegistry] Shutting down...")

        # Stop hot-reload watcher
        if self._reload_task:
            self._reload_task.cancel()
            try:
                await self._reload_task
            except asyncio.CancelledError:
                pass

        self._tools.clear()
        self._initialized = False
        self.logger.info("[ToolRegistry] ✓ Shutdown complete")

    # =========================================================================
    # Tool Registration
    # =========================================================================

    async def register_tool(
        self,
        tool_id: str,
        name: str,
        description: str,
        handler: Callable,
        category: ToolCategory = ToolCategory.UTILITY,
        tier: ToolTier = ToolTier.TIER1,
        capabilities: Optional[ToolCapability] = None,
        version: str = "1.0.0",
        parameters: Optional[Dict[str, Any]] = None,
        returns: str = "Any",
        examples: Optional[List[str]] = None,
        source_path: Optional[str] = None,
    ) -> bool:
        """Register a new tool."""
        async with self._lock:
            if tool_id in self._tools:
                self.logger.warning(f"[ToolRegistry] Tool {tool_id} already registered")
                return False

            # Create metadata
            metadata = ToolMetadata(
                tool_id=tool_id,
                name=name,
                description=description,
                version=version,
                category=category,
                tier=tier,
                capabilities=capabilities or ToolCapability(
                    keywords=[],
                    actions=[],
                    domains=[],
                ),
                parameters=parameters or {},
                returns=returns,
                examples=examples or [],
            )

            # Calculate source hash for hot-reload
            source_hash = None
            if source_path and os.path.exists(source_path):
                with open(source_path, "rb") as f:
                    source_hash = hashlib.md5(f.read()).hexdigest()

            # Create registration
            registration = ToolRegistration(
                metadata=metadata,
                handler=handler,
                source_path=source_path,
                source_hash=source_hash,
            )

            # Store
            self._tools[tool_id] = registration
            self._categories[category].add(tool_id)
            self._tiers[tier].add(tool_id)
            self._stats["total_registrations"] += 1

            self.logger.debug(f"[ToolRegistry] Registered tool: {name} ({tool_id})")

            return True

    async def unregister_tool(self, tool_id: str) -> bool:
        """Unregister a tool."""
        async with self._lock:
            if tool_id not in self._tools:
                return False

            registration = self._tools[tool_id]
            category = registration.metadata.category
            tier = registration.metadata.tier

            self._categories[category].discard(tool_id)
            self._tiers[tier].discard(tool_id)
            del self._tools[tool_id]

            self.logger.debug(f"[ToolRegistry] Unregistered tool: {tool_id}")

            return True

    # =========================================================================
    # Built-in Tools
    # =========================================================================

    async def _register_builtin_tools(self) -> None:
        """Register built-in Computer Use and system tools."""

        # Screenshot tool
        await self.register_tool(
            tool_id="computer_use.screenshot",
            name="Screenshot",
            description="Capture a screenshot of the current screen",
            handler=self._screenshot_handler,
            category=ToolCategory.COMPUTER_USE,
            tier=ToolTier.TIER2,
            capabilities=ToolCapability(
                keywords=["screenshot", "capture", "screen", "image"],
                actions=["capture", "screenshot", "view"],
                domains=["screen", "display", "vision"],
                requires_screen=True,
            ),
        )

        # Click tool
        await self.register_tool(
            tool_id="computer_use.click",
            name="Mouse Click",
            description="Click at a specific screen location",
            handler=self._click_handler,
            category=ToolCategory.COMPUTER_USE,
            tier=ToolTier.TIER2,
            capabilities=ToolCapability(
                keywords=["click", "press", "tap", "select"],
                actions=["click", "press", "select"],
                domains=["mouse", "input", "interaction"],
                requires_mouse=True,
            ),
            parameters={
                "x": "int - X coordinate",
                "y": "int - Y coordinate",
                "button": "str - Mouse button (left, right, middle)",
            },
        )

        # Type tool
        await self.register_tool(
            tool_id="computer_use.type",
            name="Keyboard Type",
            description="Type text using the keyboard",
            handler=self._type_handler,
            category=ToolCategory.COMPUTER_USE,
            tier=ToolTier.TIER2,
            capabilities=ToolCapability(
                keywords=["type", "write", "enter", "input", "text"],
                actions=["type", "write", "input"],
                domains=["keyboard", "input", "text"],
                requires_keyboard=True,
            ),
            parameters={
                "text": "str - Text to type",
            },
        )

        # Key press tool
        await self.register_tool(
            tool_id="computer_use.key",
            name="Key Press",
            description="Press a keyboard key or combination",
            handler=self._key_handler,
            category=ToolCategory.COMPUTER_USE,
            tier=ToolTier.TIER2,
            capabilities=ToolCapability(
                keywords=["key", "hotkey", "shortcut", "press"],
                actions=["press", "key", "shortcut"],
                domains=["keyboard", "input", "control"],
                requires_keyboard=True,
            ),
            parameters={
                "key": "str - Key to press (e.g., 'enter', 'escape', 'cmd+c')",
            },
        )

        # Scroll tool
        await self.register_tool(
            tool_id="computer_use.scroll",
            name="Mouse Scroll",
            description="Scroll the mouse wheel",
            handler=self._scroll_handler,
            category=ToolCategory.COMPUTER_USE,
            tier=ToolTier.TIER2,
            capabilities=ToolCapability(
                keywords=["scroll", "wheel", "up", "down"],
                actions=["scroll"],
                domains=["mouse", "input", "navigation"],
                requires_mouse=True,
            ),
            parameters={
                "direction": "str - Scroll direction (up, down)",
                "amount": "int - Scroll amount",
            },
        )

        self.logger.info("[ToolRegistry] Registered 5 built-in Computer Use tools")

    # Placeholder handlers - these will be wired to actual implementations
    async def _screenshot_handler(self, **kwargs) -> Any:
        """Screenshot handler placeholder."""
        try:
            from autonomy.computer_use_tool import ComputerUseTool
            tool = ComputerUseTool()
            return await tool.take_screenshot()
        except ImportError:
            return {"error": "Computer Use Tool not available"}

    async def _click_handler(self, x: int, y: int, button: str = "left", **kwargs) -> Any:
        """Click handler placeholder."""
        try:
            from autonomy.computer_use_tool import ComputerUseTool
            tool = ComputerUseTool()
            return await tool.click(x, y, button)
        except ImportError:
            return {"error": "Computer Use Tool not available"}

    async def _type_handler(self, text: str, **kwargs) -> Any:
        """Type handler placeholder."""
        try:
            from autonomy.computer_use_tool import ComputerUseTool
            tool = ComputerUseTool()
            return await tool.type_text(text)
        except ImportError:
            return {"error": "Computer Use Tool not available"}

    async def _key_handler(self, key: str, **kwargs) -> Any:
        """Key press handler placeholder."""
        try:
            from autonomy.computer_use_tool import ComputerUseTool
            tool = ComputerUseTool()
            return await tool.press_key(key)
        except ImportError:
            return {"error": "Computer Use Tool not available"}

    async def _scroll_handler(self, direction: str = "down", amount: int = 3, **kwargs) -> Any:
        """Scroll handler placeholder."""
        try:
            from autonomy.computer_use_tool import ComputerUseTool
            tool = ComputerUseTool()
            return await tool.scroll(direction, amount)
        except ImportError:
            return {"error": "Computer Use Tool not available"}

    # =========================================================================
    # Tool Discovery
    # =========================================================================

    async def discover_tools(self) -> int:
        """Discover tools from configured paths."""
        self._stats["discovery_runs"] += 1
        discovered = 0

        # Default discovery paths
        paths = self.config.discovery_paths or []

        # Add standard tool locations
        base_path = os.path.dirname(os.path.dirname(__file__))
        default_paths = [
            os.path.join(base_path, "tools"),
            os.path.join(base_path, "autonomy", "tools"),
        ]

        for path in default_paths:
            if os.path.exists(path) and path not in paths:
                paths.append(path)

        for path in paths:
            if not os.path.exists(path):
                continue

            count = await self._discover_from_path(path)
            discovered += count

        self.logger.info(f"[ToolRegistry] Discovered {discovered} tools from {len(paths)} paths")

        return discovered

    async def _discover_from_path(self, path: str) -> int:
        """Discover tools from a specific path."""
        discovered = 0

        if not os.path.isdir(path):
            return 0

        for filename in os.listdir(path):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue

            module_path = os.path.join(path, filename)
            module_name = filename[:-3]

            if module_path in self._discovered_modules:
                continue

            try:
                # Import the module
                spec = importlib.util.spec_from_file_location(module_name, module_path)
                if not spec or not spec.loader:
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Look for tool definitions
                for name, obj in inspect.getmembers(module):
                    if hasattr(obj, "_jarvis_tool"):
                        tool_def = obj._jarvis_tool
                        await self.register_tool(
                            tool_id=tool_def.get("id", f"{module_name}.{name}"),
                            name=tool_def.get("name", name),
                            description=tool_def.get("description", ""),
                            handler=obj,
                            category=tool_def.get("category", ToolCategory.UTILITY),
                            tier=tool_def.get("tier", ToolTier.TIER1),
                            source_path=module_path,
                        )
                        discovered += 1

                self._discovered_modules.add(module_path)

            except Exception as e:
                self.logger.debug(f"[ToolRegistry] Failed to load {module_path}: {e}")

        return discovered

    # =========================================================================
    # Hot Reload
    # =========================================================================

    async def _hot_reload_watcher(self) -> None:
        """Watch for tool file changes and reload."""
        iteration_timeout = float(os.getenv("TIMEOUT_TOOL_RELOAD_CHECK", "30.0"))
        while True:
            try:
                await asyncio.sleep(self.config.reload_check_interval)

                reloaded = 0
                for tool_id, registration in list(self._tools.items()):
                    if not registration.source_path:
                        continue

                    if not os.path.exists(registration.source_path):
                        continue

                    # Check if file changed
                    with open(registration.source_path, "rb") as f:
                        current_hash = hashlib.md5(f.read()).hexdigest()

                    if current_hash != registration.source_hash:
                        self.logger.info(f"[ToolRegistry] Reloading changed tool: {tool_id}")

                        # Unregister and re-discover with timeout
                        await asyncio.wait_for(
                            self.unregister_tool(tool_id),
                            timeout=iteration_timeout
                        )
                        await asyncio.wait_for(
                            self._discover_from_path(
                                os.path.dirname(registration.source_path)
                            ),
                            timeout=iteration_timeout
                        )
                        reloaded += 1
                        self._stats["hot_reloads"] += 1

                if reloaded > 0:
                    self.logger.info(f"[ToolRegistry] Hot-reloaded {reloaded} tools")

            except asyncio.TimeoutError:
                self.logger.warning("[ToolRegistry] Hot-reload iteration timed out")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[ToolRegistry] Hot-reload error: {e}")

    # =========================================================================
    # Tool Access
    # =========================================================================

    def get_tool(self, tool_id: str) -> Optional[ToolRegistration]:
        """Get a tool by ID."""
        return self._tools.get(tool_id)

    def get_tools_by_category(self, category: ToolCategory) -> List[ToolRegistration]:
        """Get all tools in a category."""
        tool_ids = self._categories.get(category, set())
        return [self._tools[tid] for tid in tool_ids if tid in self._tools]

    def get_tools_by_tier(self, tier: ToolTier) -> List[ToolRegistration]:
        """Get all tools requiring a specific tier."""
        tool_ids = self._tiers.get(tier, set())
        return [self._tools[tid] for tid in tool_ids if tid in self._tools]

    def list_tools(self) -> List[ToolMetadata]:
        """List all registered tool metadata."""
        return [reg.metadata for reg in self._tools.values()]

    # =========================================================================
    # Goal-based Matching
    # =========================================================================

    async def match_tools_for_goal(
        self,
        goal: str,
        max_results: int = 5,
        allowed_tiers: Optional[List[ToolTier]] = None,
    ) -> List[ToolMatch]:
        """
        Find tools that match a given goal description.

        Uses keyword and action matching to find relevant tools.

        Args:
            goal: Natural language goal description
            max_results: Maximum number of matches to return
            allowed_tiers: List of allowed access tiers

        Returns:
            List of ToolMatch results sorted by confidence
        """
        self._stats["match_queries"] += 1

        goal_lower = goal.lower()
        goal_words = set(goal_lower.split())

        matches: List[ToolMatch] = []

        for tool_id, registration in self._tools.items():
            meta = registration.metadata
            caps = meta.capabilities

            # Filter by tier if specified
            if allowed_tiers and meta.tier not in allowed_tiers:
                continue

            # Calculate keyword matches
            matched_keywords = [
                kw for kw in caps.keywords
                if kw.lower() in goal_lower
            ]

            # Calculate action matches
            matched_actions = [
                act for act in caps.actions
                if act.lower() in goal_lower
            ]

            # Calculate domain relevance
            domain_score = sum(
                0.1 for domain in caps.domains
                if domain.lower() in goal_lower
            )

            # Calculate word overlap with description
            desc_words = set(meta.description.lower().split())
            word_overlap = len(goal_words & desc_words) / max(len(goal_words), 1)

            # Combined confidence
            confidence = (
                0.4 * min(len(matched_keywords) / max(len(caps.keywords), 1), 1.0)
                + 0.3 * min(len(matched_actions) / max(len(caps.actions), 1), 1.0)
                + 0.2 * domain_score
                + 0.1 * word_overlap
            )

            if confidence >= self.config.match_threshold:
                matches.append(ToolMatch(
                    tool_id=tool_id,
                    metadata=meta,
                    confidence=confidence,
                    matched_keywords=matched_keywords,
                    matched_actions=matched_actions,
                ))

        # Sort by confidence
        matches.sort(key=lambda m: m.confidence, reverse=True)

        return matches[:max_results]

    # =========================================================================
    # Tool Invocation
    # =========================================================================

    async def invoke_tool(
        self,
        tool_id: str,
        tier_level: ToolTier = ToolTier.TIER1,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """
        Invoke a tool with tier validation.

        Args:
            tool_id: ID of the tool to invoke
            tier_level: Current authentication tier level
            **kwargs: Arguments to pass to the tool

        Returns:
            Tuple of (success, result_or_error)
        """
        registration = self._tools.get(tool_id)
        if not registration:
            return False, f"Tool not found: {tool_id}"

        # Check tier access
        if registration.metadata.tier.value > tier_level.value:
            return False, f"Tool requires {registration.metadata.tier.name} access"

        try:
            # Update usage stats
            registration.metadata.last_used = time.time()
            registration.metadata.usage_count += 1
            self._stats["total_invocations"] += 1

            # Invoke the handler
            result = await registration.handler(**kwargs)
            return True, result

        except Exception as e:
            self.logger.error(f"[ToolRegistry] Tool {tool_id} invocation error: {e}")
            return False, str(e)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get registry statistics."""
        return {
            **self._stats,
            "registered_tools": len(self._tools),
            "categories": {cat.name: len(ids) for cat, ids in self._categories.items()},
            "tiers": {tier.name: len(ids) for tier, ids in self._tiers.items()},
        }

    @property
    def is_ready(self) -> bool:
        """Check if registry is ready."""
        return self._initialized


# =============================================================================
# Module-level Singleton Access
# =============================================================================

_registry_instance: Optional[UnifiedToolRegistry] = None


def get_tool_registry() -> Optional[UnifiedToolRegistry]:
    """Get the global tool registry instance."""
    return _registry_instance


def set_tool_registry(registry: UnifiedToolRegistry) -> None:
    """Set the global tool registry instance."""
    global _registry_instance
    _registry_instance = registry


async def start_tool_registry(
    config: Optional[ToolRegistryConfig] = None,
) -> UnifiedToolRegistry:
    """Start and initialize a new tool registry."""
    global _registry_instance

    if _registry_instance is not None:
        return _registry_instance

    registry = UnifiedToolRegistry(config=config)
    await registry.initialize()
    _registry_instance = registry

    return registry


async def stop_tool_registry() -> None:
    """Stop the global tool registry."""
    global _registry_instance

    if _registry_instance is not None:
        await _registry_instance.shutdown()
        _registry_instance = None


# =============================================================================
# Decorator for Tool Registration
# =============================================================================


def jarvis_tool(
    tool_id: Optional[str] = None,
    name: Optional[str] = None,
    description: str = "",
    category: ToolCategory = ToolCategory.UTILITY,
    tier: ToolTier = ToolTier.TIER1,
    keywords: Optional[List[str]] = None,
    actions: Optional[List[str]] = None,
):
    """
    Decorator to mark a function as a JARVIS tool.

    Usage:
        @jarvis_tool(
            tool_id="my.tool",
            name="My Tool",
            description="Does something useful",
            category=ToolCategory.UTILITY,
            keywords=["useful", "tool"],
        )
        async def my_tool_function(**kwargs):
            return "result"
    """
    def decorator(func):
        func._jarvis_tool = {
            "id": tool_id or f"custom.{func.__name__}",
            "name": name or func.__name__,
            "description": description or func.__doc__ or "",
            "category": category,
            "tier": tier,
            "keywords": keywords or [],
            "actions": actions or [],
        }
        return func
    return decorator
