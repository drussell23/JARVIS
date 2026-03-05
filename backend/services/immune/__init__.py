"""Immune-tier services — extracted from unified_supervisor.py.

These 8 services form the security / compliance backbone of the JARVIS
governance framework.  Each module is independently importable and carries
its own supporting dataclasses so that nothing outside this package needs
to import the 73K-line monolith.

Usage::

    from backend.services.immune import SecurityPolicyEngine, AnomalyDetector

Or import everything::

    from backend.services.immune import (
        AccessControlManager,
        AnomalyDetector,
        AuditTrailRecorder,
        ComplianceAuditor,
        DataClassificationManager,
        IncidentResponseCoordinator,
        SecurityPolicyEngine,
        ThreatIntelligenceManager,
    )
"""

from backend.services.immune.access_control_manager import AccessControlManager
from backend.services.immune.anomaly_detector import AnomalyDetector
from backend.services.immune.audit_trail_recorder import AuditTrailRecorder
from backend.services.immune.compliance_auditor import ComplianceAuditor
from backend.services.immune.data_classification_manager import DataClassificationManager
from backend.services.immune.incident_response_coordinator import IncidentResponseCoordinator
from backend.services.immune.security_policy_engine import SecurityPolicyEngine
from backend.services.immune.threat_intelligence_manager import ThreatIntelligenceManager

__all__ = [
    "AccessControlManager",
    "AnomalyDetector",
    "AuditTrailRecorder",
    "ComplianceAuditor",
    "DataClassificationManager",
    "IncidentResponseCoordinator",
    "SecurityPolicyEngine",
    "ThreatIntelligenceManager",
]
