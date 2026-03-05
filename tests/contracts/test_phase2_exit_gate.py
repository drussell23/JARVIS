"""Phase 2 Exit Gate: governance infrastructure must enforce all invariants."""
import os
import pytest
from unittest.mock import patch


class TestPhase2ExitGate:
    def test_inv_g1_promoted_requires_complete_contract(self):
        """INV-G1: Promoted services cannot register without complete contracts."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ComponentRegistry,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="incomplete", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
        )
        with pytest.raises(ValueError):
            reg.register(defn)

    def test_inv_g2_state_domain_conflict(self):
        """INV-G2: One writer per state domain."""
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
            ResourceBudget, FailurePolicy, RetryStrategy, StateDomain,
            OwnershipMode, ObservabilityContract,
        )
        reg = ComponentRegistry()
        base = dict(
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.NON_BLOCKING,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
        )
        defn1 = ComponentDefinition(
            name="svc_a",
            state_domain=StateDomain("exclusive_domain", OwnershipMode.EXCLUSIVE_WRITE),
            **base,
        )
        defn2 = ComponentDefinition(
            name="svc_b",
            state_domain=StateDomain("exclusive_domain", OwnershipMode.EXCLUSIVE_WRITE),
            **base,
        )
        reg.register(defn1)
        with pytest.raises(ValueError, match="already owned"):
            reg.register(defn2)

    def test_inv_g3_cross_tier_blocked(self):
        """INV-G3: No upward cross-tier dependencies without allowlist."""
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
            ResourceBudget, FailurePolicy, RetryStrategy, StateDomain,
            OwnershipMode, ObservabilityContract,
        )
        reg = ComponentRegistry()
        higher = ComponentDefinition(
            name="higher_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            activation_tier=ActivationTier.METABOLIC,
        )
        reg.register(higher)
        base = dict(
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.NON_BLOCKING,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain("cross_tier_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
        )
        defn = ComponentDefinition(
            name="immune_svc",
            dependencies=["higher_svc"],
            max_dependency_tier=ActivationTier.FOUNDATION,
            **base,
        )
        with pytest.raises(ValueError, match="exceeds max_dependency_tier"):
            reg.register(defn)

    def test_inv_g4_kill_switch_hierarchy(self):
        """INV-G4: Kill-switch hierarchy global > tier > service."""
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
            ResourceBudget, FailurePolicy, RetryStrategy, StateDomain,
            OwnershipMode, ObservabilityContract, ComponentStatus,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="kill_test",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.NON_BLOCKING,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain("kill_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
            kill_switch_env="JARVIS_SVC_KILL_TEST_ENABLED",
            tier_kill_switch_env="JARVIS_TIER_IMMUNE_ENABLED",
        )
        with patch.dict(os.environ, {"JARVIS_PROMOTED_SERVICES_ENABLED": "false"}):
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_inv_g5_constructor_purity(self):
        """INV-G5: Constructor purity for promoted services."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ComponentRegistry,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="impure", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            constructor_pure=False,
        )
        with pytest.raises(ValueError, match="constructor_pure"):
            reg.register(defn)

    def test_inv_g6_required_deferred_conflict(self):
        """INV-G6: REQUIRED criticality cannot be DEFERRED_AFTER_READY."""
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
            ResourceBudget, FailurePolicy, RetryStrategy, StateDomain,
            OwnershipMode, ObservabilityContract,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="req_deferred",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain("req_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
        )
        with pytest.raises(ValueError, match="REQUIRED.*DEFERRED"):
            reg.register(defn)

    def test_governance_types_importable(self):
        """All governance types must be importable from component_registry."""
        from backend.core.component_registry import (
            PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
            RetryStrategy, OwnershipMode, ResourceBudget, FailurePolicy,
            StateDomain, ObservabilityContract, HealthPolicy, HealthProbeSet,
        )
