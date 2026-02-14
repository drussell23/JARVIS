"""
Dynamic LangChain Tool Registry for JARVIS

This module provides a sophisticated tool management system that:
- Auto-discovers tools from the codebase
- Dynamically wraps existing JARVIS capabilities as LangChain tools
- Supports async tool execution
- Provides tool composition and chaining
- Integrates with the permission system

Features:
- Zero hardcoding - tools are discovered and registered dynamically
- Async-first design
- Tool capability matching
- Automatic schema generation
- Permission-aware execution
- Tool versioning and hot-reload support
"""

from __future__ import annotations

import asyncio
import errno
import functools
import importlib
import inspect
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Dict, Generic, List, Literal, Optional,
    Protocol, Sequence, Set, Tuple, Type, TypeVar, Union, get_type_hints
)
from uuid import uuid4

try:
    from langchain_core.tools import BaseTool, StructuredTool, tool
    from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun
    # Fix for pydantic v2 - use pydantic directly instead of compatibility shim
    from pydantic import BaseModel, Field
    LANGCHAIN_AVAILABLE = True
except ImportError:
    try:
        from langchain.tools import BaseTool, StructuredTool, tool
        from langchain.callbacks.manager import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun
        from pydantic import BaseModel, Field
        LANGCHAIN_AVAILABLE = True
    except ImportError:
        LANGCHAIN_AVAILABLE = False
        BaseTool = object
        StructuredTool = None
        tool = lambda x: x
        BaseModel = object
        Field = lambda **kwargs: None

from pydantic import BaseModel as PydanticBaseModel, Field as PydanticField, validator

from backend.core.resilience.atomic_file_ops import get_atomic_file_ops

logger = logging.getLogger(__name__)


# ============================================================================
# Enums and Constants
# ============================================================================

class ToolCategory(str, Enum):
    """Categories for organizing tools."""
    SYSTEM = "system"
    FILE_SYSTEM = "file_system"
    NETWORK = "network"
    COMMUNICATION = "communication"
    ANALYSIS = "analysis"
    AUTOMATION = "automation"
    MONITORING = "monitoring"
    SECURITY = "security"
    UI = "ui"
    INTEGRATION = "integration"
    UTILITY = "utility"


class ToolRiskLevel(str, Enum):
    """Risk levels for tools."""
    SAFE = "safe"           # Read-only, no side effects
    LOW = "low"             # Minor side effects, easily reversible
    MEDIUM = "medium"       # Noticeable side effects, reversible
    HIGH = "high"           # Significant side effects, hard to reverse
    CRITICAL = "critical"   # System-altering, may be irreversible


class ToolExecutionMode(str, Enum):
    """Execution modes for tools."""
    SYNC = "sync"
    ASYNC = "async"
    BOTH = "both"


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ToolMetadata:
    """Metadata about a tool."""
    name: str
    description: str
    category: ToolCategory
    risk_level: ToolRiskLevel
    version: str = "1.0.0"
    author: str = "JARVIS"
    tags: List[str] = field(default_factory=list)
    requires_permission: bool = True
    execution_mode: ToolExecutionMode = ToolExecutionMode.ASYNC
    timeout_seconds: float = 30.0
    retry_count: int = 3
    cooldown_seconds: float = 0.0
    dependencies: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ToolExecutionRecord:
    """Record of a tool execution."""
    tool_name: str
    execution_id: str
    input_args: Dict[str, Any]
    output: Any
    success: bool
    error: Optional[str] = None
    duration_ms: float = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    permission_status: str = "granted"


@dataclass
class ToolCapability:
    """Describes what a tool can do."""
    name: str
    description: str
    input_types: List[str]
    output_types: List[str]
    side_effects: List[str]


# ============================================================================
# Tool Schema Generation
# ============================================================================

class ToolSchemaGenerator:
    """Generates tool schemas from Python functions."""

    TYPE_MAPPING = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
        type(None): "null"
    }

    @classmethod
    def generate_schema(cls, func: Callable) -> Dict[str, Any]:
        """Generate JSON schema from function signature."""
        sig = inspect.signature(func)
        hints = get_type_hints(func) if hasattr(func, '__annotations__') else {}
        doc = inspect.getdoc(func) or ""

        properties = {}
        required = []

        for name, param in sig.parameters.items():
            if name in ('self', 'cls', 'run_manager', 'callback_manager'):
                continue

            param_type = hints.get(name, Any)
            param_schema = cls._type_to_schema(param_type)

            # Extract description from docstring
            param_desc = cls._extract_param_description(doc, name)
            if param_desc:
                param_schema["description"] = param_desc

            properties[name] = param_schema

            # Check if required
            if param.default is inspect.Parameter.empty:
                required.append(name)

        return {
            "type": "object",
            "properties": properties,
            "required": required
        }

    @classmethod
    def _type_to_schema(cls, type_hint: Any) -> Dict[str, Any]:
        """Convert Python type hint to JSON schema."""
        # Handle basic types
        if type_hint in cls.TYPE_MAPPING:
            return {"type": cls.TYPE_MAPPING[type_hint]}

        # Handle Optional
        origin = getattr(type_hint, '__origin__', None)
        if origin is Union:
            args = type_hint.__args__
            if type(None) in args:
                # Optional type
                non_none = [a for a in args if a is not type(None)]
                if len(non_none) == 1:
                    schema = cls._type_to_schema(non_none[0])
                    schema["nullable"] = True
                    return schema

        # Handle List
        if origin is list:
            item_type = type_hint.__args__[0] if type_hint.__args__ else Any
            return {
                "type": "array",
                "items": cls._type_to_schema(item_type)
            }

        # Handle Dict
        if origin is dict:
            return {"type": "object"}

        # Handle Literal
        if origin is Literal:
            return {"enum": list(type_hint.__args__)}

        # Default
        return {"type": "string"}

    @classmethod
    def _extract_param_description(cls, docstring: str, param_name: str) -> Optional[str]:
        """Extract parameter description from docstring."""
        # Support multiple docstring formats
        patterns = [
            rf":param {param_name}:\s*(.+?)(?=\n|:param|:return|$)",  # Sphinx
            rf"{param_name}\s*:\s*(.+?)(?=\n\s*\w+\s*:|$)",  # Google
            rf"Args:\s*.*?{param_name}\s*(?:\([^)]+\))?\s*:\s*(.+?)(?=\n|$)"  # Google Args
        ]

        for pattern in patterns:
            match = re.search(pattern, docstring, re.DOTALL)
            if match:
                return match.group(1).strip()

        return None


# ============================================================================
# Base Tool Classes
# ============================================================================

class JARVISTool(ABC):
    """
    Base class for JARVIS tools.

    Provides common functionality for all tools including:
    - Async execution support
    - Permission checking
    - Execution tracking
    - Error handling
    """

    def __init__(
        self,
        metadata: ToolMetadata,
        permission_manager: Optional[Any] = None
    ):
        self.metadata = metadata
        self.permission_manager = permission_manager
        self.logger = logging.getLogger(f"{__name__}.{metadata.name}")
        self._execution_count = 0
        self._last_execution: Optional[datetime] = None
        self._execution_history: List[ToolExecutionRecord] = []

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @abstractmethod
    async def _execute(self, **kwargs) -> Any:
        """Execute the tool. Override in subclasses."""
        pass

    async def run(self, **kwargs) -> Any:
        """
        Run the tool with permission checking and tracking.

        Args:
            **kwargs: Tool arguments

        Returns:
            Tool output
        """
        execution_id = str(uuid4())
        start_time = time.time()

        # Check cooldown
        if self._last_execution and self.metadata.cooldown_seconds > 0:
            elapsed = (datetime.utcnow() - self._last_execution).total_seconds()
            if elapsed < self.metadata.cooldown_seconds:
                raise RuntimeError(
                    f"Tool on cooldown. Wait {self.metadata.cooldown_seconds - elapsed:.1f}s"
                )

        # Check permission
        permission_status = "granted"
        if self.metadata.requires_permission and self.permission_manager:
            has_permission = await self._check_permission(kwargs)
            if not has_permission:
                permission_status = "denied"
                record = ToolExecutionRecord(
                    tool_name=self.name,
                    execution_id=execution_id,
                    input_args=kwargs,
                    output=None,
                    success=False,
                    error="Permission denied",
                    permission_status=permission_status
                )
                self._execution_history.append(record)
                raise PermissionError(f"Permission denied for tool: {self.name}")

        try:
            # Execute with timeout
            result = await asyncio.wait_for(
                self._execute(**kwargs),
                timeout=self.metadata.timeout_seconds
            )

            duration_ms = (time.time() - start_time) * 1000
            self._execution_count += 1
            self._last_execution = datetime.utcnow()

            # Record execution
            record = ToolExecutionRecord(
                tool_name=self.name,
                execution_id=execution_id,
                input_args=kwargs,
                output=result,
                success=True,
                duration_ms=duration_ms,
                permission_status=permission_status
            )
            self._execution_history.append(record)

            return result

        except asyncio.TimeoutError:
            duration_ms = (time.time() - start_time) * 1000
            record = ToolExecutionRecord(
                tool_name=self.name,
                execution_id=execution_id,
                input_args=kwargs,
                output=None,
                success=False,
                error="Execution timed out",
                duration_ms=duration_ms,
                permission_status=permission_status
            )
            self._execution_history.append(record)
            raise

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            record = ToolExecutionRecord(
                tool_name=self.name,
                execution_id=execution_id,
                input_args=kwargs,
                output=None,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
                permission_status=permission_status
            )
            self._execution_history.append(record)
            raise

    async def _check_permission(self, kwargs: Dict[str, Any]) -> bool:
        """Check if tool execution is permitted."""
        if self.permission_manager is None:
            return True

        try:
            return await self.permission_manager.check_permission(
                action_type=f"tool:{self.name}",
                target=self.name,
                context=kwargs
            )
        except Exception as e:
            self.logger.warning(f"Permission check failed: {e}")
            return False

    def get_execution_stats(self) -> Dict[str, Any]:
        """Get execution statistics."""
        if not self._execution_history:
            return {
                "total_executions": 0,
                "success_rate": 0,
                "avg_duration_ms": 0
            }

        successful = [r for r in self._execution_history if r.success]
        durations = [r.duration_ms for r in self._execution_history]

        return {
            "total_executions": len(self._execution_history),
            "success_rate": len(successful) / len(self._execution_history),
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "last_execution": self._last_execution.isoformat() if self._last_execution else None
        }

    def to_langchain_tool(self) -> Optional[BaseTool]:
        """Convert to LangChain BaseTool."""
        if not LANGCHAIN_AVAILABLE:
            return None

        return JARVISLangChainTool(jarvis_tool=self)


class JARVISLangChainTool(BaseTool):
    """LangChain wrapper for JARVIS tools."""

    name: str = ""
    description: str = ""
    jarvis_tool: Any = None

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, jarvis_tool: JARVISTool, **kwargs):
        super().__init__(**kwargs)
        self.jarvis_tool = jarvis_tool
        self.name = jarvis_tool.name
        self.description = jarvis_tool.description

    def _run(
        self,
        *args,
        run_manager: Optional[CallbackManagerForToolRun] = None,
        **kwargs
    ) -> str:
        """Sync execution (wraps async)."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Create new loop in thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run, self.jarvis_tool.run(**kwargs)
                    )
                    result = future.result()
            else:
                result = loop.run_until_complete(self.jarvis_tool.run(**kwargs))
            return json.dumps(result) if not isinstance(result, str) else result
        except Exception as e:
            return f"Error: {str(e)}"

    async def _arun(
        self,
        *args,
        run_manager: Optional[AsyncCallbackManagerForToolRun] = None,
        **kwargs
    ) -> str:
        """Async execution."""
        try:
            result = await self.jarvis_tool.run(**kwargs)
            return json.dumps(result) if not isinstance(result, str) else result
        except Exception as e:
            return f"Error: {str(e)}"


# ============================================================================
# Function-Based Tool Wrapper
# ============================================================================

class FunctionTool(JARVISTool):
    """Tool created from a function."""

    def __init__(
        self,
        func: Callable,
        metadata: ToolMetadata,
        permission_manager: Optional[Any] = None
    ):
        super().__init__(metadata, permission_manager)
        self.func = func
        self.is_async = asyncio.iscoroutinefunction(func)
        self.schema = ToolSchemaGenerator.generate_schema(func)

    async def _execute(self, **kwargs) -> Any:
        """Execute the wrapped function."""
        if self.is_async:
            return await self.func(**kwargs)
        else:
            # Run sync function in thread pool
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, functools.partial(self.func, **kwargs))


# ============================================================================
# Neural Mesh Agent Tool (v239.0)
# ============================================================================

class NeuralMeshAgentTool(JARVISTool):
    """Tool that delegates to a Neural Mesh agent's execute_task().

    Each mesh agent capability (e.g., "fetch_unread_emails") gets wrapped
    as a JARVISTool so the agent runtime's THINK step can discover it
    and the ACT step can execute it via the standard tool.run() pipeline.
    """

    def __init__(
        self,
        agent,  # BaseNeuralMeshAgent (untyped to avoid circular import)
        capability: str,
        category: ToolCategory = ToolCategory.INTEGRATION,
        risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
        timeout_seconds: float = 30.0,
    ):
        tool_name = f"mesh:{agent.agent_name}:{capability}"
        metadata = ToolMetadata(
            name=tool_name,
            description=f"[Neural Mesh] {capability} via {agent.agent_name}",
            category=category,
            risk_level=risk_level,
            requires_permission=False,
            timeout_seconds=timeout_seconds,
            capabilities=[capability],
            tags=["neural_mesh", agent.agent_type, agent.agent_name],
        )
        super().__init__(metadata)
        self._agent = agent
        self._capability = capability

    async def _execute(self, **kwargs) -> Any:
        """Delegate to the mesh agent's execute_task method."""
        payload = {"action": self._capability, **kwargs}
        return await self._agent.execute_task(payload)


# ============================================================================
# Tool Registry
# ============================================================================

class ToolRegistry:
    """
    Dynamic tool registry with auto-discovery.

    Features:
    - Register tools by name or decorator
    - Auto-discover tools from modules
    - Query tools by capability
    - Hot-reload support
    """

    _instance: Optional["ToolRegistry"] = None

    def __init__(self):
        self._tools: Dict[str, JARVISTool] = {}
        self._categories: Dict[ToolCategory, Set[str]] = {cat: set() for cat in ToolCategory}
        self._capabilities: Dict[str, Set[str]] = {}  # capability -> tool names
        self._loaded_modules: Set[str] = set()
        self.logger = logging.getLogger(__name__)

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(
        self,
        tool: JARVISTool,
        replace: bool = False
    ) -> None:
        """
        Register a tool.

        Args:
            tool: Tool to register
            replace: Whether to replace existing tool with same name
        """
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool '{tool.name}' already registered")

        self._tools[tool.name] = tool
        self._categories[tool.metadata.category].add(tool.name)

        # Index capabilities
        for cap in tool.metadata.capabilities:
            if cap not in self._capabilities:
                self._capabilities[cap] = set()
            self._capabilities[cap].add(tool.name)

        self.logger.info(f"Registered tool: {tool.name}")

    def register_function(
        self,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        category: ToolCategory = ToolCategory.UTILITY,
        risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
        **metadata_kwargs
    ) -> JARVISTool:
        """
        Register a function as a tool.

        Args:
            func: Function to register
            name: Tool name (defaults to function name)
            description: Tool description (defaults to docstring)
            category: Tool category
            risk_level: Risk level
            **metadata_kwargs: Additional metadata

        Returns:
            Created tool
        """
        tool_name = name or func.__name__
        tool_desc = description or inspect.getdoc(func) or f"Tool: {tool_name}"

        metadata = ToolMetadata(
            name=tool_name,
            description=tool_desc,
            category=category,
            risk_level=risk_level,
            **metadata_kwargs
        )

        tool = FunctionTool(func=func, metadata=metadata)
        self.register(tool)
        return tool

    def unregister(self, name: str) -> bool:
        """Unregister a tool."""
        if name not in self._tools:
            return False

        tool = self._tools[name]
        del self._tools[name]

        # Clean up indices
        self._categories[tool.metadata.category].discard(name)
        for cap in tool.metadata.capabilities:
            if cap in self._capabilities:
                self._capabilities[cap].discard(name)

        self.logger.info(f"Unregistered tool: {name}")
        return True

    def get(self, name: str) -> Optional[JARVISTool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_all(self) -> List[JARVISTool]:
        """Get all registered tools."""
        return list(self._tools.values())

    def get_by_category(self, category: ToolCategory) -> List[JARVISTool]:
        """Get tools by category."""
        names = self._categories.get(category, set())
        return [self._tools[n] for n in names if n in self._tools]

    def get_by_capability(self, capability: str) -> List[JARVISTool]:
        """Get tools by capability."""
        names = self._capabilities.get(capability, set())
        return [self._tools[n] for n in names if n in self._tools]

    def search(
        self,
        query: str,
        category: Optional[ToolCategory] = None,
        risk_level_max: Optional[ToolRiskLevel] = None
    ) -> List[JARVISTool]:
        """
        Search for tools matching criteria.

        Args:
            query: Search query (matches name, description, tags)
            category: Filter by category
            risk_level_max: Maximum risk level

        Returns:
            Matching tools
        """
        results = []
        query_lower = query.lower()
        risk_order = list(ToolRiskLevel)

        for tool in self._tools.values():
            # Check category filter
            if category and tool.metadata.category != category:
                continue

            # Check risk level filter
            if risk_level_max:
                tool_risk_idx = risk_order.index(tool.metadata.risk_level)
                max_risk_idx = risk_order.index(risk_level_max)
                if tool_risk_idx > max_risk_idx:
                    continue

            # Check query match
            if query_lower in tool.name.lower():
                results.append(tool)
            elif query_lower in tool.description.lower():
                results.append(tool)
            elif any(query_lower in tag.lower() for tag in tool.metadata.tags):
                results.append(tool)

        return results

    def discover_from_module(self, module_path: str) -> int:
        """
        Discover and register tools from a module.

        Args:
            module_path: Python module path (e.g., 'backend.tools.file_tools')

        Returns:
            Number of tools discovered
        """
        if module_path in self._loaded_modules:
            self.logger.debug(f"Module already loaded: {module_path}")
            return 0

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            self.logger.error(f"Failed to import module {module_path}: {e}")
            return 0

        count = 0

        # Look for decorated functions
        for name, obj in inspect.getmembers(module):
            if hasattr(obj, '_jarvis_tool_metadata'):
                metadata = obj._jarvis_tool_metadata
                tool = FunctionTool(func=obj, metadata=metadata)
                self.register(tool, replace=True)
                count += 1

            # Look for JARVISTool subclasses
            elif (inspect.isclass(obj)
                  and issubclass(obj, JARVISTool)
                  and obj is not JARVISTool):
                try:
                    tool_instance = obj()
                    self.register(tool_instance, replace=True)
                    count += 1
                except Exception as e:
                    self.logger.warning(f"Failed to instantiate {name}: {e}")

        self._loaded_modules.add(module_path)
        self.logger.info(f"Discovered {count} tools from {module_path}")
        return count

    def discover_from_directory(
        self,
        directory: Path,
        pattern: str = "*.py",
        base_module: str = ""
    ) -> int:
        """
        Discover tools from all Python files in a directory.

        Args:
            directory: Directory to scan
            pattern: File pattern to match
            base_module: Base module path

        Returns:
            Total tools discovered
        """
        total = 0

        if not directory.exists():
            self.logger.warning(f"Directory not found: {directory}")
            return 0

        for path in directory.glob(pattern):
            if path.name.startswith('_'):
                continue

            # Build module path
            relative = path.relative_to(directory.parent)
            module_parts = list(relative.with_suffix('').parts)
            if base_module:
                module_path = f"{base_module}.{'.'.join(module_parts)}"
            else:
                module_path = '.'.join(module_parts)

            total += self.discover_from_module(module_path)

        return total

    def to_langchain_tools(self) -> List[BaseTool]:
        """Convert all tools to LangChain tools."""
        if not LANGCHAIN_AVAILABLE:
            return []

        return [
            tool.to_langchain_tool()
            for tool in self._tools.values()
            if tool.to_langchain_tool() is not None
        ]

    def get_tool_manifest(self) -> Dict[str, Any]:
        """Get complete tool manifest."""
        return {
            "total_tools": len(self._tools),
            "categories": {
                cat.value: len(names)
                for cat, names in self._categories.items()
            },
            "capabilities": list(self._capabilities.keys()),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "category": tool.metadata.category.value,
                    "risk_level": tool.metadata.risk_level.value,
                    "version": tool.metadata.version,
                    "capabilities": tool.metadata.capabilities,
                    "tags": tool.metadata.tags
                }
                for tool in self._tools.values()
            ]
        }


# ============================================================================
# Tool Decorator
# ============================================================================

def jarvis_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    category: ToolCategory = ToolCategory.UTILITY,
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
    requires_permission: bool = True,
    capabilities: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    **kwargs
):
    """
    Decorator to mark a function as a JARVIS tool.

    Usage:
        @jarvis_tool(
            name="search_files",
            description="Search for files matching a pattern",
            category=ToolCategory.FILE_SYSTEM,
            risk_level=ToolRiskLevel.SAFE,
            capabilities=["file_search", "pattern_matching"]
        )
        async def search_files(pattern: str, directory: str = ".") -> List[str]:
            '''Search for files matching the given pattern.'''
            ...
    """
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        tool_desc = description or inspect.getdoc(func) or f"Tool: {tool_name}"

        metadata = ToolMetadata(
            name=tool_name,
            description=tool_desc,
            category=category,
            risk_level=risk_level,
            requires_permission=requires_permission,
            capabilities=capabilities or [],
            tags=tags or [],
            **kwargs
        )

        func._jarvis_tool_metadata = metadata

        @functools.wraps(func)
        async def wrapper(*args, **kw):
            return await func(*args, **kw) if asyncio.iscoroutinefunction(func) else func(*args, **kw)

        wrapper._jarvis_tool_metadata = metadata
        return wrapper

    return decorator


# ============================================================================
# Built-in Tools
# ============================================================================

@dataclass(frozen=True)
class FileSystemAccessPolicy:
    """Resolved filesystem access policy for the file-system tool."""

    read_roots: Tuple[Path, ...]
    write_roots: Tuple[Path, ...]
    deny_roots: Tuple[Path, ...]

    def as_dict(self) -> Dict[str, List[str]]:
        return {
            "read_roots": [str(path) for path in self.read_roots],
            "write_roots": [str(path) for path in self.write_roots],
            "deny_roots": [str(path) for path in self.deny_roots],
        }


class FileSystemAgentTool(JARVISTool):
    """Secure, policy-driven file system operations with allowlist enforcement."""

    READ_ALLOWLIST_ENV = "JARVIS_FS_READ_ALLOWLIST"
    WRITE_ALLOWLIST_ENV = "JARVIS_FS_WRITE_ALLOWLIST"
    DENYLIST_ENV = "JARVIS_FS_DENYLIST"
    REPO_ROOTS_ENV = "JARVIS_REPO_ROOTS"
    MAX_READ_BYTES_ENV = "JARVIS_FS_MAX_READ_BYTES"
    MAX_LIST_RESULTS_ENV = "JARVIS_FS_MAX_LIST_RESULTS"
    ALLOW_ROOT_PATHS_ENV = "JARVIS_FS_ALLOW_ROOT_PATHS"

    REPO_ENV_KEYS: Tuple[str, ...] = (
        "JARVIS_PATH",
        "JARVIS_REPO_PATH",
        "JARVIS_CORE_PATH",
        "JARVIS_PRIME_PATH",
        "JARVIS_PRIME_REPO_PATH",
        "REACTOR_CORE_PATH",
        "JARVIS_REACTOR_PATH",
    )

    SUPPORTED_OPERATIONS: Tuple[str, ...] = (
        "read_text",
        "read_json",
        "write_text",
        "write_json",
        "list_files",
        "stat",
        "exists",
        "mkdir",
        "delete_file",
        "delete_dir",
        "copy_file",
        "move_file",
        "get_policy",
        "get_metrics",
    )

    OPERATION_ALIASES: Dict[str, str] = {
        "read_file": "read_text",
        "write_file": "write_text",
        "list": "list_files",
        "ls": "list_files",
        "policy": "get_policy",
        "metrics": "get_metrics",
        "copy": "copy_file",
        "move": "move_file",
        "remove_file": "delete_file",
        "remove_dir": "delete_dir",
    }

    def __init__(self, permission_manager: Optional[Any] = None):
        metadata = ToolMetadata(
            name="filesystem_agent",
            description=(
                "Secure file system operations with scoped read/write permissions, "
                "atomic writes, and policy-driven path controls"
            ),
            category=ToolCategory.FILE_SYSTEM,
            risk_level=ToolRiskLevel.MEDIUM,
            requires_permission=False,
            timeout_seconds=60.0,
            capabilities=[
                "file_read",
                "file_write",
                "file_delete",
                "file_listing",
                "filesystem_metadata",
                "secure_filesystem",
            ],
            tags=["filesystem", "allowlist", "atomic", "secure"],
        )
        super().__init__(metadata, permission_manager)
        self._atomic_ops = get_atomic_file_ops()
        self._policy_lock = asyncio.Lock()
        self._policy_signature: Optional[Tuple[str, ...]] = None
        self._policy = self._build_policy()
        self._policy_signature = self._compute_policy_signature()
        self._max_read_bytes = self._env_int(self.MAX_READ_BYTES_ENV, 1_000_000, minimum=1_024)
        self._max_list_results = self._env_int(self.MAX_LIST_RESULTS_ENV, 1_000, minimum=1)

    async def _execute(
        self,
        operation: str = "",
        path: Optional[str] = None,
        source: Optional[str] = None,
        destination: Optional[str] = None,
        content: Optional[Any] = None,
        data: Optional[Any] = None,
        pattern: str = "*",
        recursive: bool = False,
        encoding: str = "utf-8",
        max_results: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Execute a secure file operation."""
        await self._refresh_policy_if_changed()

        op = (operation or kwargs.get("action") or kwargs.get("op") or "").strip().lower()
        op = self.OPERATION_ALIASES.get(op, op)
        if not op:
            raise ValueError("Missing required parameter: operation")
        if op not in self.SUPPORTED_OPERATIONS:
            raise ValueError(
                f"Unsupported operation '{op}'. Supported operations: {', '.join(self.SUPPORTED_OPERATIONS)}"
            )

        resolved_path = path or kwargs.get("file_path")
        source_path = source or kwargs.get("source_path")
        destination_path = destination or kwargs.get("destination_path")

        if op == "read_text":
            target = self._resolve_and_validate(resolved_path, access="read")
            raw = await self._atomic_ops.read_bytes(target)
            self._enforce_read_limit(raw, target)
            return {
                "operation": op,
                "path": str(target),
                "content": raw.decode(encoding),
                "bytes_read": len(raw),
            }

        if op == "read_json":
            target = self._resolve_and_validate(resolved_path, access="read")
            raw = await self._atomic_ops.read_bytes(target)
            self._enforce_read_limit(raw, target)
            try:
                parsed = json.loads(raw.decode(encoding))
            except json.JSONDecodeError as exc:
                raise ValueError(f"File is not valid JSON: {target}") from exc
            return {
                "operation": op,
                "path": str(target),
                "data": parsed,
                "bytes_read": len(raw),
            }

        if op == "write_text":
            target = self._resolve_and_validate(resolved_path, access="write")
            text_content = self._resolve_text_payload(content=content, data=data, kwargs=kwargs)
            create_parents = self._to_bool(kwargs.get("create_parents"), default=True)
            if create_parents:
                await self._ensure_directory(target.parent)
            checksum = await self._atomic_ops.write_text(target, text_content, encoding=encoding)
            return {
                "operation": op,
                "path": str(target),
                "checksum": checksum,
                "bytes_written": len(text_content.encode(encoding)),
            }

        if op == "write_json":
            target = self._resolve_and_validate(resolved_path, access="write")
            payload = self._resolve_json_payload(content=content, data=data, kwargs=kwargs)
            create_parents = self._to_bool(kwargs.get("create_parents"), default=True)
            if create_parents:
                await self._ensure_directory(target.parent)
            indent = self._coerce_int(kwargs.get("indent"), default=2, minimum=0)
            checksum = await self._atomic_ops.write_json(target, payload, indent=indent)
            return {
                "operation": op,
                "path": str(target),
                "checksum": checksum,
            }

        if op == "list_files":
            target = self._resolve_and_validate(resolved_path or ".", access="read")
            recursive_flag = self._to_bool(kwargs.get("recursive", recursive), default=recursive)
            listed = await self._atomic_ops.list_files(
                directory=target,
                pattern=pattern or kwargs.get("glob", "*"),
                recursive=recursive_flag,
            )
            limit = self._coerce_int(
                max_results if max_results is not None else kwargs.get("max_results"),
                default=self._max_list_results,
                minimum=1,
            )
            normalized_files = sorted(str(path) for path in listed)[:limit]
            return {
                "operation": op,
                "directory": str(target),
                "pattern": pattern or kwargs.get("glob", "*"),
                "recursive": recursive_flag,
                "count": len(normalized_files),
                "files": normalized_files,
            }

        if op == "stat":
            target = self._resolve_and_validate(resolved_path, access="read")
            exists = await asyncio.to_thread(target.exists)
            if not exists:
                return {"operation": op, "path": str(target), "exists": False}
            stat_result = await asyncio.to_thread(target.stat)
            return {
                "operation": op,
                "path": str(target),
                "exists": True,
                "is_file": await asyncio.to_thread(target.is_file),
                "is_dir": await asyncio.to_thread(target.is_dir),
                "size_bytes": stat_result.st_size,
                "modified_ts": stat_result.st_mtime,
                "created_ts": stat_result.st_ctime,
                "mode": stat_result.st_mode,
            }

        if op == "exists":
            target = self._resolve_and_validate(resolved_path, access="read")
            return {
                "operation": op,
                "path": str(target),
                "exists": await asyncio.to_thread(target.exists),
            }

        if op == "mkdir":
            target = self._resolve_and_validate(resolved_path, access="write")
            parents = self._to_bool(kwargs.get("parents"), default=True)
            exist_ok = self._to_bool(kwargs.get("exist_ok"), default=True)
            await asyncio.to_thread(target.mkdir, parents=parents, exist_ok=exist_ok)
            return {"operation": op, "path": str(target), "created": True}

        if op == "delete_file":
            target = self._resolve_and_validate(resolved_path, access="write")
            missing_ok = self._to_bool(kwargs.get("missing_ok"), default=True)
            deleted = await self._atomic_ops.delete(target, missing_ok=missing_ok)
            return {"operation": op, "path": str(target), "deleted": deleted}

        if op == "delete_dir":
            target = self._resolve_and_validate(resolved_path, access="write")
            recursive_flag = self._to_bool(kwargs.get("recursive", recursive), default=False)
            missing_ok = self._to_bool(kwargs.get("missing_ok"), default=True)
            deleted = await self._atomic_ops.delete_dir(
                target,
                recursive=recursive_flag,
                missing_ok=missing_ok,
            )
            return {
                "operation": op,
                "path": str(target),
                "deleted": deleted,
                "recursive": recursive_flag,
            }

        if op == "copy_file":
            src = self._resolve_and_validate(source_path or resolved_path, access="read")
            dest = self._resolve_and_validate(destination_path, access="write")
            overwrite = self._to_bool(kwargs.get("overwrite"), default=True)
            create_parents = self._to_bool(kwargs.get("create_parents"), default=True)
            await self._ensure_file_exists(src)
            if create_parents:
                await self._ensure_directory(dest.parent)
            if not overwrite and await asyncio.to_thread(dest.exists):
                raise FileExistsError(f"Destination already exists: {dest}")
            data_bytes = await self._atomic_ops.read_bytes(src)
            checksum = await self._atomic_ops.write_bytes(dest, data_bytes)
            return {
                "operation": op,
                "source": str(src),
                "destination": str(dest),
                "checksum": checksum,
                "bytes_copied": len(data_bytes),
            }

        if op == "move_file":
            src = self._resolve_and_validate(source_path or resolved_path, access="write")
            dest = self._resolve_and_validate(destination_path, access="write")
            self._assert_access(src, "read")
            overwrite = self._to_bool(kwargs.get("overwrite"), default=True)
            create_parents = self._to_bool(kwargs.get("create_parents"), default=True)
            await self._ensure_file_exists(src)
            if create_parents:
                await self._ensure_directory(dest.parent)
            if src == dest:
                return {
                    "operation": op,
                    "source": str(src),
                    "destination": str(dest),
                    "moved": True,
                    "note": "source and destination are identical",
                }
            if not overwrite and await asyncio.to_thread(dest.exists):
                raise FileExistsError(f"Destination already exists: {dest}")

            moved_via_copy = False
            try:
                await asyncio.to_thread(os.replace, src, dest)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                moved_via_copy = True
                data_bytes = await self._atomic_ops.read_bytes(src)
                await self._atomic_ops.write_bytes(dest, data_bytes)
                await self._atomic_ops.delete(src, missing_ok=False)

            return {
                "operation": op,
                "source": str(src),
                "destination": str(dest),
                "moved": True,
                "used_cross_device_fallback": moved_via_copy,
            }

        if op == "get_policy":
            return {
                "operation": op,
                "policy": self._policy.as_dict(),
                "max_read_bytes": self._max_read_bytes,
                "max_list_results": self._max_list_results,
            }

        if op == "get_metrics":
            return {"operation": op, "metrics": self._atomic_ops.get_metrics()}

        # Should not be reachable due supported operation check
        raise RuntimeError(f"Unhandled operation: {op}")

    def _resolve_and_validate(self, raw_path: Optional[str], access: str) -> Path:
        if not raw_path:
            raise ValueError("Missing required parameter: path")
        resolved = self._resolve_user_path(raw_path)
        self._assert_access(resolved, access)
        return resolved

    async def _refresh_policy_if_changed(self) -> None:
        signature = self._compute_policy_signature()
        if signature == self._policy_signature:
            return
        async with self._policy_lock:
            signature = self._compute_policy_signature()
            if signature == self._policy_signature:
                return
            self._policy = self._build_policy()
            self._policy_signature = signature

    def _build_policy(self) -> FileSystemAccessPolicy:
        read_roots = self._canonicalize_paths(self._parse_env_paths(self.READ_ALLOWLIST_ENV))
        write_roots = self._canonicalize_paths(self._parse_env_paths(self.WRITE_ALLOWLIST_ENV))
        deny_roots = self._canonicalize_paths(self._parse_env_paths(self.DENYLIST_ENV))

        if not read_roots:
            read_roots = self._default_read_roots()
        if not write_roots:
            write_roots = self._default_write_roots(read_roots)

        if not read_roots:
            cwd_root = self._resolve_user_path(str(Path.cwd()))
            read_roots = (cwd_root,)
        if not write_roots:
            write_roots = tuple(path for path in read_roots if self._is_relative(path, Path.cwd()))
            if not write_roots:
                write_roots = (self._resolve_user_path(str(Path.cwd())),)

        return FileSystemAccessPolicy(
            read_roots=read_roots,
            write_roots=write_roots,
            deny_roots=deny_roots,
        )

    def _default_read_roots(self) -> Tuple[Path, ...]:
        roots: List[Path] = []
        roots.extend(self._parse_env_paths(self.REPO_ROOTS_ENV))
        for key in self.REPO_ENV_KEYS:
            value = os.getenv(key, "").strip()
            if value:
                roots.append(Path(value))
        roots.append(Path(__file__).resolve().parents[2])
        roots.append(Path.home() / ".jarvis")
        return self._canonicalize_paths(roots)

    def _default_write_roots(self, read_roots: Tuple[Path, ...]) -> Tuple[Path, ...]:
        write_roots: List[Path] = [Path.home() / ".jarvis"]
        for root in read_roots:
            if any(self._is_relative(root, candidate) for candidate in write_roots):
                continue
            write_roots.append(root)
        return self._canonicalize_paths(write_roots)

    def _parse_env_paths(self, env_key: str) -> List[Path]:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            return []

        values: List[str] = []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    values = [str(item).strip() for item in parsed if str(item).strip()]
            except Exception:
                values = []
        if not values:
            values = [item.strip() for item in re.split(r"[,;\n]+", raw) if item.strip()]

        return [Path(value).expanduser() for value in values]

    def _canonicalize_paths(self, paths: Sequence[Path]) -> Tuple[Path, ...]:
        allow_root_paths = self._to_bool(os.getenv(self.ALLOW_ROOT_PATHS_ENV), default=False)
        normalized: List[Path] = []
        for path in paths:
            try:
                resolved = self._resolve_user_path(str(path))
                if not allow_root_paths and resolved == Path(resolved.anchor):
                    self.logger.warning("Skipping filesystem allowlist root path for safety: %s", resolved)
                    continue
                normalized.append(resolved)
            except Exception:
                continue

        unique: List[Path] = []
        for path in sorted(set(normalized), key=lambda candidate: len(candidate.parts)):
            if any(self._is_relative(path, existing) for existing in unique):
                continue
            unique.append(path)

        return tuple(unique)

    def _compute_policy_signature(self) -> Tuple[str, ...]:
        values = [
            os.getenv(self.READ_ALLOWLIST_ENV, ""),
            os.getenv(self.WRITE_ALLOWLIST_ENV, ""),
            os.getenv(self.DENYLIST_ENV, ""),
            os.getenv(self.REPO_ROOTS_ENV, ""),
        ]
        values.extend(os.getenv(key, "") for key in self.REPO_ENV_KEYS)
        return tuple(values)

    @staticmethod
    def _resolve_user_path(raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate

        missing_parts: List[str] = []
        probe = candidate
        while not probe.exists():
            missing_parts.append(probe.name)
            parent = probe.parent
            if parent == probe:
                break
            probe = parent

        resolved_base = probe.resolve()
        for part in reversed(missing_parts):
            resolved_base = resolved_base / part
        return resolved_base

    def _assert_access(self, path: Path, access: str) -> None:
        normalized = self._resolve_user_path(str(path))

        if any(self._is_relative(normalized, denied) for denied in self._policy.deny_roots):
            raise PermissionError(f"Path is explicitly denied by filesystem policy: {normalized}")

        allowed_roots = self._policy.read_roots if access == "read" else self._policy.write_roots
        if not any(self._is_relative(normalized, root) for root in allowed_roots):
            raise PermissionError(f"{access} access denied by filesystem policy: {normalized}")

    @staticmethod
    def _is_relative(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    async def _ensure_directory(self, directory: Path) -> None:
        self._assert_access(directory, "write")
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)

    async def _ensure_file_exists(self, path: Path) -> None:
        exists = await asyncio.to_thread(path.exists)
        if not exists:
            raise FileNotFoundError(f"Path does not exist: {path}")
        is_file = await asyncio.to_thread(path.is_file)
        if not is_file:
            raise IsADirectoryError(f"Expected a file path, got: {path}")

    def _enforce_read_limit(self, payload: bytes, path: Path) -> None:
        if len(payload) > self._max_read_bytes:
            raise ValueError(
                f"Read aborted: {path} is {len(payload)} bytes, exceeding "
                f"limit of {self._max_read_bytes} bytes"
            )

    def _resolve_text_payload(self, content: Any, data: Any, kwargs: Dict[str, Any]) -> str:
        candidate = content if content is not None else data
        if candidate is None:
            candidate = kwargs.get("text")
        if candidate is None:
            raise ValueError("Missing text payload. Provide 'content', 'data', or 'text'.")
        if isinstance(candidate, bytes):
            return candidate.decode("utf-8")
        if isinstance(candidate, str):
            return candidate
        return json.dumps(candidate, default=str, indent=2)

    def _resolve_json_payload(self, content: Any, data: Any, kwargs: Dict[str, Any]) -> Any:
        candidate = data if data is not None else kwargs.get("json_data")
        if candidate is None:
            candidate = content
        if candidate is None:
            raise ValueError("Missing JSON payload. Provide 'data', 'json_data', or 'content'.")
        if isinstance(candidate, str):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise ValueError("String payload is not valid JSON") from exc
        return candidate

    @staticmethod
    def _to_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return bool(value)

    @staticmethod
    def _coerce_int(value: Any, default: int, minimum: int = 0) -> int:
        if value is None:
            return default
        try:
            coerced = int(value)
            return max(coerced, minimum)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _env_int(cls, env_key: str, default: int, minimum: int = 0) -> int:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            return max(value, minimum)
        except ValueError:
            return default


class SystemInfoTool(JARVISTool):
    """Tool for getting system information."""

    def __init__(self, permission_manager: Optional[Any] = None):
        metadata = ToolMetadata(
            name="system_info",
            description="Get current system information including CPU, memory, and disk usage",
            category=ToolCategory.SYSTEM,
            risk_level=ToolRiskLevel.SAFE,
            requires_permission=False,
            capabilities=["system_monitoring", "resource_info"]
        )
        super().__init__(metadata, permission_manager)

    async def _execute(self, **kwargs) -> Dict[str, Any]:
        """Get system information."""
        import platform
        import os

        return {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "pid": os.getpid()
        }


class CalculatorTool(JARVISTool):
    """Tool for mathematical calculations."""

    def __init__(self, permission_manager: Optional[Any] = None):
        metadata = ToolMetadata(
            name="calculator",
            description="Perform mathematical calculations safely",
            category=ToolCategory.UTILITY,
            risk_level=ToolRiskLevel.SAFE,
            requires_permission=False,
            capabilities=["math", "calculation"]
        )
        super().__init__(metadata, permission_manager)

    async def _execute(self, expression: str, **kwargs) -> Dict[str, Any]:
        """
        Evaluate a mathematical expression safely.

        Args:
            expression: Mathematical expression to evaluate

        Returns:
            Result of the calculation
        """
        import ast
        import operator

        # Safe operators
        operators = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos
        }

        def eval_node(node):
            if isinstance(node, ast.Constant):
                return node.value
            elif isinstance(node, ast.BinOp):
                left = eval_node(node.left)
                right = eval_node(node.right)
                return operators[type(node.op)](left, right)
            elif isinstance(node, ast.UnaryOp):
                operand = eval_node(node.operand)
                return operators[type(node.op)](operand)
            else:
                raise ValueError(f"Unsupported operation: {type(node)}")

        try:
            tree = ast.parse(expression, mode='eval')
            result = eval_node(tree.body)
            return {
                "expression": expression,
                "result": result,
                "success": True
            }
        except Exception as e:
            return {
                "expression": expression,
                "result": None,
                "success": False,
                "error": str(e)
            }


class DateTimeTool(JARVISTool):
    """Tool for date and time operations."""

    def __init__(self, permission_manager: Optional[Any] = None):
        metadata = ToolMetadata(
            name="datetime",
            description="Get current date/time or perform date calculations",
            category=ToolCategory.UTILITY,
            risk_level=ToolRiskLevel.SAFE,
            requires_permission=False,
            capabilities=["datetime", "timezone"]
        )
        super().__init__(metadata, permission_manager)

    async def _execute(
        self,
        operation: str = "now",
        timezone: Optional[str] = None,
        format: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Perform date/time operations.

        Args:
            operation: Operation to perform ('now', 'date', 'time', 'timestamp')
            timezone: Timezone name (optional)
            format: Output format string (optional)
        """
        from datetime import datetime, timezone as tz

        now = datetime.now(tz.utc)

        if operation == "now":
            result = now.isoformat()
        elif operation == "date":
            result = now.date().isoformat()
        elif operation == "time":
            result = now.time().isoformat()
        elif operation == "timestamp":
            result = now.timestamp()
        else:
            result = now.isoformat()

        if format:
            try:
                result = now.strftime(format)
            except ValueError:
                pass

        return {
            "operation": operation,
            "result": result,
            "timezone": "UTC",
            "timestamp": now.timestamp()
        }


# ============================================================================
# Tool Factory
# ============================================================================

class ToolFactory:
    """Factory for creating tools dynamically."""

    @staticmethod
    def create_from_action_handler(
        handler: Callable,
        action_type: str,
        permission_manager: Optional[Any] = None
    ) -> JARVISTool:
        """
        Create a tool from an existing JARVIS action handler.

        Args:
            handler: Action handler function
            action_type: Type of action
            permission_manager: Permission manager instance

        Returns:
            Created tool
        """
        # Infer category from action type
        category_mapping = {
            "handle_notifications": ToolCategory.COMMUNICATION,
            "prepare_meeting": ToolCategory.AUTOMATION,
            "organize_workspace": ToolCategory.UI,
            "security_alert": ToolCategory.SECURITY,
            "respond_message": ToolCategory.COMMUNICATION,
            "cleanup_workspace": ToolCategory.FILE_SYSTEM,
            "minimize_distractions": ToolCategory.UI,
            "routine_automation": ToolCategory.AUTOMATION,
            "handle_urgent_item": ToolCategory.AUTOMATION
        }

        category = category_mapping.get(action_type, ToolCategory.UTILITY)

        # Infer risk level
        risk_mapping = {
            "security_alert": ToolRiskLevel.HIGH,
            "cleanup_workspace": ToolRiskLevel.MEDIUM,
            "organize_workspace": ToolRiskLevel.LOW,
            "handle_notifications": ToolRiskLevel.SAFE
        }

        risk_level = risk_mapping.get(action_type, ToolRiskLevel.LOW)

        metadata = ToolMetadata(
            name=f"action_{action_type}",
            description=f"Execute {action_type} action",
            category=category,
            risk_level=risk_level,
            requires_permission=True,
            capabilities=[action_type, "action_execution"]
        )

        return FunctionTool(
            func=handler,
            metadata=metadata,
            permission_manager=permission_manager
        )

    @staticmethod
    def create_from_config(
        config: Dict[str, Any],
        permission_manager: Optional[Any] = None
    ) -> Optional[JARVISTool]:
        """
        Create a tool from configuration dictionary.

        Args:
            config: Tool configuration
            permission_manager: Permission manager instance

        Returns:
            Created tool or None
        """
        try:
            # Load function from module
            module_path = config.get("module")
            function_name = config.get("function")

            if not module_path or not function_name:
                return None

            module = importlib.import_module(module_path)
            func = getattr(module, function_name)

            metadata = ToolMetadata(
                name=config.get("name", function_name),
                description=config.get("description", ""),
                category=ToolCategory(config.get("category", "utility")),
                risk_level=ToolRiskLevel(config.get("risk_level", "low")),
                requires_permission=config.get("requires_permission", True),
                timeout_seconds=config.get("timeout", 30.0),
                retry_count=config.get("retry_count", 3),
                capabilities=config.get("capabilities", []),
                tags=config.get("tags", [])
            )

            return FunctionTool(
                func=func,
                metadata=metadata,
                permission_manager=permission_manager
            )

        except Exception as e:
            logger.error(f"Failed to create tool from config: {e}")
            return None


# ============================================================================
# Auto-Registration
# ============================================================================

def register_builtin_tools(registry: Optional[ToolRegistry] = None) -> int:
    """
    Register built-in tools.

    Args:
        registry: Tool registry (uses singleton if not provided)

    Returns:
        Number of tools registered
    """
    if registry is None:
        registry = ToolRegistry.get_instance()

    builtin_tools = [
        SystemInfoTool(),
        CalculatorTool(),
        DateTimeTool(),
        FileSystemAgentTool(),
    ]

    for tool in builtin_tools:
        registry.register(tool, replace=True)

    return len(builtin_tools)


def auto_discover_tools(
    base_paths: Optional[List[str]] = None,
    registry: Optional[ToolRegistry] = None
) -> int:
    """
    Auto-discover and register tools from the codebase.

    Args:
        base_paths: Paths to search (defaults to JARVIS tool paths)
        registry: Tool registry (uses singleton if not provided)

    Returns:
        Total tools discovered
    """
    if registry is None:
        registry = ToolRegistry.get_instance()

    if base_paths is None:
        # Default JARVIS tool paths
        base_dir = Path(__file__).parent.parent
        base_paths = [
            str(base_dir / "tools"),
            str(base_dir / "api" / "action_executors.py")
        ]

    total = 0

    for path_str in base_paths:
        path = Path(path_str)

        if path.is_file() and path.suffix == '.py':
            # Single file - derive module path
            parts = path.with_suffix('').parts
            # Find backend index
            try:
                backend_idx = parts.index('backend')
                module_path = '.'.join(parts[backend_idx:])
                total += registry.discover_from_module(module_path)
            except ValueError:
                pass

        elif path.is_dir():
            total += registry.discover_from_directory(path)

    return total


# ============================================================================
# Convenience Functions
# ============================================================================

def get_tool(name: str) -> Optional[JARVISTool]:
    """Get a tool by name from the global registry."""
    return ToolRegistry.get_instance().get(name)


def list_tools() -> List[str]:
    """List all registered tool names."""
    return [t.name for t in ToolRegistry.get_instance().get_all()]


def search_tools(query: str, **kwargs) -> List[JARVISTool]:
    """Search for tools matching a query."""
    return ToolRegistry.get_instance().search(query, **kwargs)


async def execute_tool(name: str, **kwargs) -> Any:
    """Execute a tool by name."""
    tool = get_tool(name)
    if tool is None:
        raise ValueError(f"Tool not found: {name}")
    return await tool.run(**kwargs)
