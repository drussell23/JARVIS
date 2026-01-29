"""
Intelligent Request Router for JARVIS Hybrid Architecture
Automatically routes requests to optimal backend based on:
- Task capabilities
- Resource requirements
- Backend health and availability
- Historical performance
- Real-time load
- RAM-aware routing with memory pressure detection
- Cost awareness (budget checking before GCP routing)
- Supervisor state (no GCP during updates/maintenance)

v2.0.0 - Supervisor-aware with cost optimization
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Supervisor-aware GCP controller (lazy loaded)
_gcp_controller = None


def _get_gcp_controller():
    """Lazy load the supervisor-aware GCP controller."""
    global _gcp_controller
    if _gcp_controller is None:
        try:
            from core.supervisor_gcp_controller import get_supervisor_gcp_controller
            _gcp_controller = get_supervisor_gcp_controller()
        except ImportError:
            logger.debug("Supervisor GCP controller not available")
    return _gcp_controller

# Import RAM monitor (lazy to avoid circular imports)
_ram_monitor = None


def _dummy_monitor(config=None):
    """Dummy monitor when RAM monitor unavailable"""
    return None


def _get_ram_monitor():
    """Lazy load RAM monitor"""
    global _ram_monitor
    if _ram_monitor is None:
        try:
            from backend.core.advanced_ram_monitor import get_ram_monitor

            _ram_monitor = get_ram_monitor
        except ImportError:
            logger.warning("RAM monitor not available")
            _ram_monitor = _dummy_monitor
    return _ram_monitor


class RouteDecision(Enum):
    """Routing decision"""

    LOCAL = "local"
    CLOUD = "cloud"
    AUTO = "auto"
    NONE = "none"


@dataclass
class RoutingContext:
    """Context for routing decision"""

    command: str
    command_type: Optional[str] = None
    memory_required: Optional[str] = None
    keywords: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class HybridRouter:
    """
    Intelligent router that decides where to execute requests
    Zero hardcoding - all rules from configuration
    """

    def __init__(self, config: Dict):
        self.config = config or {}

        # v117.0: Defensive config access - prevents KeyError: 'routing' in background threads
        # This handles malformed or incomplete config during parallel initialization
        hybrid_config = self.config.get("hybrid", {}) or {}
        self.routing_config = hybrid_config.get("routing", {}) or {}
        self.rules = self.routing_config.get("rules", []) or []
        self.strategy = self.routing_config.get("strategy", "capability_based")

        # Compile regex patterns for performance
        self._compiled_patterns = {}
        self._compile_patterns()

        # Performance tracking
        self.routing_history = []
        self.max_history = 1000

        # ============== GCP Activity Tracking (Phase 2.5) ==============
        # Track last activity time for each backend
        self._backend_activity = {}
        self._activity_threshold_minutes = 10  # Config-driven

        # Backend capabilities cache (from discovery)
        self._backend_capabilities = {}
        self._capabilities_cache_ttl = 60  # seconds
        self._capabilities_last_updated = {}
        # ================================================================

        # ============== Supervisor-Aware Cost Optimization (v2.0) ==============
        # Track cost-based routing decisions
        self._cost_denied_count = 0
        self._supervisor_denied_count = 0
        self._fallback_to_local_count = 0
        # ======================================================================

        logger.info(f"🎯 HybridRouter initialized with {len(self.rules)} rules")

    def _compile_patterns(self):
        """Pre-compile regex patterns for efficiency"""
        for rule in self.rules:
            if "match" in rule and "pattern" in rule["match"]:
                pattern = rule["match"]["pattern"]
                self._compiled_patterns[rule["name"]] = re.compile(pattern, re.IGNORECASE)

    def route(self, context: RoutingContext) -> Tuple[RouteDecision, Optional[str], Dict[str, Any]]:
        """
        Determine optimal backend for request

        Returns:
            (decision, backend_name, metadata)
        """
        # Extract keywords from command if not provided
        if context.keywords is None:
            context.keywords = self._extract_keywords(context.command)

        # Apply routing rules in order
        for rule in self.rules:
            if self._matches_rule(rule, context):
                decision = self._make_decision(rule, context)
                metadata = {
                    "rule": rule["name"],
                    "strategy": self.strategy,
                    "confidence": self._calculate_confidence(rule, context),
                }

                # Track routing decision
                self._track_decision(context, decision, metadata)

                logger.debug(
                    f"Routed '{context.command}' via rule '{rule['name']}' → {decision.value}"
                )
                return decision, rule.get("route_to"), metadata

        # No rule matched - use default strategy
        decision = RouteDecision.AUTO
        metadata = {"rule": "default", "strategy": self.strategy, "confidence": 0.5}
        return decision, None, metadata

    def _matches_rule(self, rule: Dict, context: RoutingContext) -> bool:
        """Check if context matches routing rule"""
        match_config = rule.get("match", {})

        # Check 'all' wildcard
        if match_config.get("all"):
            return True

        # ============== CAPABILITIES MATCHING (Phase 2.5) ==============
        # Check capabilities - match against available backends
        if "capabilities" in match_config:
            required_capabilities = match_config["capabilities"]
            if not self._backend_has_capabilities(required_capabilities, context):
                logger.debug(
                    f"Rule '{rule['name']}' skipped: No backend with capabilities {required_capabilities}"
                )
                return False

        # ============== NEW: RAM-Aware Routing (Phase 2) ==============
        # Check memory pressure conditions
        if "memory_pressure" in match_config:
            required_pressure = match_config["memory_pressure"]
            current_pressure = self._get_current_memory_pressure()

            if not self._matches_memory_pressure(current_pressure, required_pressure):
                return False

        # ============== GCP IDLE TIME TRACKING (Phase 2.5) ==============
        # Check GCP idle time (for cost optimization)
        if "gcp_idle_minutes" in match_config:
            required_idle_str = match_config["gcp_idle_minutes"]
            gcp_idle_minutes = self._get_backend_idle_minutes("gcp")

            if not self._matches_idle_time(gcp_idle_minutes, required_idle_str):
                logger.debug(
                    f"Rule '{rule['name']}' skipped: GCP idle {gcp_idle_minutes:.1f}min "
                    f"doesn't match requirement '{required_idle_str}'"
                )
                return False

            logger.info(
                f"✅ Cost optimization: GCP idle for {gcp_idle_minutes:.1f}min (threshold: {required_idle_str})"
            )
        # ===============================================================

        # Check memory requirements
        if "memory_required" in match_config:
            required_mem = match_config["memory_required"]
            if context.memory_required:
                if not self._matches_memory_requirement(context.memory_required, required_mem):
                    return False
            else:
                # Estimate memory from command
                estimated_mem = self._estimate_memory(context.command)
                if not self._matches_memory_requirement(estimated_mem, required_mem):
                    return False

        # Check keywords
        if "keywords" in match_config and context.keywords:
            required_keywords = match_config["keywords"]
            command_lower = context.command.lower()
            if not any(kw.lower() in command_lower for kw in required_keywords):
                return False

        # Check command type
        if "command_type" in match_config:
            if context.command_type != match_config["command_type"]:
                return False

        # Check regex pattern
        if rule["name"] in self._compiled_patterns:
            pattern = self._compiled_patterns[rule["name"]]
            if not pattern.search(context.command):
                return False

        # Check custom metadata
        if "metadata" in match_config and context.metadata:
            for key, value in match_config["metadata"].items():
                if context.metadata.get(key) != value:
                    return False

        return True

    def _get_current_memory_pressure(self) -> float:
        """Get current memory pressure as percentage"""
        try:
            ram_monitor_factory = _get_ram_monitor()
            if ram_monitor_factory:
                ram_monitor = ram_monitor_factory(self.config)
                return ram_monitor.get_current_pressure_percent()
        except Exception as e:
            logger.debug(f"Could not get RAM pressure: {e}")

        return 0.0  # Safe default

    def _matches_memory_pressure(self, current: float, required: str) -> bool:
        """Check if memory pressure condition is met"""
        # Parse conditions like ">70", "<40", ">=85"
        if required.startswith(">="):
            threshold = float(required[2:])
            return current >= threshold
        elif required.startswith("<="):
            threshold = float(required[2:])
            return current <= threshold
        elif required.startswith(">"):
            threshold = float(required[1:])
            return current > threshold
        elif required.startswith("<"):
            threshold = float(required[1:])
            return current < threshold
        else:
            # Exact match
            try:
                threshold = float(required)
                return abs(current - threshold) < 5  # Within 5% tolerance
            except ValueError:
                return False

    def _matches_memory_requirement(self, actual: str, required: str) -> bool:
        """Check if memory requirement is met"""
        # Parse memory strings like "8GB", ">8GB", "<4GB"
        actual_gb = self._parse_memory(actual)
        required_gb = self._parse_memory(required)

        if required.startswith(">"):
            return actual_gb > required_gb
        elif required.startswith("<"):
            return actual_gb < required_gb
        elif required.startswith(">="):
            return actual_gb >= required_gb
        elif required.startswith("<="):
            return actual_gb <= required_gb
        else:
            return actual_gb >= required_gb

    def _parse_memory(self, mem_str: str) -> float:
        """Parse memory string to GB value"""
        # Remove comparison operators
        mem_str = re.sub(r"^[><=]+", "", mem_str).strip()

        # Extract number and unit
        match = re.match(r"([\d.]+)\s*([KMGT]?B)?", mem_str, re.IGNORECASE)
        if not match:
            return 0.0

        value = float(match.group(1))
        unit = (match.group(2) or "GB").upper()

        # Convert to GB
        multipliers = {"B": 1 / (1024**3), "KB": 1 / (1024**2), "MB": 1 / 1024, "GB": 1, "TB": 1024}

        return value * multipliers.get(unit, 1)

    def _estimate_memory(self, command: str) -> str:
        """Estimate memory requirements from command"""
        command_lower = command.lower()

        # ML/AI tasks
        ml_keywords = ["train", "model", "neural", "deep learning", "llm", "gpt", "analyze large"]
        if any(kw in command_lower for kw in ml_keywords):
            return ">8GB"

        # Vision/image processing
        vision_keywords = ["image", "video", "screenshot", "vision", "ocr"]
        if any(kw in command_lower for kw in vision_keywords):
            return ">2GB"

        # Light tasks
        return "<1GB"

    def _extract_keywords(self, command: str) -> List[str]:
        """Extract meaningful keywords from command"""
        # Remove common stop words
        stop_words = {
            "the",
            "a",
            "an",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "can",
        }

        words = command.lower().split()
        keywords = [w for w in words if w not in stop_words and len(w) > 2]

        return keywords

    def _make_decision(self, rule: Dict, context: RoutingContext) -> RouteDecision:
        """
        Make routing decision based on rule with cost and supervisor awareness.
        
        v2.0.0: Added checks for:
        - Budget availability before routing to GCP
        - Supervisor state (no GCP during updates/maintenance)
        - Fallback to local when GCP is denied
        """
        route_to = rule.get("route_to", "auto")

        if route_to == "auto":
            return RouteDecision.AUTO
        elif route_to in ["local"]:
            return RouteDecision.LOCAL
        elif route_to in ["gcp", "cloud"]:
            # ============== COST/SUPERVISOR CHECK (v2.0) ==============
            # Before routing to GCP, check if we should/can
            can_use_gcp, reason = self._can_route_to_gcp()
            if not can_use_gcp:
                logger.info(f"💰 GCP routing denied → falling back to local: {reason}")
                self._fallback_to_local_count += 1
                return RouteDecision.LOCAL
            # ============================================================
            return RouteDecision.CLOUD
        elif route_to == "none":
            return RouteDecision.NONE
        else:
            return RouteDecision.AUTO
    
    def _can_route_to_gcp(self) -> Tuple[bool, str]:
        """
        Check if we can route to GCP based on cost and supervisor state.
        
        Returns:
            Tuple of (can_route, reason)
        """
        controller = _get_gcp_controller()
        if controller is None:
            # Controller not available - default to allowing GCP
            return True, "No controller"
        
        # Check 1: Supervisor in maintenance?
        if controller.is_maintenance_mode():
            self._supervisor_denied_count += 1
            return False, f"Supervisor in maintenance ({controller._supervisor_state.value})"
        
        # Check 2: Budget check (conservative - assume 0.5 hour usage)
        can_afford, budget_reason = controller.can_afford_vm(0.5, is_emergency=False)
        if not can_afford:
            self._cost_denied_count += 1
            return False, budget_reason
        
        return True, "OK"
    
    def get_cost_routing_stats(self) -> Dict[str, Any]:
        """Get cost-aware routing statistics."""
        return {
            "cost_denied_count": self._cost_denied_count,
            "supervisor_denied_count": self._supervisor_denied_count,
            "fallback_to_local_count": self._fallback_to_local_count,
        }

    def _calculate_confidence(self, rule: Dict, context: RoutingContext) -> float:
        """Calculate confidence score for routing decision"""
        confidence = 0.5  # Base confidence

        # Higher confidence for specific rules
        match_config = rule.get("match", {})

        if "command_type" in match_config:
            confidence += 0.2

        if "capabilities" in match_config:
            confidence += 0.15

        if "keywords" in match_config:
            # More keywords matched = higher confidence
            required_keywords = match_config["keywords"]
            command_lower = context.command.lower()
            matched = sum(1 for kw in required_keywords if kw.lower() in command_lower)
            confidence += 0.15 * (matched / len(required_keywords))

        if "memory_required" in match_config:
            confidence += 0.1

        return min(confidence, 1.0)

    def _track_decision(self, context: RoutingContext, decision: RouteDecision, metadata: Dict):
        """Track routing decision for analytics"""
        self.routing_history.append(
            {"command": context.command, "decision": decision.value, "metadata": metadata}
        )

        # Limit history size
        if len(self.routing_history) > self.max_history:
            self.routing_history = self.routing_history[-self.max_history :]

    def get_analytics(self) -> Dict[str, Any]:
        """Get routing analytics"""
        if not self.routing_history:
            return {"total": 0}

        total = len(self.routing_history)
        local_count = sum(1 for h in self.routing_history if h["decision"] == "local")
        cloud_count = sum(1 for h in self.routing_history if h["decision"] == "cloud")
        auto_count = sum(1 for h in self.routing_history if h["decision"] == "auto")

        # Rule usage stats
        rule_usage = {}
        for entry in self.routing_history:
            rule = entry["metadata"].get("rule", "unknown")
            rule_usage[rule] = rule_usage.get(rule, 0) + 1

        return {
            "total": total,
            "local": local_count,
            "cloud": cloud_count,
            "auto": auto_count,
            "local_pct": (local_count / total) * 100 if total > 0 else 0,
            "cloud_pct": (cloud_count / total) * 100 if total > 0 else 0,
            "rule_usage": rule_usage,
            "avg_confidence": (
                sum(h["metadata"].get("confidence", 0) for h in self.routing_history) / total
                if total > 0
                else 0
            ),
            "backend_activity": self._get_activity_summary(),
            # v2.0: Cost-aware routing stats
            "cost_routing": self.get_cost_routing_stats(),
        }

    # ============== GCP IDLE TIME TRACKING IMPLEMENTATION (Phase 2.5) ==============

    def record_backend_activity(self, backend_name: str):
        """
        Record activity for a backend (called after successful execution)

        Args:
            backend_name: Name of backend ("local", "gcp", etc.)
        """
        import time

        self._backend_activity[backend_name] = time.time()
        logger.debug(f"📝 Recorded activity for backend '{backend_name}'")

    def _get_backend_idle_minutes(self, backend_name: str) -> float:
        """
        Get idle time in minutes for a backend

        Args:
            backend_name: Name of backend

        Returns:
            Minutes since last activity (0.0 if never used)
        """
        import time

        if backend_name not in self._backend_activity:
            # Never been used - return 0 idle time
            return 0.0

        last_activity = self._backend_activity[backend_name]
        idle_seconds = time.time() - last_activity
        idle_minutes = idle_seconds / 60.0

        return idle_minutes

    def _matches_idle_time(self, actual_minutes: float, required: str) -> bool:
        """
        Check if idle time matches requirement

        Args:
            actual_minutes: Actual idle time in minutes
            required: Requirement string like ">10", "<5", ">=15"

        Returns:
            True if matches, False otherwise
        """
        # Parse conditions like ">10", "<5", ">=15"
        if required.startswith(">="):
            threshold = float(required[2:])
            return actual_minutes >= threshold
        elif required.startswith("<="):
            threshold = float(required[2:])
            return actual_minutes <= threshold
        elif required.startswith(">"):
            threshold = float(required[1:])
            return actual_minutes > threshold
        elif required.startswith("<"):
            threshold = float(required[1:])
            return actual_minutes < threshold
        else:
            # Exact match with tolerance
            try:
                threshold = float(required)
                return abs(actual_minutes - threshold) < 1.0  # Within 1 minute
            except ValueError:
                return False

    def _get_activity_summary(self) -> Dict[str, Any]:
        """Get summary of backend activity for analytics"""
        import time

        summary = {}
        for backend_name, last_activity_time in self._backend_activity.items():
            idle_minutes = (time.time() - last_activity_time) / 60.0
            summary[backend_name] = {
                "last_activity": last_activity_time,
                "idle_minutes": round(idle_minutes, 2),
                "is_idle": idle_minutes > self._activity_threshold_minutes,
            }

        return summary

    # ============== CAPABILITIES MATCHING IMPLEMENTATION (Phase 2.5) ==============

    def register_backend_capabilities(self, backend_name: str, capabilities: List[str]):
        """
        Register capabilities for a backend (from service discovery)

        Args:
            backend_name: Name of backend
            capabilities: List of capability strings
        """
        import time

        self._backend_capabilities[backend_name] = capabilities
        self._capabilities_last_updated[backend_name] = time.time()
        logger.info(f"✅ Registered {len(capabilities)} capabilities for '{backend_name}'")

    def _backend_has_capabilities(
        self, required_capabilities: List[str], context: RoutingContext
    ) -> bool:
        """
        Check if any backend has the required capabilities

        Args:
            required_capabilities: List of required capability strings
            context: Routing context (for potential backend hints)

        Returns:
            True if at least one backend has all required capabilities
        """
        # If no backends registered yet, check config
        if not self._backend_capabilities:
            self._load_capabilities_from_config()

        # Check if any backend has ALL required capabilities
        for backend_name, backend_caps in self._backend_capabilities.items():
            if all(cap in backend_caps for cap in required_capabilities):
                logger.debug(
                    f"✅ Backend '{backend_name}' has required capabilities: {required_capabilities}"
                )
                return True

        logger.warning(
            f"⚠️  No backend has all required capabilities: {required_capabilities}. "
            f"Available backends: {list(self._backend_capabilities.keys())}"
        )
        return False

    def _load_capabilities_from_config(self):
        """Load backend capabilities from config (fallback)"""
        backends_config = self.config.get("hybrid", {}).get("backends", {})

        for backend_name, backend_config in backends_config.items():
            if backend_config.get("enabled", True):
                capabilities = backend_config.get("capabilities", [])
                self.register_backend_capabilities(backend_name, capabilities)

    def get_backends_for_capability(self, capability: str) -> List[str]:
        """
        Get list of backends that support a capability

        Args:
            capability: Capability string (e.g., "vision_capture")

        Returns:
            List of backend names
        """
        if not self._backend_capabilities:
            self._load_capabilities_from_config()

        matching_backends = [
            backend_name
            for backend_name, caps in self._backend_capabilities.items()
            if capability in caps
        ]

        return matching_backends

    def is_capability_cache_stale(self, backend_name: str) -> bool:
        """Check if capability cache for a backend is stale"""
        import time

        if backend_name not in self._capabilities_last_updated:
            return True

        age = time.time() - self._capabilities_last_updated[backend_name]
        return age > self._capabilities_cache_ttl
