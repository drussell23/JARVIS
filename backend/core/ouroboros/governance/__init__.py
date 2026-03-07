"""
Ouroboros Governance Layer
=========================

Deterministic policy enforcement for autonomous self-programming.
All risk classification, operation identity, and lifecycle authority
lives here. No LLM calls in this package -- pure rule-based logic.

Phase 0 Components:
    - OperationID: UUIDv7-based globally unique operation identity
    - RiskEngine: Deterministic policy classifier (SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED)
    - ContractGate: Schema version compatibility enforcement
    - SupervisorController: Lifecycle authority bridge to unified_supervisor
    - CommProtocol: Mandatory 5-phase communication emitter
    - OperationLedger: Append-only operation state log

Phase 1 Components:
    - GovernanceLockManager: Hierarchical read/write lease locks (8 levels)
    - BreakGlassManager: Time-limited tokens for BLOCKED operation promotion
    - ChangeEngine: 8-phase transactional change pipeline with rollback
    - TUITransport: Fault-isolated TUI transport for CommProtocol

Phase 2 Components:
    - ResourceMonitor: Multi-signal pressure collection (RAM/CPU/IO/latency)
    - DegradationController: 4-mode autonomy state machine
    - RoutingPolicy: Deterministic task routing with cost guardrails
    - MultiFileChangeEngine: Atomic multi-file operations with rollback

Phase 3 Components:
    - EventBridge: Governance-to-CrossRepo event mapping (fault-isolated)
    - BlastRadiusAdapter: Oracle integration for auto-populating blast radius
    - LearningBridge: Operation feedback to LearningMemory with op_id correlation
    - RuntimeContractChecker: N/N-1 schema validation at runtime
    - CanaryController: Per-domain-slice promotion with rollout criteria
    - CLICommands: Importable break-glass functions for supervisor CLI
"""

from backend.core.ouroboros.governance.operation_id import (
    generate_operation_id,
    OperationMetadata,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    RiskTier,
    RiskClassification,
    OperationProfile,
    ChangeType,
    HardInvariantViolation,
    POLICY_VERSION,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    LedgerEntry,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    CommMessage,
    MessageType,
    LogTransport,
)
from backend.core.ouroboros.governance.supervisor_controller import (
    SupervisorOuroborosController,
    AutonomyMode,
)
from backend.core.ouroboros.governance.contract_gate import (
    ContractGate,
    ContractVersion,
    CompatibilityResult,
    BootCheckResult,
)
from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
    LeaseHandle,
    LockOrderViolation,
    FencingTokenError,
    LOCK_TTLS,
)
from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
    BreakGlassToken,
    BreakGlassAuditEntry,
    BreakGlassExpired,
    BreakGlassScopeMismatch,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangeResult,
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.tui_transport import (
    TUITransport,
    TUIMessageFormatter,
)
from backend.core.ouroboros.governance.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    PressureLevel,
    PRESSURE_THRESHOLDS,
)
from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
    DegradationReason,
    ModeTransition,
)
from backend.core.ouroboros.governance.routing_policy import (
    RoutingPolicy,
    RoutingDecision,
    TaskCategory,
    CostGuardrail,
)
from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
    MultiFileChangeResult,
)
from backend.core.ouroboros.governance.event_bridge import (
    EventBridge,
    GovernanceEventMapper,
)
from backend.core.ouroboros.governance.blast_radius_adapter import (
    BlastRadiusAdapter,
    BlastRadiusResult,
)
from backend.core.ouroboros.governance.learning_bridge import (
    LearningBridge,
    OperationOutcome,
)
from backend.core.ouroboros.governance.runtime_contracts import (
    RuntimeContractChecker,
    ContractCheckResult,
    ContractViolation,
)
from backend.core.ouroboros.governance.canary_controller import (
    CanaryController,
    DomainSlice,
    SliceMetrics,
    PromotionResult,
    CanaryState,
)
from backend.core.ouroboros.governance.cli_commands import (
    issue_break_glass,
    list_active_tokens,
    revoke_break_glass,
    get_audit_report,
)
