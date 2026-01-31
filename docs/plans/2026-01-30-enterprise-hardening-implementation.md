# Enterprise Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build ComponentRegistry as single source of truth for component criticality, dependencies, and lifecycle, enabling automatic log severity derivation and deterministic startup ordering.

**Architecture:** Two parallel tracks - Track 1 provides immediate log noise relief via surgical fixes, while Track 2 builds the foundational ComponentRegistry infrastructure. Track 1 code becomes a thin bridge that gets replaced once Track 2 completes.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, JSON for state persistence, existing JARVIS logging infrastructure.

---

## Phase 1: Track 1 - Immediate Log Severity Fixes (Tasks 1-4)

### Task 1: Create Log Severity Bridge Module

**Files:**
- Create: `backend/core/log_severity_bridge.py`
- Test: `tests/unit/core/test_log_severity_bridge.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_log_severity_bridge.py
"""Tests for log severity bridge - temporary module until ComponentRegistry exists."""
import pytest
from unittest.mock import patch, MagicMock

class TestComponentCriticality:
    """Test criticality lookup and normalization."""

    def test_normalize_component_name_kebab_case(self):
        from backend.core.log_severity_bridge import _normalize_component_name
        assert _normalize_component_name("jarvis_prime") == "jarvis-prime"
        assert _normalize_component_name("JARVIS_PRIME") == "jarvis-prime"
        assert _normalize_component_name("Jarvis Prime") == "jarvis-prime"
        assert _normalize_component_name("jarvis-prime") == "jarvis-prime"

    def test_get_criticality_returns_default_for_unknown(self):
        from backend.core.log_severity_bridge import _get_criticality
        assert _get_criticality("unknown-component") == "optional"

    def test_get_criticality_returns_required_for_core(self):
        from backend.core.log_severity_bridge import _get_criticality
        assert _get_criticality("jarvis-core") == "required"
        assert _get_criticality("backend") == "required"

    def test_get_criticality_returns_degraded_ok_for_prime(self):
        from backend.core.log_severity_bridge import _get_criticality
        assert _get_criticality("jarvis-prime") == "degraded_ok"
        assert _get_criticality("voice-unlock") == "degraded_ok"

    def test_get_criticality_respects_env_override(self):
        from backend.core.log_severity_bridge import _get_criticality
        import os
        os.environ["REDIS_CRITICALITY"] = "required"
        try:
            assert _get_criticality("redis") == "required"
        finally:
            del os.environ["REDIS_CRITICALITY"]


class TestLogComponentFailure:
    """Test the log_component_failure bridge function."""

    def test_required_component_logs_error(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            log_component_failure("jarvis-core", "Startup failed")
            mock_logger.error.assert_called_once()
            assert "jarvis-core" in str(mock_logger.error.call_args)

    def test_optional_component_logs_info(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            log_component_failure("redis", "Connection refused")
            mock_logger.info.assert_called_once()
            assert "optional" in str(mock_logger.info.call_args)

    def test_degraded_ok_component_logs_warning(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            log_component_failure("jarvis-prime", "GPU not available")
            mock_logger.warning.assert_called_once()

    def test_exception_info_included_when_provided(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            try:
                raise ValueError("Test error")
            except ValueError as e:
                log_component_failure("jarvis-core", "Failed", error=e)

            call_kwargs = mock_logger.error.call_args[1]
            assert "exc_info" in call_kwargs
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/.worktrees/enterprise-hardening && python3 -m pytest tests/unit/core/test_log_severity_bridge.py -v`

Expected: FAIL with "ModuleNotFoundError: No module named 'backend.core.log_severity_bridge'"

**Step 3: Write minimal implementation**

```python
# backend/core/log_severity_bridge.py
"""
Log Severity Bridge - Temporary module for criticality-based logging.

This module provides immediate log noise relief by deriving log severity
from component criticality. It will be replaced by ComponentLogger once
the full ComponentRegistry is implemented.

Usage:
    from backend.core.log_severity_bridge import log_component_failure

    try:
        await connect_redis()
    except Exception as e:
        log_component_failure("redis", "Connection failed", error=e)
"""
import os
import logging
from typing import Optional

logger = logging.getLogger("jarvis.component_bridge")

# Component criticality map
# REQUIRED: System cannot start without this - logs ERROR
# DEGRADED_OK: Can run degraded if unavailable - logs WARNING
# OPTIONAL: Nice to have - logs INFO
COMPONENT_CRITICALITY = {
    # Required - system cannot function without these
    "jarvis-core": "required",
    "backend": "required",

    # Degraded OK - preferred but can fallback
    "jarvis-prime": "degraded_ok",
    "cloud-sql": "degraded_ok",
    "gcp-vm": "degraded_ok",
    "gcp-prewarm": "degraded_ok",
    "voice-unlock": "degraded_ok",

    # Optional - nice to have
    "redis": "optional",
    "reactor-core": "optional",
    "frontend": "optional",
    "trinity": "optional",
    "trinity-indexer": "optional",
    "trinity-bridge": "optional",
    "ouroboros": "optional",
    "uae": "optional",
    "sai": "optional",
    "neural-mesh": "optional",
    "mas": "optional",
    "cai": "optional",
    "docker-manager": "optional",
    "infrastructure-orchestrator": "optional",
    "ipc-hub": "optional",
    "state-manager": "optional",
    "observability": "optional",
    "di-container": "optional",
    "cost-sync": "optional",
    "cost-tracker": "optional",
    "hybrid-router": "optional",
    "heartbeat-system": "optional",
    "knowledge-indexer": "optional",
    "voice-coordinator": "optional",
    "coding-council": "optional",
    "slim": "optional",
    "hollow-client": "optional",
}


def _normalize_component_name(name: str) -> str:
    """Normalize component name to canonical kebab-case.

    Examples:
        "jarvis_prime" -> "jarvis-prime"
        "JARVIS_PRIME" -> "jarvis-prime"
        "Jarvis Prime" -> "jarvis-prime"
    """
    return name.lower().replace("_", "-").replace(" ", "-")


def _get_criticality(canonical: str) -> str:
    """Get criticality for component, checking env override first.

    Environment variable format: {COMPONENT_NAME}_CRITICALITY
    Example: REDIS_CRITICALITY=required
    """
    env_key = f"{canonical.upper().replace('-', '_')}_CRITICALITY"
    override = os.environ.get(env_key)
    if override and override.lower() in ("required", "degraded_ok", "optional"):
        return override.lower()
    return COMPONENT_CRITICALITY.get(canonical, "optional")


def log_component_failure(
    component: str,
    message: str,
    error: Optional[Exception] = None,
    **context
) -> None:
    """Log a component failure at appropriate severity based on criticality.

    This is a temporary bridge function that will be replaced by ComponentLogger
    once the full ComponentRegistry is implemented.

    Args:
        component: Component name (will be normalized)
        message: Failure message
        error: Optional exception to include traceback
        **context: Additional context to include in log
    """
    canonical = _normalize_component_name(component)
    criticality = _get_criticality(canonical)

    log_kwargs = {"extra": context} if context else {}
    if error:
        log_kwargs["exc_info"] = (type(error), error, error.__traceback__)

    if criticality == "required":
        logger.error(f"{canonical}: {message}", **log_kwargs)
    elif criticality == "degraded_ok":
        logger.warning(f"{canonical}: {message}", **log_kwargs)
    else:  # optional
        logger.info(f"{canonical} (optional): {message}", **log_kwargs)


def is_component_required(component: str) -> bool:
    """Check if a component is required for system operation."""
    canonical = _normalize_component_name(component)
    return _get_criticality(canonical) == "required"


def is_component_optional(component: str) -> bool:
    """Check if a component is fully optional."""
    canonical = _normalize_component_name(component)
    return _get_criticality(canonical) == "optional"
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/.worktrees/enterprise-hardening && python3 -m pytest tests/unit/core/test_log_severity_bridge.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/.worktrees/enterprise-hardening
git add backend/core/log_severity_bridge.py tests/unit/core/test_log_severity_bridge.py
git commit -m "feat(core): add log severity bridge for criticality-based logging

Temporary module providing immediate log noise relief by deriving
log severity from component criticality:
- REQUIRED -> ERROR
- DEGRADED_OK -> WARNING
- OPTIONAL -> INFO

Will be replaced by ComponentLogger once ComponentRegistry is complete.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 2: Integrate Bridge into cross_repo_startup_orchestrator.py

**Files:**
- Modify: `backend/supervisor/cross_repo_startup_orchestrator.py`

**Step 1: Find error logging sites to update**

The file has ~60 error logging sites. Focus on the highest-impact ones in `_spawn_service_core()` around lines 16341-16997.

**Step 2: Create a helper import block at the top**

Find line ~50 (after existing imports) and add:

```python
# Log severity bridge for criticality-aware logging
try:
    from backend.core.log_severity_bridge import log_component_failure, is_component_required
except ImportError:
    # Fallback if bridge not available
    def log_component_failure(component, message, error=None, **ctx):
        logger.error(f"{component}: {message}")
    def is_component_required(component):
        return True
```

**Step 3: Update _spawn_service_core error handling**

Find the `_spawn_service_core` method (~line 16350) and update error handling:

**Before (example from ~line 16366):**
```python
logger.error(f"[v137.1] _spawn_service_core({definition.name}): exception: {e}")
```

**After:**
```python
log_component_failure(
    definition.name,
    f"[v137.1] _spawn_service_core exception",
    error=e,
    phase="spawn_core"
)
```

**Step 4: Update pre-spawn validation errors**

Find ~line 16341:

**Before:**
```python
logger.error(f"Cannot spawn {definition.name}: pre-spawn validation failed")
```

**After:**
```python
log_component_failure(
    definition.name,
    "Cannot spawn: pre-spawn validation failed",
    phase="pre_spawn_validation"
)
```

**Step 5: Run existing tests**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/.worktrees/enterprise-hardening && python3 -m pytest tests/integration/ -k "orchestrator" -v --tb=short 2>&1 | head -50`

Expected: Existing tests still pass (or skip gracefully if dependencies missing)

**Step 6: Commit**

```bash
git add backend/supervisor/cross_repo_startup_orchestrator.py
git commit -m "refactor(orchestrator): use log_component_failure for criticality-aware logging

Replace direct logger.error calls with log_component_failure bridge
in _spawn_service_core for optional components like jarvis-prime,
reactor-core, trinity, etc.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 3: Integrate Bridge into run_supervisor.py

**Files:**
- Modify: `run_supervisor.py`

**Step 1: Add import near top of file (after line ~100)**

```python
# Log severity bridge for criticality-aware logging
try:
    from backend.core.log_severity_bridge import log_component_failure, is_component_required
except ImportError:
    def log_component_failure(component, message, error=None, **ctx):
        logger.error(f"{component}: {message}")
    def is_component_required(component):
        return True
```

**Step 2: Update high-volume error sites**

Focus on optional component failures. Search for patterns like:
- `logger.error.*redis`
- `logger.error.*prime`
- `logger.error.*frontend`
- `logger.error.*trinity`

Update each to use `log_component_failure()` with appropriate component name.

**Example update pattern:**

**Before:**
```python
logger.error(f"Failed to connect to Redis: {e}")
```

**After:**
```python
log_component_failure("redis", "Failed to connect", error=e)
```

**Step 3: Keep logger.error for true errors**

Do NOT change:
- Pre-flight failures (invalid config)
- Critical startup blockers
- Import errors / code bugs
- Process crashes

**Step 4: Commit**

```bash
git add run_supervisor.py
git commit -m "refactor(supervisor): use log_component_failure for optional components

Reduce log noise by using criticality-aware logging for optional
components (redis, jarvis-prime, frontend, trinity, etc).
Keep logger.error for true errors and required component failures.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 4: Integrate Bridge into Remaining High-Volume Files

**Files:**
- Modify: `backend/core/proxy/trinity_coordinator.py`
- Modify: `backend/core/gcp_vm_manager.py`
- Modify: `backend/core/cost_tracker.py`

**Step 1: Add import to each file**

Same import block as previous tasks.

**Step 2: Update trinity_coordinator.py**

Trinity is optional. Update all `logger.error` calls to use `log_component_failure("trinity", ...)`.

**Step 3: Update gcp_vm_manager.py**

GCP VM is degraded_ok. Update error calls to use `log_component_failure("gcp-vm", ...)`.

**Step 4: Update cost_tracker.py**

Cost tracker is optional. Update error calls to use `log_component_failure("cost-tracker", ...)`.

**Step 5: Commit**

```bash
git add backend/core/proxy/trinity_coordinator.py backend/core/gcp_vm_manager.py backend/core/cost_tracker.py
git commit -m "refactor(core): apply log_component_failure to trinity, gcp, cost_tracker

Complete Track 1 log severity fixes for remaining high-volume files.
All optional component failures now log at INFO level.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Phase 2: Track 2 - Foundation Build (Tasks 5-14)

### Task 5: Create ComponentRegistry Core Data Model

**Files:**
- Create: `backend/core/component_registry.py`
- Test: `tests/unit/core/test_component_registry.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_component_registry.py
"""Tests for ComponentRegistry - the single source of truth for components."""
import pytest
from dataclasses import FrozenInstanceError

class TestCriticality:
    def test_criticality_values(self):
        from backend.core.component_registry import Criticality
        assert Criticality.REQUIRED.value == "required"
        assert Criticality.DEGRADED_OK.value == "degraded_ok"
        assert Criticality.OPTIONAL.value == "optional"

class TestProcessType:
    def test_process_type_values(self):
        from backend.core.component_registry import ProcessType
        assert ProcessType.IN_PROCESS.value == "in_process"
        assert ProcessType.SUBPROCESS.value == "subprocess"
        assert ProcessType.EXTERNAL_SERVICE.value == "external"

class TestComponentStatus:
    def test_status_values(self):
        from backend.core.component_registry import ComponentStatus
        assert ComponentStatus.PENDING.value == "pending"
        assert ComponentStatus.STARTING.value == "starting"
        assert ComponentStatus.HEALTHY.value == "healthy"
        assert ComponentStatus.DEGRADED.value == "degraded"
        assert ComponentStatus.FAILED.value == "failed"
        assert ComponentStatus.DISABLED.value == "disabled"

class TestDependency:
    def test_hard_dependency(self):
        from backend.core.component_registry import Dependency
        dep = Dependency(component="jarvis-core")
        assert dep.component == "jarvis-core"
        assert dep.soft == False

    def test_soft_dependency(self):
        from backend.core.component_registry import Dependency
        dep = Dependency(component="gcp-prewarm", soft=True)
        assert dep.soft == True

class TestComponentDefinition:
    def test_minimal_definition(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test-component",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.name == "test-component"
        assert defn.criticality == Criticality.OPTIONAL
        assert defn.dependencies == []
        assert defn.startup_timeout == 60.0

    def test_full_definition(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType,
            HealthCheckType, FallbackStrategy, Dependency
        )
        defn = ComponentDefinition(
            name="jarvis-prime",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.SUBPROCESS,
            dependencies=[
                "jarvis-core",
                Dependency("gcp-prewarm", soft=True),
            ],
            provides_capabilities=["local-inference", "llm"],
            health_check_type=HealthCheckType.HTTP,
            health_endpoint="http://localhost:8000/health",
            startup_timeout=120.0,
            retry_max_attempts=3,
            fallback_for_capabilities={"inference": "claude-api"},
            disable_env_var="JARVIS_PRIME_ENABLED",
        )
        assert defn.name == "jarvis-prime"
        assert len(defn.dependencies) == 2
        assert defn.provides_capabilities == ["local-inference", "llm"]

    def test_effective_criticality_no_override(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.effective_criticality == Criticality.OPTIONAL

    def test_effective_criticality_with_env_override(self):
        import os
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType
        )
        defn = ComponentDefinition(
            name="test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            criticality_override_env="TEST_REQUIRED",
        )
        os.environ["TEST_REQUIRED"] = "true"
        try:
            assert defn.effective_criticality == Criticality.REQUIRED
        finally:
            del os.environ["TEST_REQUIRED"]
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_component_registry.py -v`

Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

```python
# backend/core/component_registry.py
"""
ComponentRegistry - Single source of truth for component lifecycle.

This module provides:
- ComponentDefinition: Declares a component's criticality, dependencies, capabilities
- ComponentRegistry: Manages component registration, status tracking, capability queries
- Automatic log severity derivation based on criticality
- Startup DAG construction from dependencies
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any, Optional, Union, Dict, List
from datetime import datetime

logger = logging.getLogger("jarvis.component_registry")


class Criticality(Enum):
    """Component criticality levels determining log severity and startup behavior."""
    REQUIRED = "required"       # System cannot start without this -> ERROR
    DEGRADED_OK = "degraded_ok" # Can run degraded if unavailable -> WARNING
    OPTIONAL = "optional"       # Nice to have -> INFO


class ProcessType(Enum):
    """How the component runs."""
    IN_PROCESS = "in_process"           # Python module, same process
    SUBPROCESS = "subprocess"           # Managed child process
    EXTERNAL_SERVICE = "external"       # External dependency (Redis, CloudSQL)


class HealthCheckType(Enum):
    """Type of health check to perform."""
    HTTP = "http"       # HTTP endpoint check
    TCP = "tcp"         # TCP port check
    CUSTOM = "custom"   # Callback function
    NONE = "none"       # No health check


class FallbackStrategy(Enum):
    """Strategy when component fails to start."""
    BLOCK = "block"                     # Block startup on failure
    CONTINUE = "continue"               # Continue without component
    RETRY_THEN_CONTINUE = "retry"       # Retry N times, then continue


class ComponentStatus(Enum):
    """Runtime status of a component."""
    PENDING = "pending"       # Not yet started
    STARTING = "starting"     # In progress
    HEALTHY = "healthy"       # Running and healthy
    DEGRADED = "degraded"     # Running with reduced capability
    FAILED = "failed"         # Startup failed
    DISABLED = "disabled"     # Explicitly disabled


@dataclass
class Dependency:
    """A dependency on another component."""
    component: str
    soft: bool = False  # If True, failure doesn't block dependent


@dataclass
class ComponentDefinition:
    """Complete definition of a component."""
    name: str
    criticality: Criticality
    process_type: ProcessType

    # Dependencies & capabilities
    dependencies: List[Union[str, Dependency]] = field(default_factory=list)
    provides_capabilities: List[str] = field(default_factory=list)

    # Health checking
    health_check_type: HealthCheckType = HealthCheckType.NONE
    health_endpoint: Optional[str] = None
    health_check_callback: Optional[Callable] = None

    # Subprocess/external config
    repo_path: Optional[str] = None

    # Retry & timeout
    startup_timeout: float = 60.0
    retry_max_attempts: int = 3
    retry_delay_seconds: float = 5.0
    fallback_strategy: FallbackStrategy = FallbackStrategy.CONTINUE

    # Fallback configuration
    fallback_for_capabilities: Dict[str, str] = field(default_factory=dict)
    conservative_skip_priority: int = 50  # Lower = skipped first

    # Environment integration
    disable_env_var: Optional[str] = None
    criticality_override_env: Optional[str] = None

    @property
    def effective_criticality(self) -> Criticality:
        """Get criticality, checking env override first."""
        if self.criticality_override_env:
            override = os.environ.get(self.criticality_override_env, "").lower()
            if override == "true":
                return Criticality.REQUIRED
        return self.criticality

    def is_disabled_by_env(self) -> bool:
        """Check if component is disabled via environment variable."""
        if self.disable_env_var:
            value = os.environ.get(self.disable_env_var, "true").lower()
            return value in ("false", "0", "no", "disabled")
        return False


@dataclass
class ComponentState:
    """Runtime state of a registered component."""
    definition: ComponentDefinition
    status: ComponentStatus = ComponentStatus.PENDING
    started_at: Optional[datetime] = None
    healthy_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    attempt_count: int = 0

    def mark_starting(self):
        self.status = ComponentStatus.STARTING
        self.started_at = datetime.utcnow()
        self.attempt_count += 1

    def mark_healthy(self):
        self.status = ComponentStatus.HEALTHY
        self.healthy_at = datetime.utcnow()
        self.failure_reason = None

    def mark_degraded(self, reason: str):
        self.status = ComponentStatus.DEGRADED
        self.failure_reason = reason

    def mark_failed(self, reason: str):
        self.status = ComponentStatus.FAILED
        self.failed_at = datetime.utcnow()
        self.failure_reason = reason

    def mark_disabled(self, reason: str):
        self.status = ComponentStatus.DISABLED
        self.failure_reason = reason
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_component_registry.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/component_registry.py tests/unit/core/test_component_registry.py
git commit -m "feat(core): add ComponentRegistry data model

Foundation for component lifecycle management:
- Criticality enum (REQUIRED/DEGRADED_OK/OPTIONAL)
- ProcessType enum (IN_PROCESS/SUBPROCESS/EXTERNAL_SERVICE)
- ComponentStatus enum for runtime state
- Dependency with soft flag support
- ComponentDefinition with full configuration
- ComponentState for runtime tracking

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 6: Add ComponentRegistry Class

**Files:**
- Modify: `backend/core/component_registry.py`
- Test: `tests/unit/core/test_component_registry.py`

**Step 1: Add tests for registry class**

```python
# Add to tests/unit/core/test_component_registry.py

class TestComponentRegistry:
    def test_singleton_pattern(self):
        from backend.core.component_registry import get_component_registry
        reg1 = get_component_registry()
        reg2 = get_component_registry()
        assert reg1 is reg2

    def test_register_component(self):
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType, ComponentStatus
        )
        registry = get_component_registry()
        registry._reset_for_testing()

        defn = ComponentDefinition(
            name="test-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        registry.register(defn)

        assert registry.has("test-comp")
        state = registry.get_state("test-comp")
        assert state.status == ComponentStatus.PENDING

    def test_get_by_capability(self):
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )
        registry = get_component_registry()
        registry._reset_for_testing()

        defn = ComponentDefinition(
            name="inference-provider",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.SUBPROCESS,
            provides_capabilities=["inference", "llm"],
        )
        registry.register(defn)

        assert registry.has_capability("inference")
        assert registry.get_provider("inference") == "inference-provider"
        assert not registry.has_capability("vision")

    def test_all_definitions(self):
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )
        registry = get_component_registry()
        registry._reset_for_testing()

        registry.register(ComponentDefinition(
            name="comp1", criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS
        ))
        registry.register(ComponentDefinition(
            name="comp2", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS
        ))

        all_defs = registry.all_definitions()
        assert len(all_defs) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_component_registry.py::TestComponentRegistry -v`

Expected: FAIL - ComponentRegistry class doesn't exist

**Step 3: Add ComponentRegistry class implementation**

```python
# Add to backend/core/component_registry.py after ComponentState class

class ComponentRegistry:
    """
    Central registry for all JARVIS components.

    Provides:
    - Component registration and lookup
    - Capability-based routing
    - Status tracking
    - Singleton pattern for global access
    """

    _instance: Optional['ComponentRegistry'] = None

    def __init__(self):
        self._components: Dict[str, ComponentState] = {}
        self._capabilities: Dict[str, str] = {}  # capability -> component name
        self._initialized = False

    @classmethod
    def get_instance(cls) -> 'ComponentRegistry':
        """Get the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _reset_for_testing(self):
        """Reset registry state for testing. NOT for production use."""
        self._components.clear()
        self._capabilities.clear()
        self._initialized = False

    def register(self, definition: ComponentDefinition) -> ComponentState:
        """Register a component definition."""
        if definition.name in self._components:
            logger.warning(f"Component {definition.name} already registered, updating")

        state = ComponentState(definition=definition)
        self._components[definition.name] = state

        # Index capabilities
        for cap in definition.provides_capabilities:
            if cap in self._capabilities:
                logger.debug(
                    f"Capability {cap} already provided by {self._capabilities[cap]}, "
                    f"now also by {definition.name}"
                )
            self._capabilities[cap] = definition.name

        logger.debug(f"Registered component: {definition.name}")
        return state

    def has(self, name: str) -> bool:
        """Check if a component is registered."""
        return name in self._components

    def get(self, name: str) -> ComponentDefinition:
        """Get component definition by name."""
        if name not in self._components:
            raise KeyError(f"Component not registered: {name}")
        return self._components[name].definition

    def get_state(self, name: str) -> ComponentState:
        """Get component state by name."""
        if name not in self._components:
            raise KeyError(f"Component not registered: {name}")
        return self._components[name]

    def has_capability(self, capability: str) -> bool:
        """Check if a capability is available."""
        if capability not in self._capabilities:
            return False
        provider = self._capabilities[capability]
        state = self._components.get(provider)
        if not state:
            return False
        return state.status in (ComponentStatus.HEALTHY, ComponentStatus.DEGRADED)

    def get_provider(self, capability: str) -> Optional[str]:
        """Get the component name that provides a capability."""
        return self._capabilities.get(capability)

    def all_definitions(self) -> List[ComponentDefinition]:
        """Get all registered component definitions."""
        return [state.definition for state in self._components.values()]

    def all_states(self) -> List[ComponentState]:
        """Get all component states."""
        return list(self._components.values())

    def mark_status(self, name: str, status: ComponentStatus, reason: str = None):
        """Update component status."""
        state = self.get_state(name)
        if status == ComponentStatus.STARTING:
            state.mark_starting()
        elif status == ComponentStatus.HEALTHY:
            state.mark_healthy()
        elif status == ComponentStatus.DEGRADED:
            state.mark_degraded(reason or "Unknown")
        elif status == ComponentStatus.FAILED:
            state.mark_failed(reason or "Unknown")
        elif status == ComponentStatus.DISABLED:
            state.mark_disabled(reason or "Disabled")
        else:
            state.status = status


def get_component_registry() -> ComponentRegistry:
    """Get the global ComponentRegistry instance."""
    return ComponentRegistry.get_instance()
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_component_registry.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/component_registry.py tests/unit/core/test_component_registry.py
git commit -m "feat(core): add ComponentRegistry class with capability routing

Singleton registry providing:
- Component registration and lookup
- Capability-based routing (has_capability, get_provider)
- Status tracking (mark_status)
- All definitions/states enumeration

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 7: Create StartupDAG Module

**Files:**
- Create: `backend/core/startup_dag.py`
- Test: `tests/unit/core/test_startup_dag.py`

**Step 1: Write failing tests**

```python
# tests/unit/core/test_startup_dag.py
"""Tests for StartupDAG - dependency-ordered startup execution."""
import pytest

class TestStartupDAG:
    def test_build_simple_dag(self):
        from backend.core.startup_dag import StartupDAG
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        # A depends on nothing, B depends on A
        registry.register(ComponentDefinition(
            name="comp-a",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))
        registry.register(ComponentDefinition(
            name="comp-b",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-a"],
        ))

        dag = StartupDAG(registry)
        tiers = dag.build()

        # comp-a should be in tier 0, comp-b in tier 1
        assert len(tiers) == 2
        assert "comp-a" in tiers[0]
        assert "comp-b" in tiers[1]

    def test_parallel_components_same_tier(self):
        from backend.core.startup_dag import StartupDAG
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        # A, B, C all independent
        for name in ["comp-a", "comp-b", "comp-c"]:
            registry.register(ComponentDefinition(
                name=name,
                criticality=Criticality.OPTIONAL,
                process_type=ProcessType.IN_PROCESS,
            ))

        dag = StartupDAG(registry)
        tiers = dag.build()

        # All should be in tier 0
        assert len(tiers) == 1
        assert len(tiers[0]) == 3

    def test_cycle_detection(self):
        from backend.core.startup_dag import StartupDAG, CycleDetectedError
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        # A -> B -> C -> A (cycle)
        registry.register(ComponentDefinition(
            name="comp-a",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-c"],
        ))
        registry.register(ComponentDefinition(
            name="comp-b",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-a"],
        ))
        registry.register(ComponentDefinition(
            name="comp-c",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            dependencies=["comp-b"],
        ))

        dag = StartupDAG(registry)
        with pytest.raises(CycleDetectedError) as exc_info:
            dag.build()

        assert "cycle" in str(exc_info.value).lower()

    def test_soft_dependency_handling(self):
        from backend.core.startup_dag import StartupDAG
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType,
            Dependency, get_component_registry
        )

        registry = get_component_registry()
        registry._reset_for_testing()

        registry.register(ComponentDefinition(
            name="gcp-prewarm",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.EXTERNAL_SERVICE,
        ))
        registry.register(ComponentDefinition(
            name="jarvis-prime",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.SUBPROCESS,
            dependencies=[
                Dependency("gcp-prewarm", soft=True),
            ],
        ))

        dag = StartupDAG(registry)
        tiers = dag.build()

        # gcp-prewarm tier 0, jarvis-prime tier 1 (still ordered despite soft)
        assert "gcp-prewarm" in tiers[0]
        assert "jarvis-prime" in tiers[1]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_dag.py -v`

Expected: FAIL - module doesn't exist

**Step 3: Write implementation**

```python
# backend/core/startup_dag.py
"""
StartupDAG - Dependency-ordered startup execution.

Builds a Directed Acyclic Graph from component dependencies and
executes startup in tiers (parallel within tier, sequential between tiers).
"""
from __future__ import annotations

import logging
from typing import List, Dict, Set, Optional, Union
from collections import defaultdict

from backend.core.component_registry import (
    ComponentRegistry, ComponentDefinition, Dependency, ComponentStatus
)

logger = logging.getLogger("jarvis.startup_dag")


class CycleDetectedError(Exception):
    """Raised when a dependency cycle is detected."""
    pass


class DependencyNotFoundError(Exception):
    """Raised when a dependency references an unknown component."""
    pass


class StartupDAG:
    """
    Builds and manages startup order from component dependencies.

    Usage:
        dag = StartupDAG(registry)
        tiers = dag.build()  # Returns [[tier0_components], [tier1_components], ...]
        await dag.execute()  # Runs startup with tier parallelism
    """

    def __init__(self, registry: ComponentRegistry):
        self.registry = registry
        self._graph: Dict[str, List[str]] = {}  # component -> dependencies
        self._tiers: Optional[List[List[str]]] = None

    def build(self) -> List[List[str]]:
        """
        Build startup tiers from component dependencies.

        Returns list of tiers, where each tier is a list of component names
        that can be started in parallel.

        Raises:
            CycleDetectedError: If dependency cycle detected
        """
        # Build dependency graph
        self._graph = {}
        for defn in self.registry.all_definitions():
            deps = []
            for dep in defn.dependencies:
                dep_name = dep.component if isinstance(dep, Dependency) else dep
                deps.append(dep_name)
            self._graph[defn.name] = deps

        # Check for cycles
        cycle = self._detect_cycles()
        if cycle:
            cycle_str = " -> ".join(cycle)
            raise CycleDetectedError(f"Circular dependency detected: {cycle_str}")

        # Build tiers via topological sort
        self._tiers = self._topological_tiers()
        return self._tiers

    def _detect_cycles(self) -> Optional[List[str]]:
        """
        Detect cycles using DFS.

        Returns cycle path if found, None otherwise.
        """
        # Collect all nodes (declared + referenced)
        all_nodes: Set[str] = set(self._graph.keys())
        for deps in self._graph.values():
            all_nodes.update(deps)

        UNVISITED, IN_PROGRESS, VISITED = 0, 1, 2
        state = {name: UNVISITED for name in all_nodes}
        path: List[str] = []

        def dfs(node: str) -> Optional[List[str]]:
            if state[node] == VISITED:
                return None
            if state[node] == IN_PROGRESS:
                cycle_start = path.index(node)
                return path[cycle_start:] + [node]

            state[node] = IN_PROGRESS
            path.append(node)

            for dep in self._graph.get(node, []):
                if cycle := dfs(dep):
                    return cycle

            path.pop()
            state[node] = VISITED
            return None

        for node in all_nodes:
            if state[node] == UNVISITED:
                if cycle := dfs(node):
                    return cycle
        return None

    def _topological_tiers(self) -> List[List[str]]:
        """
        Build tiers using Kahn's algorithm variant.

        Components with no unresolved dependencies go in the current tier.
        """
        # Calculate in-degree (number of dependencies)
        in_degree: Dict[str, int] = defaultdict(int)
        all_nodes: Set[str] = set(self._graph.keys())

        for deps in self._graph.values():
            all_nodes.update(deps)

        for node in all_nodes:
            in_degree[node] = 0

        for node, deps in self._graph.items():
            in_degree[node] = len(deps)

        tiers: List[List[str]] = []
        remaining = set(all_nodes)

        while remaining:
            # Find all nodes with no remaining dependencies
            tier = [
                node for node in remaining
                if in_degree[node] == 0
            ]

            if not tier:
                # This shouldn't happen if cycle detection passed
                raise CycleDetectedError("Unable to resolve dependencies")

            tiers.append(sorted(tier))  # Sort for determinism

            # Remove this tier and update in-degrees
            for node in tier:
                remaining.remove(node)
                # Decrease in-degree of dependents
                for other, deps in self._graph.items():
                    if node in deps and other in remaining:
                        in_degree[other] -= 1

        return tiers

    def get_tier(self, component: str) -> int:
        """Get the tier number for a component."""
        if self._tiers is None:
            self.build()
        for i, tier in enumerate(self._tiers):
            if component in tier:
                return i
        return -1
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_startup_dag.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/startup_dag.py tests/unit/core/test_startup_dag.py
git commit -m "feat(core): add StartupDAG for dependency-ordered startup

Topological sort of components into parallel execution tiers:
- Cycle detection with clear error messages
- Soft dependency support
- Deterministic ordering within tiers

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 8: Create ComponentLogger Module

**Files:**
- Create: `backend/core/component_logger.py`
- Test: `tests/unit/core/test_component_logger.py`

**Step 1: Write failing tests**

```python
# tests/unit/core/test_component_logger.py
"""Tests for ComponentLogger - registry-aware logging."""
import pytest
from unittest.mock import patch, MagicMock

class TestComponentLogger:
    def test_failure_logs_error_for_required(self):
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="critical-comp",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("critical-comp", registry)

        with patch.object(logger._logger, 'error') as mock_error:
            logger.failure("Something broke")
            mock_error.assert_called_once()

    def test_failure_logs_warning_for_degraded_ok(self):
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="degradable-comp",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("degradable-comp", registry)

        with patch.object(logger._logger, 'warning') as mock_warning:
            logger.failure("GPU not available")
            mock_warning.assert_called_once()

    def test_failure_logs_info_for_optional(self):
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="optional-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("optional-comp", registry)

        with patch.object(logger._logger, 'info') as mock_info:
            logger.failure("Not connected")
            mock_info.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_component_logger.py -v`

Expected: FAIL

**Step 3: Write implementation**

```python
# backend/core/component_logger.py
"""
ComponentLogger - Registry-aware logging with automatic severity derivation.

Replaces the temporary log_severity_bridge once ComponentRegistry is in use.
"""
from __future__ import annotations

import logging
from typing import Optional, Any, Dict

from backend.core.component_registry import (
    ComponentRegistry, Criticality, get_component_registry
)


class ComponentLogger:
    """
    Logger that derives severity from ComponentRegistry.

    Usage:
        logger = ComponentLogger("jarvis-prime", registry)
        logger.failure("GPU not available")  # Logs at WARNING (DEGRADED_OK)
        logger.info("Model loaded")          # Always INFO
    """

    def __init__(self, component_name: str, registry: Optional[ComponentRegistry] = None):
        self.component = component_name
        self.registry = registry or get_component_registry()
        self._logger = logging.getLogger(f"jarvis.{component_name}")

    def _get_criticality(self) -> Criticality:
        """Get effective criticality for this component."""
        try:
            defn = self.registry.get(self.component)
            return defn.effective_criticality
        except KeyError:
            # Component not registered, default to optional
            return Criticality.OPTIONAL

    def failure(
        self,
        message: str,
        error: Optional[Exception] = None,
        **context: Any
    ) -> None:
        """
        Log a failure at appropriate severity based on criticality.

        REQUIRED -> ERROR
        DEGRADED_OK -> WARNING
        OPTIONAL -> INFO
        """
        criticality = self._get_criticality()

        log_kwargs: Dict[str, Any] = {}
        if context:
            log_kwargs["extra"] = context
        if error:
            log_kwargs["exc_info"] = (type(error), error, error.__traceback__)

        full_message = f"{self.component}: {message}"

        if criticality == Criticality.REQUIRED:
            self._logger.error(full_message, **log_kwargs)
        elif criticality == Criticality.DEGRADED_OK:
            self._logger.warning(full_message, **log_kwargs)
        else:
            self._logger.info(f"{full_message} (optional)", **log_kwargs)

    def startup_failed(self, reason: str, error: Optional[Exception] = None) -> None:
        """Convenience for startup failures."""
        self.failure(f"Startup failed: {reason}", error=error, phase="startup")

    def health_check_failed(self, reason: str) -> None:
        """Convenience for health check failures."""
        self.failure(f"Health check failed: {reason}", phase="health_check")

    # Standard logging methods (always use stated level)
    def debug(self, message: str, **context: Any) -> None:
        self._logger.debug(f"{self.component}: {message}", extra=context or None)

    def info(self, message: str, **context: Any) -> None:
        self._logger.info(f"{self.component}: {message}", extra=context or None)

    def warning(self, message: str, **context: Any) -> None:
        self._logger.warning(f"{self.component}: {message}", extra=context or None)

    def error(self, message: str, **context: Any) -> None:
        self._logger.error(f"{self.component}: {message}", extra=context or None)


def get_component_logger(component_name: str) -> ComponentLogger:
    """Factory function for ComponentLogger."""
    return ComponentLogger(component_name)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_component_logger.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/component_logger.py tests/unit/core/test_component_logger.py
git commit -m "feat(core): add ComponentLogger with registry-aware severity

Replaces log_severity_bridge with full registry integration:
- Automatic severity from component criticality
- Convenience methods (startup_failed, health_check_failed)
- Standard logging methods for explicit severity

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 9: Create StartupContext Module

**Files:**
- Create: `backend/core/startup_context.py`
- Test: `tests/unit/core/test_startup_context.py`

**Step 1: Write failing tests**

```python
# tests/unit/core/test_startup_context.py
"""Tests for StartupContext - crash history and recovery state."""
import pytest
import tempfile
import json
from pathlib import Path
from datetime import datetime, timedelta

class TestCrashHistory:
    def test_record_and_count_crashes(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))

            # Record some crashes
            history.record_crash(1, "segfault")
            history.record_crash(1, "oom")

            assert history.crashes_in_window() == 2

    def test_crashes_outside_window_not_counted(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))

            # Manually write an old crash
            old_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
            with open(history.history_file, "w") as f:
                f.write(json.dumps({"timestamp": old_time, "exit_code": 1, "reason": "old"}) + "\n")

            # Only crashes in last hour should count
            assert history.crashes_in_window(timedelta(hours=1)) == 0

class TestStartupContext:
    def test_is_recovery_startup_after_crash(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=1,  # crash
            crash_count_recent=1,
        )
        assert ctx.is_recovery_startup

    def test_not_recovery_after_clean_shutdown(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=0,  # clean
            crash_count_recent=0,
        )
        assert not ctx.is_recovery_startup

    def test_needs_conservative_startup_after_multiple_crashes(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=1,
            crash_count_recent=3,  # threshold
        )
        assert ctx.needs_conservative_startup

    def test_no_conservative_startup_after_single_crash(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=1,
            crash_count_recent=1,
        )
        assert not ctx.needs_conservative_startup
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_context.py -v`

Expected: FAIL

**Step 3: Write implementation**

```python
# backend/core/startup_context.py
"""
StartupContext - Crash history and recovery state management.

Tracks previous run state to inform recovery decisions.
Integrates with existing shutdown_hook infrastructure.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("jarvis.startup_context")

DEFAULT_STATE_DIR = Path("~/.jarvis/state").expanduser()


class CrashHistory:
    """Persists and queries crash events."""

    DEFAULT_WINDOW = timedelta(hours=1)

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        self.history_file = state_dir / "crash_history.jsonl"

    def record_crash(self, exit_code: int, reason: str) -> None:
        """Record a crash event."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "exit_code": exit_code,
            "reason": reason,
        }

        with open(self.history_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        logger.debug(f"Recorded crash: exit_code={exit_code}, reason={reason}")

    def crashes_in_window(self, window: Optional[timedelta] = None) -> int:
        """Count crashes within the time window."""
        window = window or self.DEFAULT_WINDOW
        cutoff = datetime.utcnow() - window

        count = 0
        for entry in self._read_entries():
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts > cutoff:
                    count += 1
            except (KeyError, ValueError):
                continue

        return count

    def _read_entries(self) -> List[Dict[str, Any]]:
        """Read all crash entries."""
        if not self.history_file.exists():
            return []

        entries = []
        with open(self.history_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries


@dataclass
class StartupContext:
    """
    Information about previous run, used to inform recovery decisions.

    Exit code semantics:
    - 0: Clean shutdown
    - 1: Crash
    - 100: Update requested
    - 101: Rollback requested
    - 102: Restart requested
    """

    previous_exit_code: Optional[int] = None
    previous_exit_reason: Optional[str] = None
    crash_count_recent: int = 0
    last_successful_startup: Optional[datetime] = None
    state_markers: Dict[str, Any] = field(default_factory=dict)

    CRASH_THRESHOLD = 3  # Crashes before conservative startup

    @property
    def is_recovery_startup(self) -> bool:
        """Check if this is a recovery startup (after crash)."""
        if self.previous_exit_code is None:
            return False
        # Not a recovery if clean exit or controlled restart
        return self.previous_exit_code not in (0, 100, 101, 102)

    @property
    def needs_conservative_startup(self) -> bool:
        """Check if we should skip optional components (repeated crashes)."""
        return self.crash_count_recent >= self.CRASH_THRESHOLD

    @classmethod
    def load(cls, state_dir: Path = DEFAULT_STATE_DIR) -> 'StartupContext':
        """Load context from state files."""
        last_run_file = state_dir / "last_run.json"
        crash_history = CrashHistory(state_dir)

        previous_exit_code = None
        previous_exit_reason = None
        last_successful = None
        state_markers = {}

        if last_run_file.exists():
            try:
                data = json.loads(last_run_file.read_text())
                previous_exit_code = data.get("exit_code")
                previous_exit_reason = data.get("exit_reason")
                if data.get("last_successful_startup"):
                    last_successful = datetime.fromisoformat(
                        data["last_successful_startup"]
                    )
                state_markers = data.get("state_markers", {})
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Failed to load last_run.json: {e}")

        return cls(
            previous_exit_code=previous_exit_code,
            previous_exit_reason=previous_exit_reason,
            crash_count_recent=crash_history.crashes_in_window(),
            last_successful_startup=last_successful,
            state_markers=state_markers,
        )

    def save(self, state_dir: Path = DEFAULT_STATE_DIR, exit_code: int = 0,
             exit_reason: str = "normal") -> None:
        """Save context to state file."""
        state_dir.mkdir(parents=True, exist_ok=True)
        last_run_file = state_dir / "last_run.json"

        data = {
            "timestamp": datetime.utcnow().isoformat(),
            "exit_code": exit_code,
            "exit_reason": exit_reason,
            "state_markers": self.state_markers,
        }

        if exit_code == 0:
            data["last_successful_startup"] = datetime.utcnow().isoformat()

        last_run_file.write_text(json.dumps(data, indent=2))
        logger.debug(f"Saved startup context: exit_code={exit_code}")
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_startup_context.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/startup_context.py tests/unit/core/test_startup_context.py
git commit -m "feat(core): add StartupContext for crash history and recovery

Tracks previous run state:
- Crash history with rolling window
- is_recovery_startup detection
- needs_conservative_startup after repeated crashes
- State persistence via last_run.json

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Task 10: Create StartupLock Module

**Files:**
- Create: `backend/core/startup_lock.py`
- Test: `tests/unit/core/test_startup_lock.py`

**Step 1: Write failing tests**

```python
# tests/unit/core/test_startup_lock.py
"""Tests for StartupLock - prevents concurrent supervisor runs."""
import pytest
import tempfile
import os
from pathlib import Path

class TestStartupLock:
    def test_acquire_succeeds_when_no_lock(self):
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            assert lock.acquire()
            lock.release()

    def test_acquire_writes_pid(self):
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            lock.acquire()

            pid = lock.lock_file.read_text().strip()
            assert pid == str(os.getpid())

            lock.release()

    def test_second_acquire_fails(self):
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock1 = StartupLock(state_dir=Path(tmpdir))
            lock2 = StartupLock(state_dir=Path(tmpdir))

            assert lock1.acquire()
            assert not lock2.acquire()  # Should fail

            lock1.release()

    def test_stale_lock_recovered(self):
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "supervisor.lock"
            lock_file.parent.mkdir(parents=True, exist_ok=True)

            # Write a fake stale lock (non-existent PID)
            lock_file.write_text("999999999")  # Very unlikely to exist

            lock = StartupLock(state_dir=Path(tmpdir))
            # Should detect stale and acquire successfully
            assert lock.acquire()
            lock.release()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_lock.py -v`

Expected: FAIL

**Step 3: Write implementation**

```python
# backend/core/startup_lock.py
"""
StartupLock - Prevents concurrent supervisor runs.

Uses file locking with stale lock detection.
"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.startup_lock")

DEFAULT_STATE_DIR = Path("~/.jarvis/state").expanduser()


class StartupLock:
    """
    File-based lock to prevent concurrent supervisor runs.

    Handles stale locks from crashed processes.
    """

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        self.lock_file = state_dir / "supervisor.lock"
        self._fd: Optional[int] = None
        self._file = None

    def acquire(self) -> bool:
        """
        Attempt to acquire the lock.

        Returns True if lock acquired, False if another instance running.
        Handles stale locks from dead processes.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._file = open(self.lock_file, "w")
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Write our PID
            self._file.write(str(os.getpid()))
            self._file.flush()

            logger.debug(f"Acquired startup lock (PID {os.getpid()})")
            return True

        except BlockingIOError:
            # Lock held by another process
            if self._file:
                self._file.close()
                self._file = None

            # Check for stale lock
            if self._is_stale_lock():
                logger.info("Detected stale lock from dead process, removing")
                try:
                    self.lock_file.unlink()
                except OSError:
                    pass
                return self.acquire()  # Retry once

            existing_pid = self._read_lock_pid()
            logger.error(f"Another supervisor already running (PID {existing_pid})")
            return False

        except Exception as e:
            logger.error(f"Failed to acquire startup lock: {e}")
            if self._file:
                self._file.close()
                self._file = None
            return False

    def release(self) -> None:
        """Release the lock."""
        if self._file and not self._file.closed:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
                logger.debug("Released startup lock")
            except Exception as e:
                logger.warning(f"Error releasing lock: {e}")
            finally:
                self._file = None

    def _is_stale_lock(self) -> bool:
        """Check if the lock holder is still running."""
        pid = self._read_lock_pid()
        if pid is None:
            return True  # Can't read PID, assume stale

        return not self._is_pid_running(pid)

    def _read_lock_pid(self) -> Optional[int]:
        """Read PID from lock file."""
        try:
            content = self.lock_file.read_text().strip()
            return int(content)
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        """Check if a process is running."""
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return True
        except OSError:
            return False

    def __enter__(self) -> 'StartupLock':
        if not self.acquire():
            raise RuntimeError("Failed to acquire startup lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_startup_lock.py -v`

Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/startup_lock.py tests/unit/core/test_startup_lock.py
git commit -m "feat(core): add StartupLock to prevent concurrent supervisors

File-based locking with:
- Stale lock detection (dead process cleanup)
- Context manager support
- PID file for debugging

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Tasks 11-14: Remaining Foundation Components

Due to length, I'll summarize the remaining tasks:

**Task 11: Create HealthContracts Module**
- File: `backend/core/health_contracts.py`
- Test: `tests/unit/core/test_health_contracts.py`
- Implements: HealthReport, HealthStatus, SystemHealthAggregator

**Task 12: Create RecoveryEngine Module**
- File: `backend/core/recovery_engine.py`
- Test: `tests/unit/core/test_recovery_engine.py`
- Implements: ErrorClassifier, RecoveryStrategy, RecoveryEngine

**Task 13: Create StartupSummary Module**
- File: `backend/core/startup_summary.py`
- Test: `tests/unit/core/test_startup_summary.py`
- Implements: StartupSummary formatting and output

**Task 14: Create Default Component Definitions**
- File: `backend/core/default_components.py`
- Implements: Pre-defined ComponentDefinitions for all JARVIS components
- Includes: jarvis-core, jarvis-prime, reactor-core, redis, trinity, etc.

---

## Phase 3: Integration (Tasks 15-18)

### Task 15: Integrate ComponentRegistry into run_supervisor.py

**Files:**
- Modify: `run_supervisor.py` (line ~25821 in main())

**Integration:**
```python
# At start of main()
from backend.core.component_registry import get_component_registry
from backend.core.default_components import register_default_components
from backend.core.startup_lock import StartupLock
from backend.core.startup_context import StartupContext

async def main() -> int:
    # Acquire startup lock
    lock = StartupLock()
    if not lock.acquire():
        return 1

    try:
        # Load startup context
        context = StartupContext.load()

        # Initialize registry with defaults
        registry = get_component_registry()
        register_default_components(registry)

        # Check for conservative startup
        if context.needs_conservative_startup:
            logger.warning("Multiple recent crashes - using conservative startup")
            # Skip optional components

        # ... rest of startup
    finally:
        lock.release()
```

### Task 16: Integrate StartupDAG into Orchestrator

**Files:**
- Modify: `backend/supervisor/cross_repo_startup_orchestrator.py`

### Task 17: Replace log_severity_bridge with ComponentLogger

**Files:**
- All files modified in Tasks 2-4

### Task 18: Add StartupSummary Output

**Files:**
- Modify: `run_supervisor.py`
- Emit summary at end of startup

---

## Verification Checklist

After completing all tasks:

- [ ] `python3 -m pytest tests/unit/core/ -v` - All new tests pass
- [ ] `python3 run_supervisor.py` - Startup completes with summary
- [ ] Logs show appropriate severity (INFO for optional, ERROR for required)
- [ ] Multiple startup attempts blocked by lock
- [ ] Crash recovery detected on restart after kill -9
- [ ] Conservative startup activates after 3 crashes

---

## Rollback Plan

If issues arise:

1. **Track 1 rollback:** Revert log_severity_bridge changes, restore original logger.error calls
2. **Track 2 rollback:** ComponentRegistry is additive - simply don't call it from main()
3. **Full rollback:** `git revert` to pre-implementation commit
