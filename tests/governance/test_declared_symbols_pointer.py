"""The declared-symbol pointer: a signed goal's ``target_symbols`` reach the
resolver through the roadmap intake's own ``goal_id``, not only through a
delegated-provenance claim.

Until 2026-09-07 ``_declared_symbols_for`` honoured ONE pointer —
``evidence["provenance"]["goal_id"]`` — which exists only when delegated
provenance (a separate, optional feature) stamps a claim. Every roadmap goal
therefore resolved with ``declared_symbols=()``, the resolver fell back to
inference, widened the target with six inferred siblings and the swarm
dispatched a worker per sibling (five refine turns each, all wasted).

The security stance is unchanged: the evidence contributes only a POINTER.
What it names is re-read from the SIGNED roadmap, gated on the file being
inside that goal's own ``target_files``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import delegated_provenance
from backend.core.ouroboros.governance.candidate_generator import _declared_symbols_for

_FILE = "backend/core/ouroboros/governance/candidate_generator.py"


def _roadmap(monkeypatch, *, valid: bool = True, signed: bool = True):
    goal = SimpleNamespace(
        goal_id="ov-prod-swarm-single-target", target_files=(_FILE,),
        target_symbols=("CandidateGenerator._maybe_swarm_short_circuit",),
    )
    doc = SimpleNamespace(signature_valid=signed, goals=[goal])
    verdict = SimpleNamespace(value="valid" if valid else "invalid_signature")
    monkeypatch.setattr(delegated_provenance, "_verified_roadmap", lambda: (verdict, doc))
    return goal


def _ctx(evidence):
    return SimpleNamespace(evidence=evidence)


def test_the_intake_goal_id_is_an_accepted_pointer(monkeypatch):
    goal = _roadmap(monkeypatch)
    got = _declared_symbols_for(_ctx({"goal_id": goal.goal_id}), _FILE)
    assert got == goal.target_symbols


def test_a_provenance_claim_is_still_honoured_first(monkeypatch):
    goal = _roadmap(monkeypatch)
    ev = {"goal_id": "some-other-goal", "provenance": {"goal_id": goal.goal_id}}
    assert _declared_symbols_for(_ctx(ev), _FILE) == goal.target_symbols


def test_symbols_on_the_context_alone_confer_nothing(monkeypatch):
    """A fabricated ``target_symbols`` field with no pointer to a signed goal
    is exactly the forgery the pointer-only contract exists to prevent."""
    _roadmap(monkeypatch)
    assert _declared_symbols_for(_ctx({"target_symbols": ["os.system"]}), _FILE) == ()


def test_the_declaration_cannot_reach_a_file_outside_the_goal(monkeypatch):
    goal = _roadmap(monkeypatch)
    assert _declared_symbols_for(_ctx({"goal_id": goal.goal_id}), "backend/other.py") == ()


@pytest.mark.parametrize("valid,signed", [(False, True), (True, False)])
def test_an_unverified_roadmap_confers_nothing(monkeypatch, valid, signed):
    goal = _roadmap(monkeypatch, valid=valid, signed=signed)
    assert _declared_symbols_for(_ctx({"goal_id": goal.goal_id}), _FILE) == ()


def test_an_unknown_pointer_degrades_to_inference(monkeypatch):
    _roadmap(monkeypatch)
    assert _declared_symbols_for(_ctx({"goal_id": "never-signed"}), _FILE) == ()
    assert _declared_symbols_for(_ctx(None), _FILE) == ()


def test_the_pointer_is_read_off_the_real_context_snapshot(monkeypatch):
    """The envelope's evidence rides the context as ``intake_evidence_json``;
    the deriver read a field the context never had, so no declaration was
    ever reachable through a real OperationContext."""
    import json
    from backend.core.ouroboros.governance.op_context import OperationContext

    goal = _roadmap(monkeypatch)
    ctx = OperationContext.create(
        description="x", target_files=(_FILE,),
        intake_evidence_json=json.dumps({"goal_id": goal.goal_id, "source": "roadmap"}),
    )
    assert ctx.intake_evidence["goal_id"] == goal.goal_id
    assert _declared_symbols_for(ctx, _FILE) == goal.target_symbols
    bare = OperationContext.create(description="x", target_files=(_FILE,))
    assert bare.intake_evidence == {} and _declared_symbols_for(bare, _FILE) == ()
    corrupt = OperationContext.create(description="x", target_files=(_FILE,), intake_evidence_json="{not json")
    assert corrupt.intake_evidence == {}
