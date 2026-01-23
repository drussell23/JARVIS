# Enterprise Dependency Injection Container Design

**Date:** 2026-01-22
**Status:** Approved
**Author:** JARVIS Intelligence System

## Executive Summary

This document describes the design for an enterprise-grade dependency injection (DI) container that will replace the manual service instantiation in JARVIS. The container provides:

- **Automatic dependency resolution** with topological sorting
- **Async-native lifecycle management** with health monitoring
- **Cross-repo coordination** across JARVIS, JARVIS-Prime, and Reactor-Core
- **Graceful degradation** with configurable recovery strategies
- **Full observability** with structured logging and metrics

## Problem Statement

### The 4 Bugs in `run_supervisor.py` (Lines 12620-12820)

| # | Bug | Location | Impact |
|---|-----|----------|--------|
| 1 | Parameter mismatch | `CrossRepoCollaborationCoordinator(collaboration_engine=...)` | `TypeError: __init__() got unexpected keyword argument` |
| 2 | Wrong method call | `await coordinator.start()` | `AttributeError: 'CrossRepoCollaborationCoordinator' has no attribute 'start'` |
| 3 | Manual ordering | Services initialized in hardcoded order | Fragile, error-prone maintenance |
| 4 | Uncoordinated factories | `get_*_engine()` functions not integrated | Duplicate singletons possible |

### Root Cause

The coordinators expect `config: Optional[Config]` but receive engine instances with incorrect parameter names. Additionally, coordinators have `initialize()` method but code calls non-existent `start()`.

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    JARVIS Service Container v1.0                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐       │
│   │  Registration   │────▶│   Resolution    │────▶│   Lifecycle     │       │
│   │   (Startup)     │     │  (Dependency)   │     │   (Init/Stop)   │       │
│   └─────────────────┘     └─────────────────┘     └─────────────────┘       │
│          │                        │                        │                │
│          ▼                        ▼                        ▼                │
│   ┌─────────────────────────────────────────────────────────────────┐       │
│   │                    Service Registry                             │       │
│   │  • Singleton Scope (one instance, shared)                       │       │
│   │  • Transient Scope (new instance each resolve)                  │       │
│   │  • Scoped (per-request/session)                                 │       │
│   │  • Lazy Initialization (on first resolve)                       │       │
│   └─────────────────────────────────────────────────────────────────┘       │
│                                                                             │
│   ┌─────────────────────────────────────────────────────────────────┐       │
│   │                    Lifecycle Manager                            │       │
│   │  • Topological sort for init order                              │       │
│   │  • Reverse order for shutdown (LIFO)                            │       │
│   │  • Health monitoring hooks                                      │       │
│   │  • Graceful degradation on failures                             │       │
│   └─────────────────────────────────────────────────────────────────┘       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Service Protocol

```python
from typing import Protocol, List, Type, Optional
from enum import Enum

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

class ServiceState(Enum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    DISPOSED = "disposed"

class AsyncService(Protocol):
    """Protocol that all managed services must implement."""

    async def initialize(self) -> None:
        """Initialize the service (setup resources)."""
        ...

    async def start(self) -> None:
        """Start the service (begin operations)."""
        ...

    async def stop(self) -> None:
        """Stop the service gracefully."""
        ...

    async def health_check(self) -> "HealthReport":
        """Return current health status."""
        ...

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        """Return list of required dependency types."""
        ...
```

### Dependency Resolution

```
Resolution Strategy:
───────────────────

1. Build Dependency Graph
   CollaborationConfig ◀── CollaborationEngine ◀── CrossRepoCollaborationCoordinator
                                                          │
   CrossRepoConfig ─────────────────────────────────────◀─┘

2. Detect Cycles (Tarjan's Algorithm)
   • O(V + E) complexity
   • Reports ALL cycles with clear error messages

3. Topological Sort (Kahn's Algorithm)
   • Stable ordering (deterministic)
   • Identifies parallelizable groups

4. Parallel Initialization
   Level 0: [Configs - no deps]        ← parallel
   Level 1: [Engines - depend on configs]  ← parallel
   Level 2: [Coordinators - depend on engines] ← parallel
```

### Dependency Types

| Type | Description | Use Case |
|------|-------------|----------|
| **Required** | Must resolve before init | Engine → Config |
| **Optional** | None if unavailable | IDE → Collaboration (optional) |
| **Lazy** | Resolved on first access | Cross-repo services |
| **Factory** | Returns factory function | Session-scoped services |

### Error Recovery Strategy

```
Service Classification:
──────────────────────

CRITICAL (fail-fast):
  • ConfigProvider
  • ServiceRegistry
  → System exits with diagnostic

REQUIRED (retry with backoff):
  • Database connections
  • Cross-repo clients
  → Retry 3x, then DEGRADED mode

OPTIONAL (skip and log):
  • CollaborationEngine
  • CodeOwnershipEngine
  → Log warning, continue without
```

### Configuration Injection

```
Priority (highest to lowest):
─────────────────────────────

1. Environment Variables
   COLLAB_SESSION_TIMEOUT=3600

2. Configuration Files
   ~/.jarvis/config.yaml

3. Default Values
   Frozen dataclass defaults
```

### Cross-Repo Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  JARVIS Body    │    │  JARVIS Prime   │    │  Reactor Core   │
│  (Port 8010)    │    │  (Port 8000)    │    │  (Port 8090)    │
├─────────────────┤    ├─────────────────┤    ├─────────────────┤
│ ServiceContainer│    │ ServiceContainer│    │ ServiceContainer│
│ • Local engines │    │ • LLM inference │    │ • Voice engine  │
│ • Coordinators  │    │ • Embedding     │    │ • Vision engine │
└────────┬────────┘    └────────┬────────┘    └────────┬────────┘
         │                      │                      │
         └──────────────────────┼──────────────────────┘
                                │
                                ▼
              ~/.jarvis/registry/services.json
              (Shared service discovery)
```

### Lifecycle Events

| Event | Description |
|-------|-------------|
| `SERVICE_REGISTERED` | Service added to container |
| `SERVICE_INITIALIZING` | Initialize starting |
| `SERVICE_INITIALIZED` | Initialize complete |
| `SERVICE_STARTING` | Start beginning |
| `SERVICE_STARTED` | Start complete, service running |
| `SERVICE_STOPPING` | Stop beginning |
| `SERVICE_STOPPED` | Stop complete |
| `SERVICE_FAILED` | Error during lifecycle |
| `SERVICE_RECOVERED` | Service recovered from failure |
| `SERVICE_HEALTH_CHANGED` | Health status changed |

### Observability

**Structured Logging:**
```json
{
  "timestamp": "2026-01-22T16:30:00.123Z",
  "level": "INFO",
  "logger": "jarvis.di.container",
  "event": "SERVICE_STARTED",
  "service": "CollaborationEngine",
  "duration_ms": 245.3,
  "dependencies_resolved": 2
}
```

**Metrics (Prometheus-compatible):**
```
jarvis_service_init_duration_seconds{service="CollaborationEngine"}
jarvis_service_health_status{service="CollaborationEngine"}
jarvis_container_services_total{state="running"}
```

## Implementation Plan

### Files to Create

```
backend/core/di/
├── __init__.py           # Public API exports
├── protocols.py          # AsyncService protocol, types, enums
├── container.py          # ServiceContainer main class
├── resolution.py         # DependencyResolver, cycle detection
├── lifecycle.py          # LifecycleManager, health monitoring
├── scopes.py             # Scope implementations
├── events.py             # Event system
├── remote.py             # Cross-repo RemoteServiceProxy
├── testing.py            # TestContainer, mocking utilities
└── visualization.py      # Dependency graph export
```

### Files to Modify

```
run_supervisor.py
├── Lines 12620-12820: Replace manual init with container.initialize_all()

backend/intelligence/collaboration_engine.py
├── Update get_collaboration_engine() to delegate to container

backend/intelligence/code_ownership.py
├── Update get_ownership_engine() to delegate to container

backend/intelligence/review_workflow.py
├── Update get_review_workflow_engine() to delegate to container

backend/intelligence/ide_integration.py
├── Update get_ide_integration_engine() to delegate to container
```

### Implementation Order

1. **Phase 1: Core Container** (protocols.py, container.py, scopes.py)
2. **Phase 2: Resolution** (resolution.py with cycle detection)
3. **Phase 3: Lifecycle** (lifecycle.py with health monitoring)
4. **Phase 4: Events** (events.py)
5. **Phase 5: Integration** (update run_supervisor.py)
6. **Phase 6: Backward Compat** (update factory functions)
7. **Phase 7: Cross-Repo** (remote.py)
8. **Phase 8: Testing** (testing.py)
9. **Phase 9: Observability** (visualization.py, metrics)

### Backward Compatibility

Existing factory functions will delegate to container:

```python
def get_collaboration_engine(config=None) -> CollaborationEngine:
    from backend.core.di import get_container
    container = get_container()
    if container.is_initialized:
        return container.resolve(CollaborationEngine)
    # Fallback for standalone usage
    global _collaboration_engine
    if _collaboration_engine is None:
        _collaboration_engine = CollaborationEngine(config)
    return _collaboration_engine
```

## Testing Strategy

### Test Container

```python
async with TestContainer() as container:
    container.register_mock(CollaborationEngine, MockCollaborationEngine())
    coordinator = await container.resolve(CrossRepoCollaborationCoordinator)
    # coordinator receives mock
```

### Isolation Guarantees

- Each test gets fresh container
- Parallel tests safe (no shared singletons)
- Automatic cleanup after test

## Success Criteria

1. `python3 run_supervisor.py` starts without the 4 initialization errors
2. All services initialize in correct dependency order
3. Shutdown occurs in reverse order
4. Health checks report accurate status
5. Cross-repo coordination works seamlessly
6. Existing code continues to work (backward compatible)
7. Tests can easily mock dependencies

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Breaking existing code | Backward-compatible factory functions |
| Circular dependencies | Explicit cycle detection with clear errors |
| Performance overhead | Lazy initialization, caching |
| Complexity increase | Clear documentation, simple API |

## Appendix: Current Bug Locations

### Bug 1: Parameter Mismatch (Line 12642)
```python
# CURRENT (WRONG):
self._collab_coordinator = CrossRepoCollaborationCoordinator(
    collaboration_engine=self._collaboration_engine  # Should be config=
)
```

### Bug 2: Wrong Method (Line 12645)
```python
# CURRENT (WRONG):
await self._collab_coordinator.start()  # Method doesn't exist, should be initialize()
```

### Correct Coordinator Signatures
```python
# From collaboration_engine.py:1274
class CrossRepoCollaborationCoordinator:
    def __init__(self, config: Optional[CollaborationConfig] = None):
        self.config = config or CollaborationConfig.from_env()

    async def initialize(self) -> None:  # NOT start()
        ...
```
