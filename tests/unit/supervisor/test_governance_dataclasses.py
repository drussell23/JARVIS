"""Tests for governance dataclasses added by the Enterprise Organ Activation Program.

unified_supervisor.py is a 96K-line monolith with heavyweight module-level side
effects (subprocess probes, signal handling, BLAS guards).  A full
``importlib.import_module`` takes 30+ seconds and may hang in CI.

Strategy: we extract *only* the dataclass source region via AST, compile and
exec it, then test the resulting classes.  This keeps the test fast (~0.05 s)
while still validating the real production source.
"""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Helpers — extract governance dataclasses without importing the full module
# ---------------------------------------------------------------------------

_USP_PATH = Path(__file__).resolve().parents[3] / "unified_supervisor.py"

# The names we expect to be defined in unified_supervisor.py
_GOVERNANCE_CLASSES = (
    "ServiceHealthReport",
    "CapabilityContract",
    "BudgetGate",
    "BackoffGate",
    "ActivationContract",
)


def _extract_governance_namespace():
    """Parse unified_supervisor.py and exec only the governance dataclass region.

    Returns a dict mapping class name -> class object.
    """
    source = _USP_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    # Collect top-level class nodes whose names are in _GOVERNANCE_CLASSES
    class_sources: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in _GOVERNANCE_CLASSES:
            # Include any decorators (e.g. @dataclass(frozen=True))
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            end = node.end_lineno
            # ast line numbers are 1-based; source.splitlines() is 0-based
            lines = source.splitlines()[start - 1 : end]
            class_sources.append("\n".join(lines))

    if not class_sources:
        pytest.fail(
            f"Could not find any of {_GOVERNANCE_CLASSES} in {_USP_PATH}. "
            "Have the dataclasses been added yet?"
        )

    # Build a minimal namespace with the imports the dataclasses need
    ns: dict = {
        "__builtins__": __builtins__,
        "dataclass": dataclass,
        "field": field,
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
    }
    # Exec each class definition in order (some depend on earlier ones)
    for src in class_sources:
        exec(compile(src, str(_USP_PATH), "exec"), ns)

    return {name: ns[name] for name in _GOVERNANCE_CLASSES if name in ns}


@pytest.fixture(scope="module")
def gov():
    """Module-scoped fixture: the governance namespace dict."""
    return _extract_governance_namespace()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestServiceHealthReport:
    def test_construction_minimal(self, gov):
        ServiceHealthReport = gov["ServiceHealthReport"]
        report = ServiceHealthReport(alive=True, ready=True)
        assert report.alive is True
        assert report.ready is True
        assert report.degraded is False
        assert report.draining is False
        assert report.message == ""
        assert report.metrics == {}

    def test_frozen(self, gov):
        ServiceHealthReport = gov["ServiceHealthReport"]
        report = ServiceHealthReport(alive=True, ready=False)
        with pytest.raises(AttributeError):
            report.alive = False

    def test_degraded_state(self, gov):
        ServiceHealthReport = gov["ServiceHealthReport"]
        report = ServiceHealthReport(
            alive=True, ready=True, degraded=True, message="high latency"
        )
        assert report.degraded is True
        assert report.message == "high latency"


class TestCapabilityContract:
    def test_construction(self, gov):
        CapabilityContract = gov["CapabilityContract"]
        cc = CapabilityContract(
            name="test_svc",
            version="1.0.0",
            inputs=["topic.a"],
            outputs=["topic.b"],
            side_effects=["writes_audit_log"],
        )
        assert cc.name == "test_svc"
        assert cc.idempotent is True
        assert cc.cross_repo is False

    def test_frozen(self, gov):
        CapabilityContract = gov["CapabilityContract"]
        cc = CapabilityContract(
            name="x", version="1.0.0", inputs=[], outputs=[], side_effects=[]
        )
        with pytest.raises(AttributeError):
            cc.name = "y"


class TestActivationContract:
    def test_construction_with_defaults(self, gov):
        ActivationContract = gov["ActivationContract"]
        BudgetGate = gov["BudgetGate"]
        BackoffGate = gov["BackoffGate"]
        ac = ActivationContract(
            trigger_events=["anomaly.detected"],
            dependency_gate=["observability"],
            budget_gate=BudgetGate(),
            backoff_gate=BackoffGate(),
        )
        assert ac.max_activations_per_hour == 100
        assert ac.deactivate_after_idle_s == 300.0

    def test_budget_gate_defaults(self, gov):
        BudgetGate = gov["BudgetGate"]
        bg = BudgetGate()
        assert bg.max_memory_percent == 85.0
        assert bg.max_cpu_percent == 80.0
        assert bg.min_available_mb == 200.0

    def test_backoff_gate_defaults(self, gov):
        BackoffGate = gov["BackoffGate"]
        bo = BackoffGate()
        assert bo.initial_delay_s == 5.0
        assert bo.max_delay_s == 300.0
        assert bo.multiplier == 2.0
        assert bo.jitter is True
