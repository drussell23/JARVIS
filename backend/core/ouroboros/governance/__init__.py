"""
Ouroboros Governance Layer
=========================

Deterministic policy enforcement for autonomous self-programming.
All risk classification, operation identity, and lifecycle authority
lives here. No LLM calls in this package -- pure rule-based logic.

Components:
    - OperationID: UUIDv7-based globally unique operation identity
    - RiskEngine: Deterministic policy classifier (SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED)
    - ContractGate: Schema version compatibility enforcement
    - SupervisorController: Lifecycle authority bridge to unified_supervisor
    - CommProtocol: Mandatory 5-phase communication emitter
    - OperationLedger: Append-only operation state log
"""
