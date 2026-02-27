"""
JARVIS Unified Readiness Configuration Module
==============================================

Central configuration for component criticality, lifecycle FSM, and status
display semantics.  **Single source of truth** for all status-related maps.

This module provides:
1. ComponentCriticality enum - CRITICAL, OPTIONAL, UNKNOWN
2. ComponentStatus enum - All possible component lifecycle states (full FSM)
3. STATUS_EMOJI - Canonical emoji per status
4. STATUS_DISPLAY_MAP - 4-char terminal display codes
5. STATUS_RICH_STYLE - Rich markup styles per status
6. STATUS_NORMALIZE - Legacy alias → canonical value mapping
7. VALID_TRANSITIONS - FSM transition legality table
8. COMPONENT_REGISTRY / COMPONENT_GROUPS - Display registry & grouping
9. normalize_status() / validate_transition() - Runtime helpers
10. ReadinessConfig dataclass + get_readiness_config() singleton

CRITICAL FIX: "skipped" displays as "SKIP" (NOT "STOP")

Usage:
    from backend.core.readiness_config import (
        normalize_status, validate_transition,
        STATUS_EMOJI, STATUS_DISPLAY_MAP, STATUS_RICH_STYLE,
        STATUS_SCHEMA_VERSION, ALLOWED_WRITE_SOURCES,
        COMPONENT_REGISTRY, COMPONENT_GROUPS,
        get_display_name, get_component_group,
    )
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

_status_logger = logging.getLogger(__name__)

# =============================================================================
# Schema version — included in events for cross-repo compatibility
# =============================================================================

STATUS_SCHEMA_VERSION = "1.0.0"


# =============================================================================
# Enums
# =============================================================================

class ComponentCriticality(Enum):
    """
    Classification of component importance for system readiness.

    CRITICAL: Component must be healthy for system to be considered ready.
              Failures block readiness certification.

    OPTIONAL: Component enhances functionality but system can operate without.
              Failures do not block readiness certification.

    UNKNOWN:  Component is not recognized. Default handling applies.
    """
    CRITICAL = "critical"
    OPTIONAL = "optional"
    UNKNOWN = "unknown"


class ComponentStatus(Enum):
    """
    Canonical lifecycle states for a component.  Single source of truth.

    FSM transitions are enforced via VALID_TRANSITIONS below.
    """
    PENDING       = "pending"
    INITIALIZING  = "initializing"
    STARTING      = "starting"
    RUNNING       = "running"        # Active, doing work (NOT collapsed to "starting")
    HEALTHY       = "healthy"        # Passed health checks
    READY         = "ready"          # Accepting traffic
    DEGRADED      = "degraded"
    RECOVERING    = "recovering"
    ERROR         = "error"
    STOPPING      = "stopping"       # Teardown in progress
    STOPPED       = "stopped"
    SKIPPED       = "skipped"
    UNAVAILABLE   = "unavailable"
    WARMING_UP    = "warming_up"
    RECYCLING     = "recycling"


_CANONICAL_VALUES: Set[str] = {s.value for s in ComponentStatus}


# =============================================================================
# Status Normalization (legacy aliases → canonical values)
# =============================================================================

# Unknown values → "unavailable" (NOT silent pass-through).
STATUS_NORMALIZE: Dict[str, str] = {
    # Legacy aliases
    "complete": "healthy",
    "completed": "healthy",
    "initialized": "healthy",
    "failed": "error",
    "cancelled": "stopped",
    "started": "starting",
    "shutting_down": "stopping",
    # Identity mappings for all canonical values
    **{s.value: s.value for s in ComponentStatus},
}


# =============================================================================
# FSM Transition Table
# =============================================================================

# Valid transitions: from_state → {allowed next states}
# Unlisted transitions are rejected (warn, but not blocked — defensive).
VALID_TRANSITIONS: Dict[str, Set[str]] = {
    "pending":       {"initializing", "starting", "skipped", "error", "unavailable"},
    "initializing":  {"starting", "running", "healthy", "error", "skipped", "unavailable"},
    "starting":      {"running", "healthy", "ready", "error", "stopped", "unavailable"},
    "running":       {"healthy", "ready", "degraded", "error", "stopping", "recycling"},
    "healthy":       {"ready", "running", "degraded", "error", "stopping", "recycling"},
    "ready":         {"running", "healthy", "degraded", "error", "stopping", "recycling"},
    "degraded":      {"recovering", "healthy", "ready", "error", "stopping"},
    "recovering":    {"healthy", "ready", "degraded", "error", "stopping"},
    "error":         {"recovering", "starting", "initializing", "stopped", "unavailable"},
    "stopping":      {"stopped", "error"},
    "stopped":       {"starting", "initializing", "pending"},
    "skipped":       {"starting", "initializing", "pending"},
    "unavailable":   {"starting", "initializing", "pending", "error"},
    "warming_up":    {"running", "healthy", "ready", "error"},
    "recycling":     {"pending", "initializing", "starting", "error", "stopped"},
}


# =============================================================================
# Display Maps — Emoji, 4-char codes, Rich styles
# =============================================================================

STATUS_EMOJI: Dict[str, str] = {
    "pending":       "\u23f3",       # ⏳
    "initializing":  "\U0001f300",   # 🌀
    "starting":      "\U0001f504",   # 🔄
    "running":       "\u2699\ufe0f ",  # ⚙️
    "healthy":       "\u2705",       # ✅
    "ready":         "\U0001f7e2",   # 🟢
    "degraded":      "\u26a0\ufe0f ",  # ⚠️
    "recovering":    "\U0001fa7a",   # 🩺
    "error":         "\u274c",       # ❌
    "stopping":      "\U0001f319",   # 🌙
    "stopped":       "\u23f9\ufe0f ",  # ⏹️
    "skipped":       "\u23ed\ufe0f ",  # ⏭️
    "unavailable":   "\U0001f6ab",   # 🚫
    "warming_up":    "\U0001f525",   # 🔥
    "recycling":     "\u267b\ufe0f ",  # ♻️
}

# 4-char terminal display codes
STATUS_DISPLAY_MAP: Dict[str, str] = {
    "pending":       "PEND",
    "initializing":  "INIT",
    "starting":      "STAR",
    "running":       "LIVE",
    "healthy":       "GOOD",
    "ready":         "REDY",
    "degraded":      "DEGR",
    "recovering":    "RECV",
    "error":         "FAIL",
    "stopping":      "DOWN",
    "stopped":       "STOP",
    "skipped":       "SKIP",
    "unavailable":   "UNAV",
    "warming_up":    "WARM",
    "recycling":     "RCYL",
}

# Rich markup styles per status
STATUS_RICH_STYLE: Dict[str, str] = {
    "pending":       "dim",
    "initializing":  "bright_cyan",
    "starting":      "bright_cyan",
    "running":       "bold bright_blue",
    "healthy":       "bold green",
    "ready":         "bold bright_green",
    "degraded":      "bold yellow",
    "recovering":    "bold bright_yellow",
    "error":         "bold red",
    "stopping":      "dim yellow",
    "stopped":       "dim red",
    "skipped":       "dim",
    "unavailable":   "dim red",
    "warming_up":    "bold bright_yellow",
    "recycling":     "bold cyan",
}

# Maps internal status to dashboard-friendly status strings (identity for canonical)
# Kept for backward compat with callers that use DASHBOARD_STATUS_MAP
DASHBOARD_STATUS_MAP: Dict[str, str] = {s.value: s.value for s in ComponentStatus}


# =============================================================================
# Write-Source Enforcement
# =============================================================================

# Only these sources may call update_component().  Renderer is READ-ONLY.
ALLOWED_WRITE_SOURCES = frozenset({"supervisor", "prime", "reactor", "system"})


# =============================================================================
# Component Registry & Groups
# =============================================================================

@dataclass(frozen=True)
class ComponentRegistryEntry:
    key: str              # Dashboard key (e.g., "jarvis-body")
    display_name: str     # Human name (e.g., "JARVIS Body")
    group: str            # Functional group (e.g., "trinity")
    criticality: str      # "critical", "important", "optional"


COMPONENT_REGISTRY: Dict[str, ComponentRegistryEntry] = {
    "jarvis-body":          ComponentRegistryEntry("jarvis-body",          "JARVIS Body",       "trinity",      "critical"),
    "jarvis-prime":         ComponentRegistryEntry("jarvis-prime",         "JARVIS Prime",      "trinity",      "important"),
    "reactor-core":         ComponentRegistryEntry("reactor-core",         "Reactor Core",      "trinity",      "important"),
    "gcp-vm":               ComponentRegistryEntry("gcp-vm",               "GCP VM",            "trinity",      "optional"),
    "resources":            ComponentRegistryEntry("resources",            "Resources",         "services",     "critical"),
    "audio_input":          ComponentRegistryEntry("audio_input",          "Audio Input",       "services",     "optional"),
    "startup_gate":         ComponentRegistryEntry("startup_gate",         "Startup Gate",      "services",     "critical"),
    "loading_server":       ComponentRegistryEntry("loading_server",       "Loading Server",    "services",     "critical"),
    "preflight":            ComponentRegistryEntry("preflight",            "Preflight",         "services",     "critical"),
    "ecapa_backend":        ComponentRegistryEntry("ecapa_backend",        "ECAPA Backend",     "services",     "optional"),
    "intelligence":         ComponentRegistryEntry("intelligence",         "Intelligence",      "intelligence", "important"),
    "event_infra":          ComponentRegistryEntry("event_infra",          "Event Infra",       "intelligence", "optional"),
    "conversations":        ComponentRegistryEntry("conversations",        "Conversations",     "intelligence", "optional"),
    "agent_runner":         ComponentRegistryEntry("agent_runner",         "Agent Runner",      "intelligence", "optional"),
    "two_tier_security":    ComponentRegistryEntry("two_tier_security",    "Two-Tier Security", "intelligence", "optional"),
    "trinity":              ComponentRegistryEntry("trinity",              "Trinity Phase",     "system",       "critical"),
    "enterprise_services":  ComponentRegistryEntry("enterprise_services",  "Enterprise Svc",    "system",       "optional"),
    "enterprise_db":        ComponentRegistryEntry("enterprise_db",        "Enterprise DB",     "system",       "optional"),
    "ghost-display":        ComponentRegistryEntry("ghost-display",        "Ghost Display",     "system",       "optional"),
    "agi_os":               ComponentRegistryEntry("agi_os",               "AGI OS",            "system",       "optional"),
    "visual_pipeline":      ComponentRegistryEntry("visual_pipeline",      "Visual Pipeline",   "system",       "optional"),
    "frontend":             ComponentRegistryEntry("frontend",             "Frontend",          "system",       "critical"),
    "browser":              ComponentRegistryEntry("browser",              "Browser",           "system",       "optional"),
}

# Ordered group definitions for display: (key, emoji, label)
COMPONENT_GROUPS: List[Tuple[str, str, str]] = [
    ("trinity",      "\U0001f531", "Trinity"),         # 🔱
    ("services",     "\U0001f9e9", "Services"),        # 🧩
    ("intelligence", "\U0001f9e0", "Intelligence"),    # 🧠
    ("system",       "\U0001f680", "System"),           # 🚀
]


def get_display_name(key: str) -> str:
    """Get human-readable display name for a component key."""
    entry = COMPONENT_REGISTRY.get(key)
    return entry.display_name if entry else key.replace("-", " ").replace("_", " ").title()


def get_component_group(key: str) -> str:
    """Get group name for a component key. Unknown components go to 'system'."""
    entry = COMPONENT_REGISTRY.get(key)
    return entry.group if entry else "system"


# =============================================================================
# Runtime Helpers
# =============================================================================

# Counter for unknown status warnings (lightweight metric)
_status_unknown_total = 0


def normalize_status(raw: str) -> str:
    """Normalize any raw status string to a canonical ComponentStatus value.
    Unknown values -> 'unavailable' + warning log (NOT silent pass-through)."""
    global _status_unknown_total
    canonical = STATUS_NORMALIZE.get(raw)
    if canonical is not None:
        return canonical
    # Unknown status - route to unavailable, log warning
    _status_unknown_total += 1
    _status_logger.warning(
        "Unknown status %r normalized to 'unavailable' (total unknown: %d)",
        raw, _status_unknown_total,
    )
    return "unavailable"


def validate_transition(prev: str, next_status: str) -> Tuple[bool, str]:
    """Validate that a state transition is legal per the FSM.
    Returns (is_valid, reason). Does NOT enforce - caller decides policy."""
    allowed = VALID_TRANSITIONS.get(prev)
    if allowed is None:
        return False, f"Unknown previous state: {prev!r}"
    if next_status in allowed:
        return True, ""
    return False, f"Illegal transition: {prev!r} -> {next_status!r}"


def get_status_unknown_total() -> int:
    """Return count of unknown statuses encountered (for metrics/monitoring)."""
    return _status_unknown_total


# =============================================================================
# Configuration Defaults
# =============================================================================

# Default critical components - must be healthy for system readiness
DEFAULT_CRITICAL_COMPONENTS: FrozenSet[str] = frozenset({
    "backend",
    "loading_server",
    "preflight",
})

# Default optional components - enhance functionality but not required
DEFAULT_OPTIONAL_COMPONENTS: FrozenSet[str] = frozenset({
    "jarvis_prime",
    "reactor_core",
    "enterprise",
    "agi_os",
    "gcp_vm",
})

# Default timeout values
DEFAULT_VERIFICATION_TIMEOUT = 60.0  # seconds
DEFAULT_UNHEALTHY_THRESHOLD_FAILURES = 3  # consecutive failures
DEFAULT_UNHEALTHY_THRESHOLD_SECONDS = 30.0  # seconds
DEFAULT_REVOCATION_COOLDOWN_SECONDS = 5.0  # seconds


# =============================================================================
# ReadinessConfig Dataclass
# =============================================================================

@dataclass(frozen=False)
class ReadinessConfig:
    """
    Central configuration for readiness behavior.

    Provides single source of truth for:
    - Component criticality classification
    - Status display mappings
    - Timeout and threshold values

    Configuration can be overridden via environment variables:
    - JARVIS_VERIFICATION_TIMEOUT: Verification timeout in seconds
    - JARVIS_UNHEALTHY_THRESHOLD_FAILURES: Consecutive failures before unhealthy
    - JARVIS_UNHEALTHY_THRESHOLD_SECONDS: Seconds before unhealthy
    - JARVIS_REVOCATION_COOLDOWN_SECONDS: Cooldown between revocations
    """

    # Component classification
    critical_components: FrozenSet[str] = field(
        default_factory=lambda: DEFAULT_CRITICAL_COMPONENTS
    )
    optional_components: FrozenSet[str] = field(
        default_factory=lambda: DEFAULT_OPTIONAL_COMPONENTS
    )

    # Timeout and threshold values (populated from env vars in __post_init__)
    verification_timeout: float = field(default=DEFAULT_VERIFICATION_TIMEOUT)
    unhealthy_threshold_failures: int = field(default=DEFAULT_UNHEALTHY_THRESHOLD_FAILURES)
    unhealthy_threshold_seconds: float = field(default=DEFAULT_UNHEALTHY_THRESHOLD_SECONDS)
    revocation_cooldown_seconds: float = field(default=DEFAULT_REVOCATION_COOLDOWN_SECONDS)

    def __post_init__(self) -> None:
        """Load configuration from environment variables."""
        # Override with environment variables if set
        if env_timeout := os.environ.get("JARVIS_VERIFICATION_TIMEOUT"):
            object.__setattr__(self, "verification_timeout", float(env_timeout))

        if env_failures := os.environ.get("JARVIS_UNHEALTHY_THRESHOLD_FAILURES"):
            object.__setattr__(self, "unhealthy_threshold_failures", int(env_failures))

        if env_seconds := os.environ.get("JARVIS_UNHEALTHY_THRESHOLD_SECONDS"):
            object.__setattr__(self, "unhealthy_threshold_seconds", float(env_seconds))

        if env_cooldown := os.environ.get("JARVIS_REVOCATION_COOLDOWN_SECONDS"):
            object.__setattr__(self, "revocation_cooldown_seconds", float(env_cooldown))

    def get_criticality(self, component_name: str) -> ComponentCriticality:
        """
        Get the criticality classification for a component.

        Args:
            component_name: Name of the component (case-insensitive)

        Returns:
            ComponentCriticality enum value
        """
        name_lower = component_name.lower()

        # Check if empty
        if not name_lower:
            return ComponentCriticality.UNKNOWN

        # Check critical first
        if name_lower in {c.lower() for c in self.critical_components}:
            return ComponentCriticality.CRITICAL

        # Check optional
        if name_lower in {c.lower() for c in self.optional_components}:
            return ComponentCriticality.OPTIONAL

        return ComponentCriticality.UNKNOWN

    @staticmethod
    def status_to_display(status: str) -> str:
        """
        Convert status string to 4-character display code.

        Args:
            status: Status string (case-insensitive)

        Returns:
            4-character display code, or "????" for unknown status

        Note: CRITICAL - "skipped" returns "SKIP", not "STOP"
        """
        status_lower = status.lower()
        return STATUS_DISPLAY_MAP.get(status_lower, "????")

    @staticmethod
    def status_to_dashboard(status: str) -> str:
        """
        Convert status string to dashboard-friendly status.

        Args:
            status: Status string (case-insensitive)

        Returns:
            Dashboard status string, or "unknown" for unknown status

        Note: CRITICAL - "skipped" returns "skipped", not "stopped"
        """
        status_lower = status.lower()
        return DASHBOARD_STATUS_MAP.get(status_lower, "unknown")


# =============================================================================
# Singleton Access
# =============================================================================

_config_instance: Optional[ReadinessConfig] = None


def get_readiness_config() -> ReadinessConfig:
    """
    Get the singleton ReadinessConfig instance.

    Returns:
        ReadinessConfig instance (singleton)

    Example:
        config = get_readiness_config()
        if config.get_criticality("backend") == ComponentCriticality.CRITICAL:
            # Handle critical component
            pass
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = ReadinessConfig()
    return _config_instance


def _reset_config() -> None:
    """
    Reset the singleton instance (for testing purposes).

    This allows tests to verify that the singleton pattern works correctly
    and to reset state between tests.
    """
    global _config_instance
    _config_instance = None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Enums
    "ComponentCriticality",
    "ComponentStatus",
    # Schema
    "STATUS_SCHEMA_VERSION",
    # Display maps
    "STATUS_EMOJI",
    "STATUS_DISPLAY_MAP",
    "STATUS_RICH_STYLE",
    "DASHBOARD_STATUS_MAP",
    # Normalization & FSM
    "STATUS_NORMALIZE",
    "VALID_TRANSITIONS",
    "normalize_status",
    "validate_transition",
    "get_status_unknown_total",
    # Write enforcement
    "ALLOWED_WRITE_SOURCES",
    # Component registry
    "ComponentRegistryEntry",
    "COMPONENT_REGISTRY",
    "COMPONENT_GROUPS",
    "get_display_name",
    "get_component_group",
    # Configuration
    "DEFAULT_CRITICAL_COMPONENTS",
    "DEFAULT_OPTIONAL_COMPONENTS",
    "ReadinessConfig",
    "get_readiness_config",
    "_reset_config",
]
