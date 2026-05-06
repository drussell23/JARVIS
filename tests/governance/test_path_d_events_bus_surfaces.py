"""Path D.3 + D.4 — EventEmitter + CommandBus operator-surface
regression spine.

Closes the §36.6 final 2 of 6 unwired autonomy modules:

  * D.3 — EventEmitter — `_INSTANCES` WeakSet + `_event_counts`
    counter + `metrics_snapshot()` + `snapshot_all()` classmethod
    + `/events` REPL + `GET /observability/events`
  * D.4 — CommandBus — `_INSTANCES` WeakSet + `_dispatch_counts`
    counter + `_rejected_dedup` + `_rejected_backpressure` +
    `metrics_snapshot()` + `snapshot_all()` classmethod +
    `/bus` REPL + `GET /observability/command-bus`

Class-level Instance Registry pattern: both modules have no
global singleton (multiple internal callers construct their
own); class-level WeakSet aggregates across live instances
without forcing a single-instance contract.

Pins:

  * Counter increments inside emit() / _enqueue() (no parallel
    increment elsewhere)
  * WeakSet drops orphaned instances automatically (verified
    via gc round-trip)
  * Aggregation across multiple instances composes correctly
  * Both REPLs auto-discovered via §32.11 Slice 4 naming-cage
  * Both observability modules auto-mountable via §32.11 Slice 3
  * Authority asymmetry — both REPLs forbid orchestrator/iron_gate/
    policy/providers imports (AST-pinned)
  * Read-only — no mutating method calls on the substrate
    receivers (AST-pinned)
  * NEVER raises across all paths
  * Public APIs stable

Verifies (38 tests).
"""
from __future__ import annotations

import ast
import asyncio
import gc
import inspect
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# D.3 — EventEmitter substrate
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_emitters():
    """Reset class-level WeakSet between tests."""
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    EventEmitter.reset_instance_registry_for_tests()
    yield
    EventEmitter.reset_instance_registry_for_tests()


def test_event_emitter_registers_self_on_construction(
    fresh_emitters,
):
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    e = EventEmitter()
    assert e in EventEmitter._INSTANCES
    assert len(EventEmitter._INSTANCES) == 1
    e2 = EventEmitter()
    assert len(EventEmitter._INSTANCES) == 2


def test_event_emitter_metrics_snapshot_initial(fresh_emitters):
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    e = EventEmitter()
    snap = e.metrics_snapshot()
    assert snap["last_event_id"] is None
    assert snap["total_emissions"] == 0
    assert snap["total_subscribers"] == 0
    assert snap["by_event_type"] == {}


def test_event_emitter_emission_counter_increments(
    fresh_emitters,
):
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (  # noqa: E501
        EventEnvelope, EventType,
    )

    async def _go():
        e = EventEmitter()
        await e.emit(EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
            op_id="op-1",
        ))
        await e.emit(EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
            op_id="op-2",
        ))
        return e.metrics_snapshot()

    snap = asyncio.run(_go())
    assert snap["total_emissions"] == 2
    assert (
        snap["by_event_type"]["op_completed"]["emission_count"]
        == 2
    )


def test_event_emitter_snapshot_all_aggregates(fresh_emitters):
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (  # noqa: E501
        EventEnvelope, EventType,
    )

    async def _go():
        e1 = EventEmitter()
        e2 = EventEmitter()
        await e1.emit(EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
            op_id="x",
        ))
        await e2.emit(EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
            op_id="y",
        ))
        return EventEmitter.snapshot_all()

    agg = asyncio.run(_go())
    assert agg["instance_count"] == 2
    assert agg["total_emissions"] == 2  # 1 from each
    assert (
        agg["by_event_type"]["op_completed"]["emission_count"]
        == 2
    )


def test_event_emitter_snapshot_all_empty(fresh_emitters):
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    agg = EventEmitter.snapshot_all()
    assert agg["instance_count"] == 0
    assert agg["total_emissions"] == 0


def test_event_emitter_weakset_drops_orphans(fresh_emitters):
    """Verify WeakSet drops orphaned instances after gc."""
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    e1 = EventEmitter()
    e2 = EventEmitter()
    assert len(EventEmitter._INSTANCES) == 2
    del e1
    gc.collect()
    # Orphaned emitter dropped from WeakSet
    assert len(EventEmitter._INSTANCES) == 1
    # Use e2 to keep ref
    _ = e2.metrics_snapshot()


def test_event_emitter_metrics_snapshot_never_raises(
    fresh_emitters,
):
    from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
        EventEmitter,
    )
    e = EventEmitter()
    # Even with corrupted state, snapshot returns sane defaults
    e._subscribers = None  # type: ignore[assignment]
    snap = e.metrics_snapshot()
    assert isinstance(snap, dict)


# ---------------------------------------------------------------------------
# D.3 — /events REPL
# ---------------------------------------------------------------------------


def test_events_help_renders():
    from backend.core.ouroboros.governance.events_repl import (
        dispatch_events_command,
    )
    r = dispatch_events_command("/events help")
    assert r.ok is True
    assert "EventEmitter" in r.text


def test_events_bare_renders(fresh_emitters):
    from backend.core.ouroboros.governance.events_repl import (
        dispatch_events_command,
    )
    r = dispatch_events_command("/events")
    assert r.ok is True


def test_events_stats_renders(fresh_emitters):
    from backend.core.ouroboros.governance.events_repl import (
        dispatch_events_command,
    )
    r = dispatch_events_command("/events stats")
    assert r.ok is True


def test_events_unknown_subcommand():
    from backend.core.ouroboros.governance.events_repl import (
        dispatch_events_command,
    )
    r = dispatch_events_command("/events nonsense")
    assert r.ok is False


def test_events_non_match_returns_unmatched():
    from backend.core.ouroboros.governance.events_repl import (
        dispatch_events_command,
    )
    r = dispatch_events_command("/health")
    assert r.matched is False


def test_events_auto_discovered():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    r = try_dispatch("/events help")
    assert r.matched is True
    assert r.ok is True


# ---------------------------------------------------------------------------
# D.3 — events_observability
# ---------------------------------------------------------------------------


def test_events_observability_register_routes_signature():
    from backend.core.ouroboros.governance import (
        events_observability,
    )
    sig = inspect.signature(
        events_observability.register_routes,
    )
    assert "app" in sig.parameters
    assert "rate_limit_check" in sig.parameters
    assert "cors_headers" in sig.parameters


def test_events_observability_mounts_route():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance import (
        events_observability,
    )
    app = web.Application()
    events_observability.register_routes(app)
    canonical_paths = [
        getattr(getattr(r, "resource", None), "canonical", None)
        for r in app.router.routes()
    ]
    assert any(
        cp == "/observability/events" for cp in canonical_paths
    )


# ---------------------------------------------------------------------------
# D.4 — CommandBus substrate
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_buses():
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    CommandBus.reset_instance_registry_for_tests()
    yield
    CommandBus.reset_instance_registry_for_tests()


def test_command_bus_registers_self_on_construction(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    b = CommandBus(maxsize=10)
    assert b in CommandBus._INSTANCES
    assert len(CommandBus._INSTANCES) == 1


def test_command_bus_metrics_snapshot_initial(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    b = CommandBus(maxsize=10)
    snap = b.metrics_snapshot()
    assert snap["qsize"] == 0
    assert snap["maxsize"] == 10
    assert snap["total_dispatched"] == 0
    assert snap["rejected_dedup"] == 0
    assert snap["rejected_backpressure"] == 0
    assert snap["by_command_type"] == {}


def _make_cmd(idem_key: str, command_type=None):
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (  # noqa: E501
        CommandEnvelope, CommandType,
    )
    return CommandEnvelope(
        source_layer="L3",
        target_layer="L1",
        command_type=command_type or CommandType.REQUEST_MODE_SWITCH,
        payload={"target_mode": "REDUCED_AUTONOMY"},
        ttl_s=60.0,
        idempotency_key=idem_key,
    )


def test_command_bus_dispatch_counter_increments(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )

    async def _go():
        b = CommandBus(maxsize=10)
        await b.put(_make_cmd("k1"))
        await b.put(_make_cmd("k2"))
        return b.metrics_snapshot()

    snap = asyncio.run(_go())
    assert snap["total_dispatched"] == 2
    # Counter key is the enum's `.value` (lowercase), not name
    assert (
        snap["by_command_type"]["request_mode_switch"] == 2
    )


def test_command_bus_rejection_counters(fresh_buses):
    """Dedup + backpressure rejections increment dedicated
    counters."""
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )

    async def _go():
        b = CommandBus(maxsize=2)
        # Successful enqueue
        ok1 = await b.put(_make_cmd("dup-key"))
        assert ok1 is True
        # Duplicate idempotency_key — rejected
        ok2 = await b.put(_make_cmd("dup-key"))
        assert ok2 is False
        # Backpressure: bus full at 2, attempt to enqueue more
        await b.put(_make_cmd("k-fill"))
        bp1 = await b.put(_make_cmd("k-overflow"))
        assert bp1 is False
        return b.metrics_snapshot()

    snap = asyncio.run(_go())
    assert snap["rejected_dedup"] >= 1
    assert snap["rejected_backpressure"] >= 1


def test_command_bus_snapshot_all_aggregates(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )

    async def _go():
        b1 = CommandBus(maxsize=10)
        b2 = CommandBus(maxsize=10)
        await b1.put(_make_cmd("a"))
        await b2.put(_make_cmd("b"))
        return CommandBus.snapshot_all()

    agg = asyncio.run(_go())
    assert agg["instance_count"] == 2
    assert agg["total_dispatched"] == 2


def test_command_bus_snapshot_all_empty(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    agg = CommandBus.snapshot_all()
    assert agg["instance_count"] == 0
    assert agg["total_dispatched"] == 0


def test_command_bus_weakset_drops_orphans(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    b1 = CommandBus(maxsize=10)
    b2 = CommandBus(maxsize=10)
    assert len(CommandBus._INSTANCES) == 2
    del b1
    gc.collect()
    assert len(CommandBus._INSTANCES) == 1
    _ = b2.metrics_snapshot()


def test_command_bus_metrics_snapshot_never_raises(fresh_buses):
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    b = CommandBus(maxsize=10)
    # Corrupt internal state
    b._heap = None  # type: ignore[assignment]
    snap = b.metrics_snapshot()
    assert isinstance(snap, dict)


# ---------------------------------------------------------------------------
# D.4 — /bus REPL
# ---------------------------------------------------------------------------


def test_bus_help_renders():
    from backend.core.ouroboros.governance.bus_repl import (
        dispatch_bus_command,
    )
    r = dispatch_bus_command("/bus help")
    assert r.ok is True
    assert "CommandBus" in r.text


def test_bus_bare_renders(fresh_buses):
    from backend.core.ouroboros.governance.bus_repl import (
        dispatch_bus_command,
    )
    r = dispatch_bus_command("/bus")
    assert r.ok is True


def test_bus_stats_renders(fresh_buses):
    from backend.core.ouroboros.governance.bus_repl import (
        dispatch_bus_command,
    )
    r = dispatch_bus_command("/bus stats")
    assert r.ok is True


def test_bus_unknown_subcommand():
    from backend.core.ouroboros.governance.bus_repl import (
        dispatch_bus_command,
    )
    r = dispatch_bus_command("/bus nonsense")
    assert r.ok is False


def test_bus_non_match_returns_unmatched():
    from backend.core.ouroboros.governance.bus_repl import (
        dispatch_bus_command,
    )
    r = dispatch_bus_command("/health")
    assert r.matched is False


def test_bus_auto_discovered():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    r = try_dispatch("/bus help")
    assert r.matched is True
    assert r.ok is True


# ---------------------------------------------------------------------------
# D.4 — bus_observability
# ---------------------------------------------------------------------------


def test_bus_observability_register_routes_signature():
    from backend.core.ouroboros.governance import (
        bus_observability,
    )
    sig = inspect.signature(
        bus_observability.register_routes,
    )
    assert "app" in sig.parameters
    assert "rate_limit_check" in sig.parameters
    assert "cors_headers" in sig.parameters


def test_bus_observability_mounts_route():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance import (
        bus_observability,
    )
    app = web.Application()
    bus_observability.register_routes(app)
    canonical_paths = [
        getattr(getattr(r, "resource", None), "canonical", None)
        for r in app.router.routes()
    ]
    assert any(
        cp == "/observability/command-bus"
        for cp in canonical_paths
    )


# ---------------------------------------------------------------------------
# AST pins — both REPLs + both observability modules
# ---------------------------------------------------------------------------


def test_events_repl_pins_validate_clean():
    from backend.core.ouroboros.governance.events_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/events_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_bus_repl_pins_validate_clean():
    from backend.core.ouroboros.governance.bus_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/bus_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_events_observability_pins_validate_clean():
    from backend.core.ouroboros.governance.events_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/events_observability.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_bus_observability_pins_validate_clean():
    from backend.core.ouroboros.governance.bus_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/bus_observability.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_events_repl_public_api():
    from backend.core.ouroboros.governance import events_repl
    assert set(events_repl.__all__) == {
        "EventsReplDispatchResult",
        "dispatch_events_command",
        "register_shipped_invariants",
        "register_verbs",
    }


def test_events_observability_public_api():
    from backend.core.ouroboros.governance import (
        events_observability,
    )
    assert set(events_observability.__all__) == {
        "EVENTS_OBSERVABILITY_SCHEMA_VERSION",
        "register_routes",
        "register_shipped_invariants",
    }


def test_bus_repl_public_api():
    from backend.core.ouroboros.governance import bus_repl
    assert set(bus_repl.__all__) == {
        "BusReplDispatchResult",
        "dispatch_bus_command",
        "register_shipped_invariants",
        "register_verbs",
    }


def test_bus_observability_public_api():
    from backend.core.ouroboros.governance import (
        bus_observability,
    )
    assert set(bus_observability.__all__) == {
        "BUS_OBSERVABILITY_SCHEMA_VERSION",
        "register_routes",
        "register_shipped_invariants",
    }


# ---------------------------------------------------------------------------
# No orchestrator imports across new surfaces
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports_in_new_modules():
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    targets = (
        "backend/core/ouroboros/governance/events_repl.py",
        "backend/core/ouroboros/governance/events_observability.py",
        "backend/core/ouroboros/governance/bus_repl.py",
        "backend/core/ouroboros/governance/bus_observability.py",
    )
    for t in targets:
        path = _repo_root() / t
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        pytest.fail(
                            f"{t} imports forbidden {module!r}"
                        )
