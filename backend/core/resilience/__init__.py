"""
Cross-Repo Resilience Utilities - Production-Hardened Components v2.0
=====================================================================

Provides distributed coordination primitives for robust cross-repo communication
across JARVIS (Body), JARVIS Prime (Mind), and Reactor Core (Learning).

v2.0 Features (Unified Resilience Engine):
    - Bulkhead isolation with semaphore-based resource pools
    - Priority request queuing with backpressure support
    - Dead letter queue with persistent retry scheduling
    - Health-based routing with intelligent endpoint selection
    - Automatic failover with zero-downtime orchestration
    - Chaos engineering framework for controlled failure testing
    - Failure injection for programmable fault simulation
    - Adaptive timeouts based on operation latency history
    - Decorrelated jitter retry for optimal backoff

v1.x Features (Original):
    - Redis-backed distributed locks with fencing tokens
    - Atomic file operations with retry and temp file safety
    - Auto-reconnecting Redis client with exponential backoff
    - Correlation context for cross-repo request tracing
    - File watch guard with event deduplication and recovery
    - Enhanced circuit breaker with failure classification
    - Vector clocks for distributed event ordering and causality
    - Distributed deduplication with Bloom filters and Redis TTL
    - CRDTs for conflict-free distributed state consistency
    - Adaptive rate limiting with AIMD congestion control
    - Distributed circuit breakers with cross-instance coordination
    - Graceful shutdown with request draining and coordination
    - Predictive VM provisioning with time-series forecasting
    - Model versioning with automatic rollback
"""

from backend.core.resilience.distributed_lock import (
    DistributedLock,
    DistributedLockConfig,
    LockAcquisitionError,
    LockNotHeldError,
)
from backend.core.resilience.atomic_file_ops import (
    AtomicFileOps,
    AtomicWriteError,
    AtomicReadError,
)
from backend.core.resilience.redis_reconnector import (
    ResilientRedisClient,
    RedisConnectionConfig,
    RedisNotAvailableError,
    REDIS_AVAILABLE,  # v93.0: Check if Redis is installed before using
)
from backend.core.resilience.correlation_context import (
    CorrelationContext,
    get_current_correlation_id,
    get_current_context,
    with_correlation,
)

# v2.1: Alias for backwards compatibility with GCP Hybrid Router
get_correlation_context = get_current_context
from backend.core.resilience.file_watch_guard import (
    FileWatchGuard,
    FileWatchConfig,
    FileEvent,
    GlobalWatchRegistry,
    get_global_watch_registry,
)
from backend.core.resilience.cross_repo_circuit_breaker import (
    CrossRepoCircuitBreaker,
    CircuitBreakerConfig,  # v2.1: Export for GCP Hybrid Router
    FailureType,
    TierHealth,
)
from backend.core.resilience.vector_clock import (
    LamportTimestamp,
    VectorClock,
    CausalEvent,
    CausalEventManager,
    CausalBarrier,
    CausalityRelation,
)
from backend.core.resilience.distributed_dedup import (
    BloomFilter,
    LRUCache,
    IdempotencyKey,
    DistributedDedup,
    get_distributed_dedup,
    shutdown_distributed_dedup,
)
from backend.core.resilience.crdt import (
    GCounter,
    PNCounter,
    LWWRegister,
    ORSet,
    LWWMap,
    CRDTStateManager,
    get_crdt_state_manager,
    shutdown_crdt_state_manager,
)
from backend.core.resilience.adaptive_rate_limiter import (
    AdaptiveRateLimiter,
    TierRateLimiter,
    RateLimitConfig,
    RateLimitStrategy,
    BackpressureSignal,
    TokenBucket,
    SlidingWindowCounter,
    PriorityRequestQueue,
    get_adaptive_rate_limiter,
    get_tier_rate_limiter,
    shutdown_rate_limiters,
)
from backend.core.resilience.distributed_circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerGroup,
    DistributedCircuitBreakerManager,
    CircuitConfig,
    CircuitState,
    FailureCategory,
    CircuitOpenError,
    SlidingWindowTracker,
    get_distributed_circuit_breaker,
    shutdown_distributed_circuit_breaker,
)
from backend.core.resilience.graceful_shutdown import (
    ShutdownCoordinator,
    ShutdownPhase,
    ShutdownReason,
    ShutdownHandler,
    InFlightTracker,
    TrackedRequest,
    ShutdownInProgressError,
    get_shutdown_coordinator,
    shutdown_coordinator,
    track_request,
)
from backend.core.resilience.predictive_provisioning import (
    PredictiveProvisioner,
    HoltWintersForecaster,
    AnomalyDetector,
    PatternDetector,
    Forecast,
    ScalingRecommendation,
    ScalingAction,
    SeasonalPattern,
    get_predictive_provisioner,
    shutdown_predictive_provisioner,
)
from backend.core.resilience.model_versioning import (
    ModelVersionManager,
    ModelVersion,
    SemanticVersion,
    DeploymentType,
    DeploymentStatus,
    RollbackReason,
    RollbackEvent,
    VersionMetricTracker,
    get_model_version_manager,
    shutdown_model_version_manager,
)

# v2.0: Unified Resilience Engine
from backend.core.resilience.unified_resilience_engine import (
    # Configuration
    ResilienceConfig,
    # Enums
    BulkheadState,
    RequestPriority,
    FailureMode,
    ServiceHealth,
    RoutingStrategy,
    # Data Structures
    BulkheadMetrics,
    QueuedRequest,
    DeadLetterEntry,
    ServiceEndpoint,
    AdaptiveTimeoutState,
    ChaosExperiment,
    # Bulkhead
    BulkheadPool,
    BulkheadRejectedError,
    BulkheadTimeoutError,
    BulkheadManager,
    # Queue
    BackpressureSignal as UnifiedBackpressureSignal,
    PriorityRequestQueue as UnifiedPriorityRequestQueue,
    # DLQ
    DeadLetterQueue,
    # Routing
    HealthBasedRouter,
    # Failover
    FailoverOrchestrator,
    # Chaos
    ChaosController,
    ChaosInjectedException,
    # Timeout
    AdaptiveTimeoutManager,
    # Retry
    DecorrelatedJitterRetry,
    with_retry,
    # Main Engine
    UnifiedResilienceEngine,
    # Global Functions
    get_resilience_engine,
    initialize_resilience,
    shutdown_resilience,
    # Decorators
    with_bulkhead,
    with_adaptive_timeout,
)

# v2.0: Neural Mesh Resilience Integration
from backend.core.resilience.neural_mesh_resilience import (
    # Configuration
    MeshResilienceConfig,
    # Enums
    MeshTarget,
    MeshOperation,
    # Data Structures
    MeshCallResult,
    MeshHealthSnapshot,
    # Main Class
    NeuralMeshResilienceBridge,
    # Global Functions
    get_mesh_resilience_bridge,
    initialize_mesh_resilience,
    shutdown_mesh_resilience,
    # Decorator
    with_mesh_resilience,
)

# v2.0: Supervisor Resilience Integration
from backend.core.resilience.supervisor_integration import (
    # Configuration
    SupervisorResilienceConfig,
    # Data Structures
    ResilienceInitializationResult,
    ResilienceHealthReport,
    # Main Coordinator
    SupervisorResilienceCoordinator,
    # Global Functions
    get_supervisor_resilience_coordinator,
    initialize_supervisor_resilience,
    shutdown_supervisor_resilience,
    get_supervisor_resilience_status,
    get_supervisor_resilience_health,
    # Context Manager
    SupervisorResilienceContext,
)

# v3.0: Resilience Primitives Core Types
# These are foundational types for the new primitives layer (RetryPolicy, HealthProbe, etc.)
# Note: CircuitState here uses auto() values, distinct from the string-based CircuitState
# in distributed_circuit_breaker. Import from types.py directly for the primitives version.
from backend.core.resilience.types import (
    # Primitives CircuitState (uses auto() - distinct from distributed_circuit_breaker's)
    CircuitState as PrimitivesCircuitState,
    # Capability management states
    CapabilityState,
    # Recovery process states
    RecoveryState,
    # Protocols for health and recovery
    HealthCheckable,
    Recoverable,
)

# v3.0: RetryPolicy with exponential backoff and jitter
from backend.core.resilience.retry import (
    RetryPolicy,
    RetryExhausted,
)

# v3.0: CircuitBreaker primitive (simple state machine)
# Note: This is distinct from the distributed CircuitBreaker in distributed_circuit_breaker.
# Use PrimitivesCircuitBreaker for simple local circuit breaking.
# Use CircuitBreaker (from distributed_circuit_breaker) for distributed/advanced scenarios.
from backend.core.resilience.circuit_breaker import (
    CircuitBreaker as PrimitivesCircuitBreaker,
    CircuitOpen as PrimitivesCircuitOpen,
)

# v3.0: HealthProbe with cached health checks
from backend.core.resilience.health import (
    HealthProbe,
)

# v3.0: BackgroundRecovery with adaptive backoff
from backend.core.resilience.recovery import (
    RecoveryConfig,
    BackgroundRecovery,
)

__all__ = [
    # Distributed Lock
    "DistributedLock",
    "DistributedLockConfig",
    "LockAcquisitionError",
    "LockNotHeldError",
    # Atomic File Ops
    "AtomicFileOps",
    "AtomicWriteError",
    "AtomicReadError",
    # Redis Reconnector
    "ResilientRedisClient",
    "RedisConnectionConfig",
    "RedisNotAvailableError",
    "REDIS_AVAILABLE",  # v93.0: Check if Redis is installed
    # Correlation Context
    "CorrelationContext",
    "get_current_correlation_id",
    "get_current_context",
    "get_correlation_context",  # v2.1: Alias for backwards compatibility
    "with_correlation",
    # File Watch Guard
    "FileWatchGuard",
    "FileWatchConfig",
    "FileEvent",
    "GlobalWatchRegistry",
    "get_global_watch_registry",
    # Cross-Repo Circuit Breaker
    "CrossRepoCircuitBreaker",
    "CircuitBreakerConfig",  # v2.1: Export for GCP Hybrid Router
    "FailureType",
    "TierHealth",
    # Vector Clocks
    "LamportTimestamp",
    "VectorClock",
    "CausalEvent",
    "CausalEventManager",
    "CausalBarrier",
    "CausalityRelation",
    # Distributed Deduplication
    "BloomFilter",
    "LRUCache",
    "IdempotencyKey",
    "DistributedDedup",
    "get_distributed_dedup",
    "shutdown_distributed_dedup",
    # CRDTs
    "GCounter",
    "PNCounter",
    "LWWRegister",
    "ORSet",
    "LWWMap",
    "CRDTStateManager",
    "get_crdt_state_manager",
    "shutdown_crdt_state_manager",
    # Adaptive Rate Limiter
    "AdaptiveRateLimiter",
    "TierRateLimiter",
    "RateLimitConfig",
    "RateLimitStrategy",
    "BackpressureSignal",
    "TokenBucket",
    "SlidingWindowCounter",
    "PriorityRequestQueue",
    "get_adaptive_rate_limiter",
    "get_tier_rate_limiter",
    "shutdown_rate_limiters",
    # Distributed Circuit Breaker
    "CircuitBreaker",
    "CircuitBreakerGroup",
    "DistributedCircuitBreakerManager",
    "CircuitConfig",
    "CircuitState",
    "FailureCategory",
    "CircuitOpenError",
    "SlidingWindowTracker",
    "get_distributed_circuit_breaker",
    "shutdown_distributed_circuit_breaker",
    # Graceful Shutdown
    "ShutdownCoordinator",
    "ShutdownPhase",
    "ShutdownReason",
    "ShutdownHandler",
    "InFlightTracker",
    "TrackedRequest",
    "ShutdownInProgressError",
    "get_shutdown_coordinator",
    "shutdown_coordinator",
    "track_request",
    # Predictive Provisioning
    "PredictiveProvisioner",
    "HoltWintersForecaster",
    "AnomalyDetector",
    "PatternDetector",
    "Forecast",
    "ScalingRecommendation",
    "ScalingAction",
    "SeasonalPattern",
    "get_predictive_provisioner",
    "shutdown_predictive_provisioner",
    # Model Versioning
    "ModelVersionManager",
    "ModelVersion",
    "SemanticVersion",
    "DeploymentType",
    "DeploymentStatus",
    "RollbackReason",
    "RollbackEvent",
    "VersionMetricTracker",
    "get_model_version_manager",
    "shutdown_model_version_manager",
    # =========================================================================
    # v2.0: UNIFIED RESILIENCE ENGINE
    # =========================================================================
    # Configuration
    "ResilienceConfig",
    # Enums
    "BulkheadState",
    "RequestPriority",
    "FailureMode",
    "ServiceHealth",
    "RoutingStrategy",
    # Data Structures
    "BulkheadMetrics",
    "QueuedRequest",
    "DeadLetterEntry",
    "ServiceEndpoint",
    "AdaptiveTimeoutState",
    "ChaosExperiment",
    # Bulkhead Isolation
    "BulkheadPool",
    "BulkheadRejectedError",
    "BulkheadTimeoutError",
    "BulkheadManager",
    # Priority Queue
    "UnifiedBackpressureSignal",
    "UnifiedPriorityRequestQueue",
    # Dead Letter Queue
    "DeadLetterQueue",
    # Health-Based Routing
    "HealthBasedRouter",
    # Automatic Failover
    "FailoverOrchestrator",
    # Chaos Engineering
    "ChaosController",
    "ChaosInjectedException",
    # Adaptive Timeout
    "AdaptiveTimeoutManager",
    # Decorrelated Jitter Retry
    "DecorrelatedJitterRetry",
    "with_retry",
    # Main Engine
    "UnifiedResilienceEngine",
    # Global Functions
    "get_resilience_engine",
    "initialize_resilience",
    "shutdown_resilience",
    # Decorators
    "with_bulkhead",
    "with_adaptive_timeout",
    # =========================================================================
    # v2.0: NEURAL MESH RESILIENCE INTEGRATION
    # =========================================================================
    # Configuration
    "MeshResilienceConfig",
    # Enums
    "MeshTarget",
    "MeshOperation",
    # Data Structures
    "MeshCallResult",
    "MeshHealthSnapshot",
    # Main Class
    "NeuralMeshResilienceBridge",
    # Global Functions
    "get_mesh_resilience_bridge",
    "initialize_mesh_resilience",
    "shutdown_mesh_resilience",
    # Decorator
    "with_mesh_resilience",
    # =========================================================================
    # v2.0: SUPERVISOR RESILIENCE INTEGRATION
    # =========================================================================
    # Configuration
    "SupervisorResilienceConfig",
    # Data Structures
    "ResilienceInitializationResult",
    "ResilienceHealthReport",
    # Main Coordinator
    "SupervisorResilienceCoordinator",
    # Global Functions
    "get_supervisor_resilience_coordinator",
    "initialize_supervisor_resilience",
    "shutdown_supervisor_resilience",
    "get_supervisor_resilience_status",
    "get_supervisor_resilience_health",
    # Context Manager
    "SupervisorResilienceContext",
    # =========================================================================
    # v3.0: RESILIENCE PRIMITIVES CORE TYPES
    # =========================================================================
    # State Enums (for new primitives layer)
    "PrimitivesCircuitState",  # Aliased to avoid collision with distributed_circuit_breaker
    "CapabilityState",
    "RecoveryState",
    # Protocols
    "HealthCheckable",
    "Recoverable",
    # =========================================================================
    # v3.0: RETRY POLICY
    # =========================================================================
    # Retry with exponential backoff and jitter
    "RetryPolicy",
    "RetryExhausted",
    # =========================================================================
    # v3.0: CIRCUIT BREAKER PRIMITIVE
    # =========================================================================
    # Simple state machine circuit breaker (local, not distributed)
    "PrimitivesCircuitBreaker",  # Aliased to avoid collision with distributed_circuit_breaker
    "PrimitivesCircuitOpen",
    # =========================================================================
    # v3.0: HEALTH PROBE
    # =========================================================================
    # Cached health checks with failure tracking
    "HealthProbe",
    # =========================================================================
    # v3.0: BACKGROUND RECOVERY
    # =========================================================================
    # Adaptive background recovery with safety valves
    "RecoveryConfig",
    "BackgroundRecovery",
]
