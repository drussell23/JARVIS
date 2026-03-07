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
