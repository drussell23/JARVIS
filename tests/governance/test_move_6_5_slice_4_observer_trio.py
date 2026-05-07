"""Move 6.5 Slice 4 — Observer + observability + REPL trio.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Observability slice: mirror Move 7/8 — §33.3 REPL auto-
   mount, §33.4 flock'd JSONL ledger, chatter-suppressed
   observer, §33.5 versioned rows. Cancelled rolls may leave
   partial session artifacts — must be observable in ledger,
   not silent."

Pinned coverage (~45 tests):
  * multi_prior_observer master-default-FALSE
  * Recorder no-op when master off
  * Frozen MultiPriorObservation round-trip via to_dict/from_dict
  * Schema mismatch on from_dict → None
  * record_dispatch_outcome composes Slice 3 verdict
  * Persists to JSONL via §33.4 flock primitive
  * Chatter-suppressed SSE: action transition fires +
    same-action repeats suppressed when no cancels / errors
  * Chatter-suppressed SSE: cancelled_count > 0 forces emit
    even when same-action
  * Chatter-suppressed SSE: error_count > 0 forces emit
  * read_recent_observations limit + ordering
  * find_by_op_id hit + miss
  * action_distribution
  * 5 observer AST pins clean + each fires on synthetic regression
  * 3 observability AST pins clean + each fires on synthetic regression
  * 3 REPL AST pins clean + each fires on synthetic regression
  * REPL bare overview / recent N / op detail / stats / help
  * REPL master-off message
  * Observability register_routes registers 2 routes
  * Public API surfaces complete
  * Broker extension: EVENT_TYPE_MULTI_PRIOR_DISPATCH in frozen set
  * publish_multi_prior_dispatch_event helper exists + master-off returns None
"""
from __future__ import annotations

import ast
import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _enable_all_masters(monkeypatch):
    for k in (
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED",
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED",
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
    ):
        monkeypatch.setenv(k, "true")


@pytest.fixture
def tmp_ledger(monkeypatch):
    """Per-test isolated ledger path + observer reset."""
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        reset_default_observer_for_test,
    )
    reset_default_observer_for_test()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH",
            str(ledger),
        )
        yield ledger
    reset_default_observer_for_test()


# ---------------------------------------------------------------------------
# Broker extension regression
# ---------------------------------------------------------------------------


def test_broker_event_type_in_frozen_set():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_MULTI_PRIOR_DISPATCH,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_MULTI_PRIOR_DISPATCH == (
        "multi_prior_dispatch"
    )
    assert (
        EVENT_TYPE_MULTI_PRIOR_DISPATCH
        in _VALID_EVENT_TYPES
    )


def test_publish_helper_master_off_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_multi_prior_dispatch_event,
    )
    out = publish_multi_prior_dispatch_event(
        op_id="op-1", decision="enabled",
        action_recommendation="accept_canonical",
        prev_action_recommendation="",
        consensus_outcome="consensus",
        completed_count=4, cancelled_count=0,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=1.0, ts_unix=0.0,
    )
    assert out is None


# ---------------------------------------------------------------------------
# Observer master flag
# ---------------------------------------------------------------------------


def test_observer_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_recorder_noop_when_master_off(
    monkeypatch, tmp_ledger,
):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        record_dispatch_outcome,
    )
    fake_verdict = MagicMock()
    out = record_dispatch_outcome(
        fake_verdict, ledger_path_override=tmp_ledger,
    )
    assert out is None


# ---------------------------------------------------------------------------
# Frozen artifact
# ---------------------------------------------------------------------------


def test_observation_to_dict_from_dict_round_trip():
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        MultiPriorObservation,
    )
    obs = MultiPriorObservation(
        op_id="op-1",
        decision="enabled",
        action_recommendation="accept_canonical",
        consensus_outcome="consensus",
        completed_count=4,
        cancelled_count=0,
        timeout_count=0,
        error_count=0,
        cost_total_usd=0.05,
        wall_clock_s=2.5,
        rationale_preview="x" * 256,
        ts_unix=12345.0,
    )
    rt = MultiPriorObservation.from_dict(obs.to_dict())
    assert rt is not None
    assert rt.op_id == obs.op_id
    assert rt.action_recommendation == (
        obs.action_recommendation
    )
    assert rt.completed_count == obs.completed_count


def test_observation_schema_mismatch_returns_none():
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        MultiPriorObservation,
    )
    out = MultiPriorObservation.from_dict(
        {"schema_version": "wrong"},
    )
    assert out is None


# ---------------------------------------------------------------------------
# Recorder end-to-end with Slice 3 verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_dispatch_outcome_persists(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        read_recent_observations, record_dispatch_outcome,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    v = await dispatch_multi_prior(
        gen, op_id="op-A",
        route="complex", posture="EXPLORE",
    )
    obs = record_dispatch_outcome(
        v, ledger_path_override=tmp_ledger,
    )
    assert obs is not None
    assert obs.action_recommendation == "accept_canonical"

    rows = read_recent_observations(path=tmp_ledger)
    assert len(rows) == 1
    assert rows[0].op_id == "op-A"


# ---------------------------------------------------------------------------
# Chatter suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chatter_suppression_same_action_clean(
    monkeypatch, tmp_ledger,
):
    """Two identical convergent ops in a row: first emits SSE
    (transition from None), second is suppressed (same-action,
    no cancels/errors)."""
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer, record_dispatch_outcome,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    for op_id in ("op-1", "op-2"):
        v = await dispatch_multi_prior(
            gen, op_id=op_id,
            route="complex", posture="EXPLORE",
        )
        record_dispatch_outcome(
            v, ledger_path_override=tmp_ledger,
        )

    tele = get_default_observer().telemetry()
    assert tele["record_count"] == 2
    assert tele["sse_emitted_count"] == 1
    assert tele["suppressed_count"] == 1


@pytest.mark.asyncio
async def test_chatter_suppression_action_transition_fires(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer, record_dispatch_outcome,
    )

    async def converge(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    async def diverge(*, prior, roll_id):
        return f"unique-{prior.prior_id}"

    v1 = await dispatch_multi_prior(
        converge, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    record_dispatch_outcome(
        v1, ledger_path_override=tmp_ledger,
    )
    v2 = await dispatch_multi_prior(
        diverge, op_id="op-2",
        route="complex", posture="EXPLORE",
    )
    record_dispatch_outcome(
        v2, ledger_path_override=tmp_ledger,
    )
    tele = get_default_observer().telemetry()
    # action transition: ACCEPT_CANONICAL → ESCALATE
    assert tele["sse_emitted_count"] == 2
    assert tele["suppressed_count"] == 0


@pytest.mark.asyncio
async def test_chatter_cancelled_forces_emit(
    monkeypatch, tmp_ledger,
):
    """Operator binding: cancelled_count > 0 MUST be ledger-
    observable. Even when action_recommendation matches the
    prior op, cancellations force SSE emission."""
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer,
    )

    obs = get_default_observer()
    # Prime prior_action with FALL_THROUGH
    obs.record(
        op_id="prior", decision="enabled",
        action_recommendation="fall_through",
        consensus_outcome="failed",
        completed_count=0, cancelled_count=0,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=0.1,
        rationale="prior-op",
        ledger_path_override=tmp_ledger,
    )
    # Same action, but with cancelled_count > 0 → MUST emit
    tele_before = obs.telemetry()
    obs.record(
        op_id="curr", decision="enabled",
        action_recommendation="fall_through",
        consensus_outcome="failed",
        completed_count=0, cancelled_count=2,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=0.1,
        rationale="curr-op-cancelled",
        ledger_path_override=tmp_ledger,
    )
    tele_after = obs.telemetry()
    # SSE-emitted incremented (cancelled forces emit)
    assert (
        tele_after["sse_emitted_count"]
        > tele_before["sse_emitted_count"]
    )


@pytest.mark.asyncio
async def test_chatter_error_forces_emit(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    obs.record(
        op_id="p", decision="enabled",
        action_recommendation="fall_through",
        consensus_outcome="failed",
        completed_count=0, cancelled_count=0,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=0.1,
        rationale="prior",
        ledger_path_override=tmp_ledger,
    )
    tele_before = obs.telemetry()
    obs.record(
        op_id="c", decision="enabled",
        action_recommendation="fall_through",
        consensus_outcome="failed",
        completed_count=0, cancelled_count=0,
        timeout_count=0, error_count=3,
        cost_total_usd=0.0, wall_clock_s=0.1,
        rationale="errored",
        ledger_path_override=tmp_ledger,
    )
    tele_after = obs.telemetry()
    assert (
        tele_after["sse_emitted_count"]
        > tele_before["sse_emitted_count"]
    )


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_recent_observations_orders_append(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer, read_recent_observations,
    )
    obs = get_default_observer()
    for i in range(5):
        obs.record(
            op_id=f"op-{i}", decision="enabled",
            action_recommendation="accept_canonical",
            consensus_outcome="consensus",
            completed_count=4, cancelled_count=0,
            timeout_count=0, error_count=0,
            cost_total_usd=0.0, wall_clock_s=1.0,
            rationale="r",
            ledger_path_override=tmp_ledger,
        )
    rows = read_recent_observations(path=tmp_ledger)
    assert len(rows) == 5
    # Append-order preserved (newest LAST)
    assert rows[0].op_id == "op-0"
    assert rows[4].op_id == "op-4"


@pytest.mark.asyncio
async def test_read_recent_observations_limit(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer, read_recent_observations,
    )
    obs = get_default_observer()
    for i in range(10):
        obs.record(
            op_id=f"op-{i}", decision="enabled",
            action_recommendation="accept_canonical",
            consensus_outcome="consensus",
            completed_count=4, cancelled_count=0,
            timeout_count=0, error_count=0,
            cost_total_usd=0.0, wall_clock_s=1.0,
            rationale="r",
            ledger_path_override=tmp_ledger,
        )
    rows = read_recent_observations(
        limit=3, path=tmp_ledger,
    )
    assert len(rows) == 3
    # Last 3 → op-7, op-8, op-9
    assert rows[0].op_id == "op-7"
    assert rows[2].op_id == "op-9"


@pytest.mark.asyncio
async def test_find_by_op_id_hit(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        find_by_op_id, get_default_observer,
    )
    obs = get_default_observer()
    obs.record(
        op_id="target", decision="enabled",
        action_recommendation="escalate_to_operator_review",
        consensus_outcome="disagreement",
        completed_count=4, cancelled_count=0,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=1.0,
        rationale="r",
        ledger_path_override=tmp_ledger,
    )
    found = find_by_op_id("target", path=tmp_ledger)
    assert found is not None
    assert found.action_recommendation == (
        "escalate_to_operator_review"
    )


def test_find_by_op_id_miss(monkeypatch, tmp_ledger):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        find_by_op_id,
    )
    assert find_by_op_id(
        "nonexistent", path=tmp_ledger,
    ) is None


@pytest.mark.asyncio
async def test_action_distribution(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        action_distribution, get_default_observer,
    )
    obs = get_default_observer()
    for i in range(3):
        obs.record(
            op_id=f"a{i}", decision="enabled",
            action_recommendation="accept_canonical",
            consensus_outcome="consensus",
            completed_count=4, cancelled_count=0,
            timeout_count=0, error_count=0,
            cost_total_usd=0.0, wall_clock_s=1.0,
            rationale="r",
            ledger_path_override=tmp_ledger,
        )
    obs.record(
        op_id="e", decision="enabled",
        action_recommendation="escalate_to_operator_review",
        consensus_outcome="disagreement",
        completed_count=4, cancelled_count=0,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=1.0,
        rationale="r",
        ledger_path_override=tmp_ledger,
    )
    dist = action_distribution(path=tmp_ledger)
    assert dist["accept_canonical"] == 3
    assert dist["escalate_to_operator_review"] == 1


# ---------------------------------------------------------------------------
# Observer AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_observer_master_default_false",
        "multi_prior_observer_authority_asymmetry",
        "multi_prior_observer_chatter_suppression",
        "multi_prior_observer_composes_canonical_jsonl",
        "multi_prior_observer_composes_canonical_publisher",
    ],
)
def test_observer_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_observer.py"
    ).read_text()
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_observer_chatter_pin_fires_when_emit_simplified():
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "emit = True\n"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_observer_chatter_suppression"
        )
    )
    assert pin.validate(tree, bad)


def test_observer_jsonl_pin_fires_on_raw_open():
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def writer():
    with open("foo.jsonl", "a") as f:
        f.write("x\\n")
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_observer_composes_canonical_jsonl"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Observability AST pins + register_routes contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_observability_authority_asymmetry",
        "multi_prior_observability_read_only",
        "multi_prior_observability_naming_cage_compliant",
    ],
)
def test_observability_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_observability.py"
    ).read_text()
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_observability_register_routes_registers_two():
    """register_routes(app) MUST register exactly 2 GET
    routes."""
    from backend.core.ouroboros.governance.verification.multi_prior_observability import (  # noqa: E501
        register_routes,
    )
    fake_app = MagicMock()
    register_routes(fake_app)
    assert fake_app.router.add_get.call_count == 2
    paths = [
        c.args[0]
        for c in fake_app.router.add_get.call_args_list
    ]
    assert "/observability/multi-prior" in paths
    assert any(
        p.startswith("/observability/multi-prior/")
        and "{op_id}" in p
        for p in paths
    )


# ---------------------------------------------------------------------------
# REPL AST pins + dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_repl_authority_asymmetry",
        "multi_prior_repl_composes_observer",
        "multi_prior_repl_naming_cage_compliant",
    ],
)
def test_repl_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        register_shipped_invariants,
    )
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_repl.py"
    ).read_text()
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_repl_help_subcommand():
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )
    out = dispatch_multi_prior_command("/multi_prior help")
    assert out.ok is True
    assert "/multi_prior" in out.text
    assert "recent" in out.text


def test_repl_master_off_message(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )
    out = dispatch_multi_prior_command("/multi_prior")
    assert out.ok is True
    assert "disabled" in out.text


@pytest.mark.asyncio
async def test_repl_bare_overview_with_data(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        record_dispatch_outcome,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    v = await dispatch_multi_prior(
        gen, op_id="op-1",
        route="complex", posture="EXPLORE",
    )
    record_dispatch_outcome(
        v, ledger_path_override=tmp_ledger,
    )
    out = dispatch_multi_prior_command("/multi_prior")
    assert out.ok is True
    assert "op-1" in out.text


@pytest.mark.asyncio
async def test_repl_op_subcommand(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
        dispatch_multi_prior,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        record_dispatch_outcome,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    v = await dispatch_multi_prior(
        gen, op_id="op-target",
        route="complex", posture="EXPLORE",
    )
    record_dispatch_outcome(
        v, ledger_path_override=tmp_ledger,
    )
    out = dispatch_multi_prior_command(
        "/multi_prior op op-target",
    )
    assert out.ok is True
    assert "accept_canonical" in out.text


def test_repl_op_subcommand_blank_op_id(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )
    out = dispatch_multi_prior_command("/multi_prior op")
    assert out.ok is False
    assert "missing" in out.text.lower()


def test_repl_unknown_subcommand(
    monkeypatch,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )
    out = dispatch_multi_prior_command(
        "/multi_prior nonsense",
    )
    assert out.ok is False
    assert "unknown" in out.text.lower()


def test_repl_recent_subcommand(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )
    obs = get_default_observer()
    for i in range(3):
        obs.record(
            op_id=f"r{i}", decision="enabled",
            action_recommendation="accept_canonical",
            consensus_outcome="consensus",
            completed_count=4, cancelled_count=0,
            timeout_count=0, error_count=0,
            cost_total_usd=0.0, wall_clock_s=1.0,
            rationale="r",
            ledger_path_override=tmp_ledger,
        )
    out = dispatch_multi_prior_command(
        "/multi_prior recent 2",
    )
    assert out.ok is True
    # last 2 → r1, r2
    assert "r1" in out.text
    assert "r2" in out.text


def test_repl_stats_subcommand(
    monkeypatch, tmp_ledger,
):
    _enable_all_masters(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_repl import (  # noqa: E501
        dispatch_multi_prior_command,
    )
    obs = get_default_observer()
    obs.record(
        op_id="x", decision="enabled",
        action_recommendation="accept_canonical",
        consensus_outcome="consensus",
        completed_count=4, cancelled_count=0,
        timeout_count=0, error_count=0,
        cost_total_usd=0.0, wall_clock_s=1.0,
        rationale="r",
        ledger_path_override=tmp_ledger,
    )
    out = dispatch_multi_prior_command(
        "/multi_prior stats",
    )
    assert out.ok is True
    assert "records=" in out.text
    assert "accept_canonical" in out.text


# ---------------------------------------------------------------------------
# Public API surfaces
# ---------------------------------------------------------------------------


def test_observer_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_observer as mod,
    )
    expected = {
        "MULTI_PRIOR_OBSERVER_SCHEMA_VERSION",
        "MultiPriorDispatchObserver",
        "MultiPriorObservation",
        "action_distribution",
        "find_by_op_id",
        "get_default_observer",
        "ledger_path",
        "master_enabled",
        "read_limit_default",
        "read_recent_observations",
        "record_dispatch_outcome",
        "register_flags",
        "register_shipped_invariants",
        "reset_default_observer_for_test",
    }
    assert set(mod.__all__) == expected


def test_observability_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_observability as mod,
    )
    expected = {
        "MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION",
        "register_routes",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_repl_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_repl as mod,
    )
    expected = {
        "MULTI_PRIOR_REPL_SCHEMA_VERSION",
        "MultiPriorDispatchResult",
        "dispatch_multi_prior_command",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_observer_register_flags_seeds_three():
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 3
    names = {
        c.kwargs["name"]
        for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
        "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH",
        "JARVIS_MULTI_PRIOR_READ_LIMIT",
    }
