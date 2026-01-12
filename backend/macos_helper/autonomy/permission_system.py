"""
Fine-Grained Permission System for JARVIS Autonomous Actions.

This module provides a comprehensive permission management system that controls
what actions JARVIS can perform autonomously. It supports multiple permission
levels, scopes, and contextual decision-making.

Key Features:
    - Hierarchical permission levels (deny, ask, allow, auto)
    - Scope-based permissions (global, category, action-specific)
    - Context-aware decisions (time, location, user state)
    - Dynamic permission escalation
    - Audit logging for all decisions
    - Temporary permission grants

Environment Variables:
    JARVIS_PERMISSION_MODE: default mode (paranoid/standard/permissive)
    JARVIS_PERMISSION_AUTO_APPROVE_MINIMAL: auto-approve minimal risk (default: true)
    JARVIS_PERMISSION_AUDIT_ENABLED: enable audit logging (default: true)
    JARVIS_PERMISSION_CACHE_TTL: cache TTL in seconds (default: 60)
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock
from .action_registry import ActionCategory, ActionRiskLevel, ActionType

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class PermissionLevel(Enum):
    """Permission levels from most to least restrictive."""

    DENY = 0  # Always deny, no exceptions
    ASK = 1  # Always ask user for confirmation
    ALLOW_ONCE = 2  # Allow once, then reset to ASK
    ALLOW = 3  # Allow without asking
    AUTO = 4  # Automatically decide based on context

    def __gt__(self, other: "PermissionLevel") -> bool:
        return self.value > other.value

    def __lt__(self, other: "PermissionLevel") -> bool:
        return self.value < other.value

    def __ge__(self, other: "PermissionLevel") -> bool:
        return self.value >= other.value

    def __le__(self, other: "PermissionLevel") -> bool:
        return self.value <= other.value


class PermissionScope(Enum):
    """Scope of a permission grant."""

    GLOBAL = "global"  # Applies to all actions
    CATEGORY = "category"  # Applies to an action category
    ACTION = "action"  # Applies to a specific action type
    INSTANCE = "instance"  # Applies to a specific action instance


class DecisionReason(Enum):
    """Reasons for permission decisions."""

    EXPLICIT_GRANT = "explicit_grant"
    EXPLICIT_DENY = "explicit_deny"
    DEFAULT_POLICY = "default_policy"
    RISK_LEVEL_POLICY = "risk_level_policy"
    CONTEXT_EVALUATION = "context_evaluation"
    TIME_RESTRICTION = "time_restriction"
    RATE_LIMIT = "rate_limit"
    SAFETY_OVERRIDE = "safety_override"
    TEMPORARY_GRANT = "temporary_grant"
    CACHED_DECISION = "cached_decision"
    USER_CONFIRMATION = "user_confirmation"


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class Permission:
    """A permission grant or denial."""

    scope: PermissionScope
    level: PermissionLevel
    target: str  # Action type, category, or "*" for global
    granted_by: str = "system"
    granted_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    conditions: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_expired(self) -> bool:
        """Check if permission has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if permission is still valid."""
        return not self.is_expired

    def matches(
        self,
        action_type: ActionType,
        category: ActionCategory
    ) -> bool:
        """Check if this permission applies to the given action."""
        if not self.is_valid:
            return False

        if self.scope == PermissionScope.GLOBAL:
            return self.target == "*"

        if self.scope == PermissionScope.CATEGORY:
            return self.target == category.value

        if self.scope == PermissionScope.ACTION:
            return self.target == action_type.name

        return False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "scope": self.scope.value,
            "level": self.level.name,
            "target": self.target,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "conditions": self.conditions,
            "reason": self.reason,
        }


@dataclass
class PermissionContext:
    """Context for making permission decisions."""

    action_type: ActionType
    action_category: ActionCategory
    risk_level: ActionRiskLevel
    parameters: Dict[str, Any] = field(default_factory=dict)

    # Temporal context
    timestamp: datetime = field(default_factory=datetime.now)
    hour_of_day: int = field(default_factory=lambda: datetime.now().hour)
    day_of_week: int = field(default_factory=lambda: datetime.now().weekday())

    # User context
    user_state: str = "active"  # active, idle, away, dnd
    user_confirmed: bool = False

    # System context
    screen_locked: bool = False
    focus_mode_active: bool = False
    meeting_in_progress: bool = False

    # Recent history
    recent_failures: int = 0
    recent_successes: int = 0
    actions_in_last_minute: int = 0

    # Request metadata
    request_source: str = "unknown"  # voice, automation, schedule
    urgency: str = "normal"  # low, normal, high, critical


@dataclass
class PermissionDecision:
    """Result of a permission check."""

    allowed: bool
    level: PermissionLevel
    reason: DecisionReason
    message: str
    requires_confirmation: bool = False
    context: Optional[PermissionContext] = None
    permission: Optional[Permission] = None
    decided_at: datetime = field(default_factory=datetime.now)
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "allowed": self.allowed,
            "level": self.level.name,
            "reason": self.reason.value,
            "message": self.message,
            "requires_confirmation": self.requires_confirmation,
            "decided_at": self.decided_at.isoformat(),
            "confidence": self.confidence,
        }


@dataclass
class PermissionSystemConfig:
    """Configuration for the permission system."""

    # Default mode
    mode: str = "standard"  # paranoid, standard, permissive

    # Risk level policies
    auto_approve_minimal: bool = True
    auto_approve_low: bool = False
    always_ask_high: bool = True
    always_deny_critical: bool = False

    # Rate limiting
    rate_limit_enabled: bool = True
    max_actions_per_minute: int = 30
    max_actions_per_hour: int = 500

    # Time restrictions
    quiet_hours_enabled: bool = False
    quiet_hours_start: int = 22  # 10 PM
    quiet_hours_end: int = 7  # 7 AM
    quiet_hours_allow_critical: bool = True

    # Caching
    cache_enabled: bool = True
    cache_ttl_seconds: float = 60.0

    # Audit
    audit_enabled: bool = True

    @classmethod
    def from_env(cls) -> "PermissionSystemConfig":
        """Create configuration from environment variables."""
        mode = os.getenv("JARVIS_PERMISSION_MODE", "standard")

        # Set defaults based on mode
        if mode == "paranoid":
            auto_approve_minimal = False
            auto_approve_low = False
            always_ask_high = True
            always_deny_critical = True
        elif mode == "permissive":
            auto_approve_minimal = True
            auto_approve_low = True
            always_ask_high = False
            always_deny_critical = False
        else:  # standard
            auto_approve_minimal = True
            auto_approve_low = False
            always_ask_high = True
            always_deny_critical = False

        return cls(
            mode=mode,
            auto_approve_minimal=os.getenv(
                "JARVIS_PERMISSION_AUTO_APPROVE_MINIMAL",
                str(auto_approve_minimal)
            ).lower() == "true",
            auto_approve_low=auto_approve_low,
            always_ask_high=always_ask_high,
            always_deny_critical=always_deny_critical,
            rate_limit_enabled=os.getenv(
                "JARVIS_PERMISSION_RATE_LIMIT", "true"
            ).lower() == "true",
            max_actions_per_minute=int(os.getenv(
                "JARVIS_PERMISSION_MAX_ACTIONS_MINUTE", "30"
            )),
            max_actions_per_hour=int(os.getenv(
                "JARVIS_PERMISSION_MAX_ACTIONS_HOUR", "500"
            )),
            quiet_hours_enabled=os.getenv(
                "JARVIS_PERMISSION_QUIET_HOURS", "false"
            ).lower() == "true",
            quiet_hours_start=int(os.getenv(
                "JARVIS_PERMISSION_QUIET_START", "22"
            )),
            quiet_hours_end=int(os.getenv(
                "JARVIS_PERMISSION_QUIET_END", "7"
            )),
            cache_enabled=os.getenv(
                "JARVIS_PERMISSION_CACHE", "true"
            ).lower() == "true",
            cache_ttl_seconds=float(os.getenv(
                "JARVIS_PERMISSION_CACHE_TTL", "60"
            )),
            audit_enabled=os.getenv(
                "JARVIS_PERMISSION_AUDIT_ENABLED", "true"
            ).lower() == "true",
        )


@dataclass
class AuditEntry:
    """Audit log entry for permission decisions."""

    timestamp: datetime
    action_type: ActionType
    decision: PermissionDecision
    context_summary: Dict[str, Any]
    request_source: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "action_type": self.action_type.name,
            "decision": self.decision.to_dict(),
            "context": self.context_summary,
            "source": self.request_source,
        }


# =============================================================================
# PERMISSION SYSTEM
# =============================================================================


class PermissionSystem:
    """
    Fine-grained permission management for autonomous actions.

    This system controls what actions JARVIS can perform autonomously,
    supporting multiple permission levels, contextual decisions, and
    comprehensive audit logging.
    """

    def __init__(self, config: Optional[PermissionSystemConfig] = None):
        """Initialize the permission system."""
        self.config = config or PermissionSystemConfig.from_env()

        # Permission storage
        self._permissions: List[Permission] = []
        self._category_defaults: Dict[ActionCategory, PermissionLevel] = {}
        self._action_defaults: Dict[ActionType, PermissionLevel] = {}

        # Decision cache
        self._cache: Dict[str, Tuple[PermissionDecision, datetime]] = {}

        # Rate limiting
        self._action_history: List[Tuple[datetime, ActionType]] = []

        # Audit log
        self._audit_log: List[AuditEntry] = []
        self._max_audit_entries = 10000

        # State
        self._is_running = False
        self._lock = asyncio.Lock()
        self._callbacks: List[weakref.ref] = []

        # Initialize default policies
        self._setup_default_policies()

    async def start(self) -> None:
        """Start the permission system."""
        if self._is_running:
            return

        logger.info("Starting PermissionSystem...")
        self._is_running = True

        # Start background cleanup task
        asyncio.create_task(self._cleanup_loop())

        logger.info(f"PermissionSystem started in '{self.config.mode}' mode")

    async def stop(self) -> None:
        """Stop the permission system."""
        if not self._is_running:
            return

        logger.info("Stopping PermissionSystem...")
        self._is_running = False
        logger.info("PermissionSystem stopped")

    @property
    def is_running(self) -> bool:
        """Check if system is running."""
        return self._is_running

    async def check_permission(
        self,
        context: PermissionContext
    ) -> PermissionDecision:
        """
        Check if an action is permitted in the given context.

        Args:
            context: Permission context with action and environmental info

        Returns:
            PermissionDecision with result and reasoning
        """
        async with self._lock:
            # Check cache first
            cache_key = self._get_cache_key(context)
            if self.config.cache_enabled:
                cached = self._get_cached_decision(cache_key)
                if cached:
                    return cached

            # Make decision
            decision = await self._evaluate_permission(context)

            # Cache decision
            if self.config.cache_enabled:
                self._cache_decision(cache_key, decision)

            # Audit log
            if self.config.audit_enabled:
                self._log_decision(context, decision)

            # Record for rate limiting
            self._record_action(context.action_type)

            return decision

    async def _evaluate_permission(
        self,
        context: PermissionContext
    ) -> PermissionDecision:
        """Evaluate permission for a given context."""

        # 1. Check explicit denials
        explicit_deny = self._check_explicit_permissions(
            context.action_type,
            context.action_category,
            check_deny=True
        )
        if explicit_deny:
            return PermissionDecision(
                allowed=False,
                level=PermissionLevel.DENY,
                reason=DecisionReason.EXPLICIT_DENY,
                message=f"Action explicitly denied: {explicit_deny.reason}",
                context=context,
                permission=explicit_deny
            )

        # 2. Check rate limits
        if self.config.rate_limit_enabled:
            rate_limit_check = self._check_rate_limits()
            if not rate_limit_check[0]:
                return PermissionDecision(
                    allowed=False,
                    level=PermissionLevel.DENY,
                    reason=DecisionReason.RATE_LIMIT,
                    message=f"Rate limit exceeded: {rate_limit_check[1]}",
                    context=context
                )

        # 3. Check time restrictions
        if self.config.quiet_hours_enabled:
            time_check = self._check_time_restrictions(context)
            if not time_check[0]:
                return PermissionDecision(
                    allowed=False,
                    level=PermissionLevel.DENY,
                    reason=DecisionReason.TIME_RESTRICTION,
                    message=f"Time restriction: {time_check[1]}",
                    context=context
                )

        # 4. Check explicit grants
        explicit_grant = self._check_explicit_permissions(
            context.action_type,
            context.action_category,
            check_deny=False
        )
        if explicit_grant:
            allowed = explicit_grant.level >= PermissionLevel.ALLOW
            requires_confirmation = explicit_grant.level == PermissionLevel.ASK

            return PermissionDecision(
                allowed=allowed or requires_confirmation,
                level=explicit_grant.level,
                reason=DecisionReason.EXPLICIT_GRANT,
                message="Explicit permission grant",
                requires_confirmation=requires_confirmation,
                context=context,
                permission=explicit_grant
            )

        # 5. Apply risk level policies
        risk_decision = self._apply_risk_policy(context)
        if risk_decision:
            return risk_decision

        # 6. Context-based evaluation
        context_decision = await self._evaluate_context(context)
        if context_decision:
            return context_decision

        # 7. Fall back to default policy
        return self._apply_default_policy(context)

    def _check_explicit_permissions(
        self,
        action_type: ActionType,
        category: ActionCategory,
        check_deny: bool
    ) -> Optional[Permission]:
        """Check for explicit permission grants/denials."""
        matching_permissions = []

        for perm in self._permissions:
            if not perm.is_valid:
                continue

            if perm.matches(action_type, category):
                if check_deny and perm.level == PermissionLevel.DENY:
                    matching_permissions.append((perm, self._get_permission_priority(perm)))
                elif not check_deny and perm.level != PermissionLevel.DENY:
                    matching_permissions.append((perm, self._get_permission_priority(perm)))

        if not matching_permissions:
            return None

        # Return highest priority match
        matching_permissions.sort(key=lambda x: x[1], reverse=True)
        return matching_permissions[0][0]

    def _get_permission_priority(self, perm: Permission) -> int:
        """Get priority for a permission (higher = more specific)."""
        scope_priority = {
            PermissionScope.INSTANCE: 4,
            PermissionScope.ACTION: 3,
            PermissionScope.CATEGORY: 2,
            PermissionScope.GLOBAL: 1,
        }
        return scope_priority.get(perm.scope, 0)

    def _check_rate_limits(self) -> Tuple[bool, str]:
        """Check if rate limits are exceeded."""
        now = datetime.now()

        # Clean old entries
        minute_ago = now - timedelta(minutes=1)
        hour_ago = now - timedelta(hours=1)
        self._action_history = [
            (ts, at) for ts, at in self._action_history
            if ts > hour_ago
        ]

        # Count actions
        actions_last_minute = sum(
            1 for ts, _ in self._action_history
            if ts > minute_ago
        )
        actions_last_hour = len(self._action_history)

        if actions_last_minute >= self.config.max_actions_per_minute:
            return False, f"Exceeded {self.config.max_actions_per_minute} actions/minute"

        if actions_last_hour >= self.config.max_actions_per_hour:
            return False, f"Exceeded {self.config.max_actions_per_hour} actions/hour"

        return True, ""

    def _check_time_restrictions(
        self,
        context: PermissionContext
    ) -> Tuple[bool, str]:
        """Check time-based restrictions."""
        hour = context.hour_of_day

        # Check quiet hours
        in_quiet_hours = False
        if self.config.quiet_hours_start > self.config.quiet_hours_end:
            # Spans midnight (e.g., 22:00 to 07:00)
            in_quiet_hours = (
                hour >= self.config.quiet_hours_start or
                hour < self.config.quiet_hours_end
            )
        else:
            in_quiet_hours = (
                self.config.quiet_hours_start <= hour < self.config.quiet_hours_end
            )

        if in_quiet_hours:
            # Allow critical actions during quiet hours if configured
            if (self.config.quiet_hours_allow_critical and
                context.urgency == "critical"):
                return True, ""

            return False, f"Quiet hours ({self.config.quiet_hours_start}:00-{self.config.quiet_hours_end}:00)"

        return True, ""

    def _apply_risk_policy(
        self,
        context: PermissionContext
    ) -> Optional[PermissionDecision]:
        """Apply risk-level-based policies."""
        risk = context.risk_level

        # Critical risk
        if risk == ActionRiskLevel.CRITICAL:
            if self.config.always_deny_critical:
                return PermissionDecision(
                    allowed=False,
                    level=PermissionLevel.DENY,
                    reason=DecisionReason.RISK_LEVEL_POLICY,
                    message="Critical risk actions require explicit permission",
                    context=context
                )
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ASK,
                reason=DecisionReason.RISK_LEVEL_POLICY,
                message="Critical risk action requires confirmation",
                requires_confirmation=True,
                context=context
            )

        # High risk
        if risk == ActionRiskLevel.HIGH:
            if self.config.always_ask_high:
                return PermissionDecision(
                    allowed=True,
                    level=PermissionLevel.ASK,
                    reason=DecisionReason.RISK_LEVEL_POLICY,
                    message="High risk action requires confirmation",
                    requires_confirmation=True,
                    context=context
                )

        # Low risk
        if risk == ActionRiskLevel.LOW and self.config.auto_approve_low:
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ALLOW,
                reason=DecisionReason.RISK_LEVEL_POLICY,
                message="Low risk action auto-approved",
                context=context
            )

        # Minimal risk
        if risk == ActionRiskLevel.MINIMAL and self.config.auto_approve_minimal:
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ALLOW,
                reason=DecisionReason.RISK_LEVEL_POLICY,
                message="Minimal risk action auto-approved",
                context=context
            )

        return None

    async def _evaluate_context(
        self,
        context: PermissionContext
    ) -> Optional[PermissionDecision]:
        """Evaluate permission based on contextual factors."""

        # Screen locked - be more restrictive
        if context.screen_locked:
            if context.risk_level.value >= ActionRiskLevel.MODERATE.value:
                return PermissionDecision(
                    allowed=False,
                    level=PermissionLevel.DENY,
                    reason=DecisionReason.CONTEXT_EVALUATION,
                    message="Screen is locked, moderate+ risk actions denied",
                    context=context
                )

        # Focus mode - be more restrictive with distracting actions
        if context.focus_mode_active:
            # Allow productivity actions, restrict others
            if context.action_category not in [
                ActionCategory.PRODUCTIVITY,
                ActionCategory.SYSTEM,
                ActionCategory.SECURITY
            ]:
                return PermissionDecision(
                    allowed=True,
                    level=PermissionLevel.ASK,
                    reason=DecisionReason.CONTEXT_EVALUATION,
                    message="Focus mode active, non-productivity action needs confirmation",
                    requires_confirmation=True,
                    context=context
                )

        # Meeting in progress - be cautious about noisy actions
        if context.meeting_in_progress:
            if context.action_category == ActionCategory.MEDIA:
                return PermissionDecision(
                    allowed=True,
                    level=PermissionLevel.ASK,
                    reason=DecisionReason.CONTEXT_EVALUATION,
                    message="Meeting in progress, media action needs confirmation",
                    requires_confirmation=True,
                    context=context
                )

        # Recent failures - be more cautious
        if context.recent_failures >= 3:
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ASK,
                reason=DecisionReason.CONTEXT_EVALUATION,
                message="Recent failures detected, confirmation recommended",
                requires_confirmation=True,
                confidence=0.7,
                context=context
            )

        return None

    def _apply_default_policy(
        self,
        context: PermissionContext
    ) -> PermissionDecision:
        """Apply default policy when no other rules match."""

        # Check action-specific default
        if context.action_type in self._action_defaults:
            level = self._action_defaults[context.action_type]
            return PermissionDecision(
                allowed=level >= PermissionLevel.ALLOW,
                level=level,
                reason=DecisionReason.DEFAULT_POLICY,
                message="Action default policy applied",
                requires_confirmation=level == PermissionLevel.ASK,
                context=context
            )

        # Check category default
        if context.action_category in self._category_defaults:
            level = self._category_defaults[context.action_category]
            return PermissionDecision(
                allowed=level >= PermissionLevel.ALLOW,
                level=level,
                reason=DecisionReason.DEFAULT_POLICY,
                message="Category default policy applied",
                requires_confirmation=level == PermissionLevel.ASK,
                context=context
            )

        # Mode-based default
        if self.config.mode == "paranoid":
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ASK,
                reason=DecisionReason.DEFAULT_POLICY,
                message="Paranoid mode requires confirmation",
                requires_confirmation=True,
                context=context
            )
        elif self.config.mode == "permissive":
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ALLOW,
                reason=DecisionReason.DEFAULT_POLICY,
                message="Permissive mode auto-allows",
                context=context
            )
        else:  # standard
            return PermissionDecision(
                allowed=True,
                level=PermissionLevel.ASK,
                reason=DecisionReason.DEFAULT_POLICY,
                message="Standard mode requires confirmation",
                requires_confirmation=True,
                context=context
            )

    def _setup_default_policies(self) -> None:
        """Setup default permission policies."""

        # Category defaults - based on mode
        if self.config.mode == "permissive":
            self._category_defaults = {
                ActionCategory.APPLICATION: PermissionLevel.ALLOW,
                ActionCategory.FILE_SYSTEM: PermissionLevel.ASK,
                ActionCategory.SYSTEM: PermissionLevel.ASK,
                ActionCategory.NETWORK: PermissionLevel.ALLOW,
                ActionCategory.MEDIA: PermissionLevel.ALLOW,
                ActionCategory.NOTIFICATION: PermissionLevel.ALLOW,
                ActionCategory.SECURITY: PermissionLevel.ASK,
                ActionCategory.COMMUNICATION: PermissionLevel.ASK,
                ActionCategory.PRODUCTIVITY: PermissionLevel.ALLOW,
                ActionCategory.DISPLAY: PermissionLevel.ALLOW,
                ActionCategory.HARDWARE: PermissionLevel.ASK,
                ActionCategory.WORKFLOW: PermissionLevel.ALLOW,
                ActionCategory.CUSTOM: PermissionLevel.ASK,
            }
        elif self.config.mode == "paranoid":
            self._category_defaults = {cat: PermissionLevel.ASK for cat in ActionCategory}
            self._category_defaults[ActionCategory.SECURITY] = PermissionLevel.DENY
        else:  # standard
            self._category_defaults = {
                ActionCategory.APPLICATION: PermissionLevel.ALLOW,
                ActionCategory.FILE_SYSTEM: PermissionLevel.ASK,
                ActionCategory.SYSTEM: PermissionLevel.ASK,
                ActionCategory.NETWORK: PermissionLevel.ALLOW,
                ActionCategory.MEDIA: PermissionLevel.ALLOW,
                ActionCategory.NOTIFICATION: PermissionLevel.ALLOW,
                ActionCategory.SECURITY: PermissionLevel.ASK,
                ActionCategory.COMMUNICATION: PermissionLevel.ASK,
                ActionCategory.PRODUCTIVITY: PermissionLevel.ALLOW,
                ActionCategory.DISPLAY: PermissionLevel.ALLOW,
                ActionCategory.HARDWARE: PermissionLevel.ASK,
                ActionCategory.WORKFLOW: PermissionLevel.ASK,
                ActionCategory.CUSTOM: PermissionLevel.ASK,
            }

    # =========================================================================
    # Permission Management
    # =========================================================================

    def grant_permission(
        self,
        scope: PermissionScope,
        target: str,
        level: PermissionLevel,
        duration_minutes: Optional[int] = None,
        conditions: Optional[Dict[str, Any]] = None,
        reason: str = "",
        granted_by: str = "user"
    ) -> Permission:
        """
        Grant a permission.

        Args:
            scope: Permission scope
            target: Target (action name, category, or "*")
            level: Permission level
            duration_minutes: Optional duration (None = permanent)
            conditions: Optional conditions
            reason: Reason for grant
            granted_by: Who granted the permission

        Returns:
            Created Permission object
        """
        expires_at = None
        if duration_minutes:
            expires_at = datetime.now() + timedelta(minutes=duration_minutes)

        permission = Permission(
            scope=scope,
            level=level,
            target=target,
            granted_by=granted_by,
            expires_at=expires_at,
            conditions=conditions or {},
            reason=reason
        )

        self._permissions.append(permission)

        # Invalidate relevant cache entries
        self._invalidate_cache(target)

        logger.info(
            f"Permission granted: {level.name} for {target} "
            f"(scope: {scope.value}, expires: {expires_at})"
        )

        return permission

    def revoke_permission(
        self,
        target: str,
        scope: Optional[PermissionScope] = None
    ) -> int:
        """
        Revoke permissions for a target.

        Args:
            target: Target to revoke permissions for
            scope: Optional scope filter

        Returns:
            Number of permissions revoked
        """
        original_count = len(self._permissions)

        self._permissions = [
            p for p in self._permissions
            if not (p.target == target and (scope is None or p.scope == scope))
        ]

        revoked = original_count - len(self._permissions)

        if revoked > 0:
            self._invalidate_cache(target)
            logger.info(f"Revoked {revoked} permission(s) for {target}")

        return revoked

    def set_category_default(
        self,
        category: ActionCategory,
        level: PermissionLevel
    ) -> None:
        """Set default permission level for a category."""
        self._category_defaults[category] = level
        self._invalidate_cache(category.value)
        logger.info(f"Set category default: {category.value} = {level.name}")

    def set_action_default(
        self,
        action_type: ActionType,
        level: PermissionLevel
    ) -> None:
        """Set default permission level for an action type."""
        self._action_defaults[action_type] = level
        self._invalidate_cache(action_type.name)
        logger.info(f"Set action default: {action_type.name} = {level.name}")

    def list_permissions(
        self,
        include_expired: bool = False
    ) -> List[Permission]:
        """List all permissions."""
        if include_expired:
            return list(self._permissions)
        return [p for p in self._permissions if p.is_valid]

    def clear_expired_permissions(self) -> int:
        """Remove expired permissions."""
        original_count = len(self._permissions)
        self._permissions = [p for p in self._permissions if p.is_valid]
        removed = original_count - len(self._permissions)
        if removed > 0:
            logger.debug(f"Cleared {removed} expired permission(s)")
        return removed

    # =========================================================================
    # Cache Management
    # =========================================================================

    def _get_cache_key(self, context: PermissionContext) -> str:
        """Generate cache key for a context."""
        return f"{context.action_type.name}:{context.action_category.value}:{context.risk_level.name}"

    def _get_cached_decision(
        self,
        key: str
    ) -> Optional[PermissionDecision]:
        """Get cached decision if valid."""
        if key not in self._cache:
            return None

        decision, cached_at = self._cache[key]
        if (datetime.now() - cached_at).total_seconds() > self.config.cache_ttl_seconds:
            del self._cache[key]
            return None

        # Return with updated reason
        return PermissionDecision(
            allowed=decision.allowed,
            level=decision.level,
            reason=DecisionReason.CACHED_DECISION,
            message=f"Cached: {decision.message}",
            requires_confirmation=decision.requires_confirmation,
            context=decision.context,
            confidence=decision.confidence
        )

    def _cache_decision(
        self,
        key: str,
        decision: PermissionDecision
    ) -> None:
        """Cache a decision."""
        self._cache[key] = (decision, datetime.now())

    def _invalidate_cache(self, pattern: str) -> None:
        """Invalidate cache entries matching pattern."""
        keys_to_remove = [
            k for k in self._cache.keys()
            if pattern in k or pattern == "*"
        ]
        for key in keys_to_remove:
            del self._cache[key]

    # =========================================================================
    # Audit Logging
    # =========================================================================

    def _log_decision(
        self,
        context: PermissionContext,
        decision: PermissionDecision
    ) -> None:
        """Log a permission decision."""
        entry = AuditEntry(
            timestamp=datetime.now(),
            action_type=context.action_type,
            decision=decision,
            context_summary={
                "category": context.action_category.value,
                "risk_level": context.risk_level.name,
                "user_state": context.user_state,
                "screen_locked": context.screen_locked,
                "focus_mode": context.focus_mode_active,
            },
            request_source=context.request_source
        )

        self._audit_log.append(entry)

        # Trim if too large
        if len(self._audit_log) > self._max_audit_entries:
            self._audit_log = self._audit_log[-self._max_audit_entries:]

    def _record_action(self, action_type: ActionType) -> None:
        """Record action for rate limiting."""
        self._action_history.append((datetime.now(), action_type))

    def get_audit_log(
        self,
        limit: int = 100,
        action_type: Optional[ActionType] = None,
        allowed_only: Optional[bool] = None
    ) -> List[AuditEntry]:
        """Get audit log entries."""
        entries = self._audit_log

        if action_type:
            entries = [e for e in entries if e.action_type == action_type]

        if allowed_only is not None:
            entries = [e for e in entries if e.decision.allowed == allowed_only]

        return entries[-limit:]

    def get_statistics(self) -> Dict[str, Any]:
        """Get permission system statistics."""
        total_decisions = len(self._audit_log)
        allowed = sum(1 for e in self._audit_log if e.decision.allowed)
        denied = total_decisions - allowed

        return {
            "mode": self.config.mode,
            "total_permissions": len(self._permissions),
            "active_permissions": len([p for p in self._permissions if p.is_valid]),
            "total_decisions": total_decisions,
            "allowed_decisions": allowed,
            "denied_decisions": denied,
            "approval_rate": allowed / max(1, total_decisions),
            "cache_size": len(self._cache),
            "rate_limit_history": len(self._action_history),
            "decisions_by_reason": self._get_decisions_by_reason(),
        }

    def _get_decisions_by_reason(self) -> Dict[str, int]:
        """Get decision counts by reason."""
        counts: Dict[str, int] = defaultdict(int)
        for entry in self._audit_log:
            counts[entry.decision.reason.value] += 1
        return dict(counts)

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _cleanup_loop(self) -> None:
        """Background task for cleanup."""
        while self._is_running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                self.clear_expired_permissions()

                # Clean old rate limit history
                hour_ago = datetime.now() - timedelta(hours=1)
                self._action_history = [
                    (ts, at) for ts, at in self._action_history
                    if ts > hour_ago
                ]

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Permission cleanup error: {e}")


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================


_permission_system_instance: Optional[PermissionSystem] = None
_permission_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_permission_system() -> PermissionSystem:
    """Get the global permission system instance."""
    global _permission_system_instance
    if _permission_system_instance is None:
        _permission_system_instance = PermissionSystem()
    return _permission_system_instance


async def start_permission_system() -> PermissionSystem:
    """Start the global permission system."""
    async with _permission_lock:
        system = get_permission_system()
        if not system.is_running:
            await system.start()
        return system


async def stop_permission_system() -> None:
    """Stop the global permission system."""
    async with _permission_lock:
        global _permission_system_instance
        if _permission_system_instance and _permission_system_instance.is_running:
            await _permission_system_instance.stop()
