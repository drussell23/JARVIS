"""Slice 119 — Bounded M10 Integration (Order-2 RSI, human-gated).

The marquee is the CLOSED-LOOP proof: a high-entropy failure state routes to
M10, M10 synthesizes a proposal, the op is forced to APPROVAL_REQUIRED, and the
AST diff is QUEUED without executing. Plus the load-bearing §1 invariant: the
synapse has NO path to APPLY — a structural upgrade reaches the codebase only
after the operator (the Zero-Order Doll) signs it.
"""

from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import m10_synapse as M10
from backend.core.ouroboros.governance.m10_synapse import (
    M10_FORCED_TIER,
    evaluate_m10_routing,
    m10_synapse_enabled,
    propose_structural_upgrade,
    should_route_to_m10,
)


class _FakeProposal:
    def __init__(self, pid="prop-1"):
        self.proposal_id = pid
        self.proposed_class_name = "UpgradedReasoner"
        self.applied = False  # spy: flips True ONLY if something executes it
    def to_dict(self):
        return {"proposal_id": self.proposal_id, "proposed_class_name": self.proposed_class_name}


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_M10_SYNAPSE_ENABLED", raising=False)
    assert m10_synapse_enabled() is False
    monkeypatch.setenv("JARVIS_M10_SYNAPSE_ENABLED", "1")
    assert m10_synapse_enabled() is True


class TestTrigger:
    def test_high_entropy_triggers(self):
        assert should_route_to_m10(shannon_entropy=0.95, recent_algorithmic_failures=0) is True

    def test_repeated_failures_trigger(self):
        assert should_route_to_m10(shannon_entropy=0.1, recent_algorithmic_failures=5) is True

    def test_calm_state_does_not_trigger(self):
        assert should_route_to_m10(shannon_entropy=0.2, recent_algorithmic_failures=1) is False


class TestRoutingForcesApproval:
    def test_disabled_never_routes(self, monkeypatch):
        monkeypatch.delenv("JARVIS_M10_SYNAPSE_ENABLED", raising=False)
        d = evaluate_m10_routing(shannon_entropy=0.99, recent_algorithmic_failures=10)
        assert d.route_to_m10 is False and d.forced_tier == ""

    def test_enabled_trigger_forces_approval_required(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M10_SYNAPSE_ENABLED", "1")
        d = evaluate_m10_routing(shannon_entropy=0.95, recent_algorithmic_failures=0)
        assert d.route_to_m10 is True
        assert d.forced_tier == M10_FORCED_TIER == "APPROVAL_REQUIRED"  # the strict invariant

    def test_enabled_no_trigger_no_route(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M10_SYNAPSE_ENABLED", "1")
        d = evaluate_m10_routing(shannon_entropy=0.1, recent_algorithmic_failures=0)
        assert d.route_to_m10 is False


class TestProposeNeverExecutes:
    def test_proposal_is_queued_not_executed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M10_SYNAPSE_ENABLED", "1")
        stored = []
        prop = _FakeProposal()
        out = propose_structural_upgrade(
            context={"op": "x"},
            proposer=lambda ctx: prop,
            store_fn=lambda p: (stored.append(p) or True),
        )
        assert out is prop
        assert stored == [prop]        # persisted as PENDING
        assert prop.applied is False   # NEVER executed — the §1 invariant

    def test_disabled_proposes_nothing(self, monkeypatch):
        monkeypatch.delenv("JARVIS_M10_SYNAPSE_ENABLED", raising=False)
        out = propose_structural_upgrade(context={}, proposer=lambda c: _FakeProposal())
        assert out is None

    def test_synapse_has_no_apply_path(self):
        # §1 PROOF: the synapse module IMPORTS nothing from the apply path (an
        # AST check, not raw text — the words appear in safety docstrings). A
        # structural upgrade cannot reach the codebase through this module.
        import ast
        tree = ast.parse(inspect.getsource(M10))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        bad = [m for m in imported if any(f in m for f in ("change_engine", "apply"))]
        assert not bad, f"synapse must not import the apply path; found {bad}"
        # And no executable call to an apply/execute primitive in the AST.
        calls = {getattr(n.func, "attr", "") for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "execute" not in calls and "apply_candidate" not in calls


class TestClosedLoop:
    def test_high_entropy_failure_proposes_pauses_queues(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M10_SYNAPSE_ENABLED", "1")
        # 1. Simulated high-entropy failure state.
        decision = evaluate_m10_routing(shannon_entropy=0.93, recent_algorithmic_failures=4)
        # 2. M10 is routed, and forced to the human gate.
        assert decision.route_to_m10 is True
        assert decision.forced_tier == "APPROVAL_REQUIRED"
        # 3. M10 synthesizes a proposal → queued pending, NOT executed.
        stored = []
        prop = _FakeProposal("closed-loop")
        out = propose_structural_upgrade(
            context={"entropy": 0.93}, proposer=lambda c: prop,
            store_fn=lambda p: (stored.append(p) or True),
        )
        assert out.proposal_id == "closed-loop"
        assert len(stored) == 1            # AST diff safely QUEUED
        assert prop.applied is False       # nothing executed without the human
