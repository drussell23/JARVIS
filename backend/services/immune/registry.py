"""Registry module for immune-tier (Wave 1) services."""
from backend.core.component_registry import (
    ComponentDefinition, ComponentRegistry, Criticality, ProcessType,
    PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
    ResourceBudget, FailurePolicy, RetryStrategy, StateDomain,
    OwnershipMode, ObservabilityContract, HealthPolicy,
)

_SHARED_FAILURE_POLICY = FailurePolicy(
    retry_strategy=RetryStrategy.EXP_BACKOFF_JITTER,
    max_retries=3, backoff_base_s=1.0, backoff_max_s=30.0,
    circuit_breaker=True, breaker_threshold=5,
    breaker_recovery_s=60.0, quarantine_on_repeated=True,
)

_SHARED_OBSERVABILITY = ObservabilityContract(
    schema_version="1.0", emit_trace_id=True, emit_reason_codes=True,
    required_log_fields=("trace_id", "reason_code", "service_name",
                         "activation_mode", "readiness_class"),
    health_check_interval_s=30.0,
)

_IMMUNE_COMMON = dict(
    process_type=ProcessType.IN_PROCESS,
    promotion_level=PromotionLevel.PROMOTED,
    activation_tier=ActivationTier.IMMUNE,
    failure_policy_gov=_SHARED_FAILURE_POLICY,
    observability_contract=_SHARED_OBSERVABILITY,
    health_policy=HealthPolicy(supports_drain=True),
    constructor_pure=True,
    contract_version="1.0.0",
    tier_kill_switch_env="JARVIS_TIER_IMMUNE_ENABLED",
    max_dependency_tier=ActivationTier.FOUNDATION,
)

IMMUNE_SERVICE_DEFINITIONS = [
    ComponentDefinition(
        name="security_policy_engine",
        criticality=Criticality.REQUIRED,
        activation_mode=ActivationMode.ALWAYS_ON,
        readiness_class=ReadinessClass.BLOCK_READY,
        resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
        state_domain=StateDomain("security_policy", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_SECURITY_POLICY_ENGINE_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="anomaly_detector",
        criticality=Criticality.DEGRADED_OK,
        activation_mode=ActivationMode.WARM_STANDBY,
        readiness_class=ReadinessClass.NON_BLOCKING,
        resource_budget=ResourceBudget(128, 15.0, 2, 45.0),
        state_domain=StateDomain("anomaly_detection", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_ANOMALY_DETECTOR_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="audit_trail_recorder",
        criticality=Criticality.DEGRADED_OK,
        activation_mode=ActivationMode.ALWAYS_ON,
        readiness_class=ReadinessClass.NON_BLOCKING,
        resource_budget=ResourceBudget(32, 5.0, 8, 20.0),
        state_domain=StateDomain("audit_trail", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_AUDIT_TRAIL_RECORDER_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="threat_intelligence_manager",
        criticality=Criticality.OPTIONAL,
        activation_mode=ActivationMode.EVENT_DRIVEN,
        readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        resource_budget=ResourceBudget(96, 10.0, 2, 60.0),
        state_domain=StateDomain("threat_intel", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_THREAT_INTELLIGENCE_MANAGER_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="incident_response_coordinator",
        criticality=Criticality.OPTIONAL,
        activation_mode=ActivationMode.EVENT_DRIVEN,
        readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        resource_budget=ResourceBudget(64, 8.0, 1, 30.0),
        state_domain=StateDomain("incident_response", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_INCIDENT_RESPONSE_COORDINATOR_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="compliance_auditor",
        criticality=Criticality.OPTIONAL,
        activation_mode=ActivationMode.BATCH_WINDOW,
        readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        resource_budget=ResourceBudget(48, 8.0, 1, 45.0),
        state_domain=StateDomain("compliance_state", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_COMPLIANCE_AUDITOR_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="data_classification_manager",
        criticality=Criticality.DEGRADED_OK,
        activation_mode=ActivationMode.WARM_STANDBY,
        readiness_class=ReadinessClass.NON_BLOCKING,
        resource_budget=ResourceBudget(32, 5.0, 2, 20.0),
        state_domain=StateDomain("data_classification", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_DATA_CLASSIFICATION_MANAGER_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="access_control_manager",
        criticality=Criticality.REQUIRED,
        activation_mode=ActivationMode.ALWAYS_ON,
        readiness_class=ReadinessClass.BLOCK_READY,
        resource_budget=ResourceBudget(48, 8.0, 4, 30.0),
        state_domain=StateDomain("access_control", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_ACCESS_CONTROL_MANAGER_ENABLED",
        **_IMMUNE_COMMON,
    ),
]


def register_immune_services(registry: ComponentRegistry) -> None:
    """Register all 8 immune-tier services with full promoted contracts."""
    for defn in IMMUNE_SERVICE_DEFINITIONS:
        registry.register(defn)
