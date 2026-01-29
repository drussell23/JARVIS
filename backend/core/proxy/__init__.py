"""
JARVIS Distributed Proxy System

A production-grade, distributed Cloud SQL proxy lifecycle management system that provides:

1. Cross-repo leader election - Only one repo manages the proxy
2. Bulletproof startup orchestration - Multi-stage verification before proceeding
3. launchd persistence - Proxy survives reboots and auto-restarts on crash
4. Intelligent health monitoring - Anomaly detection with predictive restart
5. Unified observability - Audit trails and diagnostics across all repos

Architecture:
```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    JARVIS Unified Supervisor (run_supervisor.py)             │
│                         Single Entry Point for Everything                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 1: CROSS-REPO LEADER ELECTION                                        │
│  └─ DistributedProxyLeader (Raft-inspired consensus)                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 2: STARTUP BARRIER & DEPENDENCY GRAPH                                │
│  └─ AsyncStartupBarrier (Multi-stage verification)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 3: INTELLIGENT PROXY LIFECYCLE                                       │
│  └─ ProxyLifecycleController (State machine + launchd)                      │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 4: HEALTH & OBSERVABILITY                                            │
│  └─ UnifiedHealthAggregator (Anomaly detection + event sourcing)            │
└─────────────────────────────────────────────────────────────────────────────┘
```

Usage:
    from backend.core.proxy import (
        UnifiedProxyOrchestrator,
        create_orchestrator,
        ComponentManifest,
        DependencyType,
    )

    # Define components with dependencies
    components = [
        ComponentManifest(
            name="voice_unlock",
            dependencies=frozenset({DependencyType.CLOUDSQL}),
            init_func=init_voice_unlock,
            required=True,
        ),
    ]

    # Create and start orchestrator
    orchestrator = await create_orchestrator(
        repo_name="jarvis",
        components=components,
        auto_start=True,
    )

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

# Layer 1: Distributed Leader Election
from .distributed_leader import (
    DistributedProxyLeader,
    ElectionOutcome,
    ElectionResult,
    FileLock,
    LeaderHeartbeat,
    LeaderIdentity,
    LeaderState,
    create_proxy_leader,
)

# Type alias exported separately
LeaderStateCallback = "Callable[[LeaderState], Awaitable[None]]"

# Layer 2: Startup Barrier
from .startup_barrier import (
    AsyncStartupBarrier,
    BarrierConfig,
    ComponentManifest,
    DependencyType,
    InitializationWave,
    VerificationPipeline,
    VerificationResult,
    VerificationStage,
    create_startup_barrier,
    requires_cloudsql,
)

# Layer 3: Lifecycle Controller
from .lifecycle_controller import (
    DarwinSleepWakeDetector,
    EMALatencyTracker,
    InvalidTransitionError,
    LaunchdServiceManager,
    PollingSleepWakeDetector,
    ProxyConfig,
    ProxyLifecycleController,
    ProxyState,
    SleepWakeDetector,
    StateChangeCallback,
    StateTransitionEvent,
    create_lifecycle_controller,
    create_sleep_wake_detector,
)

# Layer 4: Health Aggregator
from .health_aggregator import (
    AnomalyDetector,
    AnomalyType,
    DiagnosisEngine,
    EventStore,
    EventType,
    HealthConfig,
    HealthEvent,
    HealthSnapshot,
    SlidingWindowStats,
    UnifiedHealthAggregator,
    create_health_aggregator,
)

# Orchestrator (brings everything together)
from .orchestrator import (
    OrchestratorConfig,
    OrchestratorState,
    StartupPhase,
    UnifiedProxyOrchestrator,
    create_orchestrator,
    setup_signal_handlers,
)

# Supervisor Integration (drop-in replacement for run_supervisor.py)
from .supervisor_integration import (
    ProxyInitResult,
    initialize_distributed_proxy,
    shutdown_distributed_proxy,
    get_proxy_orchestrator,
    get_proxy_status,
    diagnose_proxy_failure,
    signal_cloudsql_ready,
    create_component_manifest,
    is_distributed_proxy_enabled,
)

__all__ = [
    # Layer 1: Leader Election
    "DistributedProxyLeader",
    "LeaderState",
    "LeaderIdentity",
    "LeaderHeartbeat",
    "ElectionResult",
    "ElectionOutcome",
    "FileLock",
    "LeaderStateCallback",
    "create_proxy_leader",
    # Layer 2: Startup Barrier
    "AsyncStartupBarrier",
    "BarrierConfig",
    "ComponentManifest",
    "DependencyType",
    "VerificationStage",
    "VerificationResult",
    "VerificationPipeline",
    "InitializationWave",
    "create_startup_barrier",
    "requires_cloudsql",
    # Layer 3: Lifecycle Controller
    "ProxyLifecycleController",
    "ProxyState",
    "ProxyConfig",
    "StateTransitionEvent",
    "StateChangeCallback",
    "InvalidTransitionError",
    "EMALatencyTracker",
    "SleepWakeDetector",
    "DarwinSleepWakeDetector",
    "PollingSleepWakeDetector",
    "LaunchdServiceManager",
    "create_lifecycle_controller",
    "create_sleep_wake_detector",
    # Layer 4: Health Aggregator
    "UnifiedHealthAggregator",
    "HealthSnapshot",
    "HealthEvent",
    "HealthConfig",
    "EventType",
    "AnomalyType",
    "AnomalyDetector",
    "DiagnosisEngine",
    "EventStore",
    "SlidingWindowStats",
    "create_health_aggregator",
    # Orchestrator
    "UnifiedProxyOrchestrator",
    "OrchestratorState",
    "OrchestratorConfig",
    "StartupPhase",
    "create_orchestrator",
    "setup_signal_handlers",
    # Supervisor Integration
    "ProxyInitResult",
    "initialize_distributed_proxy",
    "shutdown_distributed_proxy",
    "get_proxy_orchestrator",
    "get_proxy_status",
    "diagnose_proxy_failure",
    "signal_cloudsql_ready",
    "create_component_manifest",
    "is_distributed_proxy_enabled",
]

__version__ = "1.0.0"
