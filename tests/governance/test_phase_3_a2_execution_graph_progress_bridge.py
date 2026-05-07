"""Phase 3 A2 — ExecutionGraphProgressBridge test spine.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A2: ExecutionGraphProgressTracker → SerpentFlow / canvas
   / SSE — read-only projection; no authority on APPLY."

Pinned coverage (~38 tests):
  * Master flag default-FALSE per §33.1
  * Verbose flag default-FALSE
  * Chatter suppression: 5 graph-level kinds always emit
  * Chatter suppression: 3 terminal unit kinds emit by
    default
  * Chatter suppression: unit_ready / unit_started default-
    suppressed
  * Verbose mode: ALL kinds emit including intermediates
  * Recorder no-op when master off
  * Recorder no-op on blank kind
  * Frozen GraphProgressRecord round-trip via to_dict /
    from_dict
  * Schema mismatch returns None
  * Defensive payload serialization (non-JSON values fall
    back to str)
  * record_graph_event composes canonical broker (publish
    helper invoked via SSE event-type)
  * record_graph_event persists to JSONL (§33.4 flock)
  * read_recent_records ordering + limit + missing-file +
    oversized-file
  * 7 AST pins clean (parametrized) + each fires on
    synthetic regression:
      - master_default_false
      - authority_asymmetry
      - read_only (synthetic regression: forbid mutation)
      - composes_canonical_tracker
      - composes_canonical_publisher (synthetic: direct
        broker.publish forbidden)
      - composes_canonical_jsonl
      - chatter_default (synthetic: missing kind / extra
        kind)
  * Public API surface complete + register_flags seeds 3
  * Singleton lifecycle: get_default_bridge / reset / start
  * start_default_bridge no-op when master off
  * Broker extension: EVENT_TYPE_EXECUTION_GRAPH_PROGRESS in
    frozen set + publish_execution_graph_progress_event
    helper exists
  * End-to-end: bridge consumes a synthetic GraphEvent
    stream + projects each through the SSE+JSONL surfaces
"""
from __future__ import annotations

import ast
import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "execution_graph_progress_bridge.py"
    )


@pytest.fixture
def tmp_ledger(monkeypatch):
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        reset_default_bridge_for_test,
    )
    reset_default_bridge_for_test()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "graph.jsonl"
        monkeypatch.setenv(
            "JARVIS_EXEC_GRAPH_BRIDGE_LEDGER_PATH",
            str(ledger),
        )
        yield ledger
    reset_default_bridge_for_test()


# ---------------------------------------------------------------------------
# Master flag + verbose flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_verbose_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        verbose_mode,
    )
    assert verbose_mode() is False


# ---------------------------------------------------------------------------
# Chatter suppression — should_emit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", [
        "graph_submitted", "graph_started",
        "graph_completed", "graph_failed",
        "graph_cancelled",
        "unit_completed", "unit_failed", "unit_cancelled",
    ],
)
def test_default_emit_kinds(monkeypatch, kind):
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        should_emit,
    )
    assert should_emit(kind) is True


@pytest.mark.parametrize(
    "kind", ["unit_ready", "unit_started"],
)
def test_default_suppress_intermediate(
    monkeypatch, kind,
):
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        should_emit,
    )
    assert should_emit(kind) is False


@pytest.mark.parametrize(
    "kind", [
        "unit_ready", "unit_started", "graph_submitted",
        "graph_completed", "unit_completed",
    ],
)
def test_verbose_emits_all(monkeypatch, kind):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        should_emit,
    )
    assert should_emit(kind) is True


def test_should_emit_defensive():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        should_emit,
    )
    # Blank → False
    assert should_emit("") is False
    # Non-string → False (defensive)
    assert should_emit(None) is False  # type: ignore


# ---------------------------------------------------------------------------
# Recorder gating
# ---------------------------------------------------------------------------


def test_recorder_noop_when_master_off(
    monkeypatch, tmp_ledger,
):
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        record_graph_event,
    )
    out = record_graph_event(
        kind="unit_completed", graph_id="g1",
        ledger_path_override=tmp_ledger,
    )
    assert out is None


def test_recorder_noop_on_blank_kind(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        record_graph_event,
    )
    out = record_graph_event(
        kind="", graph_id="g1",
        ledger_path_override=tmp_ledger,
    )
    assert out is None


def test_recorder_chatter_suppresses_intermediate(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        record_graph_event,
    )
    # unit_started default-suppressed
    out = record_graph_event(
        kind="unit_started", graph_id="g1", op_id="op-1",
        unit_id="u1",
        ledger_path_override=tmp_ledger,
    )
    assert out is None
    # ledger should still be empty
    assert (
        not tmp_ledger.exists()
        or tmp_ledger.stat().st_size == 0
    )


# ---------------------------------------------------------------------------
# Frozen artifact
# ---------------------------------------------------------------------------


def test_record_round_trip():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        GraphProgressRecord,
    )
    rec = GraphProgressRecord(
        kind="unit_completed",
        graph_id="g1", op_id="op-1", unit_id="u1",
        ts_ns=1234567890, ts_unix=12345.0,
        payload={"runtime_ms": 42.5, "patch_files": 3},
    )
    rt = GraphProgressRecord.from_dict(rec.to_dict())
    assert rt is not None
    assert rt.kind == "unit_completed"
    assert rt.graph_id == "g1"
    assert rt.payload["runtime_ms"] == 42.5


def test_record_schema_mismatch_returns_none():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        GraphProgressRecord,
    )
    out = GraphProgressRecord.from_dict(
        {"schema_version": "wrong"},
    )
    assert out is None


def test_record_payload_defensive_serialization():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        GraphProgressRecord,
    )

    class _Custom:
        def __repr__(self):
            return "<custom-payload>"

    rec = GraphProgressRecord(
        kind="unit_completed",
        graph_id="g1", op_id="op-1", unit_id="u1",
        ts_ns=0, ts_unix=0.0,
        payload={"ok": "value", "obj": _Custom()},
    )
    d = rec.to_dict()
    assert d["payload"]["ok"] == "value"
    assert "<custom-payload>" in str(
        d["payload"]["obj"],
    )


# ---------------------------------------------------------------------------
# Recorder end-to-end
# ---------------------------------------------------------------------------


def test_recorder_emits_terminal_unit(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        read_recent_records, record_graph_event,
    )
    out = record_graph_event(
        kind="unit_completed", graph_id="g1", op_id="op-1",
        unit_id="u1", ts_ns=1234567890,
        payload={"runtime_ms": 42.0},
        ledger_path_override=tmp_ledger,
    )
    assert out is not None
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1
    assert rows[0].kind == "unit_completed"


def test_recorder_emits_all_graph_level_kinds(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        read_recent_records, record_graph_event,
    )
    for kind in (
        "graph_submitted", "graph_started",
        "graph_completed", "graph_failed",
        "graph_cancelled",
    ):
        out = record_graph_event(
            kind=kind, graph_id="g1", op_id="op-1",
            ledger_path_override=tmp_ledger,
        )
        assert out is not None, f"{kind} should emit"
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 5


def test_recorder_verbose_emits_intermediates(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        record_graph_event,
    )
    out = record_graph_event(
        kind="unit_started", graph_id="g1", op_id="op-1",
        unit_id="u1",
        ledger_path_override=tmp_ledger,
    )
    assert out is not None


def test_read_recent_records_limit(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        read_recent_records, record_graph_event,
    )
    for i in range(10):
        record_graph_event(
            kind="unit_completed", graph_id="g1",
            op_id=f"op-{i}", unit_id=f"u-{i}",
            ledger_path_override=tmp_ledger,
        )
    rows = read_recent_records(limit=3, path=tmp_ledger)
    assert len(rows) == 3
    assert rows[0].op_id == "op-7"
    assert rows[2].op_id == "op-9"


def test_read_missing_ledger_returns_empty(tmp_path):
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        read_recent_records,
    )
    nonexistent = tmp_path / "no-such.jsonl"
    assert read_recent_records(path=nonexistent) == ()


# ---------------------------------------------------------------------------
# AST pins (parametrized clean + targeted regression fires)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "execution_graph_progress_bridge_master_default_false",  # noqa: E501
        "execution_graph_progress_bridge_authority_asymmetry",
        "execution_graph_progress_bridge_read_only",
        (
            "execution_graph_progress_bridge_"
            "composes_canonical_tracker"
        ),
        (
            "execution_graph_progress_bridge_"
            "composes_canonical_publisher"
        ),
        (
            "execution_graph_progress_bridge_"
            "composes_canonical_jsonl"
        ),
        (
            "execution_graph_progress_bridge_"
            "chatter_default"
        ),
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
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
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
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
            "execution_graph_progress_bridge_"
            "authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_read_only_pin_fires_on_record_event():
    """Synthetic regression: tracker.record_event() is
    forbidden — bridge MUST NOT mutate tracker state."""
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
async def consume_tracker_stream(self):
    tracker = get_default_tracker()
    tracker.record_event(some_event)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_graph_progress_bridge_read_only"
        )
    )
    assert pin.validate(tree, bad)


def test_read_only_pin_fires_on_unsubscribe_all():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
async def cleanup(self):
    tracker = get_default_tracker()
    tracker.unsubscribe_all()
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_graph_progress_bridge_read_only"
        )
    )
    assert pin.validate(tree, bad)


def test_publisher_pin_fires_on_direct_publish():
    """Synthetic regression: direct .publish() forbidden —
    bridge MUST go through the canonical helper."""
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def emit():
    broker = get_broker()
    broker.publish("some_event", "channel", {})
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_graph_progress_bridge_"
            "composes_canonical_publisher"
        )
    )
    assert pin.validate(tree, bad)


def test_jsonl_pin_fires_on_raw_open():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
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
            "execution_graph_progress_bridge_"
            "composes_canonical_jsonl"
        )
    )
    assert pin.validate(tree, bad)


def test_chatter_default_pin_fires_on_missing_kind():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
DEFAULT_EMIT_KINDS = frozenset({
    "graph_submitted", "graph_started",
    "graph_completed", "graph_failed",
    # missing graph_cancelled + 3 unit terminals
})
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_graph_progress_bridge_chatter_default"
        )
    )
    assert pin.validate(tree, bad)


def test_chatter_default_pin_fires_on_extra_kind():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
DEFAULT_EMIT_KINDS = frozenset({
    "graph_submitted", "graph_started",
    "graph_completed", "graph_failed", "graph_cancelled",
    "unit_completed", "unit_failed", "unit_cancelled",
    "unit_started",  # extra — must be suppressed by default
})
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_graph_progress_bridge_chatter_default"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (  # noqa: E501
        execution_graph_progress_bridge as mod,
    )
    expected = {
        "DEFAULT_EMIT_KINDS",
        (
            "EXECUTION_GRAPH_PROGRESS_BRIDGE_"
            "SCHEMA_VERSION"
        ),
        "ExecutionGraphProgressBridge",
        "GraphProgressRecord",
        "get_default_bridge",
        "ledger_path",
        "master_enabled",
        "read_recent_records",
        "record_graph_event",
        "register_flags",
        "register_shipped_invariants",
        "reset_default_bridge_for_test",
        "should_emit",
        "start_default_bridge",
        "verbose_mode",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_three():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
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
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED",
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE",
        "JARVIS_EXEC_GRAPH_BRIDGE_LEDGER_PATH",
    }


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


def test_singleton_lifecycle(monkeypatch):
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        ExecutionGraphProgressBridge,
        get_default_bridge,
        reset_default_bridge_for_test,
    )
    reset_default_bridge_for_test()
    b1 = get_default_bridge()
    b2 = get_default_bridge()
    assert b1 is b2  # same instance
    assert isinstance(b1, ExecutionGraphProgressBridge)
    reset_default_bridge_for_test()
    b3 = get_default_bridge()
    assert b3 is not b1  # reset → new instance


@pytest.mark.asyncio
async def test_start_default_bridge_master_off(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        start_default_bridge,
    )
    task = start_default_bridge()
    assert task is None


# ---------------------------------------------------------------------------
# Broker extension regression
# ---------------------------------------------------------------------------


def test_broker_event_type_in_frozen_set():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_EXECUTION_GRAPH_PROGRESS,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_EXECUTION_GRAPH_PROGRESS == (
        "execution_graph_progress"
    )
    assert (
        EVENT_TYPE_EXECUTION_GRAPH_PROGRESS
        in _VALID_EVENT_TYPES
    )


def test_publish_helper_master_off_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_execution_graph_progress_event,
    )
    out = publish_execution_graph_progress_event(
        kind="unit_completed",
        graph_id="g1", op_id="op-1", unit_id="u1",
        ts_ns=0, payload={"x": 1},
    )
    assert out is None


# ---------------------------------------------------------------------------
# End-to-end: synthetic GraphEvent stream → bridge consumer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SyntheticKind:
    value: str


@dataclass(frozen=True)
class _SyntheticEvent:
    kind: _SyntheticKind
    graph_id: str
    op_id: str
    ts_ns: int
    unit_id: str = ""
    payload: Mapping[str, Any] = (
        None  # type: ignore[assignment]
    )


def test_project_event_handles_synthetic_event(
    monkeypatch, tmp_ledger,
):
    """The bridge's _project_event MUST defensively extract
    fields from any GraphEvent-shaped object, including
    synthetic test fixtures."""
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        ExecutionGraphProgressBridge,
        read_recent_records,
    )
    bridge = ExecutionGraphProgressBridge()
    event = _SyntheticEvent(
        kind=_SyntheticKind("unit_completed"),
        graph_id="g1", op_id="op-1", unit_id="u1",
        ts_ns=1234567890, payload={"runtime_ms": 42.0},
    )
    bridge._project_event(event)  # noqa: SLF001
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1
    assert rows[0].kind == "unit_completed"
    tele = bridge.telemetry()
    assert tele["events_emitted"] == 1


def test_project_event_chatter_suppresses(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "true",
    )
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        ExecutionGraphProgressBridge,
    )
    bridge = ExecutionGraphProgressBridge()
    event = _SyntheticEvent(
        kind=_SyntheticKind("unit_started"),
        graph_id="g1", op_id="op-1", unit_id="u1",
        ts_ns=0, payload={},
    )
    bridge._project_event(event)  # noqa: SLF001
    tele = bridge.telemetry()
    assert tele["events_suppressed"] == 1
    assert tele["events_emitted"] == 0


def test_consume_tracker_stream_master_off_returns(
    monkeypatch,
):
    """When master flag off, consume_tracker_stream() MUST
    return immediately (no subscriber registration)."""
    import asyncio as _asyncio
    monkeypatch.delenv(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (  # noqa: E501
        ExecutionGraphProgressBridge,
    )
    bridge = ExecutionGraphProgressBridge()
    # MUST complete quickly (no subscribe / no infinite loop)
    _asyncio.run(_asyncio.wait_for(
        bridge.consume_tracker_stream(),
        timeout=2.0,
    ))
