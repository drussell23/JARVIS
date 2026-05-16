"""Regression spine for the In-Flight Registry (P2 Slice 1).

Covers the typed parallel registry that Slice 2's
:mod:`convergence_reaper` composes — the structural fix for
"ops hang past terminal" requires an observable in-flight view
that ``GovernedLoopService._active_ops: Set[str]`` cannot provide.

Coverage axes:

  * §33.1 master gate (default-FALSE, env knob, case-insensitive)
  * :class:`OpInFlight` shape (frozen, schema, lossless to_dict,
    coarse_phase mapping, deadline arithmetic)
  * :class:`InFlightPhase` 9-value taxonomy + ``from_name``
    coerce-to-OTHER for unknown phases
  * :class:`InFlightRegistry` lifecycle: register / unregister /
    lookup / snapshot / update_phase / size / clear / op_ids
  * Reap predicates: ``reap_past_deadline`` (explicit deadline)
    + ``reap_older_than`` (global ceiling fallback)
  * NEVER-raises contract on every public surface
  * Thread-safety: concurrent register/unregister/snapshot
  * Module-level singleton :func:`get_default_registry` +
    :func:`reset_default_registry`
  * 3 AST pins validate against current source
"""
from __future__ import annotations

import ast as _ast
import os
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.in_flight_registry import (
    IN_FLIGHT_REGISTRY_SCHEMA_VERSION,
    InFlightPhase,
    InFlightRegistry,
    OpInFlight,
    get_default_registry,
    master_enabled,
    register_shipped_invariants,
    reset_default_registry,
)


_MASTER_FLAG = "JARVIS_IN_FLIGHT_REGISTRY_ENABLED"


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved = os.environ.pop(_MASTER_FLAG, None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_MASTER_FLAG, None)
        else:
            os.environ[_MASTER_FLAG] = saved
    reset_default_registry()


# ---------------------------------------------------------------------------
# Master gate (§33.1)
# ---------------------------------------------------------------------------


class TestMasterGate:
    def test_default_false(self):
        assert master_enabled() is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "true")
        assert master_enabled() is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "TRUE")
        assert master_enabled() is True

    def test_garbage_is_false(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "kinda")
        assert master_enabled() is False


# ---------------------------------------------------------------------------
# InFlightPhase taxonomy
# ---------------------------------------------------------------------------


class TestInFlightPhase:
    def test_nine_value_taxonomy(self):
        values = {p.value for p in InFlightPhase}
        assert values == {
            "route", "plan", "generate", "validate", "approve",
            "apply", "verify", "postmortem", "other",
        }

    def test_from_name_canonical(self):
        assert (
            InFlightPhase.from_name("generate")
            is InFlightPhase.GENERATE
        )

    def test_from_name_case_insensitive(self):
        assert (
            InFlightPhase.from_name("GENERATE")
            is InFlightPhase.GENERATE
        )

    def test_from_name_unknown_folds_to_other(self):
        # Unknown phase MUST coerce, not raise.
        assert (
            InFlightPhase.from_name("brand_new_phase_x")
            is InFlightPhase.OTHER
        )

    def test_from_name_none_is_other(self):
        assert InFlightPhase.from_name(None) is InFlightPhase.OTHER

    def test_from_name_empty_is_other(self):
        assert InFlightPhase.from_name("") is InFlightPhase.OTHER


# ---------------------------------------------------------------------------
# OpInFlight record
# ---------------------------------------------------------------------------


class TestOpInFlightRecord:
    def test_frozen(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
        )
        with pytest.raises(Exception):
            rec.op_id = "y"  # type: ignore[misc]

    def test_carries_schema_version(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
        )
        assert rec.schema_version == (
            IN_FLIGHT_REGISTRY_SCHEMA_VERSION
        )

    def test_time_in_flight_arithmetic(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=10.0,
        )
        assert rec.time_in_flight_s(
            now_monotonic=15.0,
        ) == 5.0

    def test_time_in_flight_clamps_negative_to_zero(self):
        # Clock skew shouldn't surface as negative durations.
        rec = OpInFlight(
            op_id="x", started_at_monotonic=100.0,
        )
        assert rec.time_in_flight_s(now_monotonic=50.0) == 0.0

    def test_no_deadline_means_not_past(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
            deadline_monotonic=None,
        )
        assert rec.is_past_deadline(now_monotonic=9999) is False

    def test_explicit_deadline_past(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
            deadline_monotonic=10.0,
        )
        assert rec.is_past_deadline(now_monotonic=15.0) is True

    def test_explicit_deadline_before(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
            deadline_monotonic=10.0,
        )
        assert rec.is_past_deadline(now_monotonic=5.0) is False

    def test_coarse_phase_known(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
            last_phase_name="generate",
        )
        assert rec.coarse_phase() is InFlightPhase.GENERATE

    def test_coarse_phase_unknown(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
            last_phase_name="exotic_phase",
        )
        assert rec.coarse_phase() is InFlightPhase.OTHER

    def test_to_dict_lossless(self):
        rec = OpInFlight(
            op_id="x", started_at_monotonic=1.0,
            deadline_monotonic=10.0,
            last_phase_name="apply",
            last_phase_at_monotonic=5.0,
            metadata=(("provider", "claude"),),
        )
        d = rec.to_dict()
        assert d["op_id"] == "x"
        assert d["schema_version"] == (
            IN_FLIGHT_REGISTRY_SCHEMA_VERSION
        )
        assert d["coarse_phase"] == "apply"
        assert d["metadata"] == {"provider": "claude"}
        # ctx_ref omitted (not JSON-serializable).
        assert "ctx_ref" not in d


# ---------------------------------------------------------------------------
# Registry lifecycle
# ---------------------------------------------------------------------------


class TestRegistryLifecycle:
    def test_register_adds_record(self):
        r = InFlightRegistry()
        rec = r.register("op-1")
        assert rec is not None
        assert rec.op_id == "op-1"
        assert r.size() == 1

    def test_register_idempotent_overwrites(self):
        r = InFlightRegistry()
        r.register("op-1", last_phase_name="route")
        r.register("op-1", last_phase_name="generate")
        rec = r.lookup("op-1")
        assert rec is not None
        assert rec.last_phase_name == "generate"
        assert r.size() == 1  # still one entry

    def test_register_empty_op_id_returns_none(self):
        r = InFlightRegistry()
        assert r.register("") is None
        assert r.size() == 0

    def test_register_non_string_op_id_returns_none(self):
        r = InFlightRegistry()
        assert r.register(None) is None  # type: ignore[arg-type]

    def test_register_with_metadata(self):
        r = InFlightRegistry()
        rec = r.register(
            "op-1",
            metadata={"provider": "claude", "route": "standard"},
        )
        assert rec is not None
        assert ("provider", "claude") in rec.metadata
        assert ("route", "standard") in rec.metadata

    def test_register_with_deadline(self):
        r = InFlightRegistry()
        deadline = time.monotonic() + 100.0
        rec = r.register(
            "op-1", deadline_monotonic=deadline,
        )
        assert rec is not None
        assert rec.deadline_monotonic == deadline

    def test_register_with_ctx_ref(self):
        r = InFlightRegistry()
        sentinel = object()
        rec = r.register("op-1", ctx_ref=sentinel)
        assert rec is not None
        assert rec.ctx_ref is sentinel

    def test_unregister_removes(self):
        r = InFlightRegistry()
        r.register("op-1")
        assert r.unregister("op-1") is True
        assert r.size() == 0

    def test_unregister_missing_is_silent_false(self):
        r = InFlightRegistry()
        assert r.unregister("op-missing") is False

    def test_unregister_idempotent(self):
        r = InFlightRegistry()
        r.register("op-1")
        r.unregister("op-1")
        # Second unregister doesn't raise.
        assert r.unregister("op-1") is False

    def test_lookup_returns_none_for_missing(self):
        r = InFlightRegistry()
        assert r.lookup("missing") is None

    def test_lookup_returns_record(self):
        r = InFlightRegistry()
        r.register("op-1", last_phase_name="apply")
        rec = r.lookup("op-1")
        assert rec is not None
        assert rec.last_phase_name == "apply"


# ---------------------------------------------------------------------------
# update_phase
# ---------------------------------------------------------------------------


class TestUpdatePhase:
    def test_atomically_swaps_record(self):
        r = InFlightRegistry()
        r.register("op-1", last_phase_name="route")
        updated = r.update_phase("op-1", phase_name="generate")
        assert updated is not None
        assert updated.last_phase_name == "generate"
        # Preserves other fields.
        assert updated.started_at_monotonic == (
            r.lookup("op-1").started_at_monotonic
        )

    def test_update_advances_last_phase_at(self):
        r = InFlightRegistry()
        rec0 = r.register("op-1", last_phase_name="route")
        original_phase_at = rec0.last_phase_at_monotonic
        time.sleep(0.001)
        updated = r.update_phase("op-1", phase_name="generate")
        assert updated.last_phase_at_monotonic > original_phase_at

    def test_update_missing_returns_none(self):
        r = InFlightRegistry()
        assert r.update_phase(
            "op-missing", phase_name="apply",
        ) is None


# ---------------------------------------------------------------------------
# snapshot + reap predicates
# ---------------------------------------------------------------------------


class TestSnapshotAndReap:
    def test_snapshot_returns_tuple(self):
        r = InFlightRegistry()
        r.register("op-1")
        r.register("op-2")
        snap = r.snapshot()
        assert isinstance(snap, tuple)
        assert len(snap) == 2

    def test_snapshot_is_immutable(self):
        r = InFlightRegistry()
        r.register("op-1")
        snap = r.snapshot()
        # tuple → can't append.
        with pytest.raises(AttributeError):
            snap.append(OpInFlight(  # type: ignore[attr-defined]
                op_id="x", started_at_monotonic=0,
            ))

    def test_snapshot_decoupled_from_subsequent_mutation(self):
        r = InFlightRegistry()
        r.register("op-1")
        snap = r.snapshot()
        r.unregister("op-1")
        # Snapshot still contains the original entry.
        assert len(snap) == 1
        assert r.size() == 0

    def test_op_ids_matches_snapshot(self):
        r = InFlightRegistry()
        r.register("op-1")
        r.register("op-2")
        ids = set(r.op_ids())
        assert ids == {"op-1", "op-2"}

    def test_reap_past_deadline_filter(self):
        r = InFlightRegistry()
        now = time.monotonic()
        # op-1: deadline in past.
        r.register(
            "op-1", deadline_monotonic=now - 5.0,
        )
        # op-2: deadline in future.
        r.register(
            "op-2", deadline_monotonic=now + 100.0,
        )
        # op-3: no deadline.
        r.register("op-3")
        reaped = r.reap_past_deadline(now_monotonic=now)
        ids = {x.op_id for x in reaped}
        assert ids == {"op-1"}

    def test_reap_past_deadline_does_not_mutate(self):
        r = InFlightRegistry()
        now = time.monotonic()
        r.register("op-1", deadline_monotonic=now - 5.0)
        r.reap_past_deadline(now_monotonic=now)
        # Registry still has the entry; reap is read-only.
        assert r.size() == 1

    def test_reap_older_than_filter(self):
        r = InFlightRegistry()
        r.register("op-1")  # started just now
        time.sleep(0.05)
        now = time.monotonic()
        # op-1 has been around > 0.01s.
        reaped = r.reap_older_than(0.01, now_monotonic=now)
        assert {x.op_id for x in reaped} == {"op-1"}

    def test_reap_older_than_invalid_ceiling(self):
        r = InFlightRegistry()
        r.register("op-1")
        assert r.reap_older_than(0) == tuple()
        assert r.reap_older_than(-5) == tuple()
        assert r.reap_older_than(
            "not-a-number",  # type: ignore[arg-type]
        ) == tuple()


# ---------------------------------------------------------------------------
# Clear + size
# ---------------------------------------------------------------------------


class TestClearAndSize:
    def test_size_grows(self):
        r = InFlightRegistry()
        assert r.size() == 0
        r.register("op-1")
        assert r.size() == 1
        r.register("op-2")
        assert r.size() == 2

    def test_clear_purges_all(self):
        r = InFlightRegistry()
        r.register("op-1")
        r.register("op-2")
        purged = r.clear()
        assert purged == 2
        assert r.size() == 0

    def test_clear_on_empty(self):
        r = InFlightRegistry()
        assert r.clear() == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_register_unregister(self):
        """100 threads each register+unregister 50 ops. The
        registry must end empty + never raise."""
        r = InFlightRegistry()
        n_threads = 20
        ops_per_thread = 50

        def _worker(tid: int) -> None:
            for i in range(ops_per_thread):
                op_id = f"t{tid}-op{i}"
                r.register(op_id, last_phase_name="route")
                r.update_phase(op_id, phase_name="generate")
                r.lookup(op_id)
                r.snapshot()
                r.unregister(op_id)

        threads = [
            threading.Thread(target=_worker, args=(i,))
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), f"thread {t} hung"
        assert r.size() == 0

    def test_concurrent_snapshot_during_mutation(self):
        """Snapshots taken during concurrent mutation always
        return an internally-consistent immutable tuple."""
        r = InFlightRegistry()
        # Seed.
        for i in range(50):
            r.register(f"op-{i}")
        done = threading.Event()

        def _mutator() -> None:
            while not done.is_set():
                for i in range(10):
                    r.update_phase(
                        f"op-{i}", phase_name="apply",
                    )

        m = threading.Thread(target=_mutator)
        m.start()
        try:
            # Take many snapshots; each must be a tuple of
            # OpInFlight, no torn reads.
            for _ in range(50):
                snap = r.snapshot()
                assert isinstance(snap, tuple)
                for rec in snap:
                    assert isinstance(rec, OpInFlight)
        finally:
            done.set()
            m.join(timeout=5)
            assert not m.is_alive()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestDefaultRegistrySingleton:
    def test_returns_same_instance(self):
        a = get_default_registry()
        b = get_default_registry()
        assert a is b

    def test_reset_drops_singleton(self):
        a = get_default_registry()
        a.register("op-x")
        reset_default_registry()
        b = get_default_registry()
        # Fresh instance — entry gone.
        assert b.lookup("op-x") is None
        assert b is not a


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_returns_three_pins(self):
        pins = register_shipped_invariants()
        names = {p.invariant_name for p in pins}
        assert names == {
            "in_flight_registry_master_default_false",
            "in_flight_registry_phase_taxonomy_closed",
            "in_flight_registry_authority_asymmetry",
        }

    def test_pins_pass_on_current_source(self):
        pins = register_shipped_invariants()
        src = Path(
            "backend/core/ouroboros/governance/"
            "in_flight_registry.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        for pin in pins:
            violations = pin.validate(tree, src)
            assert violations == (), (
                f"{pin.invariant_name} drift: {violations}"
            )

    def test_no_op_context_import(self):
        """Belt-and-suspenders: directly assert the substrate
        does NOT import op_context — that would form an
        observability → state-machine cycle."""
        src = Path(
            "backend/core/ouroboros/governance/"
            "in_flight_registry.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                assert "op_context" not in mod, (
                    f"op_context dep cycle: {mod}"
                )
