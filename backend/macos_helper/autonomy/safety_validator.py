"""
Action Safety Validator for JARVIS Autonomous System.

This module provides comprehensive safety validation for autonomous actions,
including constraint checking, risk assessment, and pre-execution validation.

Key Features:
    - Multi-layer safety checks (static, dynamic, contextual)
    - Constraint-based validation
    - Risk scoring and assessment
    - Resource impact analysis
    - Anomaly detection
    - Safety override mechanisms

Environment Variables:
    JARVIS_SAFETY_LEVEL: default safety level (strict/standard/relaxed)
    JARVIS_SAFETY_MAX_RISK_SCORE: maximum allowed risk score (default: 0.7)
    JARVIS_SAFETY_ANOMALY_THRESHOLD: anomaly detection threshold (default: 0.8)
    JARVIS_SAFETY_AUDIT_ENABLED: enable safety audit logging (default: true)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock
from .action_registry import ActionCategory, ActionMetadata, ActionRiskLevel, ActionType

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class SafetyLevel(Enum):
    """Safety validation levels."""

    STRICT = "strict"  # Maximum safety, deny on any concern
    STANDARD = "standard"  # Balanced safety and functionality
    RELAXED = "relaxed"  # Minimum safety, allow most actions


class ConstraintType(Enum):
    """Types of safety constraints."""

    PATH_RESTRICTION = "path_restriction"
    APP_RESTRICTION = "app_restriction"
    TIME_RESTRICTION = "time_restriction"
    RATE_LIMIT = "rate_limit"
    RESOURCE_LIMIT = "resource_limit"
    PATTERN_BLOCK = "pattern_block"
    DEPENDENCY = "dependency"
    STATE_REQUIREMENT = "state_requirement"
    CUSTOM = "custom"


class CheckSeverity(Enum):
    """Severity levels for safety check failures."""

    INFO = 1  # Informational, action proceeds
    WARNING = 2  # Warning logged, action proceeds
    BLOCK = 3  # Action blocked unless overridden
    CRITICAL = 4  # Action absolutely blocked


class ValidationResult(Enum):
    """Results of safety validation."""

    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL_RECOVERABLE = "fail_recoverable"
    FAIL_CRITICAL = "fail_critical"


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class SafetyConstraint:
    """Definition of a safety constraint."""

    name: str
    description: str
    constraint_type: ConstraintType
    severity: CheckSeverity
    condition: Callable[[Dict[str, Any]], bool]
    error_message: str
    applies_to: List[ActionType] = field(default_factory=list)
    applies_to_categories: List[ActionCategory] = field(default_factory=list)
    enabled: bool = True
    override_allowed: bool = False

    def check(
        self,
        action_type: ActionType,
        category: ActionCategory,
        params: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Check if constraint is satisfied."""
        # Check if constraint applies
        if self.applies_to and action_type not in self.applies_to:
            return True, ""
        if self.applies_to_categories and category not in self.applies_to_categories:
            return True, ""

        # Merge params and context for condition check
        check_data = {**params, **context, "action_type": action_type, "category": category}

        try:
            satisfied = self.condition(check_data)
            if satisfied:
                return True, ""
            return False, self.error_message
        except Exception as e:
            logger.error(f"Constraint check error ({self.name}): {e}")
            return False, f"Constraint check failed: {e}"


@dataclass
class SafetyCheck:
    """Result of a single safety check."""

    check_name: str
    passed: bool
    severity: CheckSeverity
    message: str
    constraint: Optional[SafetyConstraint] = None
    details: Dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.now)


@dataclass
class RiskFactor:
    """A factor contributing to risk assessment."""

    name: str
    weight: float  # 0.0 to 1.0
    score: float  # 0.0 to 1.0
    reason: str


@dataclass
class RiskAssessment:
    """Complete risk assessment for an action."""

    action_type: ActionType
    total_score: float  # 0.0 to 1.0
    risk_level: ActionRiskLevel
    factors: List[RiskFactor] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    assessed_at: datetime = field(default_factory=datetime.now)

    def is_acceptable(self, threshold: float) -> bool:
        """Check if risk is below threshold."""
        return self.total_score <= threshold

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_type": self.action_type.name,
            "total_score": self.total_score,
            "risk_level": self.risk_level.name,
            "factors": [
                {"name": f.name, "weight": f.weight, "score": f.score, "reason": f.reason}
                for f in self.factors
            ],
            "recommendations": self.recommendations,
            "assessed_at": self.assessed_at.isoformat(),
        }


@dataclass
class SafetyCheckResult:
    """Complete result of safety validation."""

    action_type: ActionType
    result: ValidationResult
    passed: bool
    checks: List[SafetyCheck] = field(default_factory=list)
    risk_assessment: Optional[RiskAssessment] = None
    blocked_by: Optional[SafetyCheck] = None
    warnings: List[str] = field(default_factory=list)
    validated_at: datetime = field(default_factory=datetime.now)
    validation_time_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_type": self.action_type.name,
            "result": self.result.value,
            "passed": self.passed,
            "checks": [
                {"name": c.check_name, "passed": c.passed, "severity": c.severity.name, "message": c.message}
                for c in self.checks
            ],
            "risk_assessment": self.risk_assessment.to_dict() if self.risk_assessment else None,
            "blocked_by": self.blocked_by.check_name if self.blocked_by else None,
            "warnings": self.warnings,
            "validated_at": self.validated_at.isoformat(),
            "validation_time_ms": self.validation_time_ms,
        }


@dataclass
class SafetyValidatorConfig:
    """Configuration for the safety validator."""

    # Safety level
    level: SafetyLevel = SafetyLevel.STANDARD

    # Risk thresholds
    max_risk_score: float = 0.7
    warning_risk_score: float = 0.5

    # Anomaly detection
    anomaly_threshold: float = 0.8
    anomaly_window_minutes: int = 30

    # Path restrictions
    safe_directories: List[str] = field(default_factory=lambda: [
        "~/Desktop",
        "~/Documents",
        "~/Downloads",
        "~/Pictures",
        "~/Music",
        "~/Movies",
    ])
    blocked_paths: List[str] = field(default_factory=lambda: [
        "/System",
        "/Library",
        "/usr",
        "/bin",
        "/sbin",
        "~/.ssh",
        "~/.gnupg",
    ])

    # App restrictions
    blocked_apps: List[str] = field(default_factory=lambda: [
        "System Preferences",
        "System Settings",
        "Disk Utility",
        "Keychain Access",
    ])
    sensitive_apps: List[str] = field(default_factory=lambda: [
        "1Password",
        "Bitwarden",
        "Terminal",
        "iTerm",
    ])

    # Pattern blocks
    dangerous_patterns: List[str] = field(default_factory=lambda: [
        r"rm\s+-rf\s+/",
        r"sudo\s+",
        r"chmod\s+777",
        r">\s*/dev/",
        r"mkfs",
        r"dd\s+if=",
    ])

    # Resource limits
    max_windows_close: int = 5
    max_apps_launch: int = 3
    max_files_delete: int = 10
    max_actions_per_minute: int = 30

    # Audit
    audit_enabled: bool = True

    @classmethod
    def from_env(cls) -> "SafetyValidatorConfig":
        """Create configuration from environment variables."""
        level_str = os.getenv("JARVIS_SAFETY_LEVEL", "standard").lower()
        level = SafetyLevel(level_str) if level_str in [l.value for l in SafetyLevel] else SafetyLevel.STANDARD

        return cls(
            level=level,
            max_risk_score=float(os.getenv("JARVIS_SAFETY_MAX_RISK_SCORE", "0.7")),
            warning_risk_score=float(os.getenv("JARVIS_SAFETY_WARNING_RISK_SCORE", "0.5")),
            anomaly_threshold=float(os.getenv("JARVIS_SAFETY_ANOMALY_THRESHOLD", "0.8")),
            anomaly_window_minutes=int(os.getenv("JARVIS_SAFETY_ANOMALY_WINDOW", "30")),
            max_windows_close=int(os.getenv("JARVIS_SAFETY_MAX_WINDOWS_CLOSE", "5")),
            max_apps_launch=int(os.getenv("JARVIS_SAFETY_MAX_APPS_LAUNCH", "3")),
            max_files_delete=int(os.getenv("JARVIS_SAFETY_MAX_FILES_DELETE", "10")),
            max_actions_per_minute=int(os.getenv("JARVIS_SAFETY_MAX_ACTIONS_MINUTE", "30")),
            audit_enabled=os.getenv("JARVIS_SAFETY_AUDIT_ENABLED", "true").lower() == "true",
        )


# =============================================================================
# SAFETY VALIDATOR
# =============================================================================


class SafetyValidator:
    """
    Comprehensive safety validation for autonomous actions.

    This class provides multi-layer safety validation including constraint
    checking, risk assessment, and anomaly detection.
    """

    def __init__(self, config: Optional[SafetyValidatorConfig] = None):
        """Initialize the safety validator."""
        self.config = config or SafetyValidatorConfig.from_env()

        # Constraints
        self._constraints: List[SafetyConstraint] = []
        self._custom_validators: Dict[ActionType, List[Callable]] = defaultdict(list)

        # History for anomaly detection
        self._action_history: List[Tuple[datetime, ActionType, Dict[str, Any]]] = []

        # Validation results cache
        self._validation_cache: Dict[str, Tuple[SafetyCheckResult, datetime]] = {}
        self._cache_ttl_seconds = 30.0

        # Statistics
        self._validation_count = 0
        self._block_count = 0
        self._warning_count = 0

        # State
        self._is_running = False
        self._lock = asyncio.Lock()

        # Register default constraints
        self._register_default_constraints()

    async def start(self) -> None:
        """Start the safety validator."""
        if self._is_running:
            return

        logger.info("Starting SafetyValidator...")
        self._is_running = True
        logger.info(f"SafetyValidator started with level: {self.config.level.value}")

    async def stop(self) -> None:
        """Stop the safety validator."""
        if not self._is_running:
            return

        logger.info("Stopping SafetyValidator...")
        self._is_running = False
        logger.info("SafetyValidator stopped")

    @property
    def is_running(self) -> bool:
        """Check if validator is running."""
        return self._is_running

    async def validate(
        self,
        action_type: ActionType,
        metadata: ActionMetadata,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> SafetyCheckResult:
        """
        Validate an action for safety.

        Args:
            action_type: The action type to validate
            metadata: Action metadata
            params: Action parameters
            context: Optional execution context

        Returns:
            SafetyCheckResult with validation outcome
        """
        start_time = datetime.now()
        context = context or {}
        checks: List[SafetyCheck] = []
        warnings: List[str] = []

        self._validation_count += 1

        # 1. Run static constraint checks
        constraint_checks = await self._check_constraints(
            action_type, metadata.category, params, context
        )
        checks.extend(constraint_checks)

        # Check for critical failures
        critical_failures = [c for c in constraint_checks if not c.passed and c.severity == CheckSeverity.CRITICAL]
        if critical_failures:
            self._block_count += 1
            return SafetyCheckResult(
                action_type=action_type,
                result=ValidationResult.FAIL_CRITICAL,
                passed=False,
                checks=checks,
                blocked_by=critical_failures[0],
                warnings=warnings,
                validation_time_ms=(datetime.now() - start_time).total_seconds() * 1000
            )

        # 2. Run parameter validation
        param_checks = await self._validate_parameters(action_type, metadata, params)
        checks.extend(param_checks)

        # 3. Assess risk
        risk_assessment = await self._assess_risk(action_type, metadata, params, context)

        # 4. Check for anomalies
        anomaly_check = await self._check_anomalies(action_type, params, context)
        if anomaly_check:
            checks.append(anomaly_check)

        # 5. Evaluate results
        blocking_checks = [
            c for c in checks
            if not c.passed and c.severity in [CheckSeverity.BLOCK, CheckSeverity.CRITICAL]
        ]
        warning_checks = [
            c for c in checks
            if not c.passed and c.severity == CheckSeverity.WARNING
        ]

        for wc in warning_checks:
            warnings.append(wc.message)
            self._warning_count += 1

        # Determine result
        if blocking_checks:
            self._block_count += 1

            # Check if safety level allows override
            recoverable = (
                self.config.level == SafetyLevel.RELAXED and
                all(c.constraint and c.constraint.override_allowed for c in blocking_checks if c.constraint)
            )

            return SafetyCheckResult(
                action_type=action_type,
                result=ValidationResult.FAIL_RECOVERABLE if recoverable else ValidationResult.FAIL_CRITICAL,
                passed=False,
                checks=checks,
                risk_assessment=risk_assessment,
                blocked_by=blocking_checks[0],
                warnings=warnings,
                validation_time_ms=(datetime.now() - start_time).total_seconds() * 1000
            )

        # Check risk threshold
        if risk_assessment.total_score > self.config.max_risk_score:
            self._block_count += 1
            return SafetyCheckResult(
                action_type=action_type,
                result=ValidationResult.FAIL_RECOVERABLE,
                passed=False,
                checks=checks,
                risk_assessment=risk_assessment,
                warnings=warnings + [f"Risk score {risk_assessment.total_score:.2f} exceeds threshold {self.config.max_risk_score}"],
                validation_time_ms=(datetime.now() - start_time).total_seconds() * 1000
            )

        # Record action for anomaly detection
        self._record_action(action_type, params)

        # Success
        result = ValidationResult.PASS_WITH_WARNINGS if warnings else ValidationResult.PASS

        return SafetyCheckResult(
            action_type=action_type,
            result=result,
            passed=True,
            checks=checks,
            risk_assessment=risk_assessment,
            warnings=warnings,
            validation_time_ms=(datetime.now() - start_time).total_seconds() * 1000
        )

    async def _check_constraints(
        self,
        action_type: ActionType,
        category: ActionCategory,
        params: Dict[str, Any],
        context: Dict[str, Any]
    ) -> List[SafetyCheck]:
        """Run all constraint checks."""
        checks = []

        for constraint in self._constraints:
            if not constraint.enabled:
                continue

            passed, message = constraint.check(action_type, category, params, context)

            checks.append(SafetyCheck(
                check_name=constraint.name,
                passed=passed,
                severity=constraint.severity,
                message=message if not passed else "Constraint satisfied",
                constraint=constraint
            ))

        return checks

    async def _validate_parameters(
        self,
        action_type: ActionType,
        metadata: ActionMetadata,
        params: Dict[str, Any]
    ) -> List[SafetyCheck]:
        """Validate action parameters."""
        checks = []

        # Use metadata's parameter validation
        valid, errors = metadata.validate_params(params)

        if not valid:
            for error in errors:
                checks.append(SafetyCheck(
                    check_name="parameter_validation",
                    passed=False,
                    severity=CheckSeverity.BLOCK,
                    message=error
                ))
        else:
            checks.append(SafetyCheck(
                check_name="parameter_validation",
                passed=True,
                severity=CheckSeverity.INFO,
                message="All parameters valid"
            ))

        return checks

    async def _assess_risk(
        self,
        action_type: ActionType,
        metadata: ActionMetadata,
        params: Dict[str, Any],
        context: Dict[str, Any]
    ) -> RiskAssessment:
        """Assess risk of an action."""
        factors: List[RiskFactor] = []
        recommendations: List[str] = []

        # Factor 1: Base risk level
        base_risk = metadata.risk_level.value / 5.0
        factors.append(RiskFactor(
            name="base_risk_level",
            weight=0.3,
            score=base_risk,
            reason=f"Action has {metadata.risk_level.name} base risk"
        ))

        # Factor 2: Confirmation requirement
        if metadata.requires_confirmation:
            factors.append(RiskFactor(
                name="requires_confirmation",
                weight=0.1,
                score=0.7,
                reason="Action requires confirmation"
            ))
        else:
            factors.append(RiskFactor(
                name="requires_confirmation",
                weight=0.1,
                score=0.0,
                reason="No confirmation required"
            ))

        # Factor 3: Rollback availability
        if metadata.supports_rollback:
            factors.append(RiskFactor(
                name="rollback_support",
                weight=0.15,
                score=0.2,
                reason="Action can be rolled back"
            ))
        else:
            factors.append(RiskFactor(
                name="rollback_support",
                weight=0.15,
                score=0.6,
                reason="Action cannot be rolled back"
            ))
            recommendations.append("Consider if action effects can be manually reversed")

        # Factor 4: Context factors
        context_risk = 0.0
        context_reasons = []

        if context.get("screen_locked"):
            context_risk += 0.3
            context_reasons.append("screen is locked")

        if context.get("meeting_in_progress"):
            context_risk += 0.2
            context_reasons.append("meeting in progress")

        if context.get("recent_failures", 0) >= 3:
            context_risk += 0.2
            context_reasons.append("recent failures detected")

        factors.append(RiskFactor(
            name="context_risk",
            weight=0.2,
            score=min(1.0, context_risk),
            reason=f"Context factors: {', '.join(context_reasons) if context_reasons else 'none'}"
        ))

        # Factor 5: Parameter sensitivity
        param_risk = self._assess_parameter_risk(action_type, params)
        factors.append(RiskFactor(
            name="parameter_risk",
            weight=0.25,
            score=param_risk,
            reason="Risk from parameter values"
        ))

        # Calculate total score (weighted average)
        total_weight = sum(f.weight for f in factors)
        total_score = sum(f.weight * f.score for f in factors) / total_weight if total_weight > 0 else 0.5

        # Determine risk level from score
        if total_score >= 0.8:
            risk_level = ActionRiskLevel.CRITICAL
        elif total_score >= 0.6:
            risk_level = ActionRiskLevel.HIGH
        elif total_score >= 0.4:
            risk_level = ActionRiskLevel.MODERATE
        elif total_score >= 0.2:
            risk_level = ActionRiskLevel.LOW
        else:
            risk_level = ActionRiskLevel.MINIMAL

        if total_score > self.config.warning_risk_score:
            recommendations.append("Consider running in dry-run mode first")

        return RiskAssessment(
            action_type=action_type,
            total_score=total_score,
            risk_level=risk_level,
            factors=factors,
            recommendations=recommendations
        )

    def _assess_parameter_risk(
        self,
        action_type: ActionType,
        params: Dict[str, Any]
    ) -> float:
        """Assess risk from parameter values."""
        risk_score = 0.0

        # Check for path parameters
        for key, value in params.items():
            if isinstance(value, str):
                # Path risk
                if any(p in key.lower() for p in ["path", "file", "directory", "folder"]):
                    path_risk = self._assess_path_risk(value)
                    risk_score = max(risk_score, path_risk)

                # Command risk
                if any(p in key.lower() for p in ["command", "script", "shell"]):
                    cmd_risk = self._assess_command_risk(value)
                    risk_score = max(risk_score, cmd_risk)

        # Check quantity parameters
        for key, value in params.items():
            if isinstance(value, (int, float)):
                if "count" in key.lower() or "limit" in key.lower():
                    if value > 10:
                        risk_score = max(risk_score, 0.4)
                    if value > 50:
                        risk_score = max(risk_score, 0.7)

        return min(1.0, risk_score)

    def _assess_path_risk(self, path: str) -> float:
        """Assess risk of a file path."""
        expanded = os.path.expanduser(path)

        # Check blocked paths
        for blocked in self.config.blocked_paths:
            blocked_expanded = os.path.expanduser(blocked)
            if expanded.startswith(blocked_expanded):
                return 1.0

        # Check safe paths
        for safe in self.config.safe_directories:
            safe_expanded = os.path.expanduser(safe)
            if expanded.startswith(safe_expanded):
                return 0.1

        # Unknown path - moderate risk
        return 0.5

    def _assess_command_risk(self, command: str) -> float:
        """Assess risk of a command."""
        for pattern in self.config.dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return 1.0

        # Check for sudo
        if "sudo" in command.lower():
            return 0.9

        # Check for system paths
        if any(p in command for p in ["/System", "/Library", "/usr", "/bin", "/sbin"]):
            return 0.7

        return 0.2

    async def _check_anomalies(
        self,
        action_type: ActionType,
        params: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Optional[SafetyCheck]:
        """Check for anomalous behavior patterns."""
        now = datetime.now()
        window_start = now - timedelta(minutes=self.config.anomaly_window_minutes)

        # Get recent actions in window
        recent_actions = [
            (ts, at, p) for ts, at, p in self._action_history
            if ts > window_start
        ]

        if len(recent_actions) < 5:
            # Not enough history for anomaly detection
            return None

        # Check for unusual frequency
        actions_per_minute = len(recent_actions) / self.config.anomaly_window_minutes
        if actions_per_minute > self.config.max_actions_per_minute:
            return SafetyCheck(
                check_name="anomaly_rate",
                passed=False,
                severity=CheckSeverity.WARNING,
                message=f"Unusual action rate: {actions_per_minute:.1f}/min exceeds normal",
                details={"rate": actions_per_minute}
            )

        # Check for repetitive patterns
        same_action_count = sum(1 for _, at, _ in recent_actions if at == action_type)
        if same_action_count > 10:
            return SafetyCheck(
                check_name="anomaly_repetition",
                passed=False,
                severity=CheckSeverity.WARNING,
                message=f"Repetitive action pattern detected: {action_type.name} x{same_action_count}",
                details={"count": same_action_count}
            )

        return None

    def _record_action(
        self,
        action_type: ActionType,
        params: Dict[str, Any]
    ) -> None:
        """Record action for anomaly detection."""
        now = datetime.now()
        self._action_history.append((now, action_type, params))

        # Trim old history
        window_start = now - timedelta(minutes=self.config.anomaly_window_minutes * 2)
        self._action_history = [
            (ts, at, p) for ts, at, p in self._action_history
            if ts > window_start
        ]

    # =========================================================================
    # Constraint Management
    # =========================================================================

    def add_constraint(self, constraint: SafetyConstraint) -> None:
        """Add a custom safety constraint."""
        self._constraints.append(constraint)
        logger.debug(f"Added constraint: {constraint.name}")

    def remove_constraint(self, name: str) -> bool:
        """Remove a constraint by name."""
        original_count = len(self._constraints)
        self._constraints = [c for c in self._constraints if c.name != name]
        removed = original_count - len(self._constraints)
        if removed > 0:
            logger.debug(f"Removed constraint: {name}")
        return removed > 0

    def enable_constraint(self, name: str) -> bool:
        """Enable a constraint."""
        for constraint in self._constraints:
            if constraint.name == name:
                constraint.enabled = True
                return True
        return False

    def disable_constraint(self, name: str) -> bool:
        """Disable a constraint."""
        for constraint in self._constraints:
            if constraint.name == name:
                constraint.enabled = False
                return True
        return False

    def list_constraints(self) -> List[SafetyConstraint]:
        """List all constraints."""
        return list(self._constraints)

    def _register_default_constraints(self) -> None:
        """Register default safety constraints."""

        # Path restrictions
        self.add_constraint(SafetyConstraint(
            name="safe_path_only",
            description="Restrict file operations to safe directories",
            constraint_type=ConstraintType.PATH_RESTRICTION,
            severity=CheckSeverity.BLOCK,
            condition=lambda d: self._is_safe_path(d.get("file_path", d.get("path", ""))),
            error_message="File operation restricted to safe directories only",
            applies_to_categories=[ActionCategory.FILE_SYSTEM],
            override_allowed=True
        ))

        self.add_constraint(SafetyConstraint(
            name="blocked_path_check",
            description="Block operations on system paths",
            constraint_type=ConstraintType.PATH_RESTRICTION,
            severity=CheckSeverity.CRITICAL,
            condition=lambda d: not self._is_blocked_path(d.get("file_path", d.get("path", ""))),
            error_message="Operation on system-critical path is blocked",
            applies_to_categories=[ActionCategory.FILE_SYSTEM],
            override_allowed=False
        ))

        # App restrictions
        self.add_constraint(SafetyConstraint(
            name="blocked_app_check",
            description="Block operations on system applications",
            constraint_type=ConstraintType.APP_RESTRICTION,
            severity=CheckSeverity.BLOCK,
            condition=lambda d: d.get("app_name", "") not in self.config.blocked_apps,
            error_message="Operation on system application is blocked",
            applies_to_categories=[ActionCategory.APPLICATION],
            override_allowed=False
        ))

        self.add_constraint(SafetyConstraint(
            name="sensitive_app_warning",
            description="Warn about operations on sensitive applications",
            constraint_type=ConstraintType.APP_RESTRICTION,
            severity=CheckSeverity.WARNING,
            condition=lambda d: d.get("app_name", "") not in self.config.sensitive_apps,
            error_message="Operation involves sensitive application",
            applies_to_categories=[ActionCategory.APPLICATION],
            override_allowed=True
        ))

        # Resource limits
        self.add_constraint(SafetyConstraint(
            name="max_windows_close",
            description="Limit number of windows that can be closed at once",
            constraint_type=ConstraintType.RESOURCE_LIMIT,
            severity=CheckSeverity.BLOCK,
            condition=lambda d: len(d.get("window_ids", [])) <= self.config.max_windows_close,
            error_message=f"Cannot close more than {self.config.max_windows_close} windows at once",
            applies_to=[ActionType.WINDOW_CLOSE, ActionType.WORKSPACE_CLEANUP],
            override_allowed=True
        ))

        self.add_constraint(SafetyConstraint(
            name="max_apps_launch",
            description="Limit number of apps that can be launched at once",
            constraint_type=ConstraintType.RESOURCE_LIMIT,
            severity=CheckSeverity.BLOCK,
            condition=lambda d: len(d.get("apps", d.get("expected_apps", []))) <= self.config.max_apps_launch,
            error_message=f"Cannot launch more than {self.config.max_apps_launch} apps at once",
            applies_to=[ActionType.ROUTINE_EXECUTE, ActionType.WORKFLOW_EXECUTE],
            override_allowed=True
        ))

        self.add_constraint(SafetyConstraint(
            name="max_files_delete",
            description="Limit number of files that can be deleted at once",
            constraint_type=ConstraintType.RESOURCE_LIMIT,
            severity=CheckSeverity.BLOCK,
            condition=lambda d: len(d.get("files", [])) <= self.config.max_files_delete,
            error_message=f"Cannot delete more than {self.config.max_files_delete} files at once",
            applies_to=[ActionType.FILE_DELETE],
            override_allowed=True
        ))

        # Pattern blocks
        self.add_constraint(SafetyConstraint(
            name="dangerous_command_pattern",
            description="Block dangerous command patterns",
            constraint_type=ConstraintType.PATTERN_BLOCK,
            severity=CheckSeverity.CRITICAL,
            condition=lambda d: not self._has_dangerous_pattern(d.get("command", d.get("script", ""))),
            error_message="Command contains dangerous pattern",
            applies_to=[ActionType.CUSTOM_SHELL, ActionType.CUSTOM_SCRIPT],
            override_allowed=False
        ))

        # State requirements
        self.add_constraint(SafetyConstraint(
            name="screen_unlocked_for_input",
            description="Require screen to be unlocked for input actions",
            constraint_type=ConstraintType.STATE_REQUIREMENT,
            severity=CheckSeverity.BLOCK,
            condition=lambda d: not d.get("screen_locked", False),
            error_message="Screen must be unlocked for input actions",
            applies_to_categories=[ActionCategory.HARDWARE],
            override_allowed=False
        ))

        # Unsaved work check
        self.add_constraint(SafetyConstraint(
            name="check_unsaved_work",
            description="Check for unsaved work before closing apps",
            constraint_type=ConstraintType.STATE_REQUIREMENT,
            severity=CheckSeverity.WARNING,
            condition=lambda d: d.get("force", False) or not d.get("may_have_unsaved", True),
            error_message="Application may have unsaved work",
            applies_to=[ActionType.APP_CLOSE, ActionType.WORKSPACE_CLEANUP],
            override_allowed=True
        ))

    def _is_safe_path(self, path: str) -> bool:
        """Check if path is in safe directories."""
        if not path:
            return True

        expanded = os.path.expanduser(path)

        for safe in self.config.safe_directories:
            safe_expanded = os.path.expanduser(safe)
            if expanded.startswith(safe_expanded):
                return True

        return False

    def _is_blocked_path(self, path: str) -> bool:
        """Check if path is blocked."""
        if not path:
            return False

        expanded = os.path.expanduser(path)

        for blocked in self.config.blocked_paths:
            blocked_expanded = os.path.expanduser(blocked)
            if expanded.startswith(blocked_expanded):
                return True

        return False

    def _has_dangerous_pattern(self, text: str) -> bool:
        """Check if text contains dangerous patterns."""
        if not text:
            return False

        for pattern in self.config.dangerous_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True

        return False

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> Dict[str, Any]:
        """Get validator statistics."""
        return {
            "safety_level": self.config.level.value,
            "total_validations": self._validation_count,
            "blocks": self._block_count,
            "warnings": self._warning_count,
            "block_rate": self._block_count / max(1, self._validation_count),
            "warning_rate": self._warning_count / max(1, self._validation_count),
            "active_constraints": len([c for c in self._constraints if c.enabled]),
            "total_constraints": len(self._constraints),
            "action_history_size": len(self._action_history),
        }


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================


_validator_instance: Optional[SafetyValidator] = None
_validator_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_safety_validator() -> SafetyValidator:
    """Get the global safety validator instance."""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = SafetyValidator()
    return _validator_instance


async def start_safety_validator() -> SafetyValidator:
    """Start the global safety validator."""
    async with _validator_lock:
        validator = get_safety_validator()
        if not validator.is_running:
            await validator.start()
        return validator


async def stop_safety_validator() -> None:
    """Stop the global safety validator."""
    async with _validator_lock:
        global _validator_instance
        if _validator_instance and _validator_instance.is_running:
            await _validator_instance.stop()
