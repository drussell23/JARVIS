"""Tests for governance type definitions."""


class TestGovernanceEnums:
    def test_promotion_level(self):
        from backend.core.component_registry import PromotionLevel
        assert PromotionLevel.LEGACY.value == "legacy"
        assert PromotionLevel.PROMOTED.value == "promoted"

    def test_activation_mode(self):
        from backend.core.component_registry import ActivationMode
        assert len(ActivationMode) == 4
        assert hasattr(ActivationMode, "ALWAYS_ON")
        assert hasattr(ActivationMode, "WARM_STANDBY")
        assert hasattr(ActivationMode, "EVENT_DRIVEN")
        assert hasattr(ActivationMode, "BATCH_WINDOW")

    def test_readiness_class(self):
        from backend.core.component_registry import ReadinessClass
        assert len(ReadinessClass) == 3
        assert hasattr(ReadinessClass, "BLOCK_READY")
        assert hasattr(ReadinessClass, "NON_BLOCKING")
        assert hasattr(ReadinessClass, "DEFERRED_AFTER_READY")

    def test_activation_tier_is_int_enum(self):
        from backend.core.component_registry import ActivationTier
        assert ActivationTier.FOUNDATION < ActivationTier.IMMUNE
        assert ActivationTier.IMMUNE < ActivationTier.NERVOUS
        assert ActivationTier.NERVOUS < ActivationTier.METABOLIC
        assert ActivationTier.METABOLIC < ActivationTier.HIGHER
        # IntEnum comparison
        assert ActivationTier.FOUNDATION == 0
        assert ActivationTier.HIGHER == 4

    def test_retry_strategy(self):
        from backend.core.component_registry import RetryStrategy
        assert len(RetryStrategy) == 4

    def test_ownership_mode(self):
        from backend.core.component_registry import OwnershipMode
        assert hasattr(OwnershipMode, "EXCLUSIVE_WRITE")
        assert hasattr(OwnershipMode, "SHARED_READ_ONLY")


class TestGovernanceDataclasses:
    def test_resource_budget_frozen(self):
        from backend.core.component_registry import ResourceBudget
        b = ResourceBudget(max_memory_mb=64, max_cpu_percent=10.0,
                           max_concurrency=4, max_startup_time_s=30.0)
        assert b.max_memory_mb == 64
        import pytest
        with pytest.raises(AttributeError):
            b.max_memory_mb = 128  # frozen

    def test_failure_policy_uses_retry_strategy_enum(self):
        from backend.core.component_registry import FailurePolicy, RetryStrategy
        fp = FailurePolicy(
            retry_strategy=RetryStrategy.EXP_BACKOFF_JITTER,
            max_retries=3, backoff_base_s=1.0, backoff_max_s=30.0,
            circuit_breaker=True, breaker_threshold=5,
            breaker_recovery_s=60.0, quarantine_on_repeated=True,
        )
        assert fp.retry_strategy == RetryStrategy.EXP_BACKOFF_JITTER

    def test_state_domain_frozen(self):
        from backend.core.component_registry import StateDomain, OwnershipMode
        sd = StateDomain(domain="security_policy", ownership_mode=OwnershipMode.EXCLUSIVE_WRITE)
        assert sd.domain == "security_policy"

    def test_observability_contract_defaults(self):
        from backend.core.component_registry import ObservabilityContract
        oc = ObservabilityContract()
        assert oc.schema_version == "1.0"
        assert oc.emit_trace_id is True
        assert "trace_id" in oc.required_log_fields

    def test_health_policy_defaults(self):
        from backend.core.component_registry import HealthPolicy
        hp = HealthPolicy()
        assert hp.supports_liveness is True
        assert hp.hysteresis_window == 3


class TestComponentDefinitionGovernanceFields:
    def test_legacy_default(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
        )
        defn = ComponentDefinition(
            name="test", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.promotion_level == PromotionLevel.LEGACY
        assert defn.activation_mode is None
        assert defn.constructor_pure is False

    def test_promoted_all_fields(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ActivationMode, ReadinessClass, ActivationTier, ResourceBudget,
            FailurePolicy, RetryStrategy, StateDomain, OwnershipMode,
            ObservabilityContract, HealthPolicy,
        )
        defn = ComponentDefinition(
            name="test_promoted",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.BLOCK_READY,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain("test_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            health_policy=HealthPolicy(),
            constructor_pure=True,
            contract_version="1.0.0",
        )
        assert defn.promotion_level == PromotionLevel.PROMOTED
        assert defn.activation_tier == ActivationTier.IMMUNE
