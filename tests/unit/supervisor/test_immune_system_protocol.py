"""Tests for SystemService protocol on 8 Immune System classes.

Validates that all 8 security/compliance classes in unified_supervisor.py
extend SystemService and implement the governance protocol:
1. Each class has SystemService in its bases (via AST inspection)
2. Each class has capability_contract() returning a real CapabilityContract
3. Each class has activation_triggers() returning a list
4. Each class can be constructed

Uses AST-based extraction to avoid importing the full 73K-line monolith.
"""
from __future__ import annotations

import ast
import asyncio
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pytest

# ---------------------------------------------------------------------------
# Helpers -- extract classes via AST
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"

# The 8 immune system classes
_IMMUNE_CLASSES = (
    "AuditTrailRecorder",
    "SecurityPolicyEngine",
    "ComplianceAuditor",
    "DataClassificationManager",
    "AccessControlManager",
    "AnomalyDetector",
    "IncidentResponseCoordinator",
    "ThreatIntelligenceManager",
)

# Supporting classes needed for extraction
_SUPPORT_CLASSES = (
    "ServiceHealthReport",
    "CapabilityContract",
    "SystemService",
)


def _extract_immune_namespace():
    """Parse unified_supervisor.py and extract the 8 immune classes + support types.

    Returns a dict mapping class name -> class object.
    """
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    all_needed = set(_SUPPORT_CLASSES) | set(_IMMUNE_CLASSES)

    # Collect class source in definition order
    class_sources: list[tuple[str, str]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in all_needed:
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

    missing_immune = set(_IMMUNE_CLASSES) - found
    if missing_immune:
        pytest.fail(
            f"Could not find immune classes {missing_immune} in {_USP_PATH}."
        )

    # Build namespace with required imports
    ns: dict = {
        "__builtins__": __builtins__,
        "ABC": ABC,
        "abstractmethod": abstractmethod,
        "dataclass": dataclass,
        "field": field,
        "Any": Any,
        "Callable": Callable,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Set": Set,
        "Tuple": Tuple,
        "deque": deque,
        # Stubs for types referenced by immune classes but not extracted
        "SystemKernelConfig": type("SystemKernelConfig", (), {}),
        "AuditEvent": type("AuditEvent", (), {}),
        "SecurityPolicy": type("SecurityPolicy", (), {}),
        "PolicyEvaluationResult": type("PolicyEvaluationResult", (), {}),
        "ComplianceRequirement": type("ComplianceRequirement", (), {}),
        "ComplianceStatus": type("ComplianceStatus", (), {}),
        "DataClassification": type("DataClassification", (), {}),
        "ClassifiedData": type("ClassifiedData", (), {}),
        "AccessRole": type("AccessRole", (), {}),
        "AccessGrant": type("AccessGrant", (), {}),
        "AccessPermission": type("AccessPermission", (), {}),
        "AnomalyScore": type("AnomalyScore", (), {}),
        "SecurityIncident": type("SecurityIncident", (), {}),
        "ThreatIndicator": type("ThreatIndicator", (), {}),
        # Stubs for functions/modules used in constructors
        "asyncio": asyncio,
        "logging": __import__("logging"),
        "tempfile": __import__("tempfile"),
        "Path": Path,
        "create_safe_task": lambda coro: None,  # stub
        "Awaitable": __import__("typing").Awaitable,
    }

    # Exec support classes first, then immune classes
    for name, src in class_sources:
        if name in _SUPPORT_CLASSES:
            exec(compile(src, str(_USP_PATH), "exec"), ns)

    for name, src in class_sources:
        if name in _IMMUNE_CLASSES:
            exec(compile(src, str(_USP_PATH), "exec"), ns)

    return ns


@pytest.fixture(scope="module")
def ns():
    """Module-scoped fixture: the extracted namespace dict."""
    return _extract_immune_namespace()


# ---------------------------------------------------------------------------
# AST-based inheritance check (does not require exec)
# ---------------------------------------------------------------------------


def _get_class_bases_from_ast():
    """Return dict of class_name -> list of base class names from AST."""
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    result = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in _IMMUNE_CLASSES:
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


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Test 1: SystemService in bases (AST check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _IMMUNE_CLASSES)
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


@pytest.mark.parametrize("class_name", _IMMUNE_CLASSES)
class TestCapabilityContract:
    def test_has_real_contract(self, ns, class_name):
        cls = ns[class_name]
        CapabilityContract = ns["CapabilityContract"]

        # Construct with config=None for the 7 that take config
        if class_name == "AuditTrailRecorder":
            obj = cls()
        else:
            obj = cls(config=None)

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


@pytest.mark.parametrize("class_name", _IMMUNE_CLASSES)
class TestActivationTriggers:
    def test_returns_list(self, ns, class_name):
        cls = ns[class_name]

        if class_name == "AuditTrailRecorder":
            obj = cls()
        else:
            obj = cls(config=None)

        triggers = obj.activation_triggers()
        assert isinstance(triggers, list), (
            f"{class_name}.activation_triggers() returned {type(triggers)}, expected list"
        )


# ---------------------------------------------------------------------------
# Test 4: Each class can be constructed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _IMMUNE_CLASSES)
class TestConstruction:
    def test_can_construct(self, ns, class_name):
        cls = ns[class_name]

        if class_name == "AuditTrailRecorder":
            obj = cls()
        else:
            obj = cls(config=None)

        assert obj is not None
        # Verify it is a SystemService instance
        SystemService = ns["SystemService"]
        assert isinstance(obj, SystemService), (
            f"{class_name} instance is not a SystemService"
        )
