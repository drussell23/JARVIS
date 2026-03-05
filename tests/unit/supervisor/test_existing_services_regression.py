"""Regression tests: existing 10 service classes work with extended governance infra.

unified_supervisor.py is a 96K-line monolith with heavyweight module-level side
effects.  We use AST-based extraction to avoid the expensive full-module import.

Validated behaviours:
1. Each of the 10 service classes exists and extends SystemService (AST check).
2. Each class has v2 governance methods accessible via the MRO (AST check).
3. ServiceDescriptor backward compatibility -- original-fields-only construction
   still works, new fields get correct defaults.
4. SystemServiceRegistry.activate_phase() works with legacy-style descriptors
   (only original fields populated).
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"

# The 10 existing service classes and their expected base classes.
# CostTracker uses multiple inheritance (ResourceManagerBase, SystemService).
EXISTING_SERVICES = [
    "ObservabilityPipeline",
    "HealthAggregator",
    "CacheHierarchyManager",
    "TokenBucketRateLimiter",
    "CostTracker",
    "DistributedLockManager",
    "TaskQueueManager",
    "EventSourcingManager",
    "MessageBroker",
    "GracefulDegradationManager",
]

# v2 governance methods that should be accessible (inherited or overridden)
V2_METHODS = [
    "start",
    "health",
    "drain",
    "stop",
    "capability_contract",
    "activation_triggers",
]

# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

_REGISTRY_CLASSES = (
    "ServiceHealthReport",
    "CapabilityContract",
    "BudgetGate",
    "BackoffGate",
    "ActivationContract",
    "SystemService",
    "ServiceDescriptor",
    "SystemServiceRegistry",
)


def _parse_source() -> tuple[str, ast.Module]:
    """Read and parse unified_supervisor.py once.  Returns (source_text, ast_tree)."""
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    return source, tree


def _extract_class_info(source: str, tree: ast.Module) -> Dict[str, dict]:
    """For every top-level ClassDef, return {name: {bases: [...], methods: [...]}}."""
    info: Dict[str, dict] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            bases: list[str] = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(base.attr)
            methods: list[str] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(item.name)
            info[node.name] = {"bases": bases, "methods": methods, "lineno": node.lineno}
    return info


def _extract_registry_namespace():
    """Parse and exec the governance infrastructure classes (SystemService,
    ServiceDescriptor, SystemServiceRegistry, etc.) for runtime tests.

    Returns a dict mapping class name -> class object.
    """
    source, tree = _parse_source()

    class_sources: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in _REGISTRY_CLASSES:
            start = (
                node.decorator_list[0].lineno if node.decorator_list else node.lineno
            )
            end = node.end_lineno
            lines = source.splitlines()[start - 1 : end]
            class_sources.append("\n".join(lines))

    if not class_sources:
        pytest.fail(
            f"Could not find any of {_REGISTRY_CLASSES} in {_USP_PATH}. "
            "Have the governance classes been added?"
        )

    ns: dict = {
        "__builtins__": __builtins__,
        "ABC": ABC,
        "abstractmethod": abstractmethod,
        "dataclass": dataclass,
        "field": field,
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Tuple": Tuple,
        "os": os,
        "sys": sys,
        "time": time,
        "asyncio": asyncio,
    }
    ns["logger"] = logging.getLogger("test_existing_services_regression")

    for src in class_sources:
        exec(compile(src, str(_USP_PATH), "exec"), ns)

    return {name: ns[name] for name in _REGISTRY_CLASSES if name in ns}


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def class_info() -> Dict[str, dict]:
    """Parsed AST info for all top-level classes in unified_supervisor.py."""
    source, tree = _parse_source()
    return _extract_class_info(source, tree)


@pytest.fixture(scope="module")
def ns():
    """Extracted namespace with governance infrastructure classes."""
    return _extract_registry_namespace()


# ---------------------------------------------------------------------------
# Async helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Mock service for registry tests
# ---------------------------------------------------------------------------


def _make_mock_service(SystemService, ServiceHealthReport):
    """Create a minimal mock service implementing all lifecycle methods."""

    class MockService(SystemService):
        def __init__(self):
            self.initialized_called = False
            self.started = False
            self.drained = False
            self.stopped = False

        async def initialize(self) -> None:
            self.initialized_called = True

        async def health_check(self) -> Tuple[bool, str]:
            return (True, "ok")

        async def cleanup(self) -> None:
            pass

        async def start(self) -> bool:
            self.started = True
            return True

        async def health(self) -> ServiceHealthReport:
            return ServiceHealthReport(
                alive=True, ready=True, message="mock ok",
            )

        async def drain(self, deadline_s: float) -> bool:
            self.drained = True
            return True

        async def stop(self) -> None:
            self.stopped = True

    return MockService


# ===========================================================================
# 1. Each class exists and extends SystemService
# ===========================================================================


class TestServiceClassExists:
    """Parametrized test: each of the 10 service classes exists in the AST
    and has SystemService somewhere in its base class list."""

    @pytest.mark.parametrize("class_name", EXISTING_SERVICES)
    def test_class_exists(self, class_info, class_name):
        assert class_name in class_info, (
            f"{class_name} not found as a top-level class in unified_supervisor.py"
        )

    @pytest.mark.parametrize("class_name", EXISTING_SERVICES)
    def test_extends_system_service(self, class_info, class_name):
        info = class_info.get(class_name)
        assert info is not None, f"{class_name} not found"
        assert "SystemService" in info["bases"], (
            f"{class_name} does not list SystemService as a direct base class. "
            f"Found bases: {info['bases']}"
        )


# ===========================================================================
# 2. Each class has v2 governance methods accessible
# ===========================================================================


class TestV2GovernanceMethods:
    """Verify that v2 governance methods are accessible on each service class.

    We check via AST: either the class itself defines the method, or
    SystemService (which we know is in its MRO) provides a default.
    Since we cannot instantiate these classes without complex dependencies,
    we verify at the AST level that SystemService defines the defaults.
    """

    @pytest.fixture(scope="class")
    def system_service_methods(self, class_info) -> list[str]:
        """Return list of methods defined on SystemService itself."""
        ss_info = class_info.get("SystemService")
        assert ss_info is not None, "SystemService not found in AST"
        return ss_info["methods"]

    def test_system_service_defines_v2_defaults(self, system_service_methods):
        """SystemService must define all v2 governance methods as defaults."""
        for method in V2_METHODS:
            assert method in system_service_methods, (
                f"SystemService does not define default method '{method}'"
            )

    @pytest.mark.parametrize("class_name", EXISTING_SERVICES)
    def test_v2_methods_accessible(self, class_info, system_service_methods, class_name):
        """Each service class either defines the method itself or inherits it
        from SystemService.  Since SystemService is in the MRO (verified in
        test 1), the method is guaranteed accessible if defined on either."""
        info = class_info.get(class_name)
        assert info is not None

        for method in V2_METHODS:
            own_method = method in info["methods"]
            inherited = method in system_service_methods
            assert own_method or inherited, (
                f"{class_name} has no access to v2 method '{method}' -- "
                f"neither defined on class nor on SystemService"
            )


# ===========================================================================
# 3. ServiceDescriptor backward compatibility
# ===========================================================================


class TestServiceDescriptorBackwardCompat:
    """Constructing ServiceDescriptor with only original fields still works.
    New governance fields get correct defaults."""

    @pytest.fixture()
    def SD(self, ns):
        return ns["ServiceDescriptor"]

    @pytest.fixture()
    def stub_svc(self, ns):
        """A lightweight SystemService stub for descriptor construction."""
        SystemService = ns["SystemService"]
        ServiceHealthReport = ns["ServiceHealthReport"]
        MockService = _make_mock_service(SystemService, ServiceHealthReport)
        return MockService()

    def test_original_fields_only_positional(self, SD, stub_svc):
        """Construct with only (name, service, phase) -- should work."""
        sd = SD("test_svc", stub_svc, 2)
        assert sd.name == "test_svc"
        assert sd.phase == 2
        assert sd.depends_on == []
        assert sd.enabled_env is None

    def test_original_fields_only_with_kwargs(self, SD, stub_svc):
        """Construct with all original keyword fields."""
        sd = SD(
            name="obs_pipeline",
            service=stub_svc,
            phase=3,
            depends_on=["health_agg"],
            enabled_env="JARVIS_SERVICE_OBS_ENABLED",
        )
        assert sd.name == "obs_pipeline"
        assert sd.depends_on == ["health_agg"]
        assert sd.enabled_env == "JARVIS_SERVICE_OBS_ENABLED"

    def test_new_governance_fields_default_tier(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.tier == "optional"

    def test_new_governance_fields_default_activation_mode(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.activation_mode == "always_on"

    def test_new_governance_fields_default_boot_policy(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.boot_policy == "non_blocking"

    def test_new_governance_fields_default_criticality(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.criticality == "optional"

    def test_new_budget_fields_defaults(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.max_memory_mb == 50.0
        assert sd.max_cpu_percent == 10.0
        assert sd.max_concurrent_ops == 10

    def test_new_failure_fields_defaults(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.max_init_retries == 2
        assert sd.init_timeout_s == 30.0
        assert sd.circuit_breaker_threshold == 5

    def test_new_runtime_state_defaults(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.state == "pending"
        assert sd.activation_count == 0
        assert sd.last_health_check == 0.0

    def test_soft_depends_on_default(self, SD, stub_svc):
        sd = SD("x", stub_svc, 1)
        assert sd.soft_depends_on == []

    def test_initialized_and_healthy_defaults(self, SD, stub_svc):
        """Original state fields retain their defaults."""
        sd = SD("x", stub_svc, 1)
        assert sd.initialized is False
        assert sd.healthy is True
        assert sd.error is None
        assert sd.init_time_ms == 0.0
        assert sd.memory_delta_mb == 0.0


# ===========================================================================
# 4. Registry activates legacy-style descriptors
# ===========================================================================


class TestRegistryLegacyActivation:
    """SystemServiceRegistry.activate_phase() works correctly with
    descriptors using only original fields (no governance overrides)."""

    def _make_legacy_descriptor(self, ns, name, phase=1, **kwargs):
        """Build a ServiceDescriptor using only original fields."""
        SD = ns["ServiceDescriptor"]
        SystemService = ns["SystemService"]
        ServiceHealthReport = ns["ServiceHealthReport"]
        MockService = _make_mock_service(SystemService, ServiceHealthReport)
        svc = MockService()
        return SD(
            name=name,
            service=svc,
            phase=phase,
            **kwargs,
        )

    def test_activate_phase_with_legacy_descriptor(self, ns):
        """A descriptor with only original fields activates successfully."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(ns, "legacy_svc", phase=1)
        reg.register(desc)

        results = _run(reg.activate_phase(1))
        assert results["legacy_svc"] is True
        assert desc.initialized is True
        assert desc.service.initialized_called is True

    def test_legacy_descriptor_defaults_to_always_on(self, ns):
        """Legacy descriptors default to always_on, so start() is called."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(ns, "legacy_always_on", phase=1)
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.service.started is True
        assert desc.state == "active"

    def test_legacy_descriptor_in_activation_order(self, ns):
        """Legacy descriptors appear in activation order after activation."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(ns, "legacy_order", phase=1)
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert "legacy_order" in reg._activation_order

    def test_legacy_descriptor_records_telemetry(self, ns):
        """init_time_ms and memory_delta_mb are recorded for legacy descriptors."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(ns, "legacy_telemetry", phase=1)
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.init_time_ms > 0.0  # Mock is fast but non-zero

    def test_legacy_descriptor_with_depends_on(self, ns):
        """Legacy descriptors with depends_on activate in dependency order."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc_a = self._make_legacy_descriptor(ns, "dep_a", phase=1)
        desc_b = self._make_legacy_descriptor(
            ns, "dep_b", phase=1, depends_on=["dep_a"],
        )
        reg.register(desc_a)
        reg.register(desc_b)

        results = _run(reg.activate_phase(1))
        assert results["dep_a"] is True
        assert results["dep_b"] is True
        # dep_a must appear before dep_b in activation order
        idx_a = reg._activation_order.index("dep_a")
        idx_b = reg._activation_order.index("dep_b")
        assert idx_a < idx_b

    def test_legacy_descriptor_with_enabled_env_disabled(self, ns):
        """enabled_env kill switch still works on legacy descriptors."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(
            ns, "legacy_disabled", phase=1,
            enabled_env="TEST_LEGACY_SVC_DISABLED",
        )
        reg.register(desc)

        with mock.patch.dict(os.environ, {"TEST_LEGACY_SVC_DISABLED": "false"}):
            results = _run(reg.activate_phase(1))

        assert results["legacy_disabled"] is False
        assert desc.initialized is False

    def test_legacy_descriptor_with_enabled_env_enabled(self, ns):
        """enabled_env set to true allows activation."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(
            ns, "legacy_enabled", phase=1,
            enabled_env="TEST_LEGACY_SVC_ENABLED",
        )
        reg.register(desc)

        with mock.patch.dict(os.environ, {"TEST_LEGACY_SVC_ENABLED": "true"}):
            results = _run(reg.activate_phase(1))

        assert results["legacy_enabled"] is True

    def test_multiple_legacy_descriptors_same_phase(self, ns):
        """Multiple legacy descriptors in the same phase all activate."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()

        names = ["multi_a", "multi_b", "multi_c"]
        for name in names:
            desc = self._make_legacy_descriptor(ns, name, phase=2)
            reg.register(desc)

        results = _run(reg.activate_phase(2))
        for name in names:
            assert results[name] is True

    def test_legacy_descriptors_across_phases(self, ns):
        """Legacy descriptors in different phases only activate for their phase."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc_p1 = self._make_legacy_descriptor(ns, "phase1_svc", phase=1)
        desc_p2 = self._make_legacy_descriptor(ns, "phase2_svc", phase=2)
        reg.register(desc_p1)
        reg.register(desc_p2)

        results = _run(reg.activate_phase(1))
        assert "phase1_svc" in results
        assert "phase2_svc" not in results

        results2 = _run(reg.activate_phase(2))
        assert "phase2_svc" in results2

    def test_shutdown_works_for_legacy_descriptors(self, ns):
        """shutdown_all() works for services registered with legacy descriptors."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = self._make_legacy_descriptor(ns, "legacy_shutdown", phase=1)
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.state == "active"

        _run(reg.shutdown_all())
        assert desc.service.drained is True
        assert desc.service.stopped is True
        assert desc.state == "stopped"
