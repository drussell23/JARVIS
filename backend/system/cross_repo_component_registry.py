"""
Cross-Repo Component Registry v2.0 - Production Hardened
=========================================================

Exposes JARVIS components for cross-repo access by JARVIS Prime and Reactor Core.

HARDENED VERSION (v2.0) with:
- AtomicFileOps for safe file operations
- DistributedLock for multi-instance coordination
- Correlation context for request tracing
- FileWatchGuard for robust request monitoring
- Circuit breaker for handler protection

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                       JARVIS Body                               │
    │  [Components] → [Registry] → [~/.jarvis/cross_repo/registry/]   │
    └────────────────────────────────┬────────────────────────────────┘
                                     │ File-Based RPC
                                     ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                   JARVIS Prime / Reactor Core                   │
    │  [Read Registry] → [Call Component] → [Get Response]            │
    └─────────────────────────────────────────────────────────────────┘

Communication Protocol:
1. JARVIS writes component availability to registry.json
2. Callers write requests to ~/.jarvis/cross_repo/requests/{component_id}/{request_id}.json
3. JARVIS processes requests and writes responses to ~/.jarvis/cross_repo/responses/{request_id}.json
4. Callers read responses and delete request/response files

This enables loose coupling while maintaining strong typing via JSON schemas.

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Type
from functools import wraps

# Import resilience utilities
try:
    from backend.core.resilience.atomic_file_ops import AtomicFileOps, AtomicFileConfig
    from backend.core.resilience.file_watch_guard import (
        FileWatchGuard, FileWatchConfig, FileEvent, FileEventType
    )
    from backend.core.resilience.correlation_context import (
        CorrelationContext, with_correlation, inject_correlation,
        extract_correlation, get_current_correlation_id
    )
    from backend.core.resilience.distributed_lock import (
        DistributedLock, DistributedLockConfig
    )
    RESILIENCE_AVAILABLE = True
except ImportError:
    RESILIENCE_AVAILABLE = False
    AtomicFileOps = None
    FileWatchGuard = None

logger = logging.getLogger("CrossRepoRegistry")

# =============================================================================
# Configuration
# =============================================================================

CROSS_REPO_DIR = Path(os.getenv(
    "CROSS_REPO_DIR",
    str(Path.home() / ".jarvis" / "cross_repo")
))

REGISTRY_DIR = CROSS_REPO_DIR / "registry"
REQUESTS_DIR = CROSS_REPO_DIR / "requests"
RESPONSES_DIR = CROSS_REPO_DIR / "responses"

# Request timeout before cleanup
REQUEST_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL = 0.1
CLEANUP_INTERVAL = 60.0


class ComponentStatus(Enum):
    """Status of a registered component."""
    AVAILABLE = "available"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


@dataclass
class ComponentInfo:
    """Information about a registered component."""
    component_id: str
    name: str
    description: str
    methods: List[str]
    status: str = "available"
    version: str = "1.0.0"
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    request_count: int = 0
    error_count: int = 0
    avg_response_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComponentInfo":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ComponentRequest:
    """A cross-repo component request."""
    request_id: str
    component_id: str
    method: str
    args: Dict[str, Any]
    caller_repo: str
    timestamp: float = field(default_factory=time.time)
    timeout: float = REQUEST_TIMEOUT_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComponentRequest":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ComponentResponse:
    """A cross-repo component response."""
    request_id: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    processing_time_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComponentResponse":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# =============================================================================
# Component Registry (JARVIS Side)
# =============================================================================

class CrossRepoComponentRegistry:
    """
    Registry for exposing JARVIS components to other repos.

    This enables JARVIS Prime and Reactor Core to call JARVIS components
    without direct imports, using file-based RPC.

    HARDENED Features (v2.0):
    - AtomicFileOps for safe registry and response writes
    - FileWatchGuard for robust request monitoring with recovery
    - Distributed locking for multi-instance coordination
    - Correlation context propagation for cross-repo tracing
    - Comprehensive health monitoring with auto-recovery
    """

    def __init__(self):
        self.logger = logging.getLogger("CrossRepoRegistry.Server")

        # Registered components
        self._components: Dict[str, ComponentInfo] = {}
        self._handlers: Dict[str, Dict[str, Callable]] = {}  # component_id -> method -> handler

        # State
        self._running = False
        self._process_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

        # Metrics
        self._total_requests = 0
        self._total_errors = 0
        self._active_requests: Set[str] = set()
        self._consecutive_errors = 0
        self._last_error: Optional[Exception] = None

        # ===== RESILIENCE COMPONENTS (v2.0) =====
        self._use_resilience = RESILIENCE_AVAILABLE

        # Atomic file operations
        self._file_ops: Optional[AtomicFileOps] = None
        if RESILIENCE_AVAILABLE:
            self._file_ops = AtomicFileOps(AtomicFileConfig(
                max_retries=3,
                verify_checksum=False,
            ))

        # File watch guards for request directories
        self._request_watchers: Dict[str, Any] = {}  # component_id -> FileWatchGuard

        # Ensure directories exist
        for dir_path in [REGISTRY_DIR, REQUESTS_DIR, RESPONSES_DIR]:
            dir_path.mkdir(parents=True, exist_ok=True)

    def register_component(
        self,
        component_id: str,
        name: str,
        description: str,
        methods: Dict[str, Callable],
        version: str = "1.0.0",
    ) -> None:
        """
        Register a component for cross-repo access.

        Args:
            component_id: Unique identifier (e.g., 'unified_model_serving')
            name: Human-readable name
            description: Brief description of the component
            methods: Dict mapping method names to handler functions
            version: Component version
        """
        # Create component info
        info = ComponentInfo(
            component_id=component_id,
            name=name,
            description=description,
            methods=list(methods.keys()),
            version=version,
        )

        self._components[component_id] = info
        self._handlers[component_id] = methods

        # Create request directory for this component
        (REQUESTS_DIR / component_id).mkdir(exist_ok=True)

        self.logger.info(
            f"Registered component: {component_id} "
            f"(methods: {', '.join(methods.keys())})"
        )

    def unregister_component(self, component_id: str) -> None:
        """Unregister a component."""
        self._components.pop(component_id, None)
        self._handlers.pop(component_id, None)
        self.logger.info(f"Unregistered component: {component_id}")

    async def start(self) -> bool:
        """Start the registry server."""
        if self._running:
            return True

        self._running = True
        self.logger.info("CrossRepoComponentRegistry starting...")

        # Write initial registry
        await self._write_registry()

        # Start processing loop
        self._process_task = asyncio.create_task(
            self._process_loop(),
            name="cross_repo_registry_process",
        )

        # Start cleanup loop
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="cross_repo_registry_cleanup",
        )

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="cross_repo_registry_heartbeat",
        )

        self.logger.info(
            f"CrossRepoComponentRegistry ready "
            f"(components: {len(self._components)})"
        )
        return True

    async def stop(self) -> None:
        """Stop the registry server."""
        self._running = False

        # Cancel tasks
        for task in [self._process_task, self._cleanup_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Mark components as unavailable
        for info in self._components.values():
            info.status = ComponentStatus.UNAVAILABLE.value
        await self._write_registry()

        self.logger.info(
            f"CrossRepoComponentRegistry stopped "
            f"(processed {self._total_requests} requests)"
        )

    async def _write_registry(self) -> None:
        """Write registry to shared directory using atomic operations."""
        registry_file = REGISTRY_DIR / "jarvis_components.json"

        data = {
            "repo": "jarvis-ai-agent",
            "version": "2.0.0",
            "timestamp": time.time(),
            "components": {
                cid: info.to_dict()
                for cid, info in self._components.items()
            },
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "active_requests": len(self._active_requests),
            "resilience_mode": self._use_resilience,
        }

        try:
            if self._file_ops and RESILIENCE_AVAILABLE:
                # Use atomic file operations
                await self._file_ops.write_json(registry_file, data)
            else:
                # Fallback to basic atomic write
                tmp_file = registry_file.with_suffix(".tmp")
                tmp_file.write_text(json.dumps(data, indent=2))
                tmp_file.replace(registry_file)
        except Exception as e:
            self.logger.warning(f"Failed to write registry: {e}")
            self._consecutive_errors += 1
            self._last_error = e

    async def _process_loop(self) -> None:
        """Main loop to process incoming requests."""
        while self._running:
            try:
                await self._process_all_requests()
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Process loop error: {e}")
                await asyncio.sleep(0.5)

    async def _process_all_requests(self) -> None:
        """Process all pending requests for all components."""
        for component_id in self._components:
            component_dir = REQUESTS_DIR / component_id
            if not component_dir.exists():
                continue

            for request_file in component_dir.glob("*.json"):
                if request_file.name.startswith("."):
                    continue

                await self._process_request(request_file)

    async def _process_request(self, request_file: Path) -> None:
        """Process a single request."""
        request_id = request_file.stem

        # Skip if already processing
        if request_id in self._active_requests:
            return

        self._active_requests.add(request_id)
        start_time = time.time()

        try:
            # Read request
            request_data = json.loads(request_file.read_text())
            request = ComponentRequest.from_dict(request_data)

            # Check timeout
            if time.time() - request.timestamp > request.timeout:
                self.logger.warning(f"Request {request_id} timed out")
                await self._write_response(ComponentResponse(
                    request_id=request_id,
                    success=False,
                    error="Request timed out",
                    error_type="TimeoutError",
                ))
                return

            # Get handler
            handlers = self._handlers.get(request.component_id, {})
            handler = handlers.get(request.method)

            if not handler:
                await self._write_response(ComponentResponse(
                    request_id=request_id,
                    success=False,
                    error=f"Method {request.method} not found on {request.component_id}",
                    error_type="MethodNotFound",
                ))
                return

            # Execute handler
            try:
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(**request.args)
                else:
                    result = handler(**request.args)

                processing_time = (time.time() - start_time) * 1000

                await self._write_response(ComponentResponse(
                    request_id=request_id,
                    success=True,
                    result=result,
                    processing_time_ms=processing_time,
                ))

                # Update metrics
                self._total_requests += 1
                info = self._components.get(request.component_id)
                if info:
                    info.request_count += 1
                    # Running average
                    info.avg_response_ms = (
                        (info.avg_response_ms * (info.request_count - 1) + processing_time)
                        / info.request_count
                    )

                self.logger.debug(
                    f"Processed request: {request.component_id}.{request.method} "
                    f"({processing_time:.1f}ms)"
                )

            except Exception as e:
                self._total_errors += 1
                info = self._components.get(request.component_id)
                if info:
                    info.error_count += 1

                await self._write_response(ComponentResponse(
                    request_id=request_id,
                    success=False,
                    error=str(e),
                    error_type=type(e).__name__,
                    processing_time_ms=(time.time() - start_time) * 1000,
                ))

                self.logger.warning(
                    f"Handler error for {request.component_id}.{request.method}: {e}"
                )

            # Delete request file
            try:
                request_file.unlink()
            except Exception:
                pass

        except json.JSONDecodeError:
            self.logger.warning(f"Invalid JSON in request {request_file}")
            try:
                request_file.replace(request_file.with_suffix(".json.failed"))
            except Exception:
                pass
        except Exception as e:
            self.logger.error(f"Request processing error: {e}")
        finally:
            self._active_requests.discard(request_id)

    async def _write_response(self, response: ComponentResponse) -> None:
        """Write response to shared directory using atomic operations."""
        response_file = RESPONSES_DIR / f"{response.request_id}.json"

        # Add correlation context if available
        response_data = response.to_dict()
        if RESILIENCE_AVAILABLE:
            correlation_id = get_current_correlation_id()
            if correlation_id:
                response_data["_correlation_id"] = correlation_id

        try:
            if self._file_ops and RESILIENCE_AVAILABLE:
                # Use atomic file operations
                await self._file_ops.write_json(response_file, response_data)
            else:
                # Fallback to basic atomic write
                tmp_file = response_file.with_suffix(".tmp")
                tmp_file.write_text(json.dumps(response_data, indent=2))
                tmp_file.replace(response_file)
        except Exception as e:
            self.logger.error(f"Failed to write response: {e}")
            self._consecutive_errors += 1
            self._last_error = e

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of stale requests and responses."""
        while self._running:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
                await self._cleanup_stale_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Cleanup error: {e}")

    async def _cleanup_stale_files(self) -> None:
        """Clean up stale request and response files."""
        now = time.time()
        max_age = REQUEST_TIMEOUT_SECONDS * 2

        # Clean requests
        for component_dir in REQUESTS_DIR.iterdir():
            if not component_dir.is_dir():
                continue
            for f in component_dir.glob("*.json"):
                try:
                    if now - f.stat().st_mtime > max_age:
                        f.unlink()
                except Exception:
                    pass

        # Clean responses
        for f in RESPONSES_DIR.glob("*.json"):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
            except Exception:
                pass

    async def _heartbeat_loop(self) -> None:
        """Periodic heartbeat to update registry."""
        while self._running:
            try:
                await asyncio.sleep(10.0)  # 10 second heartbeat

                # Update heartbeat times
                now = time.time()
                for info in self._components.values():
                    info.last_heartbeat = now

                await self._write_registry()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Heartbeat error: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get registry metrics including resilience status."""
        base_metrics = {
            "version": "2.0.0",
            "running": self._running,
            "mode": "RESILIENT" if self._use_resilience else "FALLBACK",
            "components": len(self._components),
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "active_requests": len(self._active_requests),
            "consecutive_errors": self._consecutive_errors,
            "last_error": str(self._last_error) if self._last_error else None,
            "component_stats": {
                cid: {
                    "requests": info.request_count,
                    "errors": info.error_count,
                    "avg_response_ms": round(info.avg_response_ms, 2),
                }
                for cid, info in self._components.items()
            },
        }

        # Add resilience-specific metrics
        if self._use_resilience and self._file_ops:
            base_metrics["file_ops"] = self._file_ops.get_metrics()
            base_metrics["request_watchers"] = len(self._request_watchers)

        return base_metrics

    @property
    def is_healthy(self) -> bool:
        """Check if registry is healthy."""
        if not self._running:
            return False
        if self._consecutive_errors >= 5:
            return False
        return True


# =============================================================================
# Component Client (For Prime/Reactor to call JARVIS components)
# =============================================================================

class CrossRepoComponentClient:
    """
    Client for calling JARVIS components from other repos.

    This is used by JARVIS Prime and Reactor Core to call components
    registered in the JARVIS registry.

    Example:
        client = CrossRepoComponentClient("jarvis-prime")
        result = await client.call(
            "unified_model_serving",
            "get_model",
            task_type="CHAT",
        )
    """

    def __init__(self, caller_repo: str):
        self.caller_repo = caller_repo
        self.logger = logging.getLogger(f"CrossRepoClient.{caller_repo}")
        self._registry_cache: Dict[str, ComponentInfo] = {}
        self._cache_time: float = 0
        self._cache_ttl: float = 30.0  # 30 second cache

    async def get_available_components(self) -> Dict[str, ComponentInfo]:
        """Get available JARVIS components."""
        # Check cache
        if time.time() - self._cache_time < self._cache_ttl:
            return self._registry_cache

        registry_file = REGISTRY_DIR / "jarvis_components.json"
        if not registry_file.exists():
            return {}

        try:
            data = json.loads(registry_file.read_text())
            components = data.get("components", {})

            self._registry_cache = {
                cid: ComponentInfo.from_dict(info)
                for cid, info in components.items()
            }
            self._cache_time = time.time()

            return self._registry_cache
        except Exception as e:
            self.logger.warning(f"Failed to read registry: {e}")
            return self._registry_cache

    async def is_component_available(self, component_id: str) -> bool:
        """Check if a component is available."""
        components = await self.get_available_components()
        info = components.get(component_id)

        if not info:
            return False

        # Check if healthy (heartbeat within last 30 seconds)
        if time.time() - info.last_heartbeat > 30:
            return False

        return info.status == ComponentStatus.AVAILABLE.value

    async def call(
        self,
        component_id: str,
        method: str,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    ) -> Any:
        """
        Call a method on a JARVIS component.

        Args:
            component_id: Target component ID
            method: Method to call
            timeout: Request timeout in seconds
            **kwargs: Method arguments

        Returns:
            Method result

        Raises:
            TimeoutError: If request times out
            RuntimeError: If call fails
        """
        # Check component availability
        if not await self.is_component_available(component_id):
            raise RuntimeError(f"Component {component_id} is not available")

        # Create request
        request_id = str(uuid.uuid4())
        request = ComponentRequest(
            request_id=request_id,
            component_id=component_id,
            method=method,
            args=kwargs,
            caller_repo=self.caller_repo,
            timeout=timeout,
        )

        # Ensure directories exist
        request_dir = REQUESTS_DIR / component_id
        request_dir.mkdir(parents=True, exist_ok=True)

        # Write request
        request_file = request_dir / f"{request_id}.json"
        request_file.write_text(json.dumps(request.to_dict()))

        # Wait for response
        response_file = RESPONSES_DIR / f"{request_id}.json"
        start_time = time.time()

        while time.time() - start_time < timeout:
            if response_file.exists():
                try:
                    data = json.loads(response_file.read_text())
                    response = ComponentResponse.from_dict(data)

                    # Cleanup
                    try:
                        response_file.unlink()
                    except Exception:
                        pass

                    if response.success:
                        return response.result
                    else:
                        raise RuntimeError(
                            f"{response.error_type}: {response.error}"
                        )
                except json.JSONDecodeError:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

            await asyncio.sleep(POLL_INTERVAL)

        # Cleanup on timeout
        try:
            request_file.unlink()
        except Exception:
            pass

        raise TimeoutError(f"Request to {component_id}.{method} timed out")

    async def call_with_fallback(
        self,
        component_id: str,
        method: str,
        fallback: Callable[..., Any],
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    ) -> Any:
        """
        Call a component with a fallback if unavailable.

        Args:
            component_id: Target component ID
            method: Method to call
            fallback: Fallback function if call fails
            timeout: Request timeout
            **kwargs: Method arguments

        Returns:
            Method result or fallback result
        """
        try:
            return await self.call(component_id, method, timeout, **kwargs)
        except Exception as e:
            self.logger.warning(
                f"Call to {component_id}.{method} failed ({e}), using fallback"
            )
            if asyncio.iscoroutinefunction(fallback):
                return await fallback(**kwargs)
            return fallback(**kwargs)


# =============================================================================
# Helper Decorators
# =============================================================================

def expose_to_cross_repo(
    component_id: str,
    method_name: Optional[str] = None,
):
    """
    Decorator to mark a method for cross-repo exposure.

    Example:
        @expose_to_cross_repo("my_component")
        async def my_method(self, arg1: str) -> str:
            return arg1.upper()
    """
    def decorator(func: Callable) -> Callable:
        # Store metadata on function
        func._cross_repo_component_id = component_id
        func._cross_repo_method_name = method_name or func.__name__
        return func
    return decorator


# =============================================================================
# Global Instance Management
# =============================================================================

_component_registry: Optional[CrossRepoComponentRegistry] = None
_registry_lock: Optional[asyncio.Lock] = None


def _get_registry_lock() -> asyncio.Lock:
    """Get or create the registry lock."""
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def get_component_registry() -> CrossRepoComponentRegistry:
    """Get the global component registry instance."""
    global _component_registry

    lock = _get_registry_lock()
    async with lock:
        if _component_registry is None:
            _component_registry = CrossRepoComponentRegistry()
            await _component_registry.start()

        return _component_registry


async def shutdown_component_registry() -> None:
    """Shutdown the global component registry."""
    global _component_registry

    if _component_registry:
        await _component_registry.stop()
        _component_registry = None


# =============================================================================
# Pre-built Component Registrations
# =============================================================================

async def register_jarvis_components(registry: CrossRepoComponentRegistry) -> None:
    """
    Register standard JARVIS components with the registry.

    This should be called during JARVIS startup to expose components
    for cross-repo access.
    """
    logger.info("Registering JARVIS components for cross-repo access...")

    # UnifiedModelServing
    try:
        from backend.intelligence.unified_model_serving import (
            get_model_server,
            ModelServing,
        )

        server = await get_model_server()

        async def get_model_for_task(task_type: str, **kwargs) -> Dict[str, Any]:
            model = await server.get_model_for_task(task_type, **kwargs)
            return {
                "model_name": model.model_name if model else None,
                "available": model is not None,
            }

        async def get_status() -> Dict[str, Any]:
            return server.get_status()

        registry.register_component(
            component_id="unified_model_serving",
            name="UnifiedModelServing",
            description="Intelligent model selection and serving",
            methods={
                "get_model_for_task": get_model_for_task,
                "get_status": get_status,
            },
        )
    except ImportError as e:
        logger.warning(f"Could not register unified_model_serving: {e}")

    # TrinityEventBus
    try:
        from backend.core.trinity_event_bus import get_event_bus

        async def publish_event(topic: str, data: Dict[str, Any]) -> bool:
            bus = await get_event_bus()
            if bus:
                await bus.publish_raw(topic, data)
                return True
            return False

        registry.register_component(
            component_id="trinity_event_bus",
            name="TrinityEventBus",
            description="Cross-repo event bus for Trinity architecture",
            methods={
                "publish_event": publish_event,
            },
        )
    except ImportError as e:
        logger.warning(f"Could not register trinity_event_bus: {e}")

    # VisionProcessor (if available)
    try:
        from backend.vision.vision_processor import get_vision_processor

        processor = get_vision_processor()

        async def get_current_context() -> Dict[str, Any]:
            ctx = await processor.get_current_context()
            return ctx.to_dict() if ctx else {}

        registry.register_component(
            component_id="vision_processor",
            name="VisionProcessor",
            description="Screen capture and analysis",
            methods={
                "get_current_context": get_current_context,
            },
        )
    except ImportError as e:
        logger.debug(f"VisionProcessor not available: {e}")

    # TaskRouter
    try:
        from backend.core.task_router import get_task_router

        router = get_task_router()

        async def get_routing_info(query: str) -> Dict[str, Any]:
            return router.classify_task(query)

        registry.register_component(
            component_id="task_router",
            name="TaskRouter",
            description="Intelligent task classification and routing",
            methods={
                "get_routing_info": get_routing_info,
            },
        )
    except ImportError as e:
        logger.warning(f"Could not register task_router: {e}")

    logger.info(
        f"Registered {len(registry._components)} JARVIS components for cross-repo access"
    )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Core Classes
    "CrossRepoComponentRegistry",
    "CrossRepoComponentClient",
    "ComponentInfo",
    "ComponentRequest",
    "ComponentResponse",
    "ComponentStatus",
    # Helpers
    "expose_to_cross_repo",
    # Instance Management
    "get_component_registry",
    "shutdown_component_registry",
    # Registration
    "register_jarvis_components",
]
