"""Tests for dependency cycle detection at service registration time.

SystemServiceRegistry.register() must detect dependency cycles immediately
when a service is registered, NOT defer detection to activation time.

Uses the same AST-extraction pattern as sibling tests to avoid importing the
96K-line unified_supervisor.py monolith.

Validated behaviours:
 1. No cycle -- registers OK
 2. Direct cycle (A->B, B->A) -- raises ValueError
 3. Transitive cycle (A->B, B->C, C->A) -- raises ValueError
 4. Self-cycle (A->A) -- raises ValueError
 5. soft_depends_on included in cycle check (A soft_depends B, B depends A)
 6. Cycle detection error message contains "cycle" (case-insensitive)
 7. Failed registration rolls back (service not left in registry)
"""
from __future__ import annotations

import ast
import os
import sys
import time
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

    ns["logger"] = logging.getLogger("test_registry_cycle_detection")

    for src in class_sources:
        exec(compile(src, str(_USP_PATH), "exec"), ns)

    return {name: ns[name] for name in _NEEDED_CLASSES if name in ns}


@pytest.fixture(scope="module")
def ns():
    """Module-scoped fixture: the extracted namespace dict."""
    return _extract_registry_namespace()


# ---------------------------------------------------------------------------
# Mock service implementation
# ---------------------------------------------------------------------------


def _make_mock_service(SystemService):
    """Create a minimal mock service for registration tests."""

    class MockService(SystemService):
        async def initialize(self) -> None:
            pass

        async def health_check(self) -> Tuple[bool, str]:
            return (True, "ok")

        async def cleanup(self) -> None:
            pass

        async def start(self) -> bool:
            return True

        async def health(self):
            return None

        async def drain(self, deadline_s: float) -> bool:
            return True

        async def stop(self) -> None:
            pass

    return MockService


def _make_descriptor(ns, name, phase=1, **kwargs):
    """Helper to build a ServiceDescriptor with a mock service."""
    ServiceDescriptor = ns["ServiceDescriptor"]
    SystemService = ns["SystemService"]
    MockService = _make_mock_service(SystemService)
    svc = MockService()
    return ServiceDescriptor(
        name=name,
        service=svc,
        phase=phase,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. No cycle -- registers OK
# ---------------------------------------------------------------------------


class TestNoCycle:
    def test_register_no_deps(self, ns):
        """A service with no dependencies registers without error."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(ns, "svc_a", phase=1)
        reg.register(desc)
        assert reg._services["svc_a"] is desc

    def test_register_linear_deps(self, ns):
        """A linear chain (A->B->C) has no cycle and registers fine."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_c", phase=1))
        reg.register(_make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_c"]))
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        assert "svc_a" in reg._services
        assert "svc_b" in reg._services
        assert "svc_c" in reg._services

    def test_register_diamond_deps(self, ns):
        """Diamond shape (A->B, A->C, B->D, C->D) has no cycle."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_d", phase=1))
        reg.register(_make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_d"]))
        reg.register(_make_descriptor(ns, "svc_c", phase=1, depends_on=["svc_d"]))
        reg.register(
            _make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b", "svc_c"])
        )
        assert len(reg._services) == 4


# ---------------------------------------------------------------------------
# 2. Direct cycle (A->B, B->A) -- raises ValueError
# ---------------------------------------------------------------------------


class TestDirectCycle:
    def test_direct_cycle_raises(self, ns):
        """Adding B->A when A->B already exists should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_a"])
            )

    def test_direct_cycle_message_contains_both_names(self, ns):
        """The error message should mention both services in the cycle."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "alpha", phase=1, depends_on=["beta"]))
        with pytest.raises(ValueError, match=r"alpha"):
            reg.register(
                _make_descriptor(ns, "beta", phase=1, depends_on=["alpha"])
            )


# ---------------------------------------------------------------------------
# 3. Transitive cycle (A->B, B->C, C->A) -- raises ValueError
# ---------------------------------------------------------------------------


class TestTransitiveCycle:
    def test_transitive_cycle_raises(self, ns):
        """Adding C->A when A->B and B->C exist should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        reg.register(_make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_c"]))
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_c", phase=1, depends_on=["svc_a"])
            )

    def test_longer_transitive_cycle(self, ns):
        """A->B->C->D->A cycle is detected when D is registered."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        reg.register(_make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_c"]))
        reg.register(_make_descriptor(ns, "svc_c", phase=1, depends_on=["svc_d"]))
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_d", phase=1, depends_on=["svc_a"])
            )


# ---------------------------------------------------------------------------
# 4. Self-cycle (A->A) -- raises ValueError
# ---------------------------------------------------------------------------


class TestSelfCycle:
    def test_self_dependency_raises(self, ns):
        """A service that depends on itself should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_self", phase=1, depends_on=["svc_self"])
            )


# ---------------------------------------------------------------------------
# 5. soft_depends_on included in cycle check
# ---------------------------------------------------------------------------


class TestSoftDependsCycle:
    def test_soft_dep_creates_cycle(self, ns):
        """A soft_depends B, B depends A -- should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor(ns, "svc_a", phase=1, soft_depends_on=["svc_b"])
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_a"])
            )

    def test_mixed_hard_soft_cycle(self, ns):
        """A->B (hard), B->C (soft), C->A (hard) -- should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        reg.register(
            _make_descriptor(ns, "svc_b", phase=1, soft_depends_on=["svc_c"])
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_c", phase=1, depends_on=["svc_a"])
            )

    def test_soft_self_dependency_raises(self, ns):
        """A service with soft_depends_on itself should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(
                    ns, "svc_self_soft", phase=1, soft_depends_on=["svc_self_soft"]
                )
            )


# ---------------------------------------------------------------------------
# 6. Cycle detection error message contains "cycle" (case-insensitive)
# ---------------------------------------------------------------------------


class TestCycleErrorMessage:
    def test_error_message_contains_cycle(self, ns):
        """The ValueError message must contain the word 'cycle'."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "x", phase=1, depends_on=["y"]))
        with pytest.raises(ValueError, match=r"(?i)cycle"):
            reg.register(_make_descriptor(ns, "y", phase=1, depends_on=["x"]))

    def test_error_message_shows_cycle_path(self, ns):
        """The ValueError message should show the cycle path with ->."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "a", phase=1, depends_on=["b"]))
        with pytest.raises(ValueError, match=r"->"):
            reg.register(_make_descriptor(ns, "b", phase=1, depends_on=["a"]))


# ---------------------------------------------------------------------------
# 7. Failed registration rolls back (service not left in registry)
# ---------------------------------------------------------------------------


class TestRollback:
    def test_cyclic_service_not_in_registry(self, ns):
        """If register() raises due to a cycle, the service must be removed."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_a"])
            )
        assert "svc_b" not in reg._services

    def test_rollback_preserves_existing_services(self, ns):
        """After a failed registration, existing services remain intact."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc_a = _make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"])
        reg.register(desc_a)
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_a"])
            )
        # svc_a should still be there
        assert "svc_a" in reg._services
        assert reg._services["svc_a"] is desc_a

    def test_can_register_non_cyclic_after_rollback(self, ns):
        """After a cyclic registration fails, a valid registration works."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_b"]))
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor(ns, "svc_b", phase=1, depends_on=["svc_a"])
            )
        # Now register svc_b without the cycle
        desc_b = _make_descriptor(ns, "svc_b", phase=1)
        reg.register(desc_b)
        assert "svc_b" in reg._services
        assert reg._services["svc_b"] is desc_b


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_dependency_on_unregistered_service_is_ok(self, ns):
        """Depending on a service not yet registered is fine (no cycle)."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc = _make_descriptor(ns, "svc_a", phase=1, depends_on=["svc_unknown"])
        reg.register(desc)
        assert "svc_a" in reg._services

    def test_multiple_independent_services_no_cycle(self, ns):
        """Multiple services with no deps register without error."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        for i in range(10):
            reg.register(_make_descriptor(ns, f"svc_{i}", phase=1))
        assert len(reg._services) == 10
