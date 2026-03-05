"""Tests for SystemService protocol on 12 Nervous System classes.

Validates that all 12 control-plane / orchestration classes in
unified_supervisor.py extend SystemService and implement the governance
protocol:
1. Each class has SystemService in its bases (via AST inspection)
2. Each class has capability_contract() returning a real CapabilityContract
3. Each class has activation_triggers() returning a list
4. Each class can be constructed (with config=None where required)

Uses AST-based extraction to avoid importing the full 73K-line monolith.
"""
from __future__ import annotations

import ast
import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pytest

# ---------------------------------------------------------------------------
# Helpers -- extract classes via AST
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"

# The 12 nervous system classes
_NERVOUS_CLASSES = (
    "WorkflowEngine",
    "StateMachineManager",
    "DynamicConfigurationManager",
    "FeatureGateManager",
    "SchemaRegistry",
    "ServiceRegistryManager",
    "RulesEngine",
    "BatchProcessor",
    "NotificationDispatcher",
    "RequestCoalescer",
    "BackgroundJobManager",
    "AutoScalingController",
)

# Supporting classes needed for extraction (defined before the nervous classes)
_SUPPORT_CLASSES = (
    "ServiceHealthReport",
    "CapabilityContract",
    "SystemService",
)

# Auxiliary types referenced by nervous system class bodies / constructors
_AUX_CLASSES = (
    "WorkflowDefinition",
    "WorkflowInstance",
    "WorkflowStepStatus",
    "StateMachineDefinition",
    "StateMachineInstance",
    "FeatureGate",
    "TargetingRule",
    "ScalingDecision",
    "BatchItem",
    "NotificationChannel",
    "NotificationPriority",
    "Notification",
    "SchemaVersion",
    "ServiceRegistration",
    "ServiceQuery",
    "CoalescedRequest",
    "BackgroundJob",
    "Rule",
    "RuleResult",
)

# Classes that require config=SystemKernelConfig as first arg
_CONFIG_REQUIRED = frozenset({
    "ServiceRegistryManager",
    "RequestCoalescer",
    "BackgroundJobManager",
    "RulesEngine",
})


def _extract_nervous_namespace():
    """Parse unified_supervisor.py and extract the 12 nervous classes + support types.

    Returns a dict mapping class name -> class object.
    """
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    all_needed = set(_SUPPORT_CLASSES) | set(_NERVOUS_CLASSES) | set(_AUX_CLASSES)

    # Collect class source in definition order (first occurrence only
    # to avoid duplicate class names like NotificationChannel)
    class_sources: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in all_needed:
            if node.name in seen_names:
                continue  # skip duplicate definitions
            seen_names.add(node.name)
            start = (
                node.decorator_list[0].lineno if node.decorator_list else node.lineno
            )
            end = node.end_lineno
            lines = source.splitlines()[start - 1 : end]
            class_sources.append((node.name, "\n".join(lines)))

    found = {name for name, _ in class_sources}
    missing_support = set(_SUPPORT_CLASSES) - found
    if missing_support:
        pytest.fail(
            f"Could not find support classes {missing_support} in {_USP_PATH}."
        )

    missing_nervous = set(_NERVOUS_CLASSES) - found
    if missing_nervous:
        pytest.fail(
            f"Could not find nervous classes {missing_nervous} in {_USP_PATH}."
        )

    # Build namespace with required imports
    ns: dict = {
        "__builtins__": __builtins__,
        "ABC": ABC,
        "abstractmethod": abstractmethod,
        "dataclass": dataclass,
        "field": field,
        "Enum": Enum,
        "Any": Any,
        "Callable": Callable,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Set": Set,
        "Tuple": Tuple,
        "deque": deque,
        "defaultdict": defaultdict,
        # Stubs for types referenced by nervous classes but not extracted
        "SystemKernelConfig": type("SystemKernelConfig", (), {}),
        # Standard library modules used in class bodies
        "asyncio": asyncio,
        "logging": __import__("logging"),
        "tempfile": __import__("tempfile"),
        "time": __import__("time"),
        "json": __import__("json"),
        "uuid": __import__("uuid"),
        "re": __import__("re"),
        "os": __import__("os"),
        "Path": Path,
        "datetime": __import__("datetime").datetime,
        "create_safe_task": lambda coro: None,  # stub
        "Awaitable": __import__("typing").Awaitable,
    }

    # Phase 1: exec support classes
    for name, src in class_sources:
        if name in _SUPPORT_CLASSES:
            exec(compile(src, str(_USP_PATH), "exec"), ns)

    # Phase 2: exec auxiliary types (enums, dataclasses, namedtuples)
    for name, src in class_sources:
        if name in _AUX_CLASSES:
            try:
                exec(compile(src, str(_USP_PATH), "exec"), ns)
            except Exception:
                # Create stub if aux class has complex deps
                ns[name] = type(name, (), {})

    # Ensure all aux types exist as stubs if extraction failed
    for name in _AUX_CLASSES:
        if name not in ns:
            ns[name] = type(name, (), {})

    # Phase 3: exec nervous system classes
    for name, src in class_sources:
        if name in _NERVOUS_CLASSES:
            exec(compile(src, str(_USP_PATH), "exec"), ns)

    return ns


@pytest.fixture(scope="module")
def ns():
    """Module-scoped fixture: the extracted namespace dict."""
    return _extract_nervous_namespace()


# ---------------------------------------------------------------------------
# AST-based inheritance check (does not require exec)
# ---------------------------------------------------------------------------


def _get_class_bases_from_ast():
    """Return dict of class_name -> list of base class names from AST."""
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    result = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in _NERVOUS_CLASSES:
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(base.attr)
            result[node.name] = bases
    return result


@pytest.fixture(scope="module")
def ast_bases():
    """Module-scoped fixture: AST-extracted base classes."""
    return _get_class_bases_from_ast()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _construct(ns, class_name):
    """Construct an instance, passing config=None for classes that require it."""
    cls = ns[class_name]
    if class_name in _CONFIG_REQUIRED:
        return cls(config=None)
    return cls()


# ---------------------------------------------------------------------------
# Test 1: SystemService in bases (AST check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _NERVOUS_CLASSES)
class TestSystemServiceInBases:
    def test_has_system_service_base(self, ast_bases, class_name):
        bases = ast_bases.get(class_name, [])
        assert "SystemService" in bases, (
            f"{class_name} does not have SystemService in its bases. "
            f"Found bases: {bases}"
        )


# ---------------------------------------------------------------------------
# Test 2: capability_contract returns real CapabilityContract (version != "0.0.0")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _NERVOUS_CLASSES)
class TestCapabilityContract:
    def test_has_real_contract(self, ns, class_name):
        CapabilityContract = ns["CapabilityContract"]
        obj = _construct(ns, class_name)

        cc = obj.capability_contract()
        assert isinstance(cc, CapabilityContract), (
            f"{class_name}.capability_contract() did not return a CapabilityContract"
        )
        assert cc.version != "0.0.0", (
            f"{class_name} still has stub version '0.0.0' -- "
            "needs a real capability_contract override"
        )
        assert cc.name != "", f"{class_name} contract has empty name"
        assert len(cc.inputs) > 0 or len(cc.outputs) > 0, (
            f"{class_name} contract has no inputs or outputs declared"
        )


# ---------------------------------------------------------------------------
# Test 3: activation_triggers returns a list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _NERVOUS_CLASSES)
class TestActivationTriggers:
    def test_returns_list(self, ns, class_name):
        obj = _construct(ns, class_name)

        triggers = obj.activation_triggers()
        assert isinstance(triggers, list), (
            f"{class_name}.activation_triggers() returned {type(triggers)}, expected list"
        )


# ---------------------------------------------------------------------------
# Test 4: Each class can be constructed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _NERVOUS_CLASSES)
class TestConstruction:
    def test_can_construct(self, ns, class_name):
        obj = _construct(ns, class_name)
        assert obj is not None

        # Verify it is a SystemService instance
        SystemService = ns["SystemService"]
        assert isinstance(obj, SystemService), (
            f"{class_name} instance is not a SystemService"
        )
