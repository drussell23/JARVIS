# Enterprise-Grade System Unification & Hardening

**Date:** 2026-01-30
**Status:** Approved for Implementation
**Approach:** Parallel Tracks (Quick Wins + Foundation)

---

## Executive Summary

This design elevates the JARVIS ecosystem into an enterprise-grade, production-ready architecture. The core innovation is a **ComponentRegistry** that serves as the single source of truth for all components, their criticality, dependencies, and runtime status. This enables automatic log severity derivation, deterministic startup ordering, capability-based routing, and graceful degradation.

### Key Outcomes

1. **Log noise reduction** - Optional component failures log at INFO, not ERROR
2. **Deterministic startup** - DAG-based ordering replaces implicit timing
3. **Cross-repo coordination** - Unified lifecycle management for JARVIS, Prime, Reactor
4. **Graceful degradation** - Capability fallbacks, conservative startup after crashes
5. **Observable startup** - Single summary showing what started, what didn't, why

---

## Architecture Overview

### Core Components

| Component | Purpose | File |
|-----------|---------|------|
| **ComponentRegistry** | Single source of truth for all components | `component_registry.py` |
| **StartupDAG** | Dependency graph, tier-based parallel execution | `startup_dag.py` |
| **ComponentLogger** | Auto-derives log severity from criticality | `component_logger.py` |
| **HealthContract** | Standardized health reporting | `health_contracts.py` |
| **RecoveryEngine** | Error classification, fallback routing | `recovery_engine.py` |
| **StartupContext** | Crash history, recovery state | `startup_context.py` |
| **SubprocessManager** | Cross-repo process lifecycle | `subprocess_manager.py` |
| **StartupSummary** | Human + JSON summary at boot | `startup_summary.py` |

### New File Structure

```
backend/core/
├── component_registry.py      # ComponentDefinition, ComponentRegistry
├── component_logger.py        # ComponentLogger (severity from registry)
├── startup_dag.py             # StartupDAG, tier-based execution
├── health_contracts.py        # HealthReport, SystemHealthAggregator
├── recovery_engine.py         # RecoveryEngine, ErrorClassifier, DegradationCoordinator
├── startup_context.py         # StartupContext, CrashHistory, StatePaths
├── subprocess_manager.py      # SubprocessManager (cross-repo lifecycle)
└── startup_summary.py         # StartupSummary formatting and output
```

---

## Component Registry

### Data Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any
from datetime import datetime

class Criticality(Enum):
    REQUIRED = "required"       # System cannot start without this
    OPTIONAL = "optional"       # Nice to have, log INFO on failure
    DEGRADED_OK = "degraded_ok" # Can run in degraded mode if unavailable

class ProcessType(Enum):
    IN_PROCESS = "in_process"           # Python module, same process
    SUBPROCESS = "subprocess"           # Managed child process
    EXTERNAL_SERVICE = "external"       # External dependency (Redis, CloudSQL)

class HealthCheckType(Enum):
    HTTP = "http"
    TCP = "tcp"
    CUSTOM = "custom"
    NONE = "none"

class FallbackStrategy(Enum):
    BLOCK = "block"                     # Block startup on failure
    CONTINUE = "continue"               # Continue without component
    RETRY_THEN_CONTINUE = "retry"       # Retry N times, then continue

class ComponentStatus(Enum):
    PENDING = "pending"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"

@dataclass
class Dependency:
    """A dependency with optional soft flag."""
    component: str
    soft: bool = False  # If True, failure doesn't block dependent

@dataclass
class ComponentDefinition:
    name: str
    criticality: Criticality
    process_type: ProcessType

    # Dependencies & capabilities
    dependencies: list[str | Dependency] = field(default_factory=list)
    provides_capabilities: list[str] = field(default_factory=list)

    # Health checking
    health_check_type: HealthCheckType = HealthCheckType.NONE
    health_endpoint: str | None = None
    health_check_callback: Callable | None = None

    # Subprocess/external config
    repo_path: str | None = None

    # Retry & timeout
    startup_timeout: float = 60.0
    retry_max_attempts: int = 3
    retry_delay_seconds: float = 5.0
    fallback_strategy: FallbackStrategy = FallbackStrategy.CONTINUE

    # Fallback configuration
    fallback_for_capabilities: dict[str, str] = field(default_factory=dict)
    conservative_skip_priority: int = 50  # Lower = skipped first

    # Environment integration
    disable_env_var: str | None = None
    criticality_override_env: str | None = None

    @property
    def effective_criticality(self) -> Criticality:
        """Check env override, then return base criticality."""
        if self.criticality_override_env:
            override = os.environ.get(self.criticality_override_env, "").lower()
            if override == "true":
                return Criticality.REQUIRED
        return self.criticality
```

### Integration Strategy

**Wrap Then Migrate:**

1. Registry wraps existing definitions initially (TrinityCoordinator, cross_repo configs)
2. New code uses registry API: `registry.is_available("inference")`
3. Gradual migration of definitions into registry
4. Eventually deprecate old configs

---

## Logging Severity Derivation

### Core Principle

```
Component criticality → Log severity on failure

REQUIRED     → ERROR (blocks startup, needs attention)
DEGRADED_OK  → WARNING (system running but impaired)
OPTIONAL     → INFO (nice to know, not actionable)
```

### ComponentLogger Implementation

```python
class ComponentLogger:
    """Logger that derives severity from registry."""

    def __init__(self, component_name: str, registry: ComponentRegistry):
        self.component = component_name
        self.registry = registry
        self._logger = logging.getLogger(f"jarvis.{component_name}")

    def failure(self, message: str, error: Exception | None = None, **context):
        """Log a failure at appropriate severity based on criticality."""
        definition = self.registry.get(self.component)

        log_kwargs = {"extra": context}
        if error:
            log_kwargs["exc_info"] = (type(error), error, error.__traceback__)

        if definition.effective_criticality == Criticality.REQUIRED:
            self._logger.error(message, **log_kwargs)
        elif definition.effective_criticality == Criticality.DEGRADED_OK:
            self._logger.warning(message, **log_kwargs)
        else:
            self._logger.info(message, **log_kwargs)
```

### Track 1 Bridge (Immediate Fix)

```python
# Temporary bridge until ComponentRegistry exists

COMPONENT_CRITICALITY = {
    # Required
    "jarvis-core": "required",
    "backend": "required",

    # Degraded OK
    "jarvis-prime": "degraded_ok",
    "cloud-sql": "degraded_ok",
    "gcp-vm": "degraded_ok",
    "voice-unlock": "degraded_ok",

    # Optional
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
    "hybrid-router": "optional",
    "heartbeat-system": "optional",
    "knowledge-indexer": "optional",
    "voice-coordinator": "optional",
}

def _normalize_component_name(name: str) -> str:
    """Normalize to canonical kebab-case."""
    return name.lower().replace("_", "-").replace(" ", "-")

def _get_criticality(canonical: str) -> str:
    """Get criticality with env override support."""
    env_key = f"{canonical.upper().replace('-', '_')}_CRITICALITY"
    override = os.environ.get(env_key)
    if override and override.lower() in ("required", "degraded_ok", "optional"):
        return override.lower()
    return COMPONENT_CRITICALITY.get(canonical, "optional")

def log_component_failure(component: str, message: str, error: Exception | None = None):
    """Temporary bridge until ComponentRegistry exists."""
    canonical = _normalize_component_name(component)
    criticality = _get_criticality(canonical)

    log_kwargs = {}
    if error:
        log_kwargs["exc_info"] = (type(error), error, error.__traceback__)

    if criticality == "required":
        logger.error(f"{canonical}: {message}", **log_kwargs)
    elif criticality == "degraded_ok":
        logger.warning(f"{canonical}: {message}", **log_kwargs)
    else:
        logger.info(f"{canonical} (optional): {message}")
```

### Error Categorization

**USE `log_component_failure` for:**
- Component initialization failures (Redis connect, jarvis-prime start)
- Health check failures for optional components
- Fallback activations

**KEEP `logger.error` for:**
- Pre-flight failures (invalid config, missing env vars)
- Critical startup blockers (port already in use)
- Import errors / code bugs
- Process crashes (unexpected death)

---

## Startup DAG

### DAG Construction

```python
class StartupDAG:
    """Builds and executes startup order from component dependencies."""

    def __init__(self, registry: ComponentRegistry):
        self.registry = registry
        self._graph: dict[str, list[str | Dependency]] = {}

    def build(self) -> list[list[str]]:
        """Returns components grouped by startup tier (parallel within tier)."""
        # Validate no cycles
        if cycle := self._detect_cycles():
            raise StartupError(f"Circular dependency: {' -> '.join(cycle)}")

        # Build tiers via topological sort
        # Tier 0: Components with no dependencies
        # Tier N: Components depending only on Tier 0..N-1
        return self._topological_tiers()

    def _detect_cycles(self) -> list[str] | None:
        """Returns cycle path if found, None if DAG is valid."""
        # Collect ALL nodes (declared + referenced as dependencies)
        all_nodes = set(self._graph.keys())
        for deps in self._graph.values():
            for dep in deps:
                dep_name = dep.component if isinstance(dep, Dependency) else dep
                all_nodes.add(dep_name)

        UNVISITED, IN_PROGRESS, VISITED = 0, 1, 2
        state = {name: UNVISITED for name in all_nodes}
        path = []

        def dfs(node: str) -> list[str] | None:
            if state[node] == VISITED:
                return None
            if state[node] == IN_PROGRESS:
                cycle_start = path.index(node)
                return path[cycle_start:] + [node]

            state[node] = IN_PROGRESS
            path.append(node)

            for dep in self._graph.get(node, []):
                dep_name = dep.component if isinstance(dep, Dependency) else dep
                if cycle := dfs(dep_name):
                    return cycle

            path.pop()
            state[node] = VISITED
            return None

        for node in all_nodes:
            if state[node] == UNVISITED:
                if cycle := dfs(node):
                    return cycle
        return None
```

### Execution Strategy

```python
async def execute_startup(self, dag: list[list[str]]) -> StartupResult:
    """Execute startup tiers with parallel components, sequential tiers."""

    for tier_index, tier_components in enumerate(dag):
        tasks = [self._start_component(name) for name in tier_components]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for name, result in zip(tier_components, results):
            if isinstance(result, Exception):
                defn = self.registry.get(name)
                if defn.effective_criticality == Criticality.REQUIRED:
                    raise StartupAborted(f"Required component {name} failed")
                else:
                    self.registry.mark_disabled(name, reason=str(result))

        await self._wait_for_tier_healthy(tier_components)

async def _start_component(self, name: str) -> ComponentResult:
    """Start a component, respecting soft dependencies."""
    defn = self.registry.get(name)

    for dep in defn.dependencies:
        dep_name = dep.component if isinstance(dep, Dependency) else dep
        is_soft = isinstance(dep, Dependency) and dep.soft

        dep_status = self.registry.get_status(dep_name)

        if dep_status == ComponentStatus.FAILED:
            if is_soft:
                logger.info(f"{name}: soft dependency {dep_name} failed, continuing")
            else:
                raise DependencyFailed(f"{name} blocked by {dep_name}")

    # Proceed with actual startup...
```

---

## Cross-Repo Coordination

### Component Definitions

```python
CROSS_REPO_COMPONENTS = [
    ComponentDefinition(
        name="gcp-prewarm",
        criticality=Criticality.OPTIONAL,
        process_type=ProcessType.EXTERNAL_SERVICE,
        provides_capabilities=["gcp-vm-ready"],
        dependencies=[],
        startup_timeout=30.0,
    ),
    ComponentDefinition(
        name="jarvis-prime",
        criticality=Criticality.DEGRADED_OK,
        process_type=ProcessType.SUBPROCESS,
        repo_path="${JARVIS_PRIME_PATH}",
        provides_capabilities=["local-inference", "llm", "embeddings"],
        dependencies=[
            "jarvis-core",
            Dependency("gcp-prewarm", soft=True),  # Soft dependency
        ],
        health_check_type=HealthCheckType.HTTP,
        health_endpoint="http://localhost:${JARVIS_PRIME_PORT}/health",
        startup_timeout=120.0,
        fallback_strategy=FallbackStrategy.RETRY_THEN_CONTINUE,
        fallback_for_capabilities={"inference": "claude-api", "embeddings": "openai-api"},
        disable_env_var="JARVIS_PRIME_ENABLED",
        conservative_skip_priority=80,
    ),
    ComponentDefinition(
        name="reactor-core",
        criticality=Criticality.OPTIONAL,
        process_type=ProcessType.SUBPROCESS,
        repo_path="${REACTOR_CORE_PATH}",
        provides_capabilities=["training", "fine-tuning"],
        dependencies=["jarvis-core", "jarvis-prime"],
        health_check_type=HealthCheckType.HTTP,
        health_endpoint="http://localhost:${REACTOR_PORT}/health",
        startup_timeout=90.0,
        fallback_strategy=FallbackStrategy.CONTINUE,
        disable_env_var="REACTOR_ENABLED",
        conservative_skip_priority=10,
    ),
]
```

### SubprocessManager

```python
class SubprocessManager:
    """Manages lifecycle of cross-repo subprocesses.

    IMPORTANT: Must preserve existing _spawn_service_core behavior:
    - Hardware profile detection (SLIM)
    - GCP endpoint env wiring
    - Hollow client vs API-only mode
    - Claude fallback signal checks
    - Repo path resolution
    - Python executable detection
    """

    async def start(self, component: ComponentDefinition) -> ProcessHandle:
        repo_path = self._resolve_repo_path(component)
        cmd = self._get_startup_command(component.name, repo_path)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_child_env(component),
        )

        handle = ProcessHandle(process, component)
        self._active_processes[component.name] = handle
        asyncio.create_task(self._monitor_health(handle))

        return handle

    async def shutdown_all(self, reverse_order: bool = True):
        """Shutdown in reverse DAG order for clean teardown."""
```

### Capability-Based Routing

```python
# Old pattern (brittle)
if jarvis_prime_process and jarvis_prime_process.is_alive():
    response = await call_jarvis_prime(prompt)
else:
    response = await call_cloud_api(prompt)

# New pattern (capability-based)
if registry.has_capability("local-inference"):
    provider = registry.get_provider("local-inference")
    response = await inference_router.call(provider, prompt)
else:
    response = await inference_router.call_fallback(prompt)
```

---

## Health Contracts

### Shared Schema

```python
@dataclass
class HealthReport:
    status: HealthStatus          # HEALTHY | DEGRADED | UNHEALTHY | UNKNOWN
    component: str
    timestamp: datetime
    latency_ms: float | None
    details: dict[str, Any]
    dependencies_ok: bool
    message: str | None
    previous_status: HealthStatus | None = None  # For change detection
    version: str | None = None

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
```

### System Health Aggregator

```python
class SystemHealthAggregator:
    """Combines individual health reports into system-wide view."""

    async def collect_all(self) -> SystemHealth:
        tasks = {
            name: self._check_component(name)
            for name in self.registry.all_definitions()  # All attempted
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        return SystemHealth(
            overall=self._compute_overall(results),
            components={name: result for name, result in zip(tasks.keys(), results)},
            capabilities=self._derive_capabilities(results),
            timestamp=datetime.utcnow(),
        )
```

### Startup Summary

**Trigger Conditions:**

```python
class StartupCompletionCriteria:
    all_components_resolved: bool  # All reached terminal state
    global_timeout: float = 180.0  # 3 minutes max
    required_failure: bool         # Required component failed

    def is_complete(self) -> tuple[bool, str]:
        if self.required_failure:
            return True, "required_component_failed"
        if self.all_components_resolved:
            return True, "all_resolved"
        if time.time() > self.start_time + self.global_timeout:
            return True, "global_timeout"
        return False, "in_progress"
```

**Output Format:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JARVIS Startup Summary (v148.0)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✓ jarvis-core         HEALTHY    [required]     1.2s
✓ redis               HEALTHY    [optional]     0.3s
✓ cloud-sql           HEALTHY    [optional]     0.8s
✓ voice-unlock        HEALTHY    [degraded_ok]  2.1s
⟳ jarvis-prime        STARTING   [degraded_ok]  45s...  "Loading model"
✗ reactor-core        FAILED     [optional]     12.3s   "Connection refused"
○ trinity             DISABLED   [optional]     --      "TRINITY_ENABLED=false"

Capabilities Available:
  ✓ inference (jarvis-prime, starting)
  ✓ voice-auth (voice-unlock)
  ✓ storage (cloud-sql, redis)
  ✗ training (reactor-core failed)

Total startup time: 52.3s
System status: DEGRADED (1 starting, 1 failed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Output Locations:**

1. Structured log line (always)
2. State file: `~/.jarvis/state/startup_summary.json`
3. IPC broadcast to connected clients

---

## Graceful Degradation & Recovery

### Error Classifier

```python
class ErrorClass(Enum):
    TRANSIENT_NETWORK = "transient_network"
    NEEDS_FALLBACK = "needs_fallback"
    MISSING_RESOURCE = "missing_resource"
    RESOURCE_EXHAUSTION = "resource_exhaustion"

class ErrorClassifier:
    CLASSIFICATION_RULES = {
        ConnectionRefusedError: ErrorClass.TRANSIENT_NETWORK,
        TimeoutError: ErrorClass.TRANSIENT_NETWORK,
        FileNotFoundError: ErrorClass.MISSING_RESOURCE,
        MemoryError: ErrorClass.RESOURCE_EXHAUSTION,
        "CloudOffloadRequired": ErrorClass.NEEDS_FALLBACK,
        "GPUNotAvailable": ErrorClass.NEEDS_FALLBACK,
    }

    def classify(self, error: Exception) -> ErrorClassification:
        error_class = self._match_error(error)
        return ErrorClassification(
            error_class=error_class,
            suggested_strategy=self._suggest_strategy(error_class),
            is_retryable=error_class == ErrorClass.TRANSIENT_NETWORK,
            needs_fallback=error_class == ErrorClass.NEEDS_FALLBACK,
        )
```

### Recovery Engine

```python
class RecoveryPhase(Enum):
    STARTUP = "startup"
    RUNTIME = "runtime"

class RecoveryEngine:
    def __init__(
        self,
        registry: ComponentRegistry,
        capability_router: CapabilityRouter,
        error_classifier: ErrorClassifier,
    ):
        self.registry = registry
        self.capability_router = capability_router
        self.error_classifier = error_classifier

    async def handle_failure(
        self,
        component: str,
        error: Exception,
        context: StartupContext,
        phase: RecoveryPhase,
    ) -> RecoveryAction:
        defn = self.registry.get(component)
        classification = self.error_classifier.classify(error)

        # Fallback mode for capability-providing components
        if classification.needs_fallback and defn.fallback_for_capabilities:
            return RecoveryAction(
                strategy=RecoveryStrategy.FALLBACK_MODE,
                fallback_targets=defn.fallback_for_capabilities,
            )

        # Retry for transient errors
        if classification.is_retryable:
            if self._attempt_count[component] < defn.retry_max_attempts:
                delay = defn.retry_delay_seconds * (1.5 ** self._attempt_count[component])
                return RecoveryAction(RecoveryStrategy.FULL_RESTART, delay=delay)

        # Required component exhausted retries
        if defn.effective_criticality == Criticality.REQUIRED:
            return RecoveryAction(RecoveryStrategy.ESCALATE_TO_USER)

        return RecoveryAction(RecoveryStrategy.DISABLE_AND_CONTINUE)
```

### Startup Context

```python
class StartupContext:
    """Integrates with existing shutdown infrastructure."""

    STATE_FILE = Path("~/.jarvis/state/last_run.json").expanduser()

    def __init__(self):
        self._register_with_shutdown_hook()

    def _register_with_shutdown_hook(self):
        """Register with existing shutdown system - NO signal.signal()."""
        from backend.scripts.shutdown_hook import register_cleanup_callback
        register_cleanup_callback(self._persist_on_exit, priority=10)

    def _persist_on_exit(self, exit_code: int = 0, exit_reason: str = "normal"):
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.STATE_FILE.write_text(json.dumps({
                "timestamp": datetime.utcnow().isoformat(),
                "exit_code": exit_code,
                "exit_reason": exit_reason,
                "version": JARVIS_VERSION,
            }))
        except Exception:
            pass

    @classmethod
    def load(cls) -> "StartupContext":
        return cls(
            previous_exit_code=cls._read_exit_code(),
            crash_count_recent=CrashHistory().crashes_in_window(),
            fallback_signal_active=cls._read_fallback_signal(),
        )

    @property
    def needs_conservative_startup(self) -> bool:
        return self.crash_count_recent >= 3
```

### Crash History

```python
class CrashHistory:
    HISTORY_FILE = Path("~/.jarvis/state/crash_history.jsonl").expanduser()
    RECENT_WINDOW = timedelta(hours=1)

    def record_crash(self, exit_code: int, reason: str):
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "exit_code": exit_code,
            "reason": reason,
        }
        with self.HISTORY_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def crashes_in_window(self, window: timedelta = None) -> int:
        window = window or self.RECENT_WINDOW
        cutoff = datetime.utcnow() - window
        return sum(1 for e in self._read_entries()
                   if datetime.fromisoformat(e["timestamp"]) > cutoff)
```

### Conservative Startup

```python
def get_conservative_components(self) -> list[str]:
    """Components to start after repeated crashes."""
    return [
        c.name for c in self.registry.all_definitions()
        if c.effective_criticality in (Criticality.REQUIRED, Criticality.DEGRADED_OK)
        or c.conservative_skip_priority >= 70
    ]
    # Skip order (lowest priority first):
    # reactor-core (10) → trinity (20) → ouroboros (30) → ... → jarvis-prime (80)
```

---

## Startup Lock

```python
class StartupLock:
    """Prevents concurrent supervisor runs with stale-lock recovery."""

    LOCK_FILE = Path("~/.jarvis/state/supervisor.lock").expanduser()

    def acquire(self) -> bool:
        import fcntl

        self.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.LOCK_FILE, "w")

        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            return True
        except BlockingIOError:
            self._fd.close()

            if self._is_stale_lock():
                self.LOCK_FILE.unlink()
                return self.acquire()

            return False

    def _is_stale_lock(self) -> bool:
        try:
            existing_pid = int(self.LOCK_FILE.read_text().strip())
            os.kill(existing_pid, 0)
            return False
        except (ValueError, OSError, FileNotFoundError):
            return True

    def release(self):
        if hasattr(self, "_fd") and not self._fd.closed:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
```

---

## State Migration

### Phased Approach

```python
class StatePaths:
    """Phased migration from old to new state paths."""

    MIGRATION_PHASE = int(os.environ.get("JARVIS_STATE_MIGRATION_PHASE", "1"))

    OLD_PATHS = {
        "last_exit_code": Path("~/.jarvis/trinity/last_exit_code"),
        "cloud_lock": Path("~/.jarvis/trinity/cloud_lock"),
        "fallback_signal": Path("~/.jarvis/trinity/claude_api_fallback.json"),
    }

    NEW_PATHS = {
        "last_exit_code": Path("~/.jarvis/state/last_run.json"),
        "cloud_lock": Path("~/.jarvis/state/trinity/cloud_lock"),
        "fallback_signal": Path("~/.jarvis/state/capability_fallbacks.json"),
    }

    @classmethod
    def read(cls, key: str) -> str | None:
        new_path = cls.NEW_PATHS[key].expanduser()
        old_path = cls.OLD_PATHS[key].expanduser()

        if new_path.exists():
            return new_path.read_text()
        if old_path.exists():
            return old_path.read_text()
        return None

    @classmethod
    def write(cls, key: str, content: str):
        new_path = cls.NEW_PATHS[key].expanduser()
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(content)

        if cls.MIGRATION_PHASE == 1:
            old_path = cls.OLD_PATHS[key].expanduser()
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_text(content)
```

### Unified State Layout

```
~/.jarvis/state/                    # Single root
├── last_run.json                   # StartupContext
├── crash_history.jsonl             # CrashHistory
├── startup_summary.json            # Latest summary
├── capability_fallbacks.json       # Active fallbacks
├── supervisor.lock                 # Startup lock
├── trinity/                        # Migrated
│   ├── cloud_lock
│   └── services/
├── registry/                       # Migrated
│   └── ports.json
└── gcp/                            # Migrated
    └── database_config.json
```

---

## Implementation Plan

### Track 1: Immediate Log Severity Fixes (Days 1-3)

**Target Files:**

```python
TRACK_1_TARGET_FILES = [
    # Primary (high error volume)
    "run_supervisor.py",
    "backend/supervisor/cross_repo_startup_orchestrator.py",

    # Secondary
    "backend/core/proxy/trinity_coordinator.py",
    "backend/core/proxy/startup_barrier.py",
    "backend/core/gcp_vm_manager.py",
    "backend/core/cost_tracker.py",
    "backend/core/cross_repo_cleanup.py",
    "backend/core/supervisor/health_monitor.py",
    "backend/intelligence/unified_database_drivers.py",
]
```

**Deliverable:** Immediate log noise reduction using `log_component_failure` bridge.

### Track 2: Foundation Build (Days 1-14)

```
Week 1:
├── Day 1-2: ComponentRegistry + ComponentDefinition
│   └── File: backend/core/component_registry.py
├── Day 3-4: StartupDAG + dependency resolution
│   └── File: backend/core/startup_dag.py
├── Day 5: ComponentLogger (registry-aware)
│   └── File: backend/core/component_logger.py

Week 2:
├── Day 6-7: HealthContract + SystemHealthAggregator
│   └── File: backend/core/health_contracts.py
├── Day 8-9: DegradationCoordinator + RecoveryEngine
│   └── File: backend/core/recovery_engine.py
├── Day 10: StartupContext + CrashHistory
│   └── File: backend/core/startup_context.py
├── Day 11-12: SubprocessManager (cross-repo)
│   └── File: backend/core/subprocess_manager.py
├── Day 13-14: Integration + StartupSummary
│   └── Wire into run_supervisor.py
```

### Merge Point

Once `ComponentRegistry` exists, Track 1 bridge code migrates to `ComponentLogger`.

### Migration Strategy

| Existing File | Change |
|---------------|--------|
| `cross_repo_startup_orchestrator.py` | Delegates to `StartupDAG` + `SubprocessManager` |
| `cross_repo_cleanup.py` | Uses `ComponentLogger` for severity |
| `jarvis_supervisor.py` | Reads from `ComponentRegistry`, emits `StartupSummary` |
| `health_monitor.py` | Implements `HealthContract`, feeds `SystemHealthAggregator` |
| `TrinityCoordinator` | Wraps existing config, exposes via registry |

---

## Key Behaviors Summary

1. **Explicit > Implicit** - Dependencies declared, not inferred from timing
2. **Criticality-driven logging** - REQUIRED→ERROR, DEGRADED_OK→WARNING, OPTIONAL→INFO
3. **Capability-based routing** - Ask "has inference?" not "is jarvis-prime alive?"
4. **Graceful degradation** - Fallback to cloud APIs, conservative startup after crashes
5. **Single startup summary** - One clear view of what started, what didn't, why
6. **No signal handler conflicts** - Integrates with existing shutdown_hook
7. **Stale lock recovery** - PID check prevents stuck supervisor
8. **Phased state migration** - Old and new paths coexist during rollout

---

## Next Steps

1. **Create git worktree** for isolated development
2. **Write detailed implementation plan** with file-by-file changes
3. **Implement Track 1** (log severity fixes) for immediate relief
4. **Implement Track 2** (foundation) in parallel
5. **Merge and validate** with comprehensive testing
