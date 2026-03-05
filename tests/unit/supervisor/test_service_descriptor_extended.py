"""Tests for extended ServiceDescriptor governance fields.

ServiceDescriptor lives in unified_supervisor.py (96K+ lines).  We use the
same AST-extraction strategy as test_governance_dataclasses.py to avoid the
expensive (30+ s) full-module import and its side-effects.

This file validates:
1. Backward-compatible construction (only original fields) still works.
2. All new governance/budget/failure/health/state fields exist with correct defaults.
3. Explicit governance field values can be set and read back.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Helpers -- extract ServiceDescriptor without importing unified_supervisor
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"


def _extract_service_descriptor():
    """Parse unified_supervisor.py and exec only the ServiceDescriptor class.

    Returns the ServiceDescriptor class object.
    """
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    # Find the ServiceDescriptor class node
    sd_source: str | None = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ServiceDescriptor":
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            end = node.end_lineno
            lines = source.splitlines()[start - 1 : end]
            sd_source = "\n".join(lines)
            break

    if sd_source is None:
        pytest.fail(
            f"Could not find ServiceDescriptor in {_USP_PATH}. "
            "Has the class been defined?"
        )

    # We also need SystemService (the type annotation for `service` field).
    # Rather than extracting the real ABC, provide a lightweight stub.
    class _SystemServiceStub:
        """Minimal stand-in for the SystemService ABC."""
        pass

    ns: dict = {
        "__builtins__": __builtins__,
        "dataclass": dataclass,
        "field": field,
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Enum": Enum,
        # Stub for the SystemService type annotation
        "SystemService": _SystemServiceStub,
    }
    exec(compile(sd_source, str(_USP_PATH), "exec"), ns)

    if "ServiceDescriptor" not in ns:
        pytest.fail("ServiceDescriptor was not produced by exec.")

    return ns["ServiceDescriptor"], _SystemServiceStub


@pytest.fixture(scope="module")
def sd_classes():
    """Module-scoped fixture returning (ServiceDescriptor, SystemServiceStub)."""
    return _extract_service_descriptor()


@pytest.fixture(scope="module")
def SD(sd_classes):
    """Module-scoped fixture returning just the ServiceDescriptor class."""
    return sd_classes[0]


@pytest.fixture(scope="module")
def stub_service(sd_classes):
    """Module-scoped fixture returning a SystemService stub instance."""
    return sd_classes[1]()


# ---------------------------------------------------------------------------
# 1. Backward compatibility -- original-fields-only construction
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify that constructing ServiceDescriptor with ONLY the original fields works."""

    def test_positional_only(self, SD, stub_service):
        """name, service, phase as positional args -- no keyword args."""
        sd = SD("test_svc", stub_service, 3)
        assert sd.name == "test_svc"
        assert sd.phase == 3
        # Original defaults
        assert sd.depends_on == []
        assert sd.enabled_env is None
        assert sd.initialized is False
        assert sd.healthy is True
        assert sd.error is None
        assert sd.init_time_ms == 0.0
        assert sd.memory_delta_mb == 0.0

    def test_with_original_kwargs(self, SD, stub_service):
        """name, service, phase + original keyword fields."""
        sd = SD(
            name="observability",
            service=stub_service,
            phase=1,
            depends_on=["health_aggregator"],
            enabled_env="JARVIS_SERVICE_OBSERVABILITY_ENABLED",
        )
        assert sd.name == "observability"
        assert sd.depends_on == ["health_aggregator"]
        assert sd.enabled_env == "JARVIS_SERVICE_OBSERVABILITY_ENABLED"

    def test_all_original_fields(self, SD, stub_service):
        """All original fields supplied explicitly."""
        sd = SD(
            name="cache",
            service=stub_service,
            phase=2,
            depends_on=["a", "b"],
            enabled_env="CACHE_ON",
            initialized=True,
            healthy=False,
            error="boom",
            init_time_ms=42.5,
            memory_delta_mb=12.3,
        )
        assert sd.initialized is True
        assert sd.healthy is False
        assert sd.error == "boom"
        assert sd.init_time_ms == 42.5
        assert sd.memory_delta_mb == 12.3


# ---------------------------------------------------------------------------
# 2. New fields exist with correct defaults
# ---------------------------------------------------------------------------


class TestNewFieldDefaults:
    """Verify that all new governance fields are present with correct default values."""

    @pytest.fixture()
    def sd_defaults(self, SD, stub_service):
        """A ServiceDescriptor built with only the 3 required positional fields."""
        return SD("default_test", stub_service, 1)

    # --- Dependencies extension ---

    def test_soft_depends_on_default(self, sd_defaults):
        assert sd_defaults.soft_depends_on == []

    # --- Governance ---

    def test_tier_default(self, sd_defaults):
        assert sd_defaults.tier == "optional"

    def test_activation_mode_default(self, sd_defaults):
        assert sd_defaults.activation_mode == "always_on"

    def test_boot_policy_default(self, sd_defaults):
        assert sd_defaults.boot_policy == "non_blocking"

    def test_criticality_default(self, sd_defaults):
        assert sd_defaults.criticality == "optional"

    # --- Budget policy ---

    def test_max_memory_mb_default(self, sd_defaults):
        assert sd_defaults.max_memory_mb == 50.0

    def test_max_cpu_percent_default(self, sd_defaults):
        assert sd_defaults.max_cpu_percent == 10.0

    def test_max_concurrent_ops_default(self, sd_defaults):
        assert sd_defaults.max_concurrent_ops == 10

    # --- Failure policy ---

    def test_max_init_retries_default(self, sd_defaults):
        assert sd_defaults.max_init_retries == 2

    def test_init_timeout_s_default(self, sd_defaults):
        assert sd_defaults.init_timeout_s == 30.0

    def test_circuit_breaker_threshold_default(self, sd_defaults):
        assert sd_defaults.circuit_breaker_threshold == 5

    def test_circuit_breaker_recovery_s_default(self, sd_defaults):
        assert sd_defaults.circuit_breaker_recovery_s == 60.0

    def test_quarantine_after_failures_default(self, sd_defaults):
        assert sd_defaults.quarantine_after_failures == 10

    # --- Health semantics ---

    def test_health_check_interval_s_default(self, sd_defaults):
        assert sd_defaults.health_check_interval_s == 30.0

    def test_liveness_timeout_s_default(self, sd_defaults):
        assert sd_defaults.liveness_timeout_s == 10.0

    def test_readiness_timeout_s_default(self, sd_defaults):
        assert sd_defaults.readiness_timeout_s == 5.0

    # --- Runtime state extensions ---

    def test_state_default(self, sd_defaults):
        assert sd_defaults.state == "pending"

    def test_activation_count_default(self, sd_defaults):
        assert sd_defaults.activation_count == 0

    def test_last_health_check_default(self, sd_defaults):
        assert sd_defaults.last_health_check == 0.0


# ---------------------------------------------------------------------------
# 3. Explicit governance field values
# ---------------------------------------------------------------------------


class TestExplicitGovernanceValues:
    """Verify that explicitly supplied governance values are stored correctly."""

    def test_full_governance_construction(self, SD, stub_service):
        sd = SD(
            name="immune_firewall",
            service=stub_service,
            phase=4,
            depends_on=["observability"],
            soft_depends_on=["rate_limiter", "cost_tracker"],
            tier="immune",
            activation_mode="event_driven",
            boot_policy="block_ready",
            criticality="kernel_critical",
            max_memory_mb=100.0,
            max_cpu_percent=25.0,
            max_concurrent_ops=5,
            max_init_retries=3,
            init_timeout_s=60.0,
            circuit_breaker_threshold=3,
            circuit_breaker_recovery_s=120.0,
            quarantine_after_failures=5,
            health_check_interval_s=15.0,
            liveness_timeout_s=5.0,
            readiness_timeout_s=3.0,
            state="ready",
            activation_count=42,
            last_health_check=1234567890.0,
        )
        # Dependencies
        assert sd.depends_on == ["observability"]
        assert sd.soft_depends_on == ["rate_limiter", "cost_tracker"]

        # Governance
        assert sd.tier == "immune"
        assert sd.activation_mode == "event_driven"
        assert sd.boot_policy == "block_ready"
        assert sd.criticality == "kernel_critical"

        # Budget
        assert sd.max_memory_mb == 100.0
        assert sd.max_cpu_percent == 25.0
        assert sd.max_concurrent_ops == 5

        # Failure
        assert sd.max_init_retries == 3
        assert sd.init_timeout_s == 60.0
        assert sd.circuit_breaker_threshold == 3
        assert sd.circuit_breaker_recovery_s == 120.0
        assert sd.quarantine_after_failures == 5

        # Health
        assert sd.health_check_interval_s == 15.0
        assert sd.liveness_timeout_s == 5.0
        assert sd.readiness_timeout_s == 3.0

        # Runtime state
        assert sd.state == "ready"
        assert sd.activation_count == 42
        assert sd.last_health_check == 1234567890.0

    def test_mixed_original_and_new_fields(self, SD, stub_service):
        """Use some original fields and some new fields together."""
        sd = SD(
            name="rate_limiter",
            service=stub_service,
            phase=2,
            enabled_env="JARVIS_SERVICE_RATELIMIT_ENABLED",
            tier="metabolic",
            activation_mode="warm_standby",
            max_memory_mb=25.0,
            state="active",
        )
        # Original fields
        assert sd.name == "rate_limiter"
        assert sd.phase == 2
        assert sd.enabled_env == "JARVIS_SERVICE_RATELIMIT_ENABLED"
        assert sd.depends_on == []  # default
        assert sd.initialized is False  # default

        # New fields set explicitly
        assert sd.tier == "metabolic"
        assert sd.activation_mode == "warm_standby"
        assert sd.max_memory_mb == 25.0
        assert sd.state == "active"

        # New fields at default
        assert sd.boot_policy == "non_blocking"
        assert sd.criticality == "optional"
        assert sd.max_init_retries == 2

    def test_mutable_defaults_are_independent(self, SD, stub_service):
        """Ensure mutable default fields (lists) are independent across instances."""
        sd1 = SD("a", stub_service, 1)
        sd2 = SD("b", stub_service, 2)

        sd1.soft_depends_on.append("x")
        assert "x" not in sd2.soft_depends_on
        assert sd2.soft_depends_on == []

        sd1.depends_on.append("y")
        assert "y" not in sd2.depends_on
