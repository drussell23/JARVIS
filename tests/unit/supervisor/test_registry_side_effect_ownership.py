"""Tests for side-effect ownership validation at service registration time.

SystemServiceRegistry.register() must ensure no two services declare the same
side-effect in their capability_contract().side_effects.  This prevents state
ownership drift where multiple services mutate the same resource.

Uses the same AST-extraction pattern as sibling tests to avoid importing the
96K-line unified_supervisor.py monolith.

Validated behaviours:
 1. Non-overlapping side effects -- registers OK
 2. Overlapping side effects -- raises ValueError with "side.effect" and "conflict"
 3. Empty side effects -- always OK (multiple services with empty lists)
 4. Rollback on conflict -- failed service not left in registry
 5. Side-effect owners cleaned up on rollback
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

    ns["logger"] = logging.getLogger("test_registry_side_effect_ownership")

    for src in class_sources:
        exec(compile(src, str(_USP_PATH), "exec"), ns)

    return {name: ns[name] for name in _NEEDED_CLASSES if name in ns}


@pytest.fixture(scope="module")
def ns():
    """Module-scoped fixture: the extracted namespace dict."""
    return _extract_registry_namespace()


# ---------------------------------------------------------------------------
# Mock service with configurable side effects
# ---------------------------------------------------------------------------


def _make_service_with_side_effects(SystemService, CapabilityContract, side_effects):
    """Create a mock SystemService whose capability_contract returns given side_effects."""

    class SideEffectService(SystemService):
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

        def capability_contract(self) -> CapabilityContract:
            return CapabilityContract(
                name="test_svc",
                version="1.0.0",
                inputs=[],
                outputs=[],
                side_effects=list(side_effects),
            )

    return SideEffectService()


def _make_descriptor_with_side_effects(ns, name, side_effects, phase=1, **kwargs):
    """Helper to build a ServiceDescriptor with a service that has specific side effects."""
    ServiceDescriptor = ns["ServiceDescriptor"]
    SystemService = ns["SystemService"]
    CapabilityContract = ns["CapabilityContract"]
    svc = _make_service_with_side_effects(SystemService, CapabilityContract, side_effects)
    return ServiceDescriptor(
        name=name,
        service=svc,
        phase=phase,
        **kwargs,
    )


def _make_descriptor_default(ns, name, phase=1, **kwargs):
    """Helper to build a ServiceDescriptor with default (empty) side effects."""
    ServiceDescriptor = ns["ServiceDescriptor"]
    SystemService = ns["SystemService"]

    class DefaultService(SystemService):
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

    svc = DefaultService()
    return ServiceDescriptor(
        name=name,
        service=svc,
        phase=phase,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Non-overlapping side effects -- registers OK
# ---------------------------------------------------------------------------


class TestNonOverlappingSideEffects:
    def test_different_side_effects_register_ok(self, ns):
        """Two services with distinct side effects should both register fine."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_b", ["db.orders.write"])
        )
        assert "svc_a" in reg._services
        assert "svc_b" in reg._services

    def test_multiple_non_overlapping_side_effects(self, ns):
        """Services with multiple non-overlapping side effects register OK."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(
                ns, "svc_a", ["fs.config.write", "db.users.write"]
            )
        )
        reg.register(
            _make_descriptor_with_side_effects(
                ns, "svc_b", ["fs.logs.write", "db.sessions.write"]
            )
        )
        assert "svc_a" in reg._services
        assert "svc_b" in reg._services

    def test_single_side_effect_one_service(self, ns):
        """A single service with a side effect registers with no issue."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_solo", ["cache.invalidate"])
        )
        assert "svc_solo" in reg._services


# ---------------------------------------------------------------------------
# 2. Overlapping side effects -- raises ValueError
# ---------------------------------------------------------------------------


class TestOverlappingSideEffects:
    def test_duplicate_side_effect_raises(self, ns):
        """Two services claiming the same side effect should raise ValueError."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError) as exc_info:
            reg.register(
                _make_descriptor_with_side_effects(ns, "svc_b", ["db.users.write"])
            )
        msg = str(exc_info.value).lower()
        assert "side" in msg or "effect" in msg or "side_effect" in msg or "side-effect" in msg
        assert "conflict" in msg

    def test_partial_overlap_raises(self, ns):
        """If only one of several side effects overlaps, it still raises."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(
                ns, "svc_a", ["db.users.write", "cache.invalidate"]
            )
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor_with_side_effects(
                    ns, "svc_b", ["fs.logs.write", "cache.invalidate"]
                )
            )

    def test_error_message_mentions_conflicting_effect(self, ns):
        """The error message should mention the conflicting side-effect name."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError, match=r"db\.users\.write"):
            reg.register(
                _make_descriptor_with_side_effects(ns, "svc_b", ["db.users.write"])
            )


# ---------------------------------------------------------------------------
# 3. Empty side effects -- always OK
# ---------------------------------------------------------------------------


class TestEmptySideEffects:
    def test_empty_side_effects_register_ok(self, ns):
        """Multiple services with empty side_effects lists coexist fine."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", [])
        )
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_b", [])
        )
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_c", [])
        )
        assert len(reg._services) == 3

    def test_default_contract_empty_side_effects(self, ns):
        """Services using default capability_contract() (empty side_effects) coexist."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(_make_descriptor_default(ns, "svc_default_a"))
        reg.register(_make_descriptor_default(ns, "svc_default_b"))
        assert "svc_default_a" in reg._services
        assert "svc_default_b" in reg._services

    def test_empty_and_non_empty_coexist(self, ns):
        """A service with side effects and one without can coexist."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_with", ["db.users.write"])
        )
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_without", [])
        )
        assert "svc_with" in reg._services
        assert "svc_without" in reg._services


# ---------------------------------------------------------------------------
# 4. Rollback on conflict -- failed service not left in registry
# ---------------------------------------------------------------------------


class TestRollbackOnConflict:
    def test_conflicting_service_not_in_registry(self, ns):
        """If register() raises due to side-effect conflict, the service is removed."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor_with_side_effects(ns, "svc_b", ["db.users.write"])
            )
        assert "svc_b" not in reg._services

    def test_original_service_preserved_after_conflict(self, ns):
        """After a conflict, the original service that owns the side effect stays."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        desc_a = _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        reg.register(desc_a)
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor_with_side_effects(ns, "svc_b", ["db.users.write"])
            )
        assert "svc_a" in reg._services
        assert reg._services["svc_a"] is desc_a

    def test_can_register_non_conflicting_after_rollback(self, ns):
        """After a conflict rollback, a non-conflicting service can still register."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor_with_side_effects(ns, "svc_b", ["db.users.write"])
            )
        # Now register a non-conflicting service
        desc_c = _make_descriptor_with_side_effects(ns, "svc_c", ["db.orders.write"])
        reg.register(desc_c)
        assert "svc_c" in reg._services


# ---------------------------------------------------------------------------
# 5. Side-effect owners cleaned up on rollback
# ---------------------------------------------------------------------------


class TestSideEffectOwnerCleanupOnRollback:
    def test_owners_not_polluted_after_conflict(self, ns):
        """After a failed registration, _side_effect_owners should not contain
        any side-effects from the failed service."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor_with_side_effects(
                    ns, "svc_b", ["fs.logs.write", "db.users.write"]
                )
            )
        # svc_b's unique side-effect "fs.logs.write" should NOT be in owners
        assert reg._side_effect_owners.get("fs.logs.write") is None

    def test_original_owners_preserved_after_conflict(self, ns):
        """After a conflict rollback, original side-effect owners remain correct."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError):
            reg.register(
                _make_descriptor_with_side_effects(ns, "svc_b", ["db.users.write"])
            )
        assert reg._side_effect_owners["db.users.write"] == "svc_a"

    def test_new_service_can_claim_side_effect_after_failed_claim(self, ns):
        """After svc_b fails to claim 'fs.logs.write' (due to conflict on another),
        svc_c can still claim 'fs.logs.write'."""
        SSR = ns["SystemServiceRegistry"]
        reg = SSR()
        reg.register(
            _make_descriptor_with_side_effects(ns, "svc_a", ["db.users.write"])
        )
        with pytest.raises(ValueError):
            # svc_b tries to claim fs.logs.write AND db.users.write (conflict)
            reg.register(
                _make_descriptor_with_side_effects(
                    ns, "svc_b", ["fs.logs.write", "db.users.write"]
                )
            )
        # svc_c should be able to claim fs.logs.write since svc_b was rolled back
        desc_c = _make_descriptor_with_side_effects(ns, "svc_c", ["fs.logs.write"])
        reg.register(desc_c)
        assert reg._side_effect_owners["fs.logs.write"] == "svc_c"
