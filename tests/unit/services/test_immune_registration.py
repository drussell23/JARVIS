"""Tests for immune-tier service registration with full promoted contracts."""
import os
import pytest
from unittest.mock import patch


class TestImmuneServiceRegistration:
    def test_all_8_services_register_successfully(self):
        """All 8 immune services must register under PROMOTED mode."""
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        assert reg.has("security_policy_engine")
        assert reg.has("anomaly_detector")
        assert reg.has("audit_trail_recorder")
        assert reg.has("threat_intelligence_manager")
        assert reg.has("incident_response_coordinator")
        assert reg.has("compliance_auditor")
        assert reg.has("data_classification_manager")
        assert reg.has("access_control_manager")

    def test_all_are_promoted(self):
        from backend.core.component_registry import ComponentRegistry, PromotionLevel
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for name in ["security_policy_engine", "anomaly_detector", "audit_trail_recorder",
                      "threat_intelligence_manager", "incident_response_coordinator",
                      "compliance_auditor", "data_classification_manager", "access_control_manager"]:
            defn = reg.get(name)
            assert defn.promotion_level == PromotionLevel.PROMOTED, f"{name} must be PROMOTED"

    def test_all_are_immune_tier(self):
        from backend.core.component_registry import ComponentRegistry, ActivationTier
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for name in ["security_policy_engine", "anomaly_detector", "audit_trail_recorder",
                      "threat_intelligence_manager", "incident_response_coordinator",
                      "compliance_auditor", "data_classification_manager", "access_control_manager"]:
            defn = reg.get(name)
            assert defn.activation_tier == ActivationTier.IMMUNE

    def test_no_state_domain_conflicts(self):
        """All 8 services must have unique state domains."""
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)  # Should not raise

    def test_all_constructor_pure(self):
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            assert state.definition.constructor_pure is True

    def test_tier_kill_switch_disables_all_immune(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_TIER_IMMUNE_ENABLED": "false"}):
            register_immune_services(reg)
            for state in reg.all_states():
                assert state.status == ComponentStatus.DISABLED
