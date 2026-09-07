"""A signed goal's declared symbols must reach the code that enforces them.

2026-09-07, first production-code goal: the model answered ``2b.1-noop`` —
"the `_tracer_auth_recheck_s()` function was added alongside
`_tracer_timeout_s()`" — for a function that did not exist on disk, and the
declared-symbol contract built to refuse exactly that claim stayed silent
through fifteen ops.

Every consumer read ``getattr(ctx, "target_symbols", ())``. Three of them:
the no-op refusal in ``generate_runner``, the VALIDATE gate in
``orchestrator``, and the differential gate's acceptance names. None had a
producer: ``OperationContext`` had no such field, and
``_make_envelope_for_goal`` dropped the goal's symbols on the floor. The
contract was correct and unreachable — this repo's most expensive failure
shape.

These tests pin every link of the chain, so a future edit cannot quietly cut
one and leave the contract looking wired.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.declared_symbols import missing_declared_symbols
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    _declared_symbols_from_evidence,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.roadmap_reader import (
    GoalPriority,
    RoadmapGoal,
    _make_envelope_for_goal,
)

SYM = "_tracer_auth_recheck_s"


def _goal(symbols=(SYM,), files=("backend/core/ouroboros/governance/dw_capacity_probe.py",)):
    return RoadmapGoal(
        goal_id="g-decl", title="t", description="d", priority=GoalPriority.HIGH,
        target_files=tuple(files), success_criteria="s", depends_on=(),
        max_duration_s=60, target_symbols=tuple(symbols),
    )


# --------------------------------------------------------------------------
# link 1 — the goal's symbols ride the envelope
# --------------------------------------------------------------------------

def test_the_envelope_carries_the_goals_declared_symbols():
    env = _make_envelope_for_goal(_goal())
    assert env is not None
    assert env.evidence.get("target_symbols") == [SYM]


def test_a_goal_declaring_nothing_adds_no_key():
    env = _make_envelope_for_goal(_goal(symbols=()))
    assert "target_symbols" not in (env.evidence or {})


def test_symbol_names_are_bounded():
    env = _make_envelope_for_goal(_goal(symbols=("x" * 400,)))
    assert len(env.evidence["target_symbols"][0]) == 128


# --------------------------------------------------------------------------
# link 2 — the router normalises what the envelope carried
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({"target_symbols": [SYM]}, (SYM,)),
    ({"target_symbols": (SYM, SYM)}, (SYM,)),            # de-duplicated
    ({"target_symbols": SYM}, (SYM,)),                    # a bare string is one name
    ({"target_symbols": [" a ", "", None, "b"]}, ("a", "b")),
    ({"target_symbols": {"not": "a list"}}, ()),
    ({"target_symbols": None}, ()),
    ({}, ()),
    (None, ()),
])
def test_the_router_normalises_the_payload(raw, expected):
    assert _declared_symbols_from_evidence(raw) == expected


def test_a_malformed_payload_declares_nothing_rather_than_raising():
    class _Hostile:
        def get(self, _k):
            raise RuntimeError("boom")

    assert _declared_symbols_from_evidence(_Hostile()) == ()


# --------------------------------------------------------------------------
# link 3 — the context carries them to every consumer
# --------------------------------------------------------------------------

def test_the_context_carries_the_symbols():
    ctx = OperationContext.create(
        target_files=("a.py",), description="d", target_symbols=(SYM,),
    )
    assert ctx.target_symbols == (SYM,)


def test_an_op_that_declares_nothing_still_builds():
    assert OperationContext.create(target_files=("a.py",), description="d").target_symbols == ()


def test_the_symbols_survive_a_phase_advance():
    """Consumers read the ctx many phases after intake built it."""
    ctx = OperationContext.create(
        target_files=("a.py",), description="d", target_symbols=(SYM,),
    )
    assert dataclasses.replace(ctx, description="later").target_symbols == (SYM,)


def test_the_context_hash_stays_deterministic():
    a = OperationContext.create(target_files=("a.py",), description="d", op_id="op-x")
    b = OperationContext.create(
        target_files=("a.py",), description="d", op_id="op-x", _timestamp=a.created_at,
    )
    assert a.context_hash == b.context_hash


# --------------------------------------------------------------------------
# the whole chain — the claim the contract exists to refuse
# --------------------------------------------------------------------------

def test_a_hallucinated_noop_is_now_refusable_end_to_end():
    """Goal -> envelope -> router -> ctx -> the gap the refusal fires on."""
    goal = _goal()
    env = _make_envelope_for_goal(goal)
    ctx = OperationContext.create(
        target_files=env.target_files,
        target_symbols=_declared_symbols_from_evidence(env.evidence),
        description=env.description,
    )
    gap = missing_declared_symbols(
        getattr(ctx, "target_symbols", ()) or (), ctx.target_files,
        Path(__file__).resolve().parents[2],
    )
    assert gap == (SYM,), "the no-op claim must be refusable — this is the whole contract"


def test_a_symbol_that_does_exist_is_not_a_gap():
    goal = _goal(symbols=("_tracer_timeout_s",))
    env = _make_envelope_for_goal(goal)
    ctx = OperationContext.create(
        target_files=env.target_files,
        target_symbols=_declared_symbols_from_evidence(env.evidence),
        description=env.description,
    )
    assert missing_declared_symbols(
        ctx.target_symbols, ctx.target_files, Path(__file__).resolve().parents[2],
    ) == ()


def test_the_router_wires_the_context_at_its_one_seam():
    """Pin the call site: a producer that stops producing is the bug itself."""
    import inspect

    from backend.core.ouroboros.governance.intake import unified_intake_router as R

    src = inspect.getsource(R.UnifiedIntakeRouter._dispatch_one)
    assert "target_symbols=_decl_symbols" in src
    assert "_declared_symbols_from_evidence(envelope.evidence)" in src


def test_a_resumed_op_still_carries_the_contract():
    """A suspension must not launder a goal out of its declared symbols."""
    import json

    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        _resume_envelope_kwargs,
    )

    kwargs = _resume_envelope_kwargs({
        "op_id": "op-1",
        "resume_phase": "GENERATE",
        "intake_evidence_json": json.dumps({"goal_id": "g", "target_symbols": [SYM]}),
    })
    assert _declared_symbols_from_evidence(kwargs["evidence"]) == (SYM,)
