"""AccessControlManager — enterprise RBAC/ABAC access control.

Extracted from unified_supervisor.py (lines 42719-43134).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

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
class AccessPermission:
    """Defines an access permission."""
    permission_id: str
    resource_type: str
    actions: List[str]  # read, write, delete, admin
    conditions: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccessRole:
    """Defines an access role with permissions."""
    role_id: str
    name: str
    description: str
    permissions: List[AccessPermission]
    inherits_from: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccessGrant:
    """Records an access grant to a subject."""
    grant_id: str
    subject_type: str  # user, group, service
    subject_id: str
    role_id: str
    resource_pattern: str
    granted_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    granted_by: str = "system"
    conditions: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AccessControlManager
# ---------------------------------------------------------------------------

class AccessControlManager(SystemService):
    """
    Enterprise RBAC/ABAC access control system.

    Provides fine-grained access control with support for roles,
    permissions, attribute-based conditions, and resource patterns.

    Features:
    - Role-based access control (RBAC)
    - Attribute-based access control (ABAC) conditions
    - Role inheritance
    - Resource pattern matching
    - Time-based access grants
    - Access audit logging
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._roles: Dict[str, AccessRole] = {}
        self._grants: Dict[str, AccessGrant] = {}
        self._subject_grants: Dict[str, List[str]] = {}  # subject_id -> grant_ids
        self._audit_log: deque = deque(maxlen=100000)
        self._logger = logging.getLogger("AccessControlManager")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize with default roles."""
        try:
            async with self._lock:
                # Define default roles
                self._roles = {
                    "admin": AccessRole(
                        role_id="admin",
                        name="Administrator",
                        description="Full system access",
                        permissions=[
                            AccessPermission(
                                permission_id="admin_all",
                                resource_type="*",
                                actions=["read", "write", "delete", "admin"],
                            )
                        ],
                    ),
                    "operator": AccessRole(
                        role_id="operator",
                        name="Operator",
                        description="Operational access",
                        permissions=[
                            AccessPermission(
                                permission_id="op_read_all",
                                resource_type="*",
                                actions=["read"],
                            ),
                            AccessPermission(
                                permission_id="op_write_config",
                                resource_type="config",
                                actions=["read", "write"],
                            ),
                            AccessPermission(
                                permission_id="op_manage_processes",
                                resource_type="process",
                                actions=["read", "write", "delete"],
                            ),
                        ],
                    ),
                    "viewer": AccessRole(
                        role_id="viewer",
                        name="Viewer",
                        description="Read-only access",
                        permissions=[
                            AccessPermission(
                                permission_id="view_all",
                                resource_type="*",
                                actions=["read"],
                            )
                        ],
                    ),
                    "service": AccessRole(
                        role_id="service",
                        name="Service Account",
                        description="Limited service access",
                        permissions=[
                            AccessPermission(
                                permission_id="svc_api",
                                resource_type="api",
                                actions=["read", "write"],
                            )
                        ],
                    ),
                }

                self._initialized = True
                self._logger.info("Access control manager initialized")
                return True
        except Exception as e:
            self._logger.error(f"Failed to initialize access control: {e}")
            return False

    def create_role(self, role: AccessRole) -> bool:
        """Create a new access role."""
        if role.role_id in self._roles:
            self._logger.warning(f"Role {role.role_id} already exists")
            return False
        self._roles[role.role_id] = role
        self._logger.info(f"Created role: {role.name}")
        return True

    def delete_role(self, role_id: str) -> bool:
        """Delete an access role."""
        if role_id not in self._roles:
            return False
        del self._roles[role_id]
        self._logger.info(f"Deleted role: {role_id}")
        return True

    async def grant_access(
        self,
        subject_type: str,
        subject_id: str,
        role_id: str,
        resource_pattern: str = "*",
        expires_at: Optional[datetime] = None,
        granted_by: str = "system",
        conditions: Optional[Dict[str, Any]] = None,
    ) -> AccessGrant:
        """
        Grant access to a subject.

        Args:
            subject_type: Type of subject (user, group, service)
            subject_id: ID of the subject
            role_id: Role to grant
            resource_pattern: Pattern for resources (supports wildcards)
            expires_at: When the grant expires
            granted_by: Who granted the access
            conditions: Additional ABAC conditions

        Returns:
            AccessGrant object
        """
        if role_id not in self._roles:
            raise ValueError(f"Unknown role: {role_id}")

        grant_id = f"grant_{subject_id}_{role_id}_{int(time.time())}"
        grant = AccessGrant(
            grant_id=grant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            role_id=role_id,
            resource_pattern=resource_pattern,
            expires_at=expires_at,
            granted_by=granted_by,
            conditions=conditions or {},
        )

        async with self._lock:
            self._grants[grant_id] = grant
            if subject_id not in self._subject_grants:
                self._subject_grants[subject_id] = []
            self._subject_grants[subject_id].append(grant_id)

        self._audit_log.append({
            "timestamp": datetime.now().isoformat(),
            "action": "grant_access",
            "subject": subject_id,
            "role": role_id,
            "resource_pattern": resource_pattern,
            "granted_by": granted_by,
        })

        self._logger.info(f"Granted {role_id} to {subject_id}")
        return grant

    async def revoke_access(self, grant_id: str) -> bool:
        """Revoke an access grant."""
        if grant_id not in self._grants:
            return False

        grant = self._grants[grant_id]

        async with self._lock:
            del self._grants[grant_id]
            if grant.subject_id in self._subject_grants:
                self._subject_grants[grant.subject_id].remove(grant_id)

        self._audit_log.append({
            "timestamp": datetime.now().isoformat(),
            "action": "revoke_access",
            "grant_id": grant_id,
            "subject": grant.subject_id,
        })

        self._logger.info(f"Revoked access: {grant_id}")
        return True

    async def check_access(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        action: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """
        Check if subject has access to perform action on resource.

        Args:
            subject_id: ID of the subject requesting access
            resource_type: Type of resource
            resource_id: Specific resource ID
            action: Action being performed
            context: Additional context for ABAC evaluation

        Returns:
            Tuple of (allowed, reason)
        """
        context = context or {}

        # Get subject's grants
        grant_ids = self._subject_grants.get(subject_id, [])
        if not grant_ids:
            self._log_access_check(subject_id, resource_type, resource_id, action, False, "No grants")
            return False, "No access grants for subject"

        for grant_id in grant_ids:
            grant = self._grants.get(grant_id)
            if not grant:
                continue

            # Check expiration
            if grant.expires_at and grant.expires_at < datetime.now():
                continue

            # Check resource pattern
            if not self._matches_pattern(
                f"{resource_type}/{resource_id}",
                grant.resource_pattern,
            ):
                continue

            # Get role and permissions
            role = self._roles.get(grant.role_id)
            if not role:
                continue

            # Check permissions (including inherited)
            if await self._has_permission(role, resource_type, action):
                # Evaluate ABAC conditions
                if self._evaluate_conditions(grant.conditions, context):
                    self._log_access_check(
                        subject_id, resource_type, resource_id, action, True,
                        f"Granted via role {role.name}",
                    )
                    return True, f"Access granted via role: {role.name}"

        self._log_access_check(subject_id, resource_type, resource_id, action, False, "Insufficient permissions")
        return False, "Insufficient permissions"

    async def _has_permission(
        self,
        role: AccessRole,
        resource_type: str,
        action: str,
        checked_roles: Optional[Set[str]] = None,
    ) -> bool:
        """Check if role has permission, including inherited roles."""
        checked_roles = checked_roles or set()

        if role.role_id in checked_roles:
            return False  # Prevent infinite recursion
        checked_roles.add(role.role_id)

        # Check direct permissions
        for perm in role.permissions:
            if perm.resource_type == "*" or perm.resource_type == resource_type:
                if action in perm.actions or "*" in perm.actions:
                    return True

        # Check inherited roles
        for parent_role_id in role.inherits_from:
            parent_role = self._roles.get(parent_role_id)
            if parent_role:
                if await self._has_permission(parent_role, resource_type, action, checked_roles):
                    return True

        return False

    def _matches_pattern(self, resource: str, pattern: str) -> bool:
        """Check if resource matches pattern."""
        import fnmatch
        return fnmatch.fnmatch(resource, pattern)

    def _evaluate_conditions(
        self,
        conditions: Dict[str, Any],
        context: Dict[str, Any],
    ) -> bool:
        """Evaluate ABAC conditions against context."""
        if not conditions:
            return True

        for key, expected in conditions.items():
            actual = context.get(key)

            if isinstance(expected, dict):
                # Complex condition
                op = expected.get("op", "eq")
                value = expected.get("value")

                if op == "eq" and actual != value:
                    return False
                elif op == "neq" and actual == value:
                    return False
                elif op == "in" and actual not in value:
                    return False
                elif op == "not_in" and actual in value:
                    return False
                elif op == "gt" and not (actual and actual > value):
                    return False
                elif op == "lt" and not (actual and actual < value):
                    return False
            else:
                # Simple equality
                if actual != expected:
                    return False

        return True

    def _log_access_check(
        self,
        subject: str,
        resource_type: str,
        resource_id: str,
        action: str,
        allowed: bool,
        reason: str,
    ) -> None:
        """Log an access check for audit."""
        self._audit_log.append({
            "timestamp": datetime.now().isoformat(),
            "action": "access_check",
            "subject": subject,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "requested_action": action,
            "allowed": allowed,
            "reason": reason,
        })

    def get_subject_roles(self, subject_id: str) -> List[AccessRole]:
        """Get all roles for a subject."""
        grant_ids = self._subject_grants.get(subject_id, [])
        roles = []
        for grant_id in grant_ids:
            grant = self._grants.get(grant_id)
            if grant and grant.role_id in self._roles:
                role = self._roles[grant.role_id]
                if role not in roles:
                    roles.append(role)
        return roles

    def get_audit_log(
        self,
        subject_id: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get audit log entries."""
        entries = list(self._audit_log)

        if subject_id:
            entries = [e for e in entries if e.get("subject") == subject_id]
        if action:
            entries = [e for e in entries if e.get("action") == action]

        return entries[-limit:]

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        roles = len(self._roles)
        grants = len(self._grants)
        return (True, f"AccessControlManager: {roles} roles, {grants} grants")

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
            message=f"AccessControlManager: initialized={self._initialized}, roles={len(self._roles)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="AccessControlManager",
            version="1.0.0",
            inputs=["cross_repo.request"],
            outputs=["access.granted", "access.denied"],
            side_effects=["writes_access_log"],
        )

    def activation_triggers(self) -> List[str]:
        return []  # always_on
