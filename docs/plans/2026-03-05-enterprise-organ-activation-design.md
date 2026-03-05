# Enterprise Organ Activation Program — Governed Service Activation for unified_supervisor.py

**Date:** 2026-03-05
**Status:** Proposed
**Repo:** JARVIS-AI-Agent (primary), cross-repo integration with jarvis-prime and reactor-core
**Approach:** B+ (Tiered Activation Waves with Governed Service Contracts)
**Scope:** Activate 67 dormant enterprise classes in unified_supervisor.py as lifecycle-managed, observable, adaptively-activated service organs

---

## Problem Statement

unified_supervisor.py contains 96,058 lines and ~280 classes. Of these, **67 enterprise framework classes spanning lines 31,000-56,000 (~25K lines) are fully scaffolded but never instantiated**. They represent security, deployment, workflow, data management, monitoring, and infrastructure capabilities that were designed but never wired into the kernel.

The goal is to bring these 67 classes to life as **governed service organs** — fully implemented, lifecycle-managed, and adaptively activated — transforming JARVIS from a collection of scripts into a genuine synthetic organism with functioning organ systems.

### What "alive" means

A class is alive when it satisfies all three:

1. **Lifecycle-managed** — participates in startup/health/shutdown through the SystemServiceRegistry
2. **Observable** — appears in health dashboard, emits telemetry, has correlation IDs
3. **Activatable** — has defined trigger conditions, dependency gates, and budget guardrails

### What "alive" does NOT mean

- Always running at boot (many services are event-driven or warm-standby)
- Adding boot time (deferred services don't block startup)
- Creating new failure modes (strict budget + circuit-breaker + quarantine per service)

---

## Current Architecture

### Existing Infrastructure (already works)

**SystemServiceRegistry** (line 13147) provides:
- Phase-based activation (phases 1-8)
- Dependency topological sort within and across phases
- Per-service `enabled_env` kill switch
- Memory delta tracking per service init
- Reverse-order shutdown guarantee

**ServiceDescriptor** (line 13134) provides:
- `name`, `service`, `phase`, `depends_on`, `enabled_env`
- `initialized`, `healthy`, `error`, `init_time_ms`, `memory_delta_mb`

**Currently registered:** 10 services across phases 1-5:

| Phase | Service | Status |
|-------|---------|--------|
| 1 | observability (ObservabilityPipeline) | ALIVE |
| 1 | health_aggregator (HealthAggregator) | ALIVE |
| 2 | cache_hierarchy (CacheHierarchyManager) | ALIVE |
| 2 | rate_limiter (TokenBucketRateLimiter) | ALIVE |
| 2 | cost_tracker (CostTracker) | ALIVE |
| 2 | lock_manager (DistributedLockManager) | ALIVE |
| 3 | task_queue (TaskQueueManager) | ALIVE |
| 4 | event_sourcing (EventSourcingManager) | ALIVE |
| 4 | message_broker (MessageBroker) | ALIVE |
| 5 | degradation_manager (GracefulDegradationManager) | ALIVE |

### What must change

The `ServiceDescriptor` must be extended to support the 3-layer governance model. The 67 classes must each implement the `SystemService` protocol. The kernel's `_init_service_registry()` must register all 67 with proper contracts.

---

## Design: 3-Layer Service Governance Model

### Layer 1: Extended ServiceDescriptor

```python
@dataclass
class ServiceDescriptor:
    """Metadata for a single governed service organ."""
    # Identity
    name: str
    service: SystemService
    tier: str                    # "immune" | "nervous" | "metabolic" | "higher"
    phase: int                   # startup phase (1-8)

    # Dependencies
    depends_on: List[str] = field(default_factory=list)
    soft_depends_on: List[str] = field(default_factory=list)  # non-blocking

    # Activation policy
    activation_mode: str = "always_on"     # always_on | warm_standby | event_driven | batch_window
    boot_policy: str = "non_blocking"      # block_ready | non_blocking | deferred_after_ready
    enabled_env: Optional[str] = None      # per-service kill switch

    # Criticality
    criticality: str = "optional"          # kernel_critical | control_plane | optional

    # Budget policy
    max_memory_mb: float = 50.0            # RSS cap for this service
    max_cpu_percent: float = 10.0          # CPU cap
    max_concurrent_ops: int = 10           # concurrency bound

    # Failure policy
    max_init_retries: int = 2
    init_timeout_s: float = 30.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_recovery_s: float = 60.0
    quarantine_after_failures: int = 10    # quarantine = stop trying until manual reset

    # Health semantics
    health_check_interval_s: float = 30.0
    liveness_timeout_s: float = 10.0
    readiness_timeout_s: float = 5.0

    # Runtime state (managed by registry)
    initialized: bool = False
    healthy: bool = True
    state: str = "pending"                 # pending | initializing | ready | active | degraded | draining | stopped | quarantined
    error: Optional[str] = None
    init_time_ms: float = 0.0
    memory_delta_mb: float = 0.0
    activation_count: int = 0
    last_health_check: float = 0.0
```

### Layer 2: SystemService Protocol Extension

Every activated class must implement:

```python
class SystemService(ABC):
    """Protocol for governed service organs."""

    # --- Lifecycle ---
    @abstractmethod
    async def initialize(self) -> bool:
        """One-time setup. Must be idempotent. Budget-bounded."""
        ...

    @abstractmethod
    async def start(self) -> bool:
        """Begin active operation. Called after initialize succeeds."""
        ...

    @abstractmethod
    async def health(self) -> ServiceHealthReport:
        """Return liveness + readiness + degradation state.
        Must complete within liveness_timeout_s."""
        ...

    @abstractmethod
    async def drain(self, deadline_s: float) -> bool:
        """Stop accepting new work. Flush in-flight ops before deadline."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Release resources. Must be safe to call multiple times."""
        ...

    # --- Capability ---
    @abstractmethod
    def capability_contract(self) -> CapabilityContract:
        """Declare inputs, outputs, side effects, idempotency."""
        ...

    # --- Activation ---
    @abstractmethod
    def activation_triggers(self) -> List[str]:
        """Return list of event topics that should activate this service.
        Empty list = always_on (activated at boot)."""
        ...
```

```python
@dataclass(frozen=True)
class ServiceHealthReport:
    """Structured health report from a service."""
    alive: bool                          # liveness: process/task exists
    ready: bool                          # readiness: can accept work
    degraded: bool = False               # degraded: working but impaired
    draining: bool = False               # draining: finishing but not accepting
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class CapabilityContract:
    """What a service does, formally declared."""
    name: str
    version: str                         # semver
    inputs: List[str]                    # event topics consumed
    outputs: List[str]                   # event topics produced
    side_effects: List[str]              # state mutations
    idempotent: bool = True
    cross_repo: bool = False             # touches Prime or Reactor
```

### Layer 3: Activation Contract (Event-Driven Services)

For services with `activation_mode = "event_driven"`:

```python
@dataclass
class ActivationContract:
    """When and how a service activates."""
    trigger_events: List[str]            # event topics that trigger activation
    dependency_gate: List[str]           # services that must be healthy first
    budget_gate: BudgetGate              # resource thresholds that must be met
    backoff_gate: BackoffGate            # cooldown after activation failures
    max_activations_per_hour: int = 100  # rate limit
    deactivate_after_idle_s: float = 300 # return to warm_standby after idle

@dataclass
class BudgetGate:
    """Resource conditions that must be met for activation."""
    max_memory_percent: float = 85.0     # system RSS must be below this
    max_cpu_percent: float = 80.0        # system CPU must be below this
    min_available_mb: float = 200.0      # free RAM must exceed this

@dataclass
class BackoffGate:
    """Backoff after activation failures."""
    initial_delay_s: float = 5.0
    max_delay_s: float = 300.0
    multiplier: float = 2.0
    jitter: bool = True
```

---

## Tiered Activation Waves

### Wave 0: Foundation (Pre-activation hardening)

Before activating any dormant class:

1. Extend `ServiceDescriptor` with the new fields (backward-compatible defaults)
2. Extend `SystemService` ABC with `health()`, `drain()`, `capability_contract()`, `activation_triggers()`
3. Update `SystemServiceRegistry.activate_phase()` to respect `activation_mode`, `boot_policy`, `budget_gate`
4. Add `ServiceHealthReport` to replace boolean health checks
5. Wire correlation IDs into the `SupervisorEventBus` for cross-service tracing
6. Ensure existing 10 services still work with extended descriptors (zero regression)

**Go/no-go gate:** All existing tests pass. Boot time unchanged. 10 services still activate correctly.

### Wave 1: Immune System (8 services, phase 6)

| Service | Class | activation_mode | criticality | boot_policy |
|---------|-------|----------------|-------------|-------------|
| security_policy | SecurityPolicyEngine | always_on | control_plane | non_blocking |
| anomaly_detector | AnomalyDetector | always_on | control_plane | non_blocking |
| audit_trail | AuditTrailRecorder | always_on | control_plane | non_blocking |
| threat_intel | ThreatIntelligenceManager | event_driven | optional | deferred_after_ready |
| incident_response | IncidentResponseCoordinator | event_driven | optional | deferred_after_ready |
| compliance | ComplianceAuditor | batch_window | optional | deferred_after_ready |
| data_classification | DataClassificationManager | event_driven | optional | deferred_after_ready |
| access_control | AccessControlManager | always_on | control_plane | non_blocking |

**Trigger wiring:**
- SecurityPolicyEngine: evaluates every agent action, IPC command, file access
- AnomalyDetector: receives telemetry from ObservabilityPipeline, flags outliers
- AuditTrailRecorder: subscribes to SupervisorEventBus for all audit-worthy events
- ThreatIntelligenceManager: activated by AnomalyDetector when score > threshold
- IncidentResponseCoordinator: activated by ThreatIntelligenceManager on confirmed threat
- ComplianceAuditor: runs on cron (daily) or on data ingestion events
- DataClassificationManager: activated on data ingestion (voice samples, experience events)
- AccessControlManager: evaluates every cross-repo request

**Dependencies:**
```
observability -> anomaly_detector -> threat_intel -> incident_response
observability -> audit_trail
observability -> security_policy -> access_control
health_aggregator -> compliance
event_sourcing -> data_classification
```

**Go/no-go gate:**
- Boot time increase < 2 seconds
- All 8 services report healthy within 30s
- No restart oscillation in 10-minute soak
- Correlation IDs flow through security evaluation chain

### Wave 2: Nervous System (12 services, phase 7)

| Service | Class | activation_mode | criticality | boot_policy |
|---------|-------|----------------|-------------|-------------|
| workflow_engine | WorkflowEngine | warm_standby | control_plane | non_blocking |
| state_machines | StateMachineManager | always_on | control_plane | non_blocking |
| config_manager | ConfigurationManager | always_on | control_plane | block_ready |
| feature_gates | FeatureGateManager | always_on | control_plane | non_blocking |
| schema_registry | SchemaRegistry | always_on | control_plane | block_ready |
| service_discovery | ServiceRegistryManager | always_on | control_plane | non_blocking |
| rules_engine | RulesEngine | warm_standby | optional | deferred_after_ready |
| batch_processor | BatchProcessor | event_driven | optional | deferred_after_ready |
| cron_scheduler | CronScheduler | always_on | optional | non_blocking |
| notifications | NotificationDispatcher | event_driven | optional | deferred_after_ready |
| request_coalescer | RequestCoalescer | event_driven | optional | deferred_after_ready |
| job_manager | BackgroundJobManager | warm_standby | optional | deferred_after_ready |

**Key wiring:**
- WorkflowEngine: orchestrates multi-step agent tasks (email triage, model deployment)
- StateMachineManager: manages formal state machines for GCP lifecycle, Prime routing
- ConfigurationManager: replaces scattered os.getenv() with validated, versioned config
- FeatureGateManager: controls progressive rollout of new capabilities
- SchemaRegistry: validates cross-repo API contracts at boot
- CronScheduler: schedules Reactor Core training runs, cache cleanup, health reports

**Go/no-go gate:**
- Boot time increase < 3 seconds cumulative (waves 0+1+2)
- Config changes propagate within 5s without restart
- Workflow engine can execute a 3-step test workflow
- No dependency cycles detected by toposort

### Wave 3: Metabolic System (15 services, phase 7)

| Service | Class | activation_mode | criticality | boot_policy |
|---------|-------|----------------|-------------|-------------|
| service_mesh | ServiceMeshRouter | always_on | control_plane | non_blocking |
| load_shedding | LoadSheddingController | event_driven | control_plane | deferred_after_ready |
| auto_scaler | AutoScalingController | event_driven | optional | deferred_after_ready |
| cache_invalidation | CacheInvalidationCoordinator | event_driven | optional | deferred_after_ready |
| connection_pools | ConnectionPoolManager | always_on | control_plane | non_blocking |
| resource_quotas | ResourceQuotaManager | always_on | control_plane | non_blocking |
| resource_pools | ResourcePoolManager | always_on | optional | non_blocking |
| cost_accounting | CostAccountingManager | always_on | optional | non_blocking |
| alerting | AlertingManager | event_driven | optional | deferred_after_ready |
| profiler | PerformanceProfiler | event_driven | optional | deferred_after_ready |
| rate_limiter_mgr | RateLimiterManager | always_on | control_plane | non_blocking |
| retry_policies | RetryPolicyManager | always_on | optional | non_blocking |
| secret_vault | SecretVaultManager | always_on | control_plane | block_ready |
| network_monitor | NetworkManager | always_on | optional | non_blocking |
| filesystem | FileSystemManager | always_on | optional | non_blocking |

**Key wiring:**
- ServiceMeshRouter: replaces hardcoded HTTP calls to Prime/Reactor with circuit-broken, load-balanced routing
- LoadSheddingController: activated by DegradationManager when memory > 85%
- AutoScalingController: activated by health thresholds, triggers GCP VM scaling
- SecretVaultManager: manages API keys currently scattered in env vars
- ConnectionPoolManager: manages aiohttp.ClientSession lifecycle (currently created per-call)

**Go/no-go gate:**
- Boot time increase < 5 seconds cumulative
- ServiceMeshRouter routes 100 requests with < 5ms overhead
- LoadShedding activates correctly under simulated memory pressure
- No cross-service feedback loops (monitor -> remediate -> re-trigger)

### Wave 4: Higher Functions (32 services, phase 8)

| Service | Class | activation_mode | criticality | boot_policy |
|---------|-------|----------------|-------------|-------------|
| deployment_coord | DeploymentCoordinator | event_driven | optional | deferred_after_ready |
| blue_green | BlueGreenDeployer | event_driven | optional | deferred_after_ready |
| canary_release | CanaryReleaseManager | event_driven | optional | deferred_after_ready |
| rollback | RollbackCoordinator | event_driven | optional | deferred_after_ready |
| data_pipeline | DataPipelineManager | event_driven | optional | deferred_after_ready |
| data_lake | DataLakeManager | batch_window | optional | deferred_after_ready |
| streaming_analytics | StreamingAnalyticsEngine | warm_standby | optional | deferred_after_ready |
| mlops_registry | MLOpsModelRegistry | event_driven | optional | deferred_after_ready |
| infra_provisioner | InfrastructureProvisionerManager | event_driven | optional | deferred_after_ready |
| api_gateway | APIGatewayManager | warm_standby | optional | deferred_after_ready |
| api_versioning | APIVersionManager | always_on | optional | deferred_after_ready |
| webhook_dispatch | WebhookDispatcher | event_driven | optional | deferred_after_ready |
| graph_db | GraphDatabaseManager | event_driven | optional | deferred_after_ready |
| search_engine | SearchEngineManager | warm_standby | optional | deferred_after_ready |
| integration_bus | IntegrationBusManager | warm_standby | optional | deferred_after_ready |
| tenant_manager | TenantManager | warm_standby | optional | deferred_after_ready |
| session_manager | SessionManager | always_on | optional | deferred_after_ready |
| document_mgmt | DocumentManagementSystem | event_driven | optional | deferred_after_ready |
| notification_hub | NotificationHub | event_driven | optional | deferred_after_ready |
| consent_mgmt | ConsentManagementSystem | event_driven | optional | deferred_after_ready |
| digital_signatures | DigitalSignatureService | event_driven | optional | deferred_after_ready |
| encryption | EncryptionServiceManager | always_on | optional | deferred_after_ready |
| workflow_orch | WorkflowOrchestrator | event_driven | optional | deferred_after_ready |
| template_engine | TemplateEngine | warm_standby | optional | deferred_after_ready |
| report_generator | ReportGenerator | batch_window | optional | deferred_after_ready |
| plugin_manager | PluginManager | warm_standby | optional | deferred_after_ready |
| localization | LocalizationManager | always_on | optional | deferred_after_ready |
| ab_testing | ABTestingFramework | event_driven | optional | deferred_after_ready |
| feature_flags | FeatureFlagManager | always_on | optional | deferred_after_ready |
| external_services | ExternalServiceRegistry | always_on | optional | deferred_after_ready |
| calendar | CalendarService | event_driven | optional | deferred_after_ready |
| command_patterns | CommandPatternManager | warm_standby | optional | deferred_after_ready |

**Key wiring (highest-value services):**
- DeploymentCoordinator + BlueGreenDeployer + CanaryReleaseManager: orchestrate model updates from Reactor Core through canary -> promote -> rollback pipeline
- DataPipelineManager: manage experience ingestion -> training data preparation flow
- MLOpsModelRegistry: track model versions, lineage, A/B test results
- StreamingAnalyticsEngine: real-time windowed metrics for SLO monitoring
- SearchEngineManager: semantic search over JARVIS conversation memory

**Go/no-go gate:**
- Boot time increase < 8 seconds cumulative (all waves)
- Model deployment workflow executes end-to-end (Reactor -> Canary -> Promote)
- No service in quarantine after 1-hour soak
- All 67+10 services appear in health dashboard

---

## Hidden Risks and Mitigations

### Risk 1: Dependency Cycles
**Mitigation:** Topological sort already exists in `SystemServiceRegistry._topological_sort()`. Add cycle detection that raises `DependencyCycleError` with the cycle path during registration, not activation. Prevent cycles at registration time.

### Risk 2: Boot-Time Inflation from Constructors
**Mitigation:** `boot_policy: deferred_after_ready` services are constructed but NOT initialized during boot. Their `initialize()` is called after the kernel reaches READY state. Constructor must be side-effect-free (no I/O, no network, no file access).

### Risk 3: Event-Loop Starvation
**Mitigation:** Every `SystemService.initialize()` and `start()` runs with `asyncio.wait_for(timeout)`. The `budget_gate` checks system CPU before activation. Services must use `asyncio.Lock` (never `threading.Lock`) in async paths.

### Risk 4: Cross-Service Feedback Loops
**Mitigation:** Add `feedback_loop_guard` to event routing. If service A triggers service B which triggers service A within 1 second, the second trigger is suppressed with a warning. Configurable per-service.

### Risk 5: Config Entropy (67 kill switches)
**Mitigation:** Hierarchical namespace: `JARVIS_SERVICE_<TIER>_ENABLED` controls entire tier, `JARVIS_SERVICE_<NAME>_ENABLED` controls individual service. Tier switch overrides individual switches. Example: `JARVIS_SERVICE_IMMUNE_ENABLED=false` disables all 8 immune services.

### Risk 6: State Ownership Drift
**Mitigation:** Each service's `CapabilityContract.side_effects` must declare what state it writes. Two services cannot declare the same side effect. This is validated at registration time.

### Risk 7: Health False Positives
**Mitigation:** `ServiceHealthReport` has separate `alive` (liveness) and `ready` (readiness) flags. A service can be alive but not ready (initializing). Health checks probe both. Dashboard shows 4 states: alive+ready, alive+degraded, alive+draining, dead.

### Risk 8: Silent Partial Activation
**Mitigation:** Event-driven services register their trigger topics at construction time. The `SupervisorEventBus` tracks registered listeners per topic. If a topic has zero listeners, it logs a warning. If a service registers but never receives its trigger event in 1 hour, it's flagged as "dormant" in the dashboard.

---

## Implementation Order

### Phase A: Class Hardening (Wave 0)
1. Extend ServiceDescriptor with new fields
2. Extend SystemService ABC with new methods
3. Update SystemServiceRegistry to support activation_mode, boot_policy, budget_gate
4. Add ServiceHealthReport, CapabilityContract, ActivationContract dataclasses
5. Add tier-level kill switches
6. Add dependency cycle detection at registration time
7. Add feedback loop guard to SupervisorEventBus
8. Verify 10 existing services still work with zero regression

### Phase B: Immune System (Wave 1)
9. Implement SystemService protocol on all 8 immune classes
10. Wire capability_contract() with real inputs/outputs for each
11. Wire activation_triggers() for event-driven immune services
12. Register all 8 in _init_service_registry() at phase 6
13. Connect SecurityPolicyEngine to agent action evaluation
14. Connect AnomalyDetector to ObservabilityPipeline telemetry feed
15. Connect AuditTrailRecorder to SupervisorEventBus
16. Soak test: 10-minute run with voice unlock + agent commands

### Phase C: Nervous System (Wave 2)
17. Implement SystemService protocol on all 12 nervous classes
18. Register all 12 at phase 7
19. Wire WorkflowEngine to agent task orchestration
20. Wire ConfigurationManager to replace critical os.getenv() paths
21. Wire SchemaRegistry to cross-repo boot validation
22. Wire CronScheduler to Reactor Core training schedule
23. Soak test: 30-minute run with model routing + Trinity events

### Phase D: Metabolic System (Wave 3)
24. Implement SystemService protocol on all 15 metabolic classes
25. Register all 15 at phase 7
26. Wire ServiceMeshRouter to replace hardcoded HTTP calls
27. Wire LoadSheddingController to memory pressure events
28. Wire SecretVaultManager to centralize credential management
29. Soak test: 1-hour run under simulated memory pressure

### Phase E: Higher Functions (Wave 4)
30. Implement SystemService protocol on all 32 higher-function classes
31. Register all 32 at phase 8
32. Wire deployment pipeline (DeploymentCoordinator -> BlueGreen -> Canary -> Rollback)
33. Wire data pipeline (DataPipelineManager -> DataLakeManager -> MLOpsModelRegistry)
34. Soak test: 2-hour run with model deployment + training cycle

---

## Success Criteria

1. **All 77 services** (10 existing + 67 activated) appear in health dashboard
2. **Boot time** < 30 seconds to READY state (deferred services activate after)
3. **Zero regression** on existing startup flow
4. **No restart oscillation** in 2-hour soak under normal load
5. **Correlation IDs** flow from JARVIS -> Prime -> Reactor through activated services
6. **Model deployment** executes end-to-end through canary pipeline
7. **Security policy** blocks simulated injection attack
8. **Anomaly detection** flags simulated replay attack on voice unlock
9. **Load shedding** activates correctly at 85% memory threshold
10. **Every service** has a filled CapabilityContract (no empty declarations)

---

## Non-Negotiable Guardrails

- **No service activates without all 3 contracts** (Capability, Lifecycle, Activation)
- **No service without a kill switch** (JARVIS_SERVICE_<NAME>_ENABLED)
- **No blocking I/O in async service paths** (enforced by contract test)
- **No service writes state it doesn't own** (validated at registration)
- **No dependency cycles** (detected at registration, not runtime)
- **Monotonic deadlines** propagated through service calls
- **Quarantine after repeated failures** (not infinite retry)
- **Global safe mode** disables all optional services with one env var: JARVIS_SAFE_MODE=true
