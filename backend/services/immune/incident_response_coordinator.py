"""IncidentResponseCoordinator — security incident response coordination.

Extracted from unified_supervisor.py (lines 43645-44056).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
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
class SecurityIncident:
    """Represents a security incident."""
    incident_id: str
    severity: str  # critical, high, medium, low
    category: str  # intrusion, data_breach, malware, dos, etc.
    title: str
    description: str
    affected_resources: List[str]
    detected_at: datetime = field(default_factory=datetime.now)
    status: str = "open"  # open, investigating, contained, resolved
    assigned_to: Optional[str] = None
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    remediation_steps: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# IncidentResponseCoordinator
# ---------------------------------------------------------------------------

class IncidentResponseCoordinator(SystemService):
    """
    Security incident response coordination system.

    Manages the lifecycle of security incidents from detection
    through resolution, including automated response actions.

    Features:
    - Incident lifecycle management
    - Automated initial response
    - Runbook execution
    - Notification and escalation
    - Evidence collection
    - Post-incident reporting
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._incidents: Dict[str, SecurityIncident] = {}
        self._runbooks: Dict[str, List[Dict[str, Any]]] = {}
        self._notification_handlers: List[Callable] = []
        self._auto_response_rules: List[Dict[str, Any]] = []
        self._escalation_policy: Dict[str, List[str]] = {}
        self._logger = logging.getLogger("IncidentResponseCoordinator")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize incident response system."""
        try:
            async with self._lock:
                # Register default runbooks
                self._register_default_runbooks()

                # Set default escalation policy
                self._escalation_policy = {
                    "critical": ["security_team", "engineering_lead", "cto"],
                    "high": ["security_team", "engineering_lead"],
                    "medium": ["security_team"],
                    "low": ["security_team"],
                }

                self._initialized = True
                self._logger.info("Incident response coordinator initialized")
                return True
        except Exception as e:
            self._logger.error(f"Failed to initialize incident response: {e}")
            return False

    def _register_default_runbooks(self) -> None:
        """Register default incident response runbooks."""
        # Intrusion runbook
        self._runbooks["intrusion"] = [
            {"action": "isolate_resource", "params": {"type": "network"}},
            {"action": "collect_evidence", "params": {"types": ["logs", "memory", "disk"]}},
            {"action": "block_ips", "params": {"source": "incident"}},
            {"action": "notify", "params": {"severity": "critical"}},
            {"action": "rotate_credentials", "params": {"scope": "affected"}},
        ]

        # Data breach runbook
        self._runbooks["data_breach"] = [
            {"action": "identify_scope", "params": {}},
            {"action": "collect_evidence", "params": {"types": ["logs", "access_records"]}},
            {"action": "notify", "params": {"severity": "critical", "include_legal": True}},
            {"action": "revoke_access", "params": {"scope": "affected_data"}},
            {"action": "prepare_notification", "params": {"type": "customer"}},
        ]

        # Malware runbook
        self._runbooks["malware"] = [
            {"action": "isolate_resource", "params": {"type": "host"}},
            {"action": "collect_evidence", "params": {"types": ["memory", "disk", "network"]}},
            {"action": "scan_related", "params": {"depth": "full"}},
            {"action": "restore_from_backup", "params": {"verify": True}},
        ]

        # DoS runbook
        self._runbooks["dos"] = [
            {"action": "enable_ddos_protection", "params": {}},
            {"action": "rate_limit", "params": {"aggressive": True}},
            {"action": "block_ips", "params": {"source": "traffic_analysis"}},
            {"action": "scale_infrastructure", "params": {"multiplier": 2}},
        ]

    async def create_incident(
        self,
        severity: str,
        category: str,
        title: str,
        description: str,
        affected_resources: List[str],
        auto_respond: bool = True,
    ) -> SecurityIncident:
        """
        Create a new security incident.

        Args:
            severity: Incident severity (critical, high, medium, low)
            category: Incident category (intrusion, data_breach, etc.)
            title: Brief title
            description: Detailed description
            affected_resources: List of affected resource IDs
            auto_respond: Whether to execute automatic response

        Returns:
            Created SecurityIncident
        """
        incident_id = f"INC-{int(time.time())}-{secrets.token_hex(4).upper()}"

        incident = SecurityIncident(
            incident_id=incident_id,
            severity=severity,
            category=category,
            title=title,
            description=description,
            affected_resources=affected_resources,
        )

        # Add creation to timeline
        incident.timeline.append({
            "timestamp": datetime.now().isoformat(),
            "action": "created",
            "actor": "system",
            "details": f"Incident created: {title}",
        })

        async with self._lock:
            self._incidents[incident_id] = incident

        self._logger.warning(f"Created incident: {incident_id} - {title}")

        # Send notifications
        await self._send_notifications(incident, "created")

        # Execute automatic response if enabled
        if auto_respond:
            await self._execute_auto_response(incident)

        return incident

    async def _execute_auto_response(self, incident: SecurityIncident) -> None:
        """Execute automatic response based on runbook."""
        runbook = self._runbooks.get(incident.category, [])

        if not runbook:
            self._logger.info(f"No runbook for category: {incident.category}")
            return

        incident.timeline.append({
            "timestamp": datetime.now().isoformat(),
            "action": "auto_response_started",
            "actor": "system",
            "details": f"Executing runbook for {incident.category}",
        })

        for step in runbook:
            action = step.get("action")
            params = step.get("params", {})

            try:
                # In production, these would be actual response actions
                self._logger.info(f"Executing response action: {action}")

                incident.timeline.append({
                    "timestamp": datetime.now().isoformat(),
                    "action": f"executed_{action}",
                    "actor": "system",
                    "details": f"Parameters: {params}",
                })

            except Exception as e:
                incident.timeline.append({
                    "timestamp": datetime.now().isoformat(),
                    "action": f"failed_{action}",
                    "actor": "system",
                    "details": f"Error: {e}",
                })
                self._logger.error(f"Auto response action failed: {action} - {e}")

    async def update_status(
        self,
        incident_id: str,
        new_status: str,
        actor: str,
        notes: Optional[str] = None,
    ) -> bool:
        """Update incident status."""
        incident = self._incidents.get(incident_id)
        if not incident:
            return False

        old_status = incident.status
        incident.status = new_status

        incident.timeline.append({
            "timestamp": datetime.now().isoformat(),
            "action": "status_change",
            "actor": actor,
            "details": f"Status changed: {old_status} -> {new_status}. {notes or ''}",
        })

        self._logger.info(f"Incident {incident_id} status: {new_status}")
        await self._send_notifications(incident, "status_change")

        return True

    async def assign_incident(
        self,
        incident_id: str,
        assignee: str,
        assigner: str,
    ) -> bool:
        """Assign incident to a team member."""
        incident = self._incidents.get(incident_id)
        if not incident:
            return False

        incident.assigned_to = assignee
        incident.timeline.append({
            "timestamp": datetime.now().isoformat(),
            "action": "assigned",
            "actor": assigner,
            "details": f"Assigned to: {assignee}",
        })

        return True

    async def add_evidence(
        self,
        incident_id: str,
        evidence_type: str,
        evidence_data: Dict[str, Any],
        collector: str,
    ) -> bool:
        """Add evidence to an incident."""
        incident = self._incidents.get(incident_id)
        if not incident:
            return False

        evidence = {
            "type": evidence_type,
            "data": evidence_data,
            "collected_at": datetime.now().isoformat(),
            "collected_by": collector,
        }

        incident.evidence.append(evidence)
        incident.timeline.append({
            "timestamp": datetime.now().isoformat(),
            "action": "evidence_added",
            "actor": collector,
            "details": f"Added {evidence_type} evidence",
        })

        return True

    async def add_remediation_step(
        self,
        incident_id: str,
        step: str,
        actor: str,
    ) -> bool:
        """Add a remediation step."""
        incident = self._incidents.get(incident_id)
        if not incident:
            return False

        incident.remediation_steps.append(step)
        incident.timeline.append({
            "timestamp": datetime.now().isoformat(),
            "action": "remediation_added",
            "actor": actor,
            "details": step,
        })

        return True

    async def _send_notifications(
        self,
        incident: SecurityIncident,
        event_type: str,
    ) -> None:
        """Send notifications for incident events."""
        # Get escalation targets
        targets = self._escalation_policy.get(incident.severity, [])

        notification = {
            "incident_id": incident.incident_id,
            "severity": incident.severity,
            "category": incident.category,
            "title": incident.title,
            "event_type": event_type,
            "targets": targets,
            "timestamp": datetime.now().isoformat(),
        }

        for handler in self._notification_handlers:
            try:
                await handler(notification)
            except Exception as e:
                self._logger.error(f"Notification handler error: {e}")

    def register_notification_handler(
        self,
        handler: Callable[[Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Register a notification handler."""
        self._notification_handlers.append(handler)

    def get_incident(self, incident_id: str) -> Optional[SecurityIncident]:
        """Get an incident by ID."""
        return self._incidents.get(incident_id)

    def get_open_incidents(
        self,
        severity: Optional[str] = None,
    ) -> List[SecurityIncident]:
        """Get all open incidents."""
        open_statuses = ["open", "investigating", "contained"]
        incidents = [
            i for i in self._incidents.values()
            if i.status in open_statuses
        ]

        if severity:
            incidents = [i for i in incidents if i.severity == severity]

        return sorted(incidents, key=lambda i: i.detected_at, reverse=True)

    def generate_report(self, incident_id: str) -> Dict[str, Any]:
        """Generate a post-incident report."""
        incident = self._incidents.get(incident_id)
        if not incident:
            return {}

        return {
            "incident_id": incident.incident_id,
            "title": incident.title,
            "severity": incident.severity,
            "category": incident.category,
            "status": incident.status,
            "detected_at": incident.detected_at.isoformat(),
            "description": incident.description,
            "affected_resources": incident.affected_resources,
            "assigned_to": incident.assigned_to,
            "timeline": incident.timeline,
            "evidence_count": len(incident.evidence),
            "remediation_steps": incident.remediation_steps,
            "time_to_contain": self._calculate_ttc(incident),
            "time_to_resolve": self._calculate_ttr(incident),
        }

    def _calculate_ttc(self, incident: SecurityIncident) -> Optional[float]:
        """Calculate time to contain in minutes."""
        for entry in incident.timeline:
            if entry.get("action") == "status_change":
                if "contained" in entry.get("details", "").lower():
                    contain_time = datetime.fromisoformat(entry["timestamp"])
                    return (contain_time - incident.detected_at).total_seconds() / 60
        return None

    def _calculate_ttr(self, incident: SecurityIncident) -> Optional[float]:
        """Calculate time to resolve in minutes."""
        if incident.status != "resolved":
            return None
        for entry in reversed(incident.timeline):
            if entry.get("action") == "status_change":
                if "resolved" in entry.get("details", "").lower():
                    resolve_time = datetime.fromisoformat(entry["timestamp"])
                    return (resolve_time - incident.detected_at).total_seconds() / 60
        return None

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        open_incidents = sum(
            1 for i in self._incidents.values() if i.status not in ("resolved", "closed")
        )
        return (True, f"IncidentResponseCoordinator: {open_incidents} open incidents")

    async def cleanup(self) -> None:
        pass  # Incidents are forensic records; do not clear

    async def start(self) -> bool:
        if not self._initialized:
            await self.initialize()
        return True

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._initialized,
            message=f"IncidentResponseCoordinator: initialized={self._initialized}, incidents={len(self._incidents)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="IncidentResponseCoordinator",
            version="1.0.0",
            inputs=["threat.confirmed"],
            outputs=["incident.opened", "incident.resolved"],
            side_effects=["writes_incident_log"],
        )

    def activation_triggers(self) -> List[str]:
        return ["threat.confirmed"]  # event_driven
