"""SecurityPolicyEngine — enterprise security policy enforcement.

Extracted from unified_supervisor.py (lines 41576-41986).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import deque
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

class SecurityPolicyViolation(Exception):
    """Exception raised when a security policy is violated."""

    def __init__(
        self,
        policy_id: str,
        message: str,
        severity: str = "high",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.policy_id = policy_id
        self.severity = severity
        self.details = details or {}
        self.timestamp = datetime.now()


@dataclass
class SecurityPolicy:
    """
    Defines a security policy with rules and enforcement actions.

    Attributes:
        policy_id: Unique identifier for the policy
        name: Human-readable policy name
        description: Detailed description of what the policy enforces
        rules: List of rules that must be satisfied
        enforcement_action: Action to take on violation (block, warn, log)
        enabled: Whether the policy is active
        priority: Priority for evaluation order (higher = evaluated first)
        exceptions: Patterns or contexts that are exempt from this policy
    """
    policy_id: str
    name: str
    description: str
    rules: List[Dict[str, Any]]
    enforcement_action: str = "block"  # block, warn, log
    enabled: bool = True
    priority: int = 100
    exceptions: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyEvaluationResult:
    """Result of evaluating a security policy."""
    policy_id: str
    passed: bool
    violations: List[str]
    enforcement_action: str
    evaluated_at: datetime = field(default_factory=datetime.now)
    context: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SecurityPolicyEngine
# ---------------------------------------------------------------------------

class SecurityPolicyEngine(SystemService):
    """
    Enterprise security policy enforcement engine.

    Evaluates requests and operations against configurable security policies
    with support for rule-based evaluation, exception handling, and
    multiple enforcement actions.

    Features:
    - Rule-based policy evaluation with boolean expressions
    - Policy prioritization for conflict resolution
    - Exception patterns for legitimate bypasses
    - Audit logging of all evaluations
    - Real-time policy updates without restart
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._policies: Dict[str, SecurityPolicy] = {}
        self._evaluation_cache: Dict[str, PolicyEvaluationResult] = {}
        self._cache_ttl_seconds: float = 60.0
        self._evaluation_history: deque = deque(maxlen=10000)
        self._violation_handlers: Dict[str, Callable] = {}
        self._metrics: Dict[str, int] = {
            "evaluations": 0,
            "violations": 0,
            "blocks": 0,
            "warnings": 0,
            "cache_hits": 0,
        }
        self._logger = logging.getLogger("SecurityPolicyEngine")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the security policy engine with default policies."""
        try:
            async with self._lock:
                # Register default security policies
                await self._register_default_policies()
                self._initialized = True
                self._logger.info("Security policy engine initialized")
                return True
        except Exception as e:
            self._logger.error(f"Failed to initialize security policy engine: {e}")
            return False

    async def _register_default_policies(self) -> None:
        """Register default security policies."""
        # Policy: Prevent unauthorized file access
        self.register_policy(SecurityPolicy(
            policy_id="file_access_control",
            name="File Access Control",
            description="Restrict access to sensitive file paths",
            rules=[
                {"type": "path_pattern", "pattern": "/etc/passwd", "action": "deny"},
                {"type": "path_pattern", "pattern": "/etc/shadow", "action": "deny"},
                {"type": "path_pattern", "pattern": "**/.env*", "action": "warn"},
                {"type": "path_pattern", "pattern": "**/credentials*", "action": "warn"},
                {"type": "path_pattern", "pattern": "**/secrets*", "action": "warn"},
            ],
            enforcement_action="block",
            priority=1000
        ))

        # Policy: Rate limiting
        self.register_policy(SecurityPolicy(
            policy_id="rate_limiting",
            name="Rate Limiting",
            description="Prevent excessive requests from single source",
            rules=[
                {"type": "rate_limit", "requests_per_minute": 1000, "per": "ip"},
                {"type": "rate_limit", "requests_per_minute": 100, "per": "user"},
            ],
            enforcement_action="block",
            priority=900
        ))

        # Policy: Input validation
        self.register_policy(SecurityPolicy(
            policy_id="input_validation",
            name="Input Validation",
            description="Validate and sanitize input data",
            rules=[
                {"type": "max_length", "field": "*", "max": 1000000},
                {"type": "forbidden_patterns", "patterns": ["<script>", "javascript:"]},
                {"type": "sql_injection_check", "enabled": True},
                {"type": "command_injection_check", "enabled": True},
            ],
            enforcement_action="block",
            priority=800
        ))

        # Policy: Authentication requirements
        self.register_policy(SecurityPolicy(
            policy_id="authentication_required",
            name="Authentication Required",
            description="Require authentication for sensitive operations",
            rules=[
                {"type": "require_auth", "operations": ["write", "delete", "admin"]},
            ],
            enforcement_action="block",
            priority=700,
            exceptions=[
                {"type": "path", "pattern": "/health*"},
                {"type": "path", "pattern": "/public/*"},
            ]
        ))

    def register_policy(self, policy: SecurityPolicy) -> bool:
        """Register a new security policy."""
        try:
            self._policies[policy.policy_id] = policy
            self._logger.debug(f"Registered policy: {policy.name} ({policy.policy_id})")
            return True
        except Exception as e:
            self._logger.error(f"Failed to register policy {policy.policy_id}: {e}")
            return False

    def unregister_policy(self, policy_id: str) -> bool:
        """Unregister a security policy."""
        if policy_id in self._policies:
            del self._policies[policy_id]
            self._logger.info(f"Unregistered policy: {policy_id}")
            return True
        return False

    async def evaluate(
        self,
        context: Dict[str, Any],
        operation: str = "unknown",
    ) -> Tuple[bool, List[PolicyEvaluationResult]]:
        """
        Evaluate all applicable policies for a given context.

        Args:
            context: Dictionary containing request/operation context
            operation: Type of operation being performed

        Returns:
            Tuple of (allowed, list of evaluation results)
        """
        self._metrics["evaluations"] += 1

        # Check cache
        cache_key = self._generate_cache_key(context, operation)
        if cache_key in self._evaluation_cache:
            cached = self._evaluation_cache[cache_key]
            cache_age = (datetime.now() - cached.evaluated_at).total_seconds()
            if cache_age < self._cache_ttl_seconds:
                self._metrics["cache_hits"] += 1
                return cached.passed, [cached]

        results: List[PolicyEvaluationResult] = []
        allowed = True

        # Sort policies by priority (highest first)
        sorted_policies = sorted(
            [p for p in self._policies.values() if p.enabled],
            key=lambda p: p.priority,
            reverse=True,
        )

        for policy in sorted_policies:
            try:
                # Check if context matches any exception
                if self._matches_exception(context, policy.exceptions):
                    continue

                # Evaluate policy rules
                result = await self._evaluate_policy(policy, context, operation)
                results.append(result)

                if not result.passed:
                    self._metrics["violations"] += 1

                    if policy.enforcement_action == "block":
                        self._metrics["blocks"] += 1
                        allowed = False

                        # Trigger violation handler if registered
                        if policy.policy_id in self._violation_handlers:
                            try:
                                await self._violation_handlers[policy.policy_id](result)
                            except Exception as e:
                                self._logger.error(f"Violation handler error: {e}")

                    elif policy.enforcement_action == "warn":
                        self._metrics["warnings"] += 1
                        self._logger.warning(
                            f"Policy warning: {policy.name} - {result.violations}"
                        )

            except Exception as e:
                self._logger.error(f"Error evaluating policy {policy.policy_id}: {e}")

        # Cache result
        if results:
            combined_result = PolicyEvaluationResult(
                policy_id="combined",
                passed=allowed,
                violations=[v for r in results for v in r.violations],
                enforcement_action="block" if not allowed else "allow",
            )
            self._evaluation_cache[cache_key] = combined_result

        # Record in history
        self._evaluation_history.append({
            "timestamp": datetime.now(),
            "context": context,
            "operation": operation,
            "allowed": allowed,
            "results": [r.policy_id for r in results if not r.passed],
        })

        return allowed, results

    async def _evaluate_policy(
        self,
        policy: SecurityPolicy,
        context: Dict[str, Any],
        operation: str,
    ) -> PolicyEvaluationResult:
        """Evaluate a single policy against context."""
        violations: List[str] = []

        for rule in policy.rules:
            rule_type = rule.get("type", "unknown")

            if rule_type == "path_pattern":
                if "path" in context:
                    pattern = rule.get("pattern", "")
                    if self._path_matches_pattern(context["path"], pattern):
                        action = rule.get("action", "deny")
                        if action == "deny":
                            violations.append(f"Path '{context['path']}' matches blocked pattern '{pattern}'")

            elif rule_type == "rate_limit":
                # Rate limiting would check against a rate limiter
                # This is a simplified check
                pass

            elif rule_type == "max_length":
                field = rule.get("field", "*")
                max_len = rule.get("max", 1000000)
                for key, value in context.items():
                    if field == "*" or key == field:
                        if isinstance(value, str) and len(value) > max_len:
                            violations.append(f"Field '{key}' exceeds max length {max_len}")

            elif rule_type == "forbidden_patterns":
                patterns = rule.get("patterns", [])
                for key, value in context.items():
                    if isinstance(value, str):
                        for pattern in patterns:
                            if pattern.lower() in value.lower():
                                violations.append(f"Forbidden pattern '{pattern}' found in '{key}'")

            elif rule_type == "sql_injection_check":
                if rule.get("enabled", False):
                    for key, value in context.items():
                        if isinstance(value, str):
                            if self._detect_sql_injection(value):
                                violations.append(f"Potential SQL injection in '{key}'")

            elif rule_type == "command_injection_check":
                if rule.get("enabled", False):
                    for key, value in context.items():
                        if isinstance(value, str):
                            if self._detect_command_injection(value):
                                violations.append(f"Potential command injection in '{key}'")

            elif rule_type == "require_auth":
                operations_requiring_auth = rule.get("operations", [])
                if operation in operations_requiring_auth:
                    if not context.get("authenticated", False):
                        violations.append(f"Operation '{operation}' requires authentication")

        return PolicyEvaluationResult(
            policy_id=policy.policy_id,
            passed=len(violations) == 0,
            violations=violations,
            enforcement_action=policy.enforcement_action,
            context=context,
        )

    def _path_matches_pattern(self, path: str, pattern: str) -> bool:
        """Check if path matches a glob pattern."""
        import fnmatch
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"**/{pattern}")

    def _detect_sql_injection(self, value: str) -> bool:
        """Basic SQL injection detection."""
        suspicious_patterns = [
            r"'\s*or\s+'1'\s*=\s*'1",
            r";\s*drop\s+table",
            r";\s*delete\s+from",
            r"union\s+select",
            r"--\s*$",
        ]
        value_lower = value.lower()
        for pattern in suspicious_patterns:
            if re.search(pattern, value_lower):
                return True
        return False

    def _detect_command_injection(self, value: str) -> bool:
        """Basic command injection detection."""
        suspicious_patterns = [
            r";\s*rm\s+-rf",
            r"\|\s*sh",
            r"&&\s*cat\s+/etc",
            r"`.*`",
            r"\$\(.*\)",
        ]
        for pattern in suspicious_patterns:
            if re.search(pattern, value):
                return True
        return False

    def _matches_exception(
        self,
        context: Dict[str, Any],
        exceptions: List[Dict[str, Any]],
    ) -> bool:
        """Check if context matches any exception pattern."""
        for exception in exceptions:
            exc_type = exception.get("type", "unknown")
            if exc_type == "path":
                pattern = exception.get("pattern", "")
                if "path" in context:
                    if self._path_matches_pattern(context["path"], pattern):
                        return True
            elif exc_type == "user":
                users = exception.get("users", [])
                if context.get("user") in users:
                    return True
            elif exc_type == "role":
                roles = exception.get("roles", [])
                if context.get("role") in roles:
                    return True
        return False

    def _generate_cache_key(self, context: Dict[str, Any], operation: str) -> str:
        """Generate a cache key for evaluation result."""
        key_parts = [operation]
        for k, v in sorted(context.items()):
            key_parts.append(f"{k}={v}")
        key_string = "|".join(key_parts)
        return hashlib.md5(key_string.encode()).hexdigest()

    def register_violation_handler(
        self,
        policy_id: str,
        handler: Callable[[PolicyEvaluationResult], Awaitable[None]],
    ) -> None:
        """Register a callback for policy violations."""
        self._violation_handlers[policy_id] = handler

    def get_metrics(self) -> Dict[str, Any]:
        """Get policy engine metrics."""
        return {
            **self._metrics,
            "policies_registered": len(self._policies),
            "cache_size": len(self._evaluation_cache),
            "history_size": len(self._evaluation_history),
        }

    def get_all_policies(self) -> List[SecurityPolicy]:
        """Get all registered policies."""
        return list(self._policies.values())

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        evals = self._metrics.get("evaluations", 0)
        violations = self._metrics.get("violations", 0)
        return (True, f"SecurityPolicyEngine: {evals} evaluations, {violations} violations")

    async def cleanup(self) -> None:
        self._evaluation_cache.clear()

    async def start(self) -> bool:
        if not self._initialized:
            await self.initialize()
        return True

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._initialized,
            message=f"SecurityPolicyEngine: initialized={self._initialized}, policies={len(self._policies)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="SecurityPolicyEngine",
            version="1.0.0",
            inputs=["agent.action", "ipc.command"],
            outputs=["security.violation", "security.allow"],
            side_effects=["writes_security_audit"],
        )

    def activation_triggers(self) -> List[str]:
        return []  # always_on
