# Unified Supervisor Kernel Design

**Date:** 2026-01-31
**Status:** Approved
**Scope:** Merge `run_supervisor.py` (27k lines) + `start_system.py` (23k lines) into single `unified_supervisor.py`

## Executive Summary

Create a **Monolithic Kernel** - a single, self-contained file (~55k lines) that acts as the absolute authority for the entire JARVIS system. This eliminates "import hell" during crashes and ensures the Supervisor has total context without relying on external modules.

### Key Decisions

| Decision | Choice |
|----------|--------|
| Architecture | **Option A: Unified Monolith** - All logic inline, no imports from old files |
| Migration | **Option A: Big Bang** - Create new file, delete both old files |
| Features | **Option A: Everything** - Merge ALL features from both files |
| CLI | **Option A: Merged Superset** - Combine all flags for maximum flexibility |

---

## Zone Architecture

The file is organized into **7 distinct zones**:

```
unified_supervisor.py (~55k lines)
├── ZONE 0: EARLY PROTECTION (lines 1-500)
│   ├── Signal protection for CLI commands
│   ├── Venv auto-activation (before ANY imports)
│   ├── Fast supervisor check (skip heavy imports if running)
│   └── Python 3.9 compatibility patches
│
├── ZONE 1: FOUNDATION (lines 500-2000)
│   ├── All imports (consolidated, no duplicates)
│   ├── SystemKernelConfig (merged config class)
│   ├── Constants, enums, type definitions
│   └── Environment loading (.env, .env.gcp)
│
├── ZONE 2: CORE UTILITIES (lines 2000-5000)
│   ├── UnifiedLogger (OrganizedLogger + PerformanceLogger)
│   ├── RobustVenvDetector (enhanced)
│   ├── StartupLock (singleton enforcement)
│   ├── RetryWithBackoff, CircuitBreaker
│   └── TerminalUI (visual feedback)
│
├── ZONE 3: RESOURCE MANAGERS (lines 5000-15000)
│   ├── DockerDaemonManager
│   ├── GCPInstanceManager (Spot VMs, Cloud Run)
│   ├── ScaleToZeroCostOptimizer
│   ├── SemanticVoiceCacheManager
│   ├── DynamicPortManager
│   └── TieredStorageManager
│
├── ZONE 4: INTELLIGENCE LAYER (lines 15000-25000)
│   ├── HybridWorkloadRouter
│   ├── HybridIntelligenceCoordinator
│   ├── GoalInferenceEngine
│   ├── SAIHybridIntegration
│   └── AdaptiveThresholdManager
│
├── ZONE 5: PROCESS ORCHESTRATION (lines 25000-40000)
│   ├── UnifiedSignalHandler
│   ├── ComprehensiveZombieCleanup
│   ├── ProcessStateManager
│   ├── HotReloadWatcher
│   ├── ProgressiveReadinessManager
│   └── TrinityIntegrator
│
├── ZONE 6: THE KERNEL (lines 40000-52000)
│   ├── JarvisSystemKernel (the brain)
│   │   ├── __init__(): Initialize all managers
│   │   ├── startup(): Full boot sequence
│   │   ├── run(): Main event loop
│   │   └── cleanup(): Master shutdown
│   └── IPC Server (Unix socket)
│
└── ZONE 7: ENTRY POINT (lines 52000-55000)
    ├── Unified CLI argument parser
    ├── main() function
    └── if __name__ == "__main__"
```

---

## Zone 1: SystemKernelConfig

Unified configuration merging `BootstrapConfig` + `StartupSystemConfig`:

```python
@dataclass
class SystemKernelConfig:
    # Core Identity
    kernel_version: str
    kernel_id: str

    # Operating Mode
    mode: str  # supervisor | standalone | minimal
    in_process_backend: bool
    dev_mode: bool

    # Network (dynamic, no hardcoding)
    backend_port: int  # Auto-detected
    websocket_port: int

    # Paths (auto-discovered)
    project_root: Path
    venv_path: Optional[Path]

    # Trinity / Cross-Repo
    trinity_enabled: bool
    prime_repo_path: Optional[Path]
    reactor_repo_path: Optional[Path]

    # Docker
    docker_enabled: bool
    docker_auto_start: bool

    # GCP / Cloud
    gcp_enabled: bool
    spot_vm_enabled: bool
    prefer_cloud_run: bool

    # Cost Optimization
    scale_to_zero_enabled: bool
    idle_timeout_seconds: int
    cost_budget_daily_usd: float

    # Intelligence / ML
    hybrid_intelligence_enabled: bool
    goal_preset: str  # auto | aggressive | balanced | conservative

    # Voice / Audio
    voice_enabled: bool
    ecapa_enabled: bool

    # Memory / Resources
    memory_mode: str
    memory_target_percent: float

    # Readiness / Health
    health_check_interval: float
    startup_timeout: float

    # Hot Reload / Dev
    hot_reload_enabled: bool
    reload_check_interval: float
```

**Key Principle:** All fields use `field(default_factory=...)` for dynamic detection. Zero hardcoding.

---

## Zone 2: UnifiedLogger

Merges visual organization + performance tracking:

```python
class UnifiedLogger:
    # Visual sections (from OrganizedLogger)
    def section_start(section, title) -> SectionContext

    # Performance tracking (from PerformanceLogger)
    def phase_start(name) / phase_end(name)
    def timed(operation) -> ContextManager
    async def timed_async(operation, coro)

    # Parallel tracking (NEW)
    def parallel_start(task_names) -> ParallelTracker

    # Standard logging
    def debug/info/success/warning/error/critical(message)

    # Metrics
    def get_metrics_summary() -> Dict
    def print_startup_summary()
```

---

## Zone 3: Resource Managers

All share common base class:

```python
class ResourceManagerBase(ABC):
    async def initialize() -> bool
    async def health_check() -> Tuple[bool, str]
    async def cleanup() -> None
    @property
    def is_ready() -> bool
```

**Managers:**
- `DockerDaemonManager` - Docker lifecycle, auto-start
- `GCPInstanceManager` - Spot VMs, Cloud Run, Cloud SQL
- `ScaleToZeroCostOptimizer` - Idle detection, budget enforcement
- `DynamicPortManager` - Zero-hardcoding port allocation
- `SemanticVoiceCacheManager` - ECAPA embedding cache
- `TieredStorageManager` - Hot/warm/cold tiering

---

## Zone 4: Intelligence Layer

All share common base class with lazy loading:

```python
class IntelligenceManagerBase(ABC):
    async def initialize() -> bool
    async def load_models() -> bool  # Lazy
    async def infer(input_data) -> Any
    def get_fallback_result(input_data) -> Any  # Rule-based
```

**Managers:**
- `HybridWorkloadRouter` - Local vs Cloud vs Spot VM routing
- `HybridIntelligenceCoordinator` - Central coordinator
- `GoalInferenceEngine` - ML-powered intent classification
- `SAIHybridIntegration` - Scenario detection
- `AdaptiveThresholdManager` - NO hardcoded thresholds

---

## Zone 5: Process Orchestration

- `UnifiedSignalHandler` - SIGINT/SIGTERM handling, callback registration
- `ComprehensiveZombieCleanup` - Find and kill stale processes
- `ProcessStateManager` - Track managed process lifecycle
- `HotReloadWatcher` - File change detection, debounced restart
- `ProgressiveReadinessManager` - Multi-tier readiness (PROCESS_STARTED → FULLY_READY)
- `TrinityIntegrator` - Cross-repo Prime/Reactor integration

---

## Zone 6: JarvisSystemKernel

The brain that ties everything together:

```python
class JarvisSystemKernel:
    # Singleton
    _instance: Optional["JarvisSystemKernel"] = None

    # Lifecycle
    async def startup() -> int  # Returns exit code
    async def run() -> None     # Main event loop
    async def cleanup() -> None # Master shutdown

    # Startup Phases
    async def _phase_preflight()     # Cleanup, ports, IPC
    async def _phase_resources()     # Docker, GCP, storage (parallel)
    async def _phase_backend()       # uvicorn.Server or subprocess
    async def _phase_intelligence()  # ML layer
    async def _phase_trinity()       # Cross-repo

    # Backend Modes
    async def _start_backend_in_process()   # uvicorn.Server
    async def _start_backend_subprocess()   # asyncio.subprocess

    # Background Loops
    async def _health_monitor_loop()
    async def _cost_optimizer_loop()
    async def _ipc_server_loop()
```

---

## Zone 7: Unified CLI

All flags merged from both files:

```
Control:     --status, --shutdown, --restart, --cleanup
Mode:        --mode, --in-process, --subprocess
Network:     --port, --host, --websocket-port
Docker:      --skip-docker, --no-docker-auto-start
GCP:         --skip-gcp, --prefer-cloud-run, --enable-spot-vm
Cost:        --no-scale-to-zero, --idle-timeout, --daily-budget
ML:          --goal-preset, --enable-automation, --skip-intelligence
Voice:       --skip-voice, --no-narrator, --skip-ecapa
Memory:      --memory-mode, --memory-target
Trinity:     --skip-trinity, --prime-url
Dev:         --no-hot-reload, --reload-interval, --debug, --verbose
Advanced:    --force, --takeover, --dry-run, --config-file
```

---

## Implementation Plan

### Phase 1: Create unified_supervisor.py
1. Zone 0: Early protection (copy from run_supervisor.py)
2. Zone 1: Consolidated imports + SystemKernelConfig
3. Zone 2: UnifiedLogger + utilities
4. Zone 3: Resource managers (port from start_system.py)
5. Zone 4: Intelligence layer (port from start_system.py)
6. Zone 5: Process orchestration (port from run_supervisor.py)
7. Zone 6: JarvisSystemKernel class
8. Zone 7: CLI + main()

### Phase 2: Testing
1. Unit tests for each zone
2. Integration test: full startup/shutdown cycle
3. CLI tests: all flags work correctly
4. Regression: compare behavior to old files

### Phase 3: Migration
1. Rename old files to `_deprecated_run_supervisor.py` and `_deprecated_start_system.py`
2. Update any imports/references
3. Run in production for 1 week
4. Delete deprecated files

---

## Design Principles

1. **Zero Hardcoding** - All values from env vars or dynamic detection
2. **Async-First** - Parallel initialization where possible
3. **Graceful Degradation** - Components can fail independently
4. **Self-Healing** - Auto-restart crashed components
5. **Observable** - Metrics, logs, health endpoints
6. **Lazy Loading** - ML models only loaded when needed
7. **Adaptive** - Thresholds learn from outcomes

---

## Files to Delete After Migration

- `run_supervisor.py` (27,491 lines)
- `start_system.py` (22,839 lines)

**Total lines removed:** ~50,330
**New file:** ~55,000 lines (enhanced features)
