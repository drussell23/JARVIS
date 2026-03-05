"""Phase 3 Exit Gate: Wave 1 immune services fully governed and operational."""
import pytest


class TestPhase3ExitGate:
    def test_all_8_services_have_unique_state_domains(self):
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        domains = set()
        for state in reg.all_states():
            if state.definition.state_domain:
                assert state.definition.state_domain.domain not in domains, \
                    f"Duplicate state domain: {state.definition.state_domain.domain}"
                domains.add(state.definition.state_domain.domain)

    def test_no_cross_tier_violations(self):
        from backend.core.component_registry import ComponentRegistry, ActivationTier
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            defn = state.definition
            if defn.max_dependency_tier is not None:
                for dep in defn.dependencies:
                    dep_name = dep.component if hasattr(dep, 'component') else dep
                    if reg.has(dep_name):
                        dep_tier = reg.get(dep_name).activation_tier
                        if dep_tier is not None:
                            assert dep_tier <= defn.max_dependency_tier or \
                                dep_name in defn.cross_tier_dependency_allowlist

    def test_immune_services_importable(self):
        """All 8 service classes must be importable from their modules."""
        from backend.services.immune.security_policy_engine import SecurityPolicyEngine
        from backend.services.immune.anomaly_detector import AnomalyDetector
        from backend.services.immune.audit_trail_recorder import AuditTrailRecorder
        from backend.services.immune.threat_intelligence_manager import ThreatIntelligenceManager
        from backend.services.immune.incident_response_coordinator import IncidentResponseCoordinator
        from backend.services.immune.compliance_auditor import ComplianceAuditor
        from backend.services.immune.data_classification_manager import DataClassificationManager
        from backend.services.immune.access_control_manager import AccessControlManager

    def test_always_on_services_have_block_or_nonblocking_readiness(self):
        from backend.core.component_registry import ComponentRegistry, ActivationMode, ReadinessClass
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            defn = state.definition
            if defn.activation_mode == ActivationMode.ALWAYS_ON:
                assert defn.readiness_class in (ReadinessClass.BLOCK_READY, ReadinessClass.NON_BLOCKING), \
                    f"ALWAYS_ON service {defn.name} must be BLOCK_READY or NON_BLOCKING"

    def test_event_driven_services_are_deferred(self):
        from backend.core.component_registry import ComponentRegistry, ActivationMode, ReadinessClass
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            defn = state.definition
            if defn.activation_mode == ActivationMode.EVENT_DRIVEN:
                assert defn.readiness_class == ReadinessClass.DEFERRED_AFTER_READY, \
                    f"EVENT_DRIVEN service {defn.name} should be DEFERRED_AFTER_READY"
