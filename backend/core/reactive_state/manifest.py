"""Declarative manifest tying ownership rules, key schemas, and consistency groups.

This module is the single source of truth for the reactive state store's
configuration.  It declares:

* **OWNERSHIP_RULES** -- which writer domain owns which key prefix.
* **KEY_SCHEMAS** -- per-key type, constraints, and defaults.
* **CONSISTENCY_GROUPS** -- sets of keys that should be updated atomically.

Builder functions produce ready-to-use registries from these declarations.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only (plus sibling modules).
* All declaration tuples are module-level constants (immutable).
* ``ConsistencyGroup`` is ``@dataclass(frozen=True)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from backend.core.reactive_state.ownership import OwnershipRegistry, OwnershipRule
from backend.core.reactive_state.schemas import KeySchema, SchemaRegistry


# ---------------------------------------------------------------------------
# Shared enum value tuples
# ---------------------------------------------------------------------------

_MODE_ENUM_VALUES: Tuple[str, ...] = (
    "local_full",
    "local_optimized",
    "sequential",
    "cloud_first",
    "cloud_only",
    "minimal",
)

_MEMORY_TIER_ENUM_VALUES: Tuple[str, ...] = (
    "abundant",
    "optimal",
    "elevated",
    "constrained",
    "critical",
    "emergency",
    "unknown",
)


# ---------------------------------------------------------------------------
# OWNERSHIP_RULES
# ---------------------------------------------------------------------------

OWNERSHIP_RULES: Tuple[OwnershipRule, ...] = (
    OwnershipRule("lifecycle.", "supervisor", "Startup lifecycle state"),
    OwnershipRule("memory.", "memory_assessor", "Memory assessment and admission state"),
    OwnershipRule("gcp.", "gcp_controller", "GCP VM and offload state"),
    OwnershipRule("hollow.", "gcp_controller", "Hollow client mode state"),
    OwnershipRule("prime.", "supervisor", "Prime process management state"),
    OwnershipRule("service.", "supervisor", "Service tier enablement state"),
    OwnershipRule("port.", "supervisor", "Port allocation state"),
)


# ---------------------------------------------------------------------------
# KEY_SCHEMAS
# ---------------------------------------------------------------------------

KEY_SCHEMAS: Tuple[KeySchema, ...] = (
    # -- lifecycle --
    KeySchema(
        key="lifecycle.effective_mode",
        value_type="enum",
        nullable=False,
        default="local_full",
        description="Resolved startup mode governing resource allocation strategy.",
        enum_values=_MODE_ENUM_VALUES,
        unknown_enum_policy="default_with_violation",
    ),
    KeySchema(
        key="lifecycle.startup_complete",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether the startup sequence has completed successfully.",
    ),
    # -- memory --
    KeySchema(
        key="memory.can_spawn_heavy",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether memory conditions allow spawning heavy subsystems.",
    ),
    KeySchema(
        key="memory.available_gb",
        value_type="float",
        nullable=False,
        default=0.0,
        description="Available system memory in gigabytes.",
        min_value=0.0,
    ),
    KeySchema(
        key="memory.admission_reason",
        value_type="str",
        nullable=False,
        default="",
        description="Human-readable reason for the current memory admission decision.",
    ),
    KeySchema(
        key="memory.tier",
        value_type="enum",
        nullable=False,
        default="unknown",
        description="Current memory pressure tier classification.",
        enum_values=_MEMORY_TIER_ENUM_VALUES,
        unknown_enum_policy="default_with_violation",
    ),
    KeySchema(
        key="memory.startup_mode",
        value_type="enum",
        nullable=False,
        default="local_full",
        description="Startup mode as determined by the memory assessor.",
        enum_values=_MODE_ENUM_VALUES,
        unknown_enum_policy="default_with_violation",
    ),
    KeySchema(
        key="memory.source",
        value_type="str",
        nullable=False,
        default="",
        description="Source identifier for the latest memory assessment.",
    ),
    # -- gcp --
    KeySchema(
        key="gcp.offload_active",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether GCP offload is currently active.",
    ),
    KeySchema(
        key="gcp.node_ip",
        value_type="str",
        nullable=False,
        default="",
        description="IP address of the active GCP node.",
        pattern=r"^(\d{1,3}\.){3}\d{1,3}$|^$",
    ),
    KeySchema(
        key="gcp.node_port",
        value_type="int",
        nullable=False,
        default=8000,
        description="Port of the active GCP node.",
        min_value=1,
        max_value=65535,
    ),
    KeySchema(
        key="gcp.node_booting",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether the GCP node is currently booting.",
    ),
    KeySchema(
        key="gcp.prime_endpoint",
        value_type="str",
        nullable=False,
        default="",
        description="Endpoint URL for the prime inference service on GCP.",
    ),
    # -- hollow --
    KeySchema(
        key="hollow.client_active",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether the hollow client mode is active.",
    ),
    # -- prime --
    KeySchema(
        key="prime.early_pid",
        value_type="int",
        nullable=True,
        default=None,
        description="PID of the early-launched prime process.",
        min_value=1,
    ),
    KeySchema(
        key="prime.early_port",
        value_type="int",
        nullable=True,
        default=None,
        description="Port of the early-launched prime process.",
        min_value=1,
        max_value=65535,
    ),
    # -- service --
    KeySchema(
        key="service.backend_minimal",
        value_type="bool",
        nullable=False,
        default=False,
        description="Whether the backend is running in minimal service mode.",
    ),
)


# ---------------------------------------------------------------------------
# Consistency Groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsistencyGroup:
    """A set of keys that should be updated atomically for consistency.

    Attributes
    ----------
    name:
        Unique identifier for this group.
    keys:
        Tuple of key names that belong to this group.
    description:
        Human-readable explanation of why these keys form a group.
    """

    name: str
    keys: Tuple[str, ...]
    description: str


CONSISTENCY_GROUPS: Tuple[ConsistencyGroup, ...] = (
    ConsistencyGroup(
        "gcp_readiness",
        (
            "gcp.offload_active",
            "gcp.node_ip",
            "gcp.node_port",
            "gcp.node_booting",
            "hollow.client_active",
        ),
        "GCP VM readiness state",
    ),
    ConsistencyGroup(
        "memory_assessment",
        (
            "memory.can_spawn_heavy",
            "memory.available_gb",
            "memory.tier",
            "memory.admission_reason",
        ),
        "Memory assessment results",
    ),
    ConsistencyGroup(
        "startup_mode",
        (
            "lifecycle.effective_mode",
            "memory.startup_mode",
        ),
        "Startup mode resolution chain",
    ),
)


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------


def build_ownership_registry() -> OwnershipRegistry:
    """Create and freeze an ``OwnershipRegistry`` from ``OWNERSHIP_RULES``."""
    registry = OwnershipRegistry()
    for rule in OWNERSHIP_RULES:
        registry.register(rule)
    registry.freeze()
    return registry


def build_schema_registry() -> SchemaRegistry:
    """Create a ``SchemaRegistry`` from ``KEY_SCHEMAS``."""
    registry = SchemaRegistry()
    for schema in KEY_SCHEMAS:
        registry.register(schema)
    return registry
