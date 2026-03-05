"""Tests for governance validation in ComponentRegistry."""
import os
import pytest
from unittest.mock import patch


class TestKillSwitchHierarchy:
    def _make_promoted(self, name="test_svc", **overrides):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ActivationMode, ReadinessClass, ActivationTier, ResourceBudget,
            FailurePolicy, RetryStrategy, StateDomain, OwnershipMode,
            ObservabilityContract,
        )
        defaults = dict(
            name=name,
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
            state_domain=StateDomain(f"{name}_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
            kill_switch_env=f"JARVIS_SVC_{name.upper()}_ENABLED",
            tier_kill_switch_env="JARVIS_TIER_IMMUNE_ENABLED",
        )
        defaults.update(overrides)
        return ComponentDefinition(**defaults)

    def test_global_kill_switch_disables_all(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_PROMOTED_SERVICES_ENABLED": "false"}):
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_tier_kill_switch_disables_tier(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_TIER_IMMUNE_ENABLED": "false"}):
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_service_kill_switch_disables_one(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_SVC_TEST_SVC_ENABLED": "false"}):
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_unset_kill_switches_means_enabled(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        env = {
            "JARVIS_PROMOTED_SERVICES_ENABLED": "",
            "JARVIS_TIER_IMMUNE_ENABLED": "",
            "JARVIS_SVC_TEST_SVC_ENABLED": "",
        }
        with patch.dict(os.environ, env, clear=False):
            # Remove the keys entirely
            for k in env:
                os.environ.pop(k, None)
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status != ComponentStatus.DISABLED


class TestPromotedValidation:
    def _make_promoted(self, **overrides):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ActivationMode, ReadinessClass, ActivationTier, ResourceBudget,
            FailurePolicy, RetryStrategy, StateDomain, OwnershipMode,
            ObservabilityContract,
        )
        defaults = dict(
            name="test_promoted",
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
            state_domain=StateDomain("test_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
        )
        defaults.update(overrides)
        return ComponentDefinition(**defaults)

    def test_promoted_missing_activation_mode_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(activation_mode=None)
        with pytest.raises(ValueError, match="activation_mode"):
            reg.register(defn)

    def test_promoted_missing_readiness_class_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(readiness_class=None)
        with pytest.raises(ValueError, match="readiness_class"):
            reg.register(defn)

    def test_promoted_missing_resource_budget_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(resource_budget=None)
        with pytest.raises(ValueError, match="resource_budget"):
            reg.register(defn)

    def test_promoted_constructor_not_pure_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(constructor_pure=False)
        with pytest.raises(ValueError, match="constructor_pure"):
            reg.register(defn)

    def test_promoted_complete_succeeds(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted()
        state = reg.register(defn)
        assert state.definition.name == "test_promoted"

    def test_state_domain_conflict_rejected(self):
        from backend.core.component_registry import ComponentRegistry
        from backend.core.component_registry import StateDomain, OwnershipMode
        reg = ComponentRegistry()
        defn1 = self._make_promoted(name="svc_a", state_domain=StateDomain("shared", OwnershipMode.EXCLUSIVE_WRITE))
        defn2 = self._make_promoted(name="svc_b", state_domain=StateDomain("shared", OwnershipMode.EXCLUSIVE_WRITE))
        reg.register(defn1)
        with pytest.raises(ValueError, match="State domain.*already owned"):
            reg.register(defn2)

    def test_criticality_readiness_conflict(self):
        from backend.core.component_registry import ComponentRegistry, Criticality, ReadinessClass
        reg = ComponentRegistry()
        defn = self._make_promoted(
            criticality=Criticality.REQUIRED,
            readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        )
        with pytest.raises(ValueError, match="REQUIRED.*DEFERRED"):
            reg.register(defn)

    def test_cross_tier_dependency_rejected(self):
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            PromotionLevel, ActivationTier,
        )
        reg = ComponentRegistry()
        # Register a higher-tier component first
        higher = ComponentDefinition(
            name="higher_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            activation_tier=ActivationTier.METABOLIC,
        )
        reg.register(higher)
        # Try to register immune-tier depending on metabolic
        defn = self._make_promoted(
            dependencies=["higher_svc"],
            max_dependency_tier=ActivationTier.FOUNDATION,
        )
        with pytest.raises(ValueError, match="exceeds max_dependency_tier"):
            reg.register(defn)

    def test_cross_tier_allowlist_permits(self):
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            ActivationTier,
        )
        reg = ComponentRegistry()
        higher = ComponentDefinition(
            name="higher_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            activation_tier=ActivationTier.METABOLIC,
        )
        reg.register(higher)
        defn = self._make_promoted(
            dependencies=["higher_svc"],
            max_dependency_tier=ActivationTier.FOUNDATION,
            cross_tier_dependency_allowlist=("higher_svc",),
        )
        state = reg.register(defn)
        assert state.definition.name == "test_promoted"


class TestLegacyValidation:
    def test_legacy_warns_but_succeeds(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, ComponentRegistry,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="legacy_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = reg.register(defn)
        assert state.definition.name == "legacy_svc"
