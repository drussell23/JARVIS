"""An L3 work unit must inherit its parent op's ADMITTED state.

The unit's ``OperationContext`` was built bare — target_files, description,
op_id, repo — so two contracts that look wired were unreachable for every
subagent unit ever run:

* ``telemetry is None`` -> ``providers._ctx_schema_capability`` answers
  ``full_content_only``, so ``single_file_diff_requested`` is False no matter
  what the served model can do. The 2b.1-diff schema could not be reached on
  the subagent path at all — the same shape ``submit_background`` had at the
  op level (89a9166e05).
* ``target_symbols == ()`` and no intake evidence -> the declared-symbol
  refusal, read on this path by ``_validate_in_tree``'s differential gate and
  by ``_declared_symbols_for``, had nothing to refuse.

The executor's only inputs are ``(graph, unit)`` and graphs are persisted and
replayed after a restart, so the parent's state rides the graph. These tests
pin every link: the stamp, the persistence round-trip, the digest's
indifference to it, the unit context the executor actually builds, and the
two consumers that were vacuous.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.parent_inheritance import (
    goal_pointer_for,
    inherit_into,
    inherited_create_kwargs,
    stamp_parent_context,
    telemetry_from_json,
    telemetry_to_json,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
    execution_graph_from_dict,
    execution_graph_to_dict,
)
from backend.core.ouroboros.governance.op_context import (
    HostTelemetry,
    OperationContext,
    RoutingIntentTelemetry,
    TelemetryContext,
)

GOAL_ID = "ov-prod-some-goal-v2"
SYMS = ("_tracer_auth_recheck_s", "_tracer_timeout_s")
TARGET = "backend/core/ouroboros/governance/dw_capacity_probe.py"


def _telemetry(capability: str = "full_content_and_diff") -> TelemetryContext:
    return TelemetryContext(
        local_node=HostTelemetry(
            schema_version="1.0", arch="x86_64", cpu_percent=11.5,
            ram_available_gb=42.25, pressure="NORMAL",
            sampled_at_utc="2026-09-08T02:00:00+00:00",
            sampled_monotonic_ns=123456789, collector_status="ok", sample_age_ms=7,
        ),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="LOCAL_OV", policy_reason="PRIMARY_AVAILABLE",
            brain_id="qwen_coder_30b", brain_model="qwen3-coder-ov:30b",
            routing_reason="task_gate_heavy_code", task_complexity="heavy_code",
            schema_capability=capability, served_model="qwen3-coder-ov:30b",
        ),
    )


def _parent_ctx(*, telemetry: bool = True, goal_id: str = GOAL_ID) -> OperationContext:
    ctx = OperationContext.create(
        target_files=(TARGET,),
        description="add the auth back-off guard",
        target_symbols=SYMS,
        op_id="op-parent",
        intake_evidence_json=json.dumps({"goal_id": goal_id, "target_symbols": list(SYMS)}),
    )
    return ctx.with_telemetry(_telemetry()) if telemetry else ctx


def _unit(unit_id: str = "u1") -> WorkUnitSpec:
    return WorkUnitSpec(unit_id=unit_id, repo="jarvis", goal="g", target_files=(TARGET,))


def _graph(**kwargs) -> ExecutionGraph:
    base = dict(
        graph_id="g1", op_id="op-parent", planner_id="p1", schema_version="2d.1",
        units=(_unit(),), concurrency_limit=1,
    )
    base.update(kwargs)
    return ExecutionGraph(**base)


# --------------------------------------------------------------------------
# link 1 — the graph carries the parent, and defaults stay empty
# --------------------------------------------------------------------------

def test_an_unstamped_graph_carries_nothing():
    g = _graph()
    assert g.goal_id == ""
    assert g.target_symbols == ()
    assert g.parent_telemetry_json == ""
    assert g.parent_intake_evidence_json == ""


def test_stamping_carries_pointer_symbols_and_telemetry():
    g = stamp_parent_context(_graph(), _parent_ctx())
    assert g.goal_id == GOAL_ID
    assert g.target_symbols == SYMS
    assert g.parent_telemetry_json
    assert g.parent_intake_evidence_json


def test_a_context_with_nothing_to_give_leaves_the_graph_untouched():
    bare = OperationContext.create(target_files=(TARGET,), description="d")
    g = _graph()
    assert stamp_parent_context(g, bare) is g


@pytest.mark.parametrize("junk", [None, object(), "not-a-context", 17])
def test_stamping_never_raises(junk):
    g = _graph()
    assert stamp_parent_context(g, junk) is not None


def test_stamping_does_not_change_the_plan():
    """Two graphs differing ONLY in what they inherited are the same plan —
    the digest enumerates its own fields, so coalescing/dedup are unaffected."""
    plain = _graph()
    stamped = stamp_parent_context(_graph(), _parent_ctx())
    assert stamped.plan_digest == plain.plan_digest
    assert stamped.causal_trace_id == plain.causal_trace_id
    assert stamped.units == plain.units


# --------------------------------------------------------------------------
# link 2 — it survives persistence, because graphs are replayed after restart
# --------------------------------------------------------------------------

def test_inheritance_round_trips_through_the_store():
    stamped = stamp_parent_context(_graph(), _parent_ctx())
    restored = execution_graph_from_dict(
        json.loads(json.dumps(execution_graph_to_dict(stamped)))
    )
    assert restored.goal_id == stamped.goal_id
    assert restored.target_symbols == stamped.target_symbols
    assert restored.parent_telemetry_json == stamped.parent_telemetry_json
    assert restored.parent_intake_evidence_json == stamped.parent_intake_evidence_json


def test_a_graph_persisted_before_inheritance_existed_still_loads():
    payload = execution_graph_to_dict(_graph())
    for key in (
        "goal_id", "target_symbols",
        "parent_telemetry_json", "parent_intake_evidence_json",
    ):
        payload.pop(key)
    restored = execution_graph_from_dict(payload)
    assert restored.goal_id == ""
    assert restored.target_symbols == ()


# --------------------------------------------------------------------------
# link 3 — telemetry survives the JSON hop with the field that matters
# --------------------------------------------------------------------------

def test_telemetry_round_trips_the_served_capability():
    restored = telemetry_from_json(telemetry_to_json(_telemetry()))
    assert restored is not None
    assert restored.routing_intent.schema_capability == "full_content_and_diff"
    assert restored.routing_intent.served_model == "qwen3-coder-ov:30b"
    assert restored.routing_intent.brain_id == "qwen_coder_30b"
    assert restored.local_node.arch == "x86_64"


def test_a_field_a_future_build_added_is_dropped_not_fatal():
    data = json.loads(telemetry_to_json(_telemetry()))
    data["routing_intent"]["a_field_from_the_future"] = 1
    restored = telemetry_from_json(json.dumps(data))
    assert restored is not None
    assert restored.routing_intent.schema_capability == "full_content_and_diff"


@pytest.mark.parametrize("raw", ["", "{", "null", '{"local_node": {}}', "[]"])
def test_undecodable_telemetry_is_absent_not_an_exception(raw):
    assert telemetry_from_json(raw) is None


def test_the_goal_pointer_prefers_a_provenance_claim():
    ctx = OperationContext.create(
        target_files=(TARGET,), description="d",
        intake_evidence_json=json.dumps(
            {"goal_id": "roadmap-id", "provenance": {"goal_id": "claim-id"}}
        ),
    )
    assert goal_pointer_for(ctx) == "claim-id"


# --------------------------------------------------------------------------
# link 4 — the unit context the executor builds
# --------------------------------------------------------------------------

def test_the_unit_context_inherits_symbols_and_evidence():
    g = stamp_parent_context(_graph(), _parent_ctx())
    subctx = OperationContext.create(
        target_files=(TARGET,), description="g", op_id="op-parent:u1",
        **inherited_create_kwargs(g),
    )
    assert subctx.target_symbols == SYMS
    assert subctx.intake_evidence.get("goal_id") == GOAL_ID


def test_an_unstamped_graph_yields_the_pre_inheritance_call():
    assert inherited_create_kwargs(_graph()) == {}


def test_the_unit_context_inherits_the_routing_intent():
    g = stamp_parent_context(_graph(), _parent_ctx())
    subctx = inherit_into(
        OperationContext.create(target_files=(TARGET,), description="g"), g,
    )
    assert subctx.telemetry is not None
    assert subctx.telemetry.routing_intent.served_model == "qwen3-coder-ov:30b"


def test_inheriting_advances_the_hash_chain():
    g = stamp_parent_context(_graph(), _parent_ctx())
    before = OperationContext.create(target_files=(TARGET,), description="g")
    after = inherit_into(before, g)
    assert after.context_hash != before.context_hash
    assert after.previous_hash == before.context_hash


def test_inheriting_from_an_unstamped_graph_is_a_no_op():
    before = OperationContext.create(target_files=(TARGET,), description="g")
    assert inherit_into(before, _graph()) is before


# --------------------------------------------------------------------------
# link 5 — the two consumers that were vacuous now see something
# --------------------------------------------------------------------------

def test_the_2b1_diff_schema_is_reachable_for_a_unit():
    """``_ctx_schema_capability`` is the gate ``single_file_diff_requested``
    consults. On a bare unit context it could only ever answer
    ``full_content_only`` — the diff schema was unreachable by construction."""
    from backend.core.ouroboros.governance.providers import _ctx_schema_capability

    bare = OperationContext.create(target_files=(TARGET,), description="g")
    assert _ctx_schema_capability(bare) == "full_content_only"

    g = stamp_parent_context(_graph(), _parent_ctx())
    assert _ctx_schema_capability(inherit_into(bare, g)) == "full_content_and_diff"


def test_the_differential_gate_can_protect_the_declared_symbols():
    """``acceptance_names`` is what ``_validate_in_tree`` feeds the
    differential verdict; with ``target_symbols=()`` it protected nothing."""
    from backend.core.ouroboros.governance.differential_validation import acceptance_names

    g = stamp_parent_context(_graph(), _parent_ctx())
    subctx = OperationContext.create(
        target_files=(TARGET,), description="g", **inherited_create_kwargs(g),
    )
    assert acceptance_names("g", getattr(subctx, "target_symbols", ()))


# --------------------------------------------------------------------------
# link 6 — the executor's LIVE call site, not a reconstruction of it
# --------------------------------------------------------------------------

class _CapturingGenerator:
    """Records the context the executor hands GENERATE, then answers no-op so
    the unit returns before validation."""

    def __init__(self) -> None:
        self.seen = None

    async def generate(self, ctx, deadline):
        self.seen = ctx

        class _Noop:
            is_noop = True
            candidates = ()
            cost_usd = None

        return _Noop()


def _execute(graph: ExecutionGraph, repo_root: Path) -> object:
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        GenerationSubagentExecutor,
    )

    generator = _CapturingGenerator()
    executor = GenerationSubagentExecutor(
        generator=generator,
        validation_runner=None,
        repo_roots={"jarvis": repo_root},
        worktree_manager=None,
    )
    asyncio.run(executor.execute(graph, graph.units[0]))
    return generator.seen


def test_the_executor_hands_generate_the_inherited_context(tmp_path):
    seen = _execute(stamp_parent_context(_graph(), _parent_ctx()), tmp_path)
    assert seen is not None, "the executor never reached GENERATE"
    assert seen.op_id == "op-parent:u1"
    assert seen.target_symbols == SYMS
    assert seen.intake_evidence.get("goal_id") == GOAL_ID
    assert seen.telemetry is not None
    assert seen.telemetry.routing_intent.schema_capability == "full_content_and_diff"


def test_the_executor_still_runs_a_unit_whose_graph_inherited_nothing(tmp_path):
    seen = _execute(_graph(), tmp_path)
    assert seen is not None
    assert seen.op_id == "op-parent:u1"
    assert seen.target_symbols == ()
    assert seen.telemetry is None
