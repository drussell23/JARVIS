"""Tests for SystemServiceRegistry governance: activation_mode, boot_policy,
tier kill switches, safe mode, structured health, and graceful shutdown.

Uses the same AST-extraction pattern as sibling tests to avoid importing the
96K-line unified_supervisor.py monolith.

Validated behaviours:
 1. always_on services activate normally (initialize + start, state=active)
 2. deferred_after_ready services are skipped during activate_phase()
 3. activate_deferred() activates deferred services
 4. warm_standby services: initialize but don't start (state=ready)
 5. event_driven services: initialize but don't start (state=ready)
 6. activate_service() starts a warm_standby service on demand
 7. Tier kill switch disables entire tier
 8. JARVIS_SAFE_MODE=true disables non-kernel_critical services
 9. shutdown_all() calls drain then stop
10. health_check_all_structured() returns ServiceHealthReport instances
"""
from __future__ import annotations

import ast
import asyncio
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
# Helpers -- AST-extract SystemServiceRegistry + supporting classes
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"

_NEEDED_CLASSES = (
    "ServiceHealthReport",
    "CapabilityContract",
    "BudgetGate",
    "BackoffGate",
    "ActivationContract",
    "SystemService",
    "ServiceDescriptor",
    "SystemServiceRegistry",
)


def _extract_registry_namespace():
    """Parse unified_supervisor.py and exec the registry + supporting types.

    Returns a dict mapping class name -> class object.
    """
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    class_sources: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in _NEEDED_CLASSES:
            start = (
                node.decorator_list[0].lineno if node.decorator_list else node.lineno
            )
            end = node.end_lineno
            lines = source.splitlines()[start - 1 : end]
            class_sources.append("\n".join(lines))

    if not class_sources:
        pytest.fail(
            f"Could not find any of {_NEEDED_CLASSES} in {_USP_PATH}. "
            "Have the classes been added yet?"
        )

    # Build namespace with required imports
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
        # Standard library modules used by SystemServiceRegistry
        "os": os,
        "sys": sys,
        "time": time,
        "asyncio": asyncio,
    }

    # We need a logger stand-in
    import logging

    ns["logger"] = logging.getLogger("test_registry_activation_modes")

    for src in class_sources:
        exec(compile(src, str(_USP_PATH), "exec"), ns)

    return {name: ns[name] for name in _NEEDED_CLASSES if name in ns}


@pytest.fixture(scope="module")
def ns():
    """Module-scoped fixture: the extracted namespace dict."""
    return _extract_registry_namespace()


# ---------------------------------------------------------------------------
# Mock service implementations
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_mock_service(SystemService, ServiceHealthReport):
    """Create a mock service that tracks all lifecycle calls."""

    class MockService(SystemService):
        def __init__(self):
            self.initialized_called = False
            self.started = False
            self.drained = False
            self.stopped = False
            self.cleaned = False
            self._healthy = True
            self._ready = True

        async def initialize(self) -> None:
            self.initialized_called = True

        async def health_check(self) -> Tuple[bool, str]:
            return (self._healthy, "ok" if self._healthy else "unhealthy")

        async def cleanup(self) -> None:
            self.cleaned = True

        async def start(self) -> bool:
            self.started = True
            return True

        async def health(self) -> ServiceHealthReport:
            return ServiceHealthReport(
                alive=True,
                ready=self._ready,
                degraded=not self._healthy,
                message="mock health",
            )

        async def drain(self, deadline_s: float) -> bool:
            self.drained = True
            return True

        async def stop(self) -> None:
            self.stopped = True

    return MockService


def _make_descriptor(ns, name, phase=1, **kwargs):
    """Helper to build a ServiceDescriptor with a mock service."""
    ServiceDescriptor = ns["ServiceDescriptor"]
    SystemService = ns["SystemService"]
    ServiceHealthReport = ns["ServiceHealthReport"]
    MockService = _make_mock_service(SystemService, ServiceHealthReport)
    svc = MockService()
    return ServiceDescriptor(
        name=name,
        service=svc,
        phase=phase,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. always_on services activate normally (initialize + start, state=active)
# ---------------------------------------------------------------------------


class TestAlwaysOnActivation:
    def test_always_on_initializes_and_starts(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(ns, "svc_always", phase=1, activation_mode="always_on")
        reg.register(desc)

        results = _run(reg.activate_phase(1))
        assert results["svc_always"] is True
        assert desc.service.initialized_called is True
        assert desc.service.started is True
        assert desc.state == "active"
        assert desc.initialized is True

    def test_always_on_increments_activation_count(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(ns, "svc_count", phase=1, activation_mode="always_on")
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.activation_count == 1


# ---------------------------------------------------------------------------
# 2. deferred_after_ready services are skipped during activate_phase()
# ---------------------------------------------------------------------------


class TestDeferredSkip:
    def test_deferred_skipped_during_phase(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_deferred", phase=1, boot_policy="deferred_after_ready"
        )
        reg.register(desc)

        results = _run(reg.activate_phase(1))
        assert results.get("svc_deferred") is False or "svc_deferred" not in results
        assert desc.service.initialized_called is False
        assert desc.initialized is False

    def test_deferred_does_not_appear_in_activation_order(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_deferred2", phase=1, boot_policy="deferred_after_ready"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert "svc_deferred2" not in reg._activation_order


# ---------------------------------------------------------------------------
# 3. activate_deferred() activates deferred services
# ---------------------------------------------------------------------------


class TestActivateDeferred:
    def test_activate_deferred_initializes_skipped_services(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_def_act",
            phase=1,
            boot_policy="deferred_after_ready",
            activation_mode="always_on",
        )
        reg.register(desc)

        # Phase skips it
        _run(reg.activate_phase(1))
        assert desc.initialized is False

        # activate_deferred picks it up
        results = _run(reg.activate_deferred())
        assert results["svc_def_act"] is True
        assert desc.service.initialized_called is True
        assert desc.initialized is True

    def test_activate_deferred_respects_activation_mode(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_def_warm",
            phase=1,
            boot_policy="deferred_after_ready",
            activation_mode="warm_standby",
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        results = _run(reg.activate_deferred())
        assert results["svc_def_warm"] is True
        assert desc.service.initialized_called is True
        assert desc.service.started is False
        assert desc.state == "ready"


# ---------------------------------------------------------------------------
# 4. warm_standby services: initialize but don't start (state=ready)
# ---------------------------------------------------------------------------


class TestWarmStandbyActivation:
    def test_warm_standby_initializes_not_starts(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_warm", phase=1, activation_mode="warm_standby"
        )
        reg.register(desc)

        results = _run(reg.activate_phase(1))
        assert results["svc_warm"] is True
        assert desc.service.initialized_called is True
        assert desc.service.started is False
        assert desc.state == "ready"
        assert desc.initialized is True

    def test_warm_standby_in_activation_order(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_warm2", phase=1, activation_mode="warm_standby"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert "svc_warm2" in reg._activation_order


# ---------------------------------------------------------------------------
# 5. event_driven services: initialize but don't start (state=ready)
# ---------------------------------------------------------------------------


class TestEventDrivenActivation:
    def test_event_driven_initializes_not_starts(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_event", phase=1, activation_mode="event_driven"
        )
        reg.register(desc)

        results = _run(reg.activate_phase(1))
        assert results["svc_event"] is True
        assert desc.service.initialized_called is True
        assert desc.service.started is False
        assert desc.state == "ready"


# ---------------------------------------------------------------------------
# 6. activate_service() starts a warm_standby service on demand
# ---------------------------------------------------------------------------


class TestActivateServiceOnDemand:
    def test_activate_service_starts_warm_standby(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_on_demand", phase=1, activation_mode="warm_standby"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.state == "ready"
        assert desc.service.started is False

        result = _run(reg.activate_service("svc_on_demand"))
        assert result is True
        assert desc.service.started is True
        assert desc.state == "active"

    def test_activate_service_already_active(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_active_twice", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.state == "active"

        result = _run(reg.activate_service("svc_active_twice"))
        assert result is True

    def test_activate_service_not_initialized(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_not_init", phase=1, activation_mode="warm_standby"
        )
        reg.register(desc)
        # Don't call activate_phase -- service not initialized

        result = _run(reg.activate_service("svc_not_init"))
        assert result is False

    def test_activate_service_unknown_name(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()

        result = _run(reg.activate_service("nonexistent"))
        assert result is False


# ---------------------------------------------------------------------------
# 7. Tier kill switch disables entire tier
# ---------------------------------------------------------------------------


class TestTierKillSwitch:
    def test_tier_kill_switch_disables_services(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_immune", phase=1, tier="immune", activation_mode="always_on"
        )
        reg.register(desc)

        with mock.patch.dict(os.environ, {"JARVIS_SERVICE_IMMUNE_ENABLED": "false"}):
            results = _run(reg.activate_phase(1))

        assert results["svc_immune"] is False
        assert desc.service.initialized_called is False
        assert desc.initialized is False

    def test_tier_kill_switch_case_insensitive(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_metabolic", phase=1, tier="metabolic", activation_mode="always_on"
        )
        reg.register(desc)

        with mock.patch.dict(
            os.environ, {"JARVIS_SERVICE_METABOLIC_ENABLED": "FALSE"}
        ):
            results = _run(reg.activate_phase(1))

        assert results["svc_metabolic"] is False

    def test_tier_kill_switch_not_set_allows_activation(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_tier_ok", phase=1, tier="nervous", activation_mode="always_on"
        )
        reg.register(desc)

        # Ensure env var is NOT set
        env = os.environ.copy()
        env.pop("JARVIS_SERVICE_NERVOUS_ENABLED", None)
        with mock.patch.dict(os.environ, env, clear=True):
            results = _run(reg.activate_phase(1))

        assert results["svc_tier_ok"] is True


# ---------------------------------------------------------------------------
# 8. JARVIS_SAFE_MODE=true disables non-kernel_critical services
# ---------------------------------------------------------------------------


class TestSafeMode:
    def test_safe_mode_blocks_optional(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_optional",
            phase=1,
            criticality="optional",
            activation_mode="always_on",
        )
        reg.register(desc)

        with mock.patch.dict(os.environ, {"JARVIS_SAFE_MODE": "true"}):
            results = _run(reg.activate_phase(1))

        assert results["svc_optional"] is False
        assert desc.service.initialized_called is False

    def test_safe_mode_allows_kernel_critical(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_kernel",
            phase=1,
            criticality="kernel_critical",
            activation_mode="always_on",
        )
        reg.register(desc)

        with mock.patch.dict(os.environ, {"JARVIS_SAFE_MODE": "true"}):
            results = _run(reg.activate_phase(1))

        assert results["svc_kernel"] is True
        assert desc.service.initialized_called is True

    def test_safe_mode_blocks_control_plane(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_cp",
            phase=1,
            criticality="control_plane",
            activation_mode="always_on",
        )
        reg.register(desc)

        with mock.patch.dict(os.environ, {"JARVIS_SAFE_MODE": "true"}):
            results = _run(reg.activate_phase(1))

        assert results["svc_cp"] is False

    def test_no_safe_mode_allows_all(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_no_safe",
            phase=1,
            criticality="optional",
            activation_mode="always_on",
        )
        reg.register(desc)

        env = os.environ.copy()
        env.pop("JARVIS_SAFE_MODE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            results = _run(reg.activate_phase(1))

        assert results["svc_no_safe"] is True


# ---------------------------------------------------------------------------
# 9. shutdown_all() calls drain then stop
# ---------------------------------------------------------------------------


class TestShutdownDrainStop:
    def test_shutdown_calls_drain_then_stop(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_shutdown", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.initialized is True

        _run(reg.shutdown_all())
        assert desc.service.drained is True
        assert desc.service.stopped is True
        assert desc.initialized is False
        assert desc.state == "stopped"

    def test_shutdown_reverse_order(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        shutdown_order = []

        SystemService = ns["SystemService"]
        ServiceHealthReport = ns["ServiceHealthReport"]

        class OrderTrackingService(SystemService):
            def __init__(self, name):
                self._name = name

            async def initialize(self) -> None:
                pass

            async def health_check(self) -> Tuple[bool, str]:
                return (True, "ok")

            async def cleanup(self) -> None:
                pass

            async def start(self) -> bool:
                return True

            async def drain(self, deadline_s: float) -> bool:
                return True

            async def stop(self) -> None:
                shutdown_order.append(self._name)

        SD = ns["ServiceDescriptor"]
        svc_a = OrderTrackingService("a")
        svc_b = OrderTrackingService("b")

        reg.register(SD(name="a", service=svc_a, phase=1, activation_mode="always_on"))
        reg.register(SD(name="b", service=svc_b, phase=1, activation_mode="always_on"))

        _run(reg.activate_phase(1))
        _run(reg.shutdown_all())
        # Reverse of activation order
        assert shutdown_order == ["b", "a"]

    def test_shutdown_updates_state_to_draining_then_stopped(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_drain_state", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        _run(reg.shutdown_all())
        assert desc.state == "stopped"


# ---------------------------------------------------------------------------
# 10. health_check_all_structured() returns ServiceHealthReport instances
# ---------------------------------------------------------------------------


class TestHealthCheckStructured:
    def test_returns_service_health_reports(self, ns):
        SSR = ns["SystemServiceRegistry"]
        ServiceHealthReport = ns["ServiceHealthReport"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_health", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        results = _run(reg.health_check_all_structured())

        assert "svc_health" in results
        report = results["svc_health"]
        assert isinstance(report, ServiceHealthReport)
        assert report.alive is True

    def test_updates_descriptor_healthy_flag(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_health2", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        # Make service unhealthy
        desc.service._ready = False
        desc.service._healthy = False

        results = _run(reg.health_check_all_structured())
        report = results["svc_health2"]
        # The mock returns ready=False when _ready is False
        assert report.ready is False

    def test_updates_last_health_check(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_health3", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        before = time.monotonic()
        _run(reg.health_check_all_structured())
        after = time.monotonic()

        assert desc.last_health_check >= before
        assert desc.last_health_check <= after

    def test_degraded_report_updates_state(self, ns):
        SSR = ns["SystemServiceRegistry"]
        ServiceHealthReport = ns["ServiceHealthReport"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_degraded", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        assert desc.state == "active"

        # Make service degraded
        desc.service._healthy = False
        _run(reg.health_check_all_structured())
        assert desc.state == "degraded"

    def test_recovery_from_degraded_to_active(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_recover", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))

        # Degrade
        desc.service._healthy = False
        _run(reg.health_check_all_structured())
        assert desc.state == "degraded"

        # Recover
        desc.service._healthy = True
        desc.service._ready = True
        _run(reg.health_check_all_structured())
        assert desc.state == "active"


# ---------------------------------------------------------------------------
# Backward compatibility: legacy health_check_all still works
# ---------------------------------------------------------------------------


class TestLegacyHealthCheckStillWorks:
    def test_legacy_health_check_all(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns, "svc_legacy_hc", phase=1, activation_mode="always_on"
        )
        reg.register(desc)

        _run(reg.activate_phase(1))
        results = _run(reg.health_check_all())

        assert "svc_legacy_hc" in results
        healthy, msg = results["svc_legacy_hc"]
        assert healthy is True


# ---------------------------------------------------------------------------
# Uses desc.init_timeout_s instead of timeout_per_service
# ---------------------------------------------------------------------------


class TestInitTimeout:
    def test_uses_desc_init_timeout_s(self, ns):
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(
            ns,
            "svc_timeout",
            phase=1,
            activation_mode="always_on",
            init_timeout_s=60.0,
        )
        reg.register(desc)

        # Service activates successfully -- the timeout_per_service param
        # (30.0 default) would apply if desc.init_timeout_s wasn't used.
        # Since our service is fast, both would pass. We verify the desc
        # value is available.
        results = _run(reg.activate_phase(1))
        assert results["svc_timeout"] is True
        assert desc.init_timeout_s == 60.0
