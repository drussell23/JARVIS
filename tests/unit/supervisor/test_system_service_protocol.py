"""Tests for the extended SystemService ABC with v2 governance methods.

unified_supervisor.py is a 96K-line monolith with heavyweight module-level side
effects (subprocess probes, signal handling, BLAS guards).  A full
``importlib.import_module`` takes 30+ seconds and may hang in CI.

Strategy: we extract *only* the SystemService class and supporting governance
dataclasses via AST, compile and exec them, then test the resulting classes.
This keeps the test fast (~0.05 s) while still validating the real production
source.

Validated behaviours:
1. A subclass implementing only the original 3 abstract methods (initialize,
   health_check, cleanup) can still be instantiated.
2. The new methods have working defaults (start->True, drain->True,
   stop->calls cleanup, health->wraps health_check, capability_contract->stub,
   activation_triggers->[]).
3. A subclass can override all governance methods.
"""
from __future__ import annotations

import ast
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Helpers -- extract SystemService + governance dataclasses via AST
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"

# Classes we need to extract
_NEEDED_CLASSES = (
    "ServiceHealthReport",
    "CapabilityContract",
    "SystemService",
)


def _extract_system_service_namespace():
    """Parse unified_supervisor.py and exec SystemService + supporting types.

    Returns a dict mapping class name -> class object.
    """
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    # Collect class source in definition order
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
    }

    # Exec governance dataclasses first (ServiceHealthReport, CapabilityContract)
    # then SystemService which references them via forward-ref strings
    for src in class_sources:
        exec(compile(src, str(_USP_PATH), "exec"), ns)

    return {name: ns[name] for name in _NEEDED_CLASSES if name in ns}


@pytest.fixture(scope="module")
def ns():
    """Module-scoped fixture: the extracted namespace dict."""
    return _extract_system_service_namespace()


# ---------------------------------------------------------------------------
# Concrete subclasses for testing
# ---------------------------------------------------------------------------


def _make_legacy_subclass(SystemService):
    """Create a minimal subclass implementing ONLY the original 3 methods."""

    class LegacyService(SystemService):
        def __init__(self):
            self.initialized_called = False
            self.cleanup_called = False

        async def initialize(self) -> None:
            self.initialized_called = True

        async def health_check(self) -> Tuple[bool, str]:
            return (True, "healthy")

        async def cleanup(self) -> None:
            self.cleanup_called = True

    return LegacyService


def _make_overriding_subclass(SystemService, ServiceHealthReport, CapabilityContract):
    """Create a subclass that overrides ALL v2 governance methods."""

    class FullService(SystemService):
        def __init__(self):
            self.started = False
            self.drained = False
            self.stopped = False

        async def initialize(self) -> None:
            pass

        async def health_check(self) -> Tuple[bool, str]:
            return (True, "ok")

        async def cleanup(self) -> None:
            pass

        async def start(self) -> bool:
            self.started = True
            return True

        async def health(self) -> ServiceHealthReport:
            return ServiceHealthReport(
                alive=True, ready=True, message="custom health"
            )

        async def drain(self, deadline_s: float) -> bool:
            self.drained = True
            return deadline_s > 0

        async def stop(self) -> None:
            self.stopped = True

        def capability_contract(self) -> CapabilityContract:
            return CapabilityContract(
                name="FullService",
                version="2.0.0",
                inputs=["input.topic"],
                outputs=["output.topic"],
                side_effects=["write_db"],
            )

        def activation_triggers(self) -> List[str]:
            return ["anomaly.detected", "user.request"]

    return FullService


# ---------------------------------------------------------------------------
# Helper to run coroutines
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. Legacy subclass instantiation -- backward compatibility
# ---------------------------------------------------------------------------


class TestLegacySubclassInstantiation:
    """A subclass with only the original 3 abstract methods must instantiate."""

    def test_can_instantiate(self, ns):
        SystemService = ns["SystemService"]
        LegacyService = _make_legacy_subclass(SystemService)
        svc = LegacyService()
        assert svc is not None

    def test_initialize_works(self, ns):
        SystemService = ns["SystemService"]
        svc = _make_legacy_subclass(SystemService)()
        _run(svc.initialize())
        assert svc.initialized_called is True

    def test_health_check_works(self, ns):
        SystemService = ns["SystemService"]
        svc = _make_legacy_subclass(SystemService)()
        ok, msg = _run(svc.health_check())
        assert ok is True
        assert msg == "healthy"

    def test_cleanup_works(self, ns):
        SystemService = ns["SystemService"]
        svc = _make_legacy_subclass(SystemService)()
        _run(svc.cleanup())
        assert svc.cleanup_called is True


# ---------------------------------------------------------------------------
# 2. Default implementations of new v2 methods
# ---------------------------------------------------------------------------


class TestDefaultImplementations:
    """Verify the default implementations on a legacy (3-method) subclass."""

    @pytest.fixture()
    def svc(self, ns):
        SystemService = ns["SystemService"]
        return _make_legacy_subclass(SystemService)()

    def test_start_returns_true(self, svc):
        result = _run(svc.start())
        assert result is True

    def test_drain_returns_true(self, svc):
        result = _run(svc.drain(deadline_s=10.0))
        assert result is True

    def test_stop_delegates_to_cleanup(self, svc):
        assert svc.cleanup_called is False
        _run(svc.stop())
        assert svc.cleanup_called is True

    def test_health_wraps_health_check(self, ns, svc):
        ServiceHealthReport = ns["ServiceHealthReport"]
        report = _run(svc.health())
        assert isinstance(report, ServiceHealthReport)
        assert report.alive is True
        assert report.ready is True
        assert report.message == "healthy"

    def test_health_wraps_failing_health_check(self, ns):
        """If health_check raises, health() catches and returns a report."""
        SystemService = ns["SystemService"]
        ServiceHealthReport = ns["ServiceHealthReport"]

        class FailingHealthService(SystemService):
            async def initialize(self) -> None:
                pass

            async def health_check(self) -> Tuple[bool, str]:
                raise RuntimeError("database down")

            async def cleanup(self) -> None:
                pass

        svc = FailingHealthService()
        report = _run(svc.health())
        assert isinstance(report, ServiceHealthReport)
        assert report.alive is True
        assert report.ready is False
        assert "database down" in report.message

    def test_capability_contract_returns_stub(self, ns, svc):
        CapabilityContract = ns["CapabilityContract"]
        cc = svc.capability_contract()
        assert isinstance(cc, CapabilityContract)
        assert cc.name == "LegacyService"
        assert cc.version == "0.0.0"
        assert cc.inputs == []
        assert cc.outputs == []
        assert cc.side_effects == []

    def test_activation_triggers_returns_empty_list(self, svc):
        triggers = svc.activation_triggers()
        assert triggers == []
        assert isinstance(triggers, list)


# ---------------------------------------------------------------------------
# 3. Overriding all governance methods
# ---------------------------------------------------------------------------


class TestOverriddenMethods:
    """Verify that a subclass can override ALL v2 governance methods."""

    @pytest.fixture()
    def svc(self, ns):
        SystemService = ns["SystemService"]
        ServiceHealthReport = ns["ServiceHealthReport"]
        CapabilityContract = ns["CapabilityContract"]
        return _make_overriding_subclass(
            SystemService, ServiceHealthReport, CapabilityContract
        )()

    def test_start_override(self, svc):
        result = _run(svc.start())
        assert result is True
        assert svc.started is True

    def test_health_override(self, ns, svc):
        ServiceHealthReport = ns["ServiceHealthReport"]
        report = _run(svc.health())
        assert isinstance(report, ServiceHealthReport)
        assert report.message == "custom health"

    def test_drain_override(self, svc):
        assert _run(svc.drain(deadline_s=5.0)) is True
        assert svc.drained is True

    def test_drain_with_zero_deadline(self, ns):
        SystemService = ns["SystemService"]
        ServiceHealthReport = ns["ServiceHealthReport"]
        CapabilityContract = ns["CapabilityContract"]
        svc = _make_overriding_subclass(
            SystemService, ServiceHealthReport, CapabilityContract
        )()
        assert _run(svc.drain(deadline_s=0)) is False

    def test_stop_override(self, svc):
        _run(svc.stop())
        assert svc.stopped is True

    def test_capability_contract_override(self, ns, svc):
        CapabilityContract = ns["CapabilityContract"]
        cc = svc.capability_contract()
        assert isinstance(cc, CapabilityContract)
        assert cc.name == "FullService"
        assert cc.version == "2.0.0"
        assert cc.inputs == ["input.topic"]
        assert cc.outputs == ["output.topic"]
        assert cc.side_effects == ["write_db"]

    def test_activation_triggers_override(self, svc):
        triggers = svc.activation_triggers()
        assert triggers == ["anomaly.detected", "user.request"]
