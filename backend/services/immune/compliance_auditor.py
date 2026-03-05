"""ComplianceAuditor — enterprise compliance auditing and tracking.

Extracted from unified_supervisor.py (lines 42023-42360).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.services.immune._base import (
    CapabilityContract,
    ServiceHealthReport,
    SystemKernelConfig,
    SystemService,
)


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

@dataclass
class ComplianceRequirement:
    """
    Defines a compliance requirement.

    Attributes:
        requirement_id: Unique identifier
        framework: Compliance framework (SOC2, HIPAA, GDPR, etc.)
        control_id: Control ID within the framework
        description: Human-readable description
        evidence_types: Types of evidence needed
        automated_checks: Checks that can be automated
        review_frequency: How often to review (daily, weekly, monthly)
    """
    requirement_id: str
    framework: str
    control_id: str
    description: str
    evidence_types: List[str]
    automated_checks: List[Dict[str, Any]] = field(default_factory=list)
    review_frequency: str = "monthly"
    responsible_role: str = "security_team"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ComplianceStatus:
    """Status of a compliance requirement."""
    requirement_id: str
    status: str  # compliant, non_compliant, partial, not_assessed
    evidence: List[Dict[str, Any]]
    findings: List[str]
    last_assessed: datetime
    next_assessment: datetime
    assessor: str = "automated"


# ---------------------------------------------------------------------------
# ComplianceAuditor
# ---------------------------------------------------------------------------

class ComplianceAuditor(SystemService):
    """
    Enterprise compliance auditing and tracking system.

    Tracks compliance status across multiple frameworks, performs
    automated compliance checks, and generates audit reports.

    Features:
    - Multi-framework support (SOC2, HIPAA, GDPR, PCI-DSS)
    - Automated compliance checks where possible
    - Evidence collection and management
    - Audit trail and reporting
    - Remediation tracking
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._requirements: Dict[str, ComplianceRequirement] = {}
        self._status: Dict[str, ComplianceStatus] = {}
        self._evidence_store: Dict[str, List[Dict[str, Any]]] = {}
        self._audit_log: deque = deque(maxlen=50000)
        self._check_handlers: Dict[str, Callable] = {}
        self._logger = logging.getLogger("ComplianceAuditor")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the compliance auditor."""
        try:
            async with self._lock:
                # Register default compliance frameworks
                await self._register_default_frameworks()
                self._initialized = True
                self._logger.info("Compliance auditor initialized")
                return True
        except Exception as e:
            self._logger.error(f"Failed to initialize compliance auditor: {e}")
            return False

    async def _register_default_frameworks(self) -> None:
        """Register common compliance framework requirements."""
        # SOC2 Type II - Security
        self.register_requirement(ComplianceRequirement(
            requirement_id="soc2_cc6_1",
            framework="SOC2",
            control_id="CC6.1",
            description="Logical and physical access controls",
            evidence_types=["access_logs", "user_provisioning_records"],
            automated_checks=[
                {"type": "access_log_review", "frequency": "daily"},
                {"type": "privileged_access_review", "frequency": "weekly"},
            ],
            review_frequency="quarterly"
        ))

        self.register_requirement(ComplianceRequirement(
            requirement_id="soc2_cc6_2",
            framework="SOC2",
            control_id="CC6.2",
            description="Prior to granting access, authorization is obtained",
            evidence_types=["access_requests", "approvals"],
            automated_checks=[
                {"type": "access_approval_check", "frequency": "daily"},
            ],
            review_frequency="quarterly"
        ))

        self.register_requirement(ComplianceRequirement(
            requirement_id="soc2_cc7_1",
            framework="SOC2",
            control_id="CC7.1",
            description="Security events are monitored",
            evidence_types=["security_logs", "alert_records"],
            automated_checks=[
                {"type": "security_monitoring_active", "frequency": "hourly"},
                {"type": "alert_response_time", "threshold_minutes": 15},
            ],
            review_frequency="monthly"
        ))

        # GDPR
        self.register_requirement(ComplianceRequirement(
            requirement_id="gdpr_art_17",
            framework="GDPR",
            control_id="Article 17",
            description="Right to erasure (right to be forgotten)",
            evidence_types=["deletion_requests", "deletion_confirmations"],
            automated_checks=[
                {"type": "deletion_request_handling", "max_days": 30},
            ],
            review_frequency="monthly"
        ))

        self.register_requirement(ComplianceRequirement(
            requirement_id="gdpr_art_32",
            framework="GDPR",
            control_id="Article 32",
            description="Security of processing",
            evidence_types=["encryption_records", "access_controls"],
            automated_checks=[
                {"type": "encryption_at_rest", "required": True},
                {"type": "encryption_in_transit", "required": True},
            ],
            review_frequency="quarterly"
        ))

    def register_requirement(self, requirement: ComplianceRequirement) -> bool:
        """Register a compliance requirement."""
        try:
            self._requirements[requirement.requirement_id] = requirement
            self._status[requirement.requirement_id] = ComplianceStatus(
                requirement_id=requirement.requirement_id,
                status="not_assessed",
                evidence=[],
                findings=[],
                last_assessed=datetime.min,
                next_assessment=datetime.now(),
            )
            self._logger.debug(f"Registered requirement: {requirement.requirement_id}")
            return True
        except Exception as e:
            self._logger.error(f"Failed to register requirement: {e}")
            return False

    def register_check_handler(
        self,
        check_type: str,
        handler: Callable[[Dict[str, Any]], Awaitable[Tuple[bool, str]]],
    ) -> None:
        """Register a handler for automated compliance checks."""
        self._check_handlers[check_type] = handler

    async def assess_requirement(
        self,
        requirement_id: str,
        evidence: Optional[List[Dict[str, Any]]] = None,
        assessor: str = "automated",
    ) -> ComplianceStatus:
        """
        Assess a compliance requirement.

        Args:
            requirement_id: ID of requirement to assess
            evidence: Evidence for the assessment
            assessor: Who performed the assessment

        Returns:
            Updated compliance status
        """
        if requirement_id not in self._requirements:
            raise ValueError(f"Unknown requirement: {requirement_id}")

        requirement = self._requirements[requirement_id]
        findings: List[str] = []
        collected_evidence: List[Dict[str, Any]] = evidence or []

        # Run automated checks
        for check in requirement.automated_checks:
            check_type = check.get("type")
            if check_type in self._check_handlers:
                try:
                    passed, finding = await self._check_handlers[check_type](check)
                    if not passed:
                        findings.append(finding)
                    collected_evidence.append({
                        "type": "automated_check",
                        "check_type": check_type,
                        "passed": passed,
                        "finding": finding,
                        "timestamp": datetime.now().isoformat(),
                    })
                except Exception as e:
                    findings.append(f"Check {check_type} failed with error: {e}")

        # Determine status
        if findings:
            status = "non_compliant" if any("critical" in f.lower() for f in findings) else "partial"
        elif collected_evidence:
            status = "compliant"
        else:
            status = "not_assessed"

        # Calculate next assessment date
        frequency_days = {
            "daily": 1,
            "weekly": 7,
            "monthly": 30,
            "quarterly": 90,
            "annually": 365,
        }
        days_until_next = frequency_days.get(requirement.review_frequency, 30)
        next_assessment = datetime.now() + timedelta(days=days_until_next)

        # Update status
        new_status = ComplianceStatus(
            requirement_id=requirement_id,
            status=status,
            evidence=collected_evidence,
            findings=findings,
            last_assessed=datetime.now(),
            next_assessment=next_assessment,
            assessor=assessor,
        )
        self._status[requirement_id] = new_status

        # Store evidence
        if requirement_id not in self._evidence_store:
            self._evidence_store[requirement_id] = []
        self._evidence_store[requirement_id].extend(collected_evidence)

        # Audit log
        self._audit_log.append({
            "timestamp": datetime.now().isoformat(),
            "action": "assess_requirement",
            "requirement_id": requirement_id,
            "status": status,
            "assessor": assessor,
            "findings_count": len(findings),
        })

        self._logger.info(f"Assessed {requirement_id}: {status}")
        return new_status

    async def assess_all(self, assessor: str = "automated") -> Dict[str, ComplianceStatus]:
        """Assess all registered requirements."""
        results = {}
        for req_id in self._requirements:
            try:
                results[req_id] = await self.assess_requirement(req_id, assessor=assessor)
            except Exception as e:
                self._logger.error(f"Failed to assess {req_id}: {e}")
        return results

    def get_status(self, requirement_id: str) -> Optional[ComplianceStatus]:
        """Get status for a specific requirement."""
        return self._status.get(requirement_id)

    def get_all_status(self) -> Dict[str, ComplianceStatus]:
        """Get status for all requirements."""
        return dict(self._status)

    def get_framework_summary(self, framework: str) -> Dict[str, Any]:
        """Get summary for a specific compliance framework."""
        requirements = [
            r for r in self._requirements.values()
            if r.framework == framework
        ]
        statuses = [self._status.get(r.requirement_id) for r in requirements]

        return {
            "framework": framework,
            "total_requirements": len(requirements),
            "compliant": sum(1 for s in statuses if s and s.status == "compliant"),
            "non_compliant": sum(1 for s in statuses if s and s.status == "non_compliant"),
            "partial": sum(1 for s in statuses if s and s.status == "partial"),
            "not_assessed": sum(1 for s in statuses if s and s.status == "not_assessed"),
        }

    def generate_report(
        self,
        framework: Optional[str] = None,
        format: str = "json",
    ) -> Dict[str, Any]:
        """Generate a compliance report."""
        if framework:
            requirements = [
                r for r in self._requirements.values()
                if r.framework == framework
            ]
        else:
            requirements = list(self._requirements.values())

        report: Dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "framework_filter": framework,
            "summary": {
                "total": len(requirements),
                "by_status": {},
            },
            "requirements": [],
        }

        status_counts: Dict[str, int] = {}
        for req in requirements:
            status = self._status.get(req.requirement_id)
            if status:
                status_counts[status.status] = status_counts.get(status.status, 0) + 1
                report["requirements"].append({
                    "requirement_id": req.requirement_id,
                    "framework": req.framework,
                    "control_id": req.control_id,
                    "description": req.description,
                    "status": status.status,
                    "findings": status.findings,
                    "last_assessed": status.last_assessed.isoformat(),
                    "next_assessment": status.next_assessment.isoformat(),
                })

        report["summary"]["by_status"] = status_counts
        return report

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        reqs = len(self._requirements)
        return (True, f"ComplianceAuditor: {reqs} requirements tracked")

    async def cleanup(self) -> None:
        self._audit_log.clear()

    async def start(self) -> bool:
        if not self._initialized:
            await self.initialize()
        return True

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._initialized,
            message=f"ComplianceAuditor: initialized={self._initialized}, requirements={len(self._requirements)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="ComplianceAuditor",
            version="1.0.0",
            inputs=["health.report"],
            outputs=["compliance.violation", "compliance.pass"],
            side_effects=["writes_compliance_report"],
        )

    def activation_triggers(self) -> List[str]:
        return []  # batch_window (always_on for lifecycle purposes)
