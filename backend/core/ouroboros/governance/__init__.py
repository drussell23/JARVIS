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

Integration Components:
    - GovernanceMode: Operating mode enum (PENDING/SANDBOX/READ_ONLY_PLANNING/GOVERNED/EMERGENCY_STOP)
    - GovernanceConfig: Frozen configuration with policy hashes
    - GovernanceStack: Component holder with lifecycle, write gate, and replay
    - create_governance_stack: Factory with timeout and partial-init rollback
    - register_governance_argparse: CLI flag registration
    - handle_break_glass_command: Break-glass CLI dispatch
"""

# ── Lazy export surface (Supervisor Campaign Step 2, 2026-07-19) ──
# This init cost 1.16s and dragged torch into EVERY governance
# subimport (audio_state_ipc paid it; so did every thin client).
# PEP 562 — same cure as backend/core/ouroboros/__init__.py.
_LAZY_EXPORTS = {
    'AdapterResult': ('backend.core.ouroboros.governance.test_runner', 'AdapterResult'),
    'AffectedFile': ('backend.core.ouroboros.governance.multi_repo', 'AffectedFile'),
    'ApprovalProvider': ('backend.core.ouroboros.governance.approval_provider', 'ApprovalProvider'),
    'ApprovalResult': ('backend.core.ouroboros.governance.approval_provider', 'ApprovalResult'),
    'ApprovalStatus': ('backend.core.ouroboros.governance.approval_provider', 'ApprovalStatus'),
    'AutonomyGate': ('backend.core.ouroboros.governance.autonomy', 'AutonomyGate'),
    'AutonomyMode': ('backend.core.ouroboros.governance.supervisor_controller', 'AutonomyMode'),
    'AutonomyState': ('backend.core.ouroboros.governance.autonomy', 'AutonomyState'),
    'AutonomyTier': ('backend.core.ouroboros.governance.autonomy', 'AutonomyTier'),
    'BacklogSensor': ('backend.core.ouroboros.governance.intake.sensors', 'BacklogSensor'),
    'BacklogTask': ('backend.core.ouroboros.governance.intake.sensors', 'BacklogTask'),
    'BlastRadiusAdapter': ('backend.core.ouroboros.governance.blast_radius_adapter', 'BlastRadiusAdapter'),
    'BlastRadiusReport': ('backend.core.ouroboros.governance.multi_repo', 'BlastRadiusReport'),
    'BlastRadiusResult': ('backend.core.ouroboros.governance.blast_radius_adapter', 'BlastRadiusResult'),
    'BlockedPathError': ('backend.core.ouroboros.governance.test_runner', 'BlockedPathError'),
    'BootCheckResult': ('backend.core.ouroboros.governance.contract_gate', 'BootCheckResult'),
    'BreakGlassAuditEntry': ('backend.core.ouroboros.governance.break_glass', 'BreakGlassAuditEntry'),
    'BreakGlassExpired': ('backend.core.ouroboros.governance.break_glass', 'BreakGlassExpired'),
    'BreakGlassManager': ('backend.core.ouroboros.governance.break_glass', 'BreakGlassManager'),
    'BreakGlassScopeMismatch': ('backend.core.ouroboros.governance.break_glass', 'BreakGlassScopeMismatch'),
    'BreakGlassToken': ('backend.core.ouroboros.governance.break_glass', 'BreakGlassToken'),
    'CAISnapshot': ('backend.core.ouroboros.governance.autonomy', 'CAISnapshot'),
    'CLIApprovalProvider': ('backend.core.ouroboros.governance.approval_provider', 'CLIApprovalProvider'),
    'CanaryController': ('backend.core.ouroboros.governance.canary_controller', 'CanaryController'),
    'CanaryState': ('backend.core.ouroboros.governance.canary_controller', 'CanaryState'),
    'CandidateGenerator': ('backend.core.ouroboros.governance.candidate_generator', 'CandidateGenerator'),
    'CandidateProvider': ('backend.core.ouroboros.governance.candidate_generator', 'CandidateProvider'),
    'CapabilityStatus': ('backend.core.ouroboros.governance.integration', 'CapabilityStatus'),
    'ChangeEngine': ('backend.core.ouroboros.governance.change_engine', 'ChangeEngine'),
    'ChangePhase': ('backend.core.ouroboros.governance.change_engine', 'ChangePhase'),
    'ChangeRequest': ('backend.core.ouroboros.governance.change_engine', 'ChangeRequest'),
    'ChangeResult': ('backend.core.ouroboros.governance.change_engine', 'ChangeResult'),
    'ChangeType': ('backend.core.ouroboros.governance.risk_engine', 'ChangeType'),
    'ClaudeProvider': ('backend.core.ouroboros.governance.providers', 'ClaudeProvider'),
    'CognitiveLoad': ('backend.core.ouroboros.governance.autonomy', 'CognitiveLoad'),
    'CommMessage': ('backend.core.ouroboros.governance.comm_protocol', 'CommMessage'),
    'CommProtocol': ('backend.core.ouroboros.governance.comm_protocol', 'CommProtocol'),
    'CompareMode': ('backend.core.ouroboros.governance.shadow_harness', 'CompareMode'),
    'CompatibilityResult': ('backend.core.ouroboros.governance.contract_gate', 'CompatibilityResult'),
    'CompletionSummary': ('backend.core.ouroboros.governance.comms', 'CompletionSummary'),
    'ContextBuilder': ('backend.core.ouroboros.governance.multi_repo', 'ContextBuilder'),
    'ContextFile': ('backend.core.ouroboros.governance.multi_repo', 'ContextFile'),
    'ContractCheckResult': ('backend.core.ouroboros.governance.runtime_contracts', 'ContractCheckResult'),
    'ContractGate': ('backend.core.ouroboros.governance.contract_gate', 'ContractGate'),
    'ContractVersion': ('backend.core.ouroboros.governance.contract_gate', 'ContractVersion'),
    'ContractViolation': ('backend.core.ouroboros.governance.runtime_contracts', 'ContractViolation'),
    'CostGuardrail': ('backend.core.ouroboros.governance.routing_policy', 'CostGuardrail'),
    'CppAdapter': ('backend.core.ouroboros.governance.test_runner', 'CppAdapter'),
    'CrossRepoBlastRadius': ('backend.core.ouroboros.governance.multi_repo', 'CrossRepoBlastRadius'),
    'CrossRepoContext': ('backend.core.ouroboros.governance.multi_repo', 'CrossRepoContext'),
    'DedupTracker': ('backend.core.ouroboros.governance.intent', 'DedupTracker'),
    'DegradationController': ('backend.core.ouroboros.governance.degradation', 'DegradationController'),
    'DegradationMode': ('backend.core.ouroboros.governance.degradation', 'DegradationMode'),
    'DegradationReason': ('backend.core.ouroboros.governance.degradation', 'DegradationReason'),
    'DomainSlice': ('backend.core.ouroboros.governance.canary_controller', 'DomainSlice'),
    'EnvelopeValidationError': ('backend.core.ouroboros.governance.intake', 'EnvelopeValidationError'),
    'ErrorInterceptor': ('backend.core.ouroboros.governance.intent', 'ErrorInterceptor'),
    'EventBridge': ('backend.core.ouroboros.governance.event_bridge', 'EventBridge'),
    'FailbackState': ('backend.core.ouroboros.governance.candidate_generator', 'FailbackState'),
    'FailbackStateMachine': ('backend.core.ouroboros.governance.candidate_generator', 'FailbackStateMachine'),
    'FencingTokenError': ('backend.core.ouroboros.governance.lock_manager', 'FencingTokenError'),
    'FileMatch': ('backend.core.ouroboros.governance.multi_repo', 'FileMatch'),
    'GenerationResult': ('backend.core.ouroboros.governance.op_context', 'GenerationResult'),
    'GovernanceConfig': ('backend.core.ouroboros.governance.integration', 'GovernanceConfig'),
    'GovernanceEventMapper': ('backend.core.ouroboros.governance.event_bridge', 'GovernanceEventMapper'),
    'GovernanceInitError': ('backend.core.ouroboros.governance.integration', 'GovernanceInitError'),
    'GovernanceLockManager': ('backend.core.ouroboros.governance.lock_manager', 'GovernanceLockManager'),
    'GovernanceMode': ('backend.core.ouroboros.governance.integration', 'GovernanceMode'),
    'GovernanceStack': ('backend.core.ouroboros.governance.integration', 'GovernanceStack'),
    'GovernedLoopConfig': ('backend.core.ouroboros.governance.governed_loop_service', 'GovernedLoopConfig'),
    'GovernedLoopService': ('backend.core.ouroboros.governance.governed_loop_service', 'GovernedLoopService'),
    'GovernedOrchestrator': ('backend.core.ouroboros.governance.orchestrator', 'GovernedOrchestrator'),
    'GraduationMetrics': ('backend.core.ouroboros.governance.autonomy', 'GraduationMetrics'),
    'HardInvariantViolation': ('backend.core.ouroboros.governance.risk_engine', 'HardInvariantViolation'),
    'INTAKE_SCHEMA_VERSION': ('backend.core.ouroboros.governance.intake', 'SCHEMA_VERSION'),
    'IntakeLayerConfig': ('backend.core.ouroboros.governance.intake', 'IntakeLayerConfig'),
    'IntakeLayerService': ('backend.core.ouroboros.governance.intake', 'IntakeLayerService'),
    'IntakeNarrator': ('backend.core.ouroboros.governance.intake', 'IntakeNarrator'),
    'IntakeRouterConfig': ('backend.core.ouroboros.governance.intake', 'IntakeRouterConfig'),
    'IntakeServiceState': ('backend.core.ouroboros.governance.intake', 'IntakeServiceState'),
    'IntentEngine': ('backend.core.ouroboros.governance.intent', 'IntentEngine'),
    'IntentEngineConfig': ('backend.core.ouroboros.governance.intent', 'IntentEngineConfig'),
    'IntentEnvelope': ('backend.core.ouroboros.governance.intake', 'IntentEnvelope'),
    'IntentSignal': ('backend.core.ouroboros.governance.intent', 'IntentSignal'),
    'LOCK_TTLS': ('backend.core.ouroboros.governance.lock_manager', 'LOCK_TTLS'),
    'LanguageRouter': ('backend.core.ouroboros.governance.test_runner', 'LanguageRouter'),
    'LearningBridge': ('backend.core.ouroboros.governance.learning_bridge', 'LearningBridge'),
    'LeaseHandle': ('backend.core.ouroboros.governance.lock_manager', 'LeaseHandle'),
    'LedgerEntry': ('backend.core.ouroboros.governance.ledger', 'LedgerEntry'),
    'LockLevel': ('backend.core.ouroboros.governance.lock_manager', 'LockLevel'),
    'LockMode': ('backend.core.ouroboros.governance.lock_manager', 'LockMode'),
    'LockOrderViolation': ('backend.core.ouroboros.governance.lock_manager', 'LockOrderViolation'),
    'LogTransport': ('backend.core.ouroboros.governance.comm_protocol', 'LogTransport'),
    'MessageType': ('backend.core.ouroboros.governance.comm_protocol', 'MessageType'),
    'ModeTransition': ('backend.core.ouroboros.governance.degradation', 'ModeTransition'),
    'MultiAdapterResult': ('backend.core.ouroboros.governance.test_runner', 'MultiAdapterResult'),
    'MultiFileChangeEngine': ('backend.core.ouroboros.governance.multi_file_engine', 'MultiFileChangeEngine'),
    'MultiFileChangeRequest': ('backend.core.ouroboros.governance.multi_file_engine', 'MultiFileChangeRequest'),
    'MultiFileChangeResult': ('backend.core.ouroboros.governance.multi_file_engine', 'MultiFileChangeResult'),
    'OperationContext': ('backend.core.ouroboros.governance.op_context', 'OperationContext'),
    'OperationLedger': ('backend.core.ouroboros.governance.ledger', 'OperationLedger'),
    'OperationMetadata': ('backend.core.ouroboros.governance.operation_id', 'OperationMetadata'),
    'OperationOutcome': ('backend.core.ouroboros.governance.learning_bridge', 'OperationOutcome'),
    'OperationPhase': ('backend.core.ouroboros.governance.op_context', 'OperationPhase'),
    'OperationProfile': ('backend.core.ouroboros.governance.risk_engine', 'OperationProfile'),
    'OperationResult': ('backend.core.ouroboros.governance.governed_loop_service', 'OperationResult'),
    'OperationState': ('backend.core.ouroboros.governance.ledger', 'OperationState'),
    'OpportunityMinerSensor': ('backend.core.ouroboros.governance.intake.sensors', 'OpportunityMinerSensor'),
    'OpsLogger': ('backend.core.ouroboros.governance.comms', 'OpsLogger'),
    'OrchestratorConfig': ('backend.core.ouroboros.governance.orchestrator', 'OrchestratorConfig'),
    'OutputComparator': ('backend.core.ouroboros.governance.shadow_harness', 'OutputComparator'),
    'PHASE_TRANSITIONS': ('backend.core.ouroboros.governance.op_context', 'PHASE_TRANSITIONS'),
    'POLICY_VERSION': ('backend.core.ouroboros.governance.risk_engine', 'POLICY_VERSION'),
    'PRESSURE_THRESHOLDS': ('backend.core.ouroboros.governance.resource_monitor', 'PRESSURE_THRESHOLDS'),
    'PipelineStatus': ('backend.core.ouroboros.governance.comms', 'PipelineStatus'),
    'PressureLevel': ('backend.core.ouroboros.governance.resource_monitor', 'PressureLevel'),
    'PrimeProvider': ('backend.core.ouroboros.governance.providers', 'PrimeProvider'),
    'PromotionResult': ('backend.core.ouroboros.governance.canary_controller', 'PromotionResult'),
    'PythonAdapter': ('backend.core.ouroboros.governance.test_runner', 'PythonAdapter'),
    'RateLimiter': ('backend.core.ouroboros.governance.intent', 'RateLimiter'),
    'RateLimiterConfig': ('backend.core.ouroboros.governance.intent', 'RateLimiterConfig'),
    'ReactorEventConsumer': ('backend.core.ouroboros.governance.reactor_event_consumer', 'ReactorEventConsumer'),
    'ReadyToCommitPayload': ('backend.core.ouroboros.governance.governed_loop_service', 'ReadyToCommitPayload'),
    'RepoConfig': ('backend.core.ouroboros.governance.multi_repo', 'RepoConfig'),
    'RepoPipelineManager': ('backend.core.ouroboros.governance.multi_repo', 'RepoPipelineManager'),
    'RepoRegistry': ('backend.core.ouroboros.governance.multi_repo', 'RepoRegistry'),
    'ResourceMonitor': ('backend.core.ouroboros.governance.resource_monitor', 'ResourceMonitor'),
    'ResourceSnapshot': ('backend.core.ouroboros.governance.resource_monitor', 'ResourceSnapshot'),
    'RiskClassification': ('backend.core.ouroboros.governance.risk_engine', 'RiskClassification'),
    'RiskEngine': ('backend.core.ouroboros.governance.risk_engine', 'RiskEngine'),
    'RiskTier': ('backend.core.ouroboros.governance.risk_engine', 'RiskTier'),
    'RollbackArtifact': ('backend.core.ouroboros.governance.change_engine', 'RollbackArtifact'),
    'RouterAlreadyRunningError': ('backend.core.ouroboros.governance.intake', 'RouterAlreadyRunningError'),
    'RoutingDecision': ('backend.core.ouroboros.governance.routing_policy', 'RoutingDecision'),
    'RoutingPolicy': ('backend.core.ouroboros.governance.routing_policy', 'RoutingPolicy'),
    'RuntimeContractChecker': ('backend.core.ouroboros.governance.runtime_contracts', 'RuntimeContractChecker'),
    'SAISnapshot': ('backend.core.ouroboros.governance.autonomy', 'SAISnapshot'),
    'SelfProgramPanelState': ('backend.core.ouroboros.governance.comms', 'SelfProgramPanelState'),
    'ServiceState': ('backend.core.ouroboros.governance.governed_loop_service', 'ServiceState'),
    'ShadowHarness': ('backend.core.ouroboros.governance.shadow_harness', 'ShadowHarness'),
    'ShadowModeViolation': ('backend.core.ouroboros.governance.shadow_harness', 'ShadowModeViolation'),
    'ShadowResult': ('backend.core.ouroboros.governance.shadow_harness', 'ShadowResult'),
    'SideEffectFirewall': ('backend.core.ouroboros.governance.shadow_harness', 'SideEffectFirewall'),
    'SignalAutonomyConfig': ('backend.core.ouroboros.governance.autonomy', 'SignalAutonomyConfig'),
    'SliceMetrics': ('backend.core.ouroboros.governance.canary_controller', 'SliceMetrics'),
    'StaticCandidate': ('backend.core.ouroboros.governance.intake.sensors', 'StaticCandidate'),
    'SupervisorOuroborosController': ('backend.core.ouroboros.governance.supervisor_controller', 'SupervisorOuroborosController'),
    'TERMINAL_PHASES': ('backend.core.ouroboros.governance.op_context', 'TERMINAL_PHASES'),
    'TIER_ORDER': ('backend.core.ouroboros.governance.autonomy', 'TIER_ORDER'),
    'TUIMessageFormatter': ('backend.core.ouroboros.governance.tui_transport', 'TUIMessageFormatter'),
    'TUISelfProgramPanel': ('backend.core.ouroboros.governance.comms', 'TUISelfProgramPanel'),
    'TUITransport': ('backend.core.ouroboros.governance.tui_transport', 'TUITransport'),
    'TaskCategory': ('backend.core.ouroboros.governance.routing_policy', 'TaskCategory'),
    'TestFailure': ('backend.core.ouroboros.governance.intent', 'TestFailure'),
    'TestFailureSensor': ('backend.core.ouroboros.governance.intake.sensors', 'TestFailureSensor'),
    'TestWatcher': ('backend.core.ouroboros.governance.intent', 'TestWatcher'),
    'TrustGraduator': ('backend.core.ouroboros.governance.autonomy', 'TrustGraduator'),
    'UAESnapshot': ('backend.core.ouroboros.governance.autonomy', 'UAESnapshot'),
    'UnifiedIntakeRouter': ('backend.core.ouroboros.governance.intake', 'UnifiedIntakeRouter'),
    'ValidationResult': ('backend.core.ouroboros.governance.op_context', 'ValidationResult'),
    'VoiceCommandPayload': ('backend.core.ouroboros.governance.intake.sensors', 'VoiceCommandPayload'),
    'VoiceCommandSensor': ('backend.core.ouroboros.governance.intake.sensors', 'VoiceCommandSensor'),
    'VoiceNarrator': ('backend.core.ouroboros.governance.comms', 'VoiceNarrator'),
    'WAL': ('backend.core.ouroboros.governance.intake', 'WAL'),
    'WALEntry': ('backend.core.ouroboros.governance.intake', 'WALEntry'),
    'WorkContext': ('backend.core.ouroboros.governance.autonomy', 'WorkContext'),
    'create_governance_stack': ('backend.core.ouroboros.governance.integration', 'create_governance_stack'),
    'generate_operation_id': ('backend.core.ouroboros.governance.operation_id', 'generate_operation_id'),
    'get_audit_report': ('backend.core.ouroboros.governance.cli_commands', 'get_audit_report'),
    'handle_approve': ('backend.core.ouroboros.governance.loop_cli', 'handle_approve'),
    'handle_break_glass_command': ('backend.core.ouroboros.governance.integration', 'handle_break_glass_command'),
    'handle_reject': ('backend.core.ouroboros.governance.loop_cli', 'handle_reject'),
    'handle_self_modify': ('backend.core.ouroboros.governance.loop_cli', 'handle_self_modify'),
    'handle_status': ('backend.core.ouroboros.governance.loop_cli', 'handle_status'),
    'issue_break_glass': ('backend.core.ouroboros.governance.cli_commands', 'issue_break_glass'),
    'list_active_tokens': ('backend.core.ouroboros.governance.cli_commands', 'list_active_tokens'),
    'make_intent_envelope': ('backend.core.ouroboros.governance.intake', 'make_envelope'),
    'register_governance_argparse': ('backend.core.ouroboros.governance.integration', 'register_governance_argparse'),
    'revoke_break_glass': ('backend.core.ouroboros.governance.cli_commands', 'revoke_break_glass'),
}


def __getattr__(name):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    import importlib
    value = getattr(importlib.import_module(entry[0]), entry[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(list(globals()) + list(_LAZY_EXPORTS))
