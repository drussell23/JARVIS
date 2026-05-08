"""Phase 3 A3 — AutonomyCommandBusBridge test spine.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A3: CommandBus advisory commands → IDE stream new event
   type — rate-limited, CORS/loopback same as existing
   observability slices."

Pinned coverage (~38 tests):
  * Master flag default-FALSE per §33.1
  * Poll interval clamps [0.5, 60.0]
  * Recorder no-op when master off
  * Frozen CommandBusSnapshotRecord round-trip
  * Schema mismatch → from_dict returns None
  * compute_delta: first poll (prev=None) emits baseline
  * compute_delta: identical poll → empty (chatter suppress)
  * compute_delta: increment → non-empty per-counter delta
  * compute_delta: per-command-type increments use
    ``cmd:<TYPE>`` keys
  * compute_delta: defensive on malformed input
  * record_snapshot end-to-end: persist + delta + chatter
  * record_snapshot composes canonical broker (single emit
    per delta)
  * read_recent_records ordering + limit + missing-file
  * 7 AST pins clean (parametrized) + targeted regression
    fires:
      - master_default_false
      - authority_asymmetry (synthetic regression)
      - read_only (synthetic: bus.put / bus.get forbidden)
      - composes_canonical_bus (synthetic: missing import)
      - composes_canonical_publisher (synthetic: direct
        broker.publish forbidden)
      - composes_canonical_jsonl (synthetic: raw open(..a))
      - chatter_suppression (synthetic: missing early-return)
  * Public API surface complete + register_flags seeds 3
  * Singleton lifecycle
  * start_default_bridge no-op when master off
  * Broker extension: EVENT_TYPE_AUTONOMY_COMMAND_BUS in
    frozen set + publish helper exists
  * End-to-end: real CommandBus.snapshot_all() yields
    polled metrics
"""
from __future__ import annotations

import ast
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "autonomy_command_bus_bridge.py"
    )


@pytest.fixture
def tmp_ledger(monkeypatch):
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        reset_default_bridge_for_test,
    )
    reset_default_bridge_for_test()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "cmdbus.jsonl"
        monkeypatch.setenv(
            "JARVIS_COMMAND_BUS_BRIDGE_LEDGER_PATH",
            str(ledger),
        )
        yield ledger
    reset_default_bridge_for_test()


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_poll_interval_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMMAND_BUS_BRIDGE_POLL_S", raising=False,
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        poll_interval_s,
    )
    assert poll_interval_s() == 2.0


def test_poll_interval_clamps(monkeypatch):
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        poll_interval_s,
    )
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_POLL_S", "0.1",
    )
    assert poll_interval_s() == 0.5  # floor
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_POLL_S", "9999",
    )
    assert poll_interval_s() == 60.0  # ceiling
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_POLL_S", "junk",
    )
    assert poll_interval_s() == 2.0  # fallback


# ---------------------------------------------------------------------------
# compute_delta — pure chatter-suppression decision
# ---------------------------------------------------------------------------


def test_compute_delta_first_poll_baseline():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        compute_delta,
    )
    d = compute_delta(
        prev=None,
        curr={
            "total_dispatched": 5,
            "total_rejected_dedup": 2,
            "total_rejected_backpressure": 0,
            "by_command_type": {
                "REQUEST_MODE_SWITCH": 5,
            },
        },
    )
    assert d == {
        "total_dispatched": 5,
        "total_rejected_dedup": 2,
        "cmd:REQUEST_MODE_SWITCH": 5,
    }


def test_compute_delta_identical_poll_empty():
    """Operator binding: chatter suppression — identical
    poll yields empty delta → no SSE / no JSONL row."""
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        compute_delta,
    )
    snap = {
        "total_dispatched": 5,
        "total_rejected_dedup": 2,
        "total_rejected_backpressure": 0,
        "by_command_type": {"REQUEST_MODE_SWITCH": 5},
    }
    assert compute_delta(prev=snap, curr=snap) == {}


def test_compute_delta_increment_per_counter():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        compute_delta,
    )
    d = compute_delta(
        prev={
            "total_dispatched": 5,
            "total_rejected_dedup": 2,
            "total_rejected_backpressure": 0,
            "by_command_type": {"REQUEST_MODE_SWITCH": 5},
        },
        curr={
            "total_dispatched": 8,
            "total_rejected_dedup": 2,
            "total_rejected_backpressure": 1,
            "by_command_type": {
                "REQUEST_MODE_SWITCH": 7,
                "GENERATE_BACKLOG_ENTRY": 1,
            },
        },
    )
    assert d == {
        "total_dispatched": 3,
        "total_rejected_backpressure": 1,
        "cmd:REQUEST_MODE_SWITCH": 2,
        "cmd:GENERATE_BACKLOG_ENTRY": 1,
    }


def test_compute_delta_negative_decrement():
    """Counters are monotonic in production but decrement
    edge case (e.g. test reset) MUST surface as negative
    delta — operator wants to see the anomaly."""
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        compute_delta,
    )
    d = compute_delta(
        prev={"total_dispatched": 10},
        curr={"total_dispatched": 3},
    )
    assert d == {"total_dispatched": -7}


def test_compute_delta_defensive_on_malformed():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        compute_delta,
    )
    # Empty curr → empty delta
    assert compute_delta(prev={}, curr={}) == {}
    # Missing key on prev → treated as 0
    d = compute_delta(
        prev={},
        curr={"total_dispatched": 3},
    )
    assert d == {"total_dispatched": 3}


# ---------------------------------------------------------------------------
# Recorder gating
# ---------------------------------------------------------------------------


def test_recorder_noop_when_master_off(
    monkeypatch, tmp_ledger,
):
    monkeypatch.delenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        record_snapshot,
    )
    out = record_snapshot(
        snapshot={"total_dispatched": 5},
        ledger_path_override=tmp_ledger,
    )
    assert out is None


def test_recorder_emits_baseline(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        read_recent_records, record_snapshot,
    )
    rec = record_snapshot(
        snapshot={
            "instance_count": 3,
            "total_qsize": 2,
            "total_dispatched": 10,
            "total_rejected_dedup": 1,
            "total_rejected_backpressure": 0,
            "by_command_type": {
                "REQUEST_MODE_SWITCH": 6,
                "ADJUST_BRAIN_HINT": 4,
            },
        },
        prev_snapshot=None,
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    assert rec.total_dispatched == 10
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1


def test_recorder_chatter_suppresses_identical(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        read_recent_records, record_snapshot,
    )
    snap = {
        "instance_count": 1,
        "total_qsize": 0,
        "total_dispatched": 5,
        "total_rejected_dedup": 0,
        "total_rejected_backpressure": 0,
        "by_command_type": {"REQUEST_MODE_SWITCH": 5},
    }
    record_snapshot(
        snapshot=snap, prev_snapshot=None,
        ledger_path_override=tmp_ledger,
    )
    # Re-poll identical → MUST suppress
    out = record_snapshot(
        snapshot=snap, prev_snapshot=snap,
        ledger_path_override=tmp_ledger,
    )
    assert out is None
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1  # only baseline


def test_recorder_emits_on_dispatch_increment(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        read_recent_records, record_snapshot,
    )
    prev = {
        "total_dispatched": 5, "total_rejected_dedup": 0,
        "total_rejected_backpressure": 0,
        "by_command_type": {"REQUEST_MODE_SWITCH": 5},
    }
    rec = record_snapshot(
        snapshot={
            "total_dispatched": 7,
            "total_rejected_dedup": 0,
            "total_rejected_backpressure": 0,
            "by_command_type": {
                "REQUEST_MODE_SWITCH": 7,
            },
        },
        prev_snapshot=prev,
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    assert rec.delta == {
        "total_dispatched": 2,
        "cmd:REQUEST_MODE_SWITCH": 2,
    }


# ---------------------------------------------------------------------------
# Frozen artifact
# ---------------------------------------------------------------------------


def test_record_round_trip():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        CommandBusSnapshotRecord,
    )
    rec = CommandBusSnapshotRecord(
        instance_count=2,
        total_qsize=3,
        total_dispatched=10,
        total_rejected_dedup=1,
        total_rejected_backpressure=0,
        by_command_type={"REQUEST_MODE_SWITCH": 10},
        delta={"total_dispatched": 3},
        ts_unix=12345.0,
    )
    rt = CommandBusSnapshotRecord.from_dict(rec.to_dict())
    assert rt is not None
    assert rt.instance_count == 2
    assert rt.delta["total_dispatched"] == 3


def test_record_schema_mismatch_returns_none():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        CommandBusSnapshotRecord,
    )
    out = CommandBusSnapshotRecord.from_dict(
        {"schema_version": "wrong"},
    )
    assert out is None


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def test_read_missing_ledger_returns_empty(tmp_path):
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        read_recent_records,
    )
    nonexistent = tmp_path / "no-such.jsonl"
    assert read_recent_records(path=nonexistent) == ()


def test_read_recent_records_limit(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        read_recent_records, record_snapshot,
    )
    # Emit 5 distinct deltas
    prev = None
    for i in range(5):
        snap = {
            "total_dispatched": (i + 1) * 10,
            "total_rejected_dedup": 0,
            "total_rejected_backpressure": 0,
            "by_command_type": {},
        }
        record_snapshot(
            snapshot=snap, prev_snapshot=prev,
            ledger_path_override=tmp_ledger,
        )
        prev = snap
    rows = read_recent_records(limit=3, path=tmp_ledger)
    assert len(rows) == 3
    # Last 3 → total_dispatched 30, 40, 50
    assert rows[0].total_dispatched == 30
    assert rows[2].total_dispatched == 50


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "autonomy_command_bus_bridge_master_default_false",
        "autonomy_command_bus_bridge_authority_asymmetry",
        "autonomy_command_bus_bridge_read_only",
        (
            "autonomy_command_bus_bridge_"
            "composes_canonical_bus"
        ),
        (
            "autonomy_command_bus_bridge_"
            "composes_canonical_publisher"
        ),
        (
            "autonomy_command_bus_bridge_"
            "composes_canonical_jsonl"
        ),
        (
            "autonomy_command_bus_bridge_"
            "chatter_suppression"
        ),
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "autonomy_command_bus_bridge_"
            "authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_read_only_pin_fires_on_bus_put():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
async def hostile():
    bus = get_some_bus()
    await bus.put(some_envelope)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "autonomy_command_bus_bridge_read_only"
        )
    )
    assert pin.validate(tree, bad)


def test_read_only_pin_fires_on_bus_get():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
async def hostile():
    bus = get_some_bus()
    cmd = await bus.get()
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "autonomy_command_bus_bridge_read_only"
        )
    )
    assert pin.validate(tree, bad)


def test_publisher_pin_fires_on_direct_publish():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def emit():
    broker = get_broker()
    broker.publish("event", "channel", {})
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "autonomy_command_bus_bridge_"
            "composes_canonical_publisher"
        )
    )
    assert pin.validate(tree, bad)


def test_jsonl_pin_fires_on_raw_open():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
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
            "autonomy_command_bus_bridge_"
            "composes_canonical_jsonl"
        )
    )
    assert pin.validate(tree, bad)


def test_chatter_pin_fires_on_missing_early_return():
    """Synthetic regression: record_snapshot without
    ``if not delta:`` early-return MUST trip the chatter
    pin."""
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def record_snapshot(*, snapshot, prev_snapshot=None, **kwargs):
    delta = compute_delta(prev=prev_snapshot, curr=snapshot)
    # NO early-return — would emit on every poll → SSE flood
    return build_record(delta)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "autonomy_command_bus_bridge_"
            "chatter_suppression"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (  # noqa: E501
        autonomy_command_bus_bridge as mod,
    )
    expected = {
        (
            "AUTONOMY_COMMAND_BUS_BRIDGE_SCHEMA_VERSION"
        ),
        "AutonomyCommandBusBridge",
        "CommandBusSnapshotRecord",
        "compute_delta",
        "get_default_bridge",
        "ledger_path",
        "master_enabled",
        "poll_interval_s",
        "read_recent_records",
        "record_snapshot",
        "register_flags",
        "register_shipped_invariants",
        "reset_default_bridge_for_test",
        "start_default_bridge",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_three():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
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
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED",
        "JARVIS_COMMAND_BUS_BRIDGE_POLL_S",
        "JARVIS_COMMAND_BUS_BRIDGE_LEDGER_PATH",
    }


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


def test_singleton_lifecycle():
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        AutonomyCommandBusBridge,
        get_default_bridge,
        reset_default_bridge_for_test,
    )
    reset_default_bridge_for_test()
    b1 = get_default_bridge()
    b2 = get_default_bridge()
    assert b1 is b2
    assert isinstance(b1, AutonomyCommandBusBridge)
    reset_default_bridge_for_test()
    b3 = get_default_bridge()
    assert b3 is not b1


@pytest.mark.asyncio
async def test_start_default_bridge_master_off(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        start_default_bridge,
    )
    assert start_default_bridge() is None


# ---------------------------------------------------------------------------
# Broker extension regression
# ---------------------------------------------------------------------------


def test_broker_event_type_in_frozen_set():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_AUTONOMY_COMMAND_BUS,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_AUTONOMY_COMMAND_BUS == (
        "autonomy_command_bus"
    )
    assert (
        EVENT_TYPE_AUTONOMY_COMMAND_BUS
        in _VALID_EVENT_TYPES
    )


def test_publish_helper_master_off_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_autonomy_command_bus_event,
    )
    out = publish_autonomy_command_bus_event(
        instance_count=1, total_qsize=0,
        total_dispatched=5, total_rejected_dedup=0,
        total_rejected_backpressure=0,
        by_command_type={"REQUEST_MODE_SWITCH": 5},
        delta={"total_dispatched": 5},
        ts_unix=0.0,
    )
    assert out is None


# ---------------------------------------------------------------------------
# End-to-end: real CommandBus.snapshot_all() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_command_bus_snapshot_polled(
    monkeypatch, tmp_ledger,
):
    """The bridge polls the real
    :meth:`CommandBus.snapshot_all` classmethod. Construct
    a CommandBus instance + enqueue a couple of envelopes
    + verify the bridge sees the deltas."""
    monkeypatch.setenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (  # noqa: E501
        CommandEnvelope,
        CommandType,
    )
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        record_snapshot,
    )

    CommandBus.reset_instance_registry_for_tests()
    bus = CommandBus(maxsize=16)

    # Empty bus baseline
    snap_before = CommandBus.snapshot_all()
    assert snap_before["total_dispatched"] == 0

    # Enqueue a command
    env = CommandEnvelope(
        source_layer="L2",
        target_layer="L1",
        command_type=CommandType.REQUEST_MODE_SWITCH,
        payload={"to": "DEGRADED"},
        ttl_s=10.0,
    )
    assert await bus.put(env) is True

    snap_after = CommandBus.snapshot_all()
    assert snap_after["total_dispatched"] == 1

    rec = record_snapshot(
        snapshot=snap_after,
        prev_snapshot=snap_before,
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    assert rec.delta.get("total_dispatched") == 1
    assert (
        rec.delta.get("cmd:request_mode_switch") == 1
    )

    # Test cleanup
    CommandBus.reset_instance_registry_for_tests()


@pytest.mark.asyncio
async def test_consume_poll_loop_master_off_returns(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.autonomy_command_bus_bridge import (  # noqa: E501
        AutonomyCommandBusBridge,
    )
    bridge = AutonomyCommandBusBridge()
    # MUST return immediately when master flag off
    await asyncio.wait_for(
        bridge.consume_poll_loop(), timeout=2.0,
    )
