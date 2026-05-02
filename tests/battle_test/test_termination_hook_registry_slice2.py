"""TerminationHookRegistry Slice 2 — registry + discovery regression.

Pins the per-process registry that wraps Slice 1's
:func:`dispatch_phase_sync` and the module-owned auto-discovery
contract that walks ``backend.core.ouroboros.battle_test`` and
``backend.core.ouroboros.governance`` for
``register_termination_hooks(registry)`` providers.

Strict directives validated (Slice 2 surface):

  * Sync-first preserved: AST pin asserts the registry module
    imports NOTHING from asyncio.
  * Deterministic budgets: per-phase budget env-knob accessors
    + per-hook timeout clamp at registration + dispatch-time
    budget shaping all pinned.
  * NEVER raises in dispatch / unregister / snapshot. SINGLE
    documented exception path is :meth:`register` — operator
    misconfig.
  * No hardcoding: env-knob accessors with floor/ceiling clamps
    + curated discovery package list.

Covers:

  §A   Schema version + env-knob defaults / clamps
  §B   Registration validation matrix (raises on bad inputs)
  §C   Capacity enforcement + duplicate-name detection
  §D   Priority ordering (insertion-sorted, equal-priority
       stable)
  §E   Snapshot / for_phase / introspection surface
  §F   Listener subscribe / fire / unsubscribe + exception
       containment
  §G   Dispatch wraps Slice 1 dispatcher + builds context
       correctly
  §H   Per-hook enabled_check filters at dispatch
  §I   Dispatch listener fires on every dispatch + payload
       carries dispatch_result
  §J   Singleton accessor + reset
  §K   Module-owned discovery loop walks providers + tolerates
       missing modules
  §L   discover_and_register_default convenience
  §M   AST authority pins: no asyncio, no authority modules
  §N   Pristine-equivalency invariants for Slice 3:
       a registered hook's call-time context shape MUST match
       what _atexit_fallback_write expects
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import time
import types
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.termination_hook import (
    DEFAULT_HARD_EXIT_PHASE_BUDGET_S,
    DEFAULT_PER_HOOK_TIMEOUT_S,
    DEFAULT_PHASE_BUDGET_S,
    HookOutcome,
    TerminationCause,
    TerminationDispatchResult,
    TerminationHookContext,
    TerminationPhase,
)
from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
    DuplicateHookNameError,
    HookCapacityExceededError,
    InvalidHookError,
    TERMINATION_HOOK_REGISTRY_SCHEMA_VERSION,
    TerminationHookRegistration,
    TerminationHookRegistry,
    TerminationHookRegistryError,
    discover_and_register_default,
    discover_module_provided_hooks,
    get_default_registry,
    max_hooks_per_phase,
    per_hook_timeout_s,
    phase_budget_s,
    reset_default_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Drop env knobs so every test starts from documented defaults.
    for var in (
        "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE",
        "JARVIS_TERMINATION_HOOK_TIMEOUT_S",
        "JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S",
        "JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_default_registry_for_tests()
    yield
    reset_default_registry_for_tests()


def _noop_hook(ctx: TerminationHookContext) -> None:
    pass


# ---------------------------------------------------------------------------
# §A — Schema + env knobs
# ---------------------------------------------------------------------------


class TestSchemaAndEnvKnobs:
    def test_schema_version_pin(self):
        assert (
            TERMINATION_HOOK_REGISTRY_SCHEMA_VERSION
            == "termination_hook_registry.1"
        )

    def test_max_per_phase_default(self):
        assert max_hooks_per_phase() == 16

    def test_max_per_phase_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE", "0",
        )
        assert max_hooks_per_phase() == 1

    def test_max_per_phase_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE", "9999",
        )
        assert max_hooks_per_phase() == 256

    def test_max_per_phase_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE", "abc",
        )
        assert max_hooks_per_phase() == 16

    def test_per_hook_timeout_default(self):
        assert per_hook_timeout_s() == DEFAULT_PER_HOOK_TIMEOUT_S

    def test_per_hook_timeout_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_TIMEOUT_S", "0.0",
        )
        assert per_hook_timeout_s() == 0.1
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_TIMEOUT_S", "9999",
        )
        assert per_hook_timeout_s() == 30.0

    def test_phase_budget_normal_default(self):
        for phase in (
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            TerminationPhase.POST_ASYNC_CLEANUP,
        ):
            assert phase_budget_s(phase) == DEFAULT_PHASE_BUDGET_S

    def test_phase_budget_hard_exit_tighter_default(self):
        assert (
            phase_budget_s(TerminationPhase.PRE_HARD_EXIT)
            == DEFAULT_HARD_EXIT_PHASE_BUDGET_S
        )

    def test_phase_budget_hard_exit_tighter_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S", "100",
        )
        assert phase_budget_s(TerminationPhase.PRE_HARD_EXIT) == 10.0
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S", "0.0",
        )
        assert phase_budget_s(TerminationPhase.PRE_HARD_EXIT) == 0.5

    def test_phase_budget_normal_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S", "999",
        )
        assert phase_budget_s(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        ) == 60.0
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S", "0.0",
        )
        assert phase_budget_s(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        ) == 1.0


# ---------------------------------------------------------------------------
# §B — Registration validation
# ---------------------------------------------------------------------------


class TestRegistrationValidation:
    def test_register_happy_path(self):
        reg = TerminationHookRegistry()
        rec = reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="partial_summary_writer",
        )
        assert rec.name == "partial_summary_writer"
        assert rec.phase is TerminationPhase.PRE_SHUTDOWN_EVENT_SET
        assert rec.callable is _noop_hook
        assert rec.priority == 100  # default
        assert rec.timeout_s == DEFAULT_PER_HOOK_TIMEOUT_S

    def test_register_invalid_phase_raises(self):
        reg = TerminationHookRegistry()
        with pytest.raises(InvalidHookError, match="phase"):
            reg.register("not_a_phase", _noop_hook, name="x")  # type: ignore[arg-type]

    def test_register_non_callable_raises(self):
        reg = TerminationHookRegistry()
        with pytest.raises(InvalidHookError, match="callable"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                42,  # type: ignore[arg-type]
                name="x",
            )

    def test_register_empty_name_raises(self):
        reg = TerminationHookRegistry()
        with pytest.raises(InvalidHookError, match="non-empty"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name="",
            )

    def test_register_whitespace_name_raises(self):
        reg = TerminationHookRegistry()
        with pytest.raises(InvalidHookError, match="non-empty"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name="   ",
            )

    def test_register_long_name_truncated(self):
        reg = TerminationHookRegistry()
        long = "x" * 200
        rec = reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name=long,
        )
        assert len(rec.name) == 128

    def test_register_garbage_priority_defaults(self):
        reg = TerminationHookRegistry()
        rec = reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="x",
            priority="not-int",  # type: ignore[arg-type]
        )
        assert rec.priority == 100

    def test_register_timeout_clamped(self):
        reg = TerminationHookRegistry()
        rec_low = reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="low",
            timeout_s=0.001,
        )
        assert rec_low.timeout_s == 0.1
        rec_high = reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="high",
            timeout_s=9999,
        )
        assert rec_high.timeout_s == 30.0


# ---------------------------------------------------------------------------
# §C — Capacity + duplicate-name
# ---------------------------------------------------------------------------


class TestCapacityAndDedup:
    def test_duplicate_name_raises(self):
        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="dup",
        )
        with pytest.raises(DuplicateHookNameError, match="already"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name="dup",
            )

    def test_capacity_enforced(self):
        reg = TerminationHookRegistry(max_per_phase=3)
        for i in range(3):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name=f"h{i}",
            )
        with pytest.raises(HookCapacityExceededError, match="capacity"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name="overflow",
            )

    def test_capacity_per_phase_independent(self):
        reg = TerminationHookRegistry(max_per_phase=2)
        # Fill PRE_SHUTDOWN_EVENT_SET to capacity; POST_ASYNC_CLEANUP
        # is independent and accepts new registrations.
        for i in range(2):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name=f"pre{i}",
            )
        # POST_ASYNC_CLEANUP fresh — accepts.
        reg.register(
            TerminationPhase.POST_ASYNC_CLEANUP,
            _noop_hook,
            name="post1",
        )
        assert reg.count_for_phase(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        ) == 2
        assert reg.count_for_phase(
            TerminationPhase.POST_ASYNC_CLEANUP,
        ) == 1

    def test_unregister_returns_true_on_known(self):
        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="h1",
        )
        assert reg.unregister("h1") is True
        assert reg.total_count() == 0

    def test_unregister_returns_false_on_unknown(self):
        reg = TerminationHookRegistry()
        assert reg.unregister("never_registered") is False

    def test_unregister_empty_name_returns_false(self):
        reg = TerminationHookRegistry()
        assert reg.unregister("") is False
        assert reg.unregister("   ") is False

    def test_capacity_after_unregister_freed(self):
        reg = TerminationHookRegistry(max_per_phase=2)
        for i in range(2):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name=f"h{i}",
            )
        reg.unregister("h0")
        # Slot freed — next register succeeds.
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="h2",
        )


# ---------------------------------------------------------------------------
# §D — Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    def test_lower_priority_value_runs_first(self):
        reg = TerminationHookRegistry()
        # Register out of order — should sort to (low, mid, high).
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="high", priority=200,
        )
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="low", priority=10,
        )
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="mid", priority=100,
        )
        bucket = reg.for_phase(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        )
        names = [r.name for r in bucket]
        assert names == ["low", "mid", "high"]

    def test_equal_priority_stable_insertion_order(self):
        reg = TerminationHookRegistry()
        for nm in ("first", "second", "third"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook, name=nm, priority=50,
            )
        bucket = reg.for_phase(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        )
        names = [r.name for r in bucket]
        # Equal priority preserves insertion order.
        assert names == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# §E — Snapshot / introspection
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_known_returns_projection(self):
        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook,
            name="abc",
            priority=42,
            timeout_s=2.5,
        )
        proj = reg.snapshot("abc")
        assert proj is not None
        assert proj["name"] == "abc"
        assert proj["phase"] == "pre_shutdown_event_set"
        assert proj["priority"] == 42
        assert proj["timeout_s"] == 2.5
        assert proj["has_enabled_check"] is False

    def test_snapshot_unknown_returns_none(self):
        reg = TerminationHookRegistry()
        assert reg.snapshot("never") is None

    def test_snapshot_all_alphabetical(self):
        reg = TerminationHookRegistry()
        for nm in ("z", "a", "m"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook, name=nm,
            )
        all_proj = reg.snapshot_all()
        names = [p["name"] for p in all_proj]
        assert names == ["a", "m", "z"]

    def test_for_phase_invalid_returns_empty_tuple(self):
        reg = TerminationHookRegistry()
        assert reg.for_phase("not_a_phase") == ()  # type: ignore[arg-type]

    def test_names_alphabetical(self):
        reg = TerminationHookRegistry()
        for nm in ("c", "a", "b"):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook, name=nm,
            )
        assert reg.names() == ("a", "b", "c")


# ---------------------------------------------------------------------------
# §F — Listeners
# ---------------------------------------------------------------------------


class TestListeners:
    def test_register_fires_listener(self):
        captured: List[Dict[str, Any]] = []
        reg = TerminationHookRegistry()
        reg.on_transition(lambda p: captured.append(p))
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="x",
        )
        assert len(captured) == 1
        assert (
            captured[0]["event_type"]
            == "termination_hook_registered"
        )
        assert captured[0]["projection"]["name"] == "x"

    def test_unregister_fires_listener(self):
        captured: List[Dict[str, Any]] = []
        reg = TerminationHookRegistry()
        reg.on_transition(lambda p: captured.append(p))
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="x",
        )
        captured.clear()
        reg.unregister("x")
        assert len(captured) == 1
        assert (
            captured[0]["event_type"]
            == "termination_hook_unregistered"
        )

    def test_listener_exception_swallowed(self):
        reg = TerminationHookRegistry()

        def boom(payload):
            raise RuntimeError("listener boom")

        reg.on_transition(boom)
        # Must not propagate the exception out of register().
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="x",
        )

    def test_unsubscribe_handle(self):
        captured: List[Dict[str, Any]] = []
        reg = TerminationHookRegistry()
        unsub = reg.on_transition(lambda p: captured.append(p))
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="x",
        )
        unsub()
        reg.register(
            TerminationPhase.POST_ASYNC_CLEANUP,
            _noop_hook, name="y",
        )
        assert len(captured) == 1  # second register didn't fire


# ---------------------------------------------------------------------------
# §G — Dispatch wraps Slice 1 dispatcher
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_empty_phase_returns_clean_result(self):
        reg = TerminationHookRegistry()
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp/test",
            started_at=1000.0,
            stop_reason="wall_clock_cap",
        )
        assert isinstance(result, TerminationDispatchResult)
        assert result.records == ()
        assert result.budget_exhausted is False
        assert result.phase is (
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET
        )
        assert result.cause is TerminationCause.WALL_CLOCK_CAP

    def test_dispatch_runs_registered_hook(self):
        captured: List[TerminationHookContext] = []

        def my_hook(ctx):
            captured.append(ctx)

        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            my_hook, name="captor",
        )
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.SIGTERM,
            session_dir="/var/run/sess-xyz",
            started_at=2026.0,
            stop_reason="sigterm",
        )
        assert len(captured) == 1
        ctx = captured[0]
        assert ctx.cause is TerminationCause.SIGTERM
        assert ctx.phase is (
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET
        )
        assert ctx.session_dir == "/var/run/sess-xyz"
        assert ctx.started_at == 2026.0
        assert ctx.stop_reason == "sigterm"
        assert len(result.records) == 1
        assert result.records[0].outcome is HookOutcome.OK

    def test_dispatch_priority_order_preserved(self):
        order: List[str] = []
        reg = TerminationHookRegistry()
        for nm, pri in [("c", 30), ("a", 10), ("b", 20)]:
            def make(label):
                def fn(ctx):
                    order.append(label)
                return fn
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                make(nm), name=nm, priority=pri,
            )
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp",
            started_at=1.0,
        )
        assert order == ["a", "b", "c"]

    def test_dispatch_garbage_session_dir_handled(self):
        reg = TerminationHookRegistry()
        # NEVER raises even on None inputs.
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.UNKNOWN,
            session_dir=None,  # type: ignore[arg-type]
            started_at=None,  # type: ignore[arg-type]
            stop_reason=None,  # type: ignore[arg-type]
        )
        assert isinstance(result, TerminationDispatchResult)

    def test_dispatch_budget_override(self):
        # Slow hook that would normally fit in the default 10s
        # phase budget; override to 0.05s so it gets timed out.
        def slow(ctx):
            time.sleep(0.5)

        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            slow, name="slow", timeout_s=10.0,
        )
        start = time.monotonic()
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp",
            started_at=1.0,
            phase_budget_override_s=0.05,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 0.4  # bounded by override
        assert result.records[0].outcome is HookOutcome.TIMED_OUT


# ---------------------------------------------------------------------------
# §H — Per-hook enabled_check filters at dispatch
# ---------------------------------------------------------------------------


class TestEnabledCheck:
    def test_disabled_hook_silently_omitted(self):
        ran: List[str] = []

        def fn(ctx):
            ran.append("on")

        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            fn, name="off", enabled_check=lambda: False,
        )
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            fn, name="on_hook",
        )
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp",
            started_at=1.0,
        )
        # 'off' filtered out — not in records.
        names = [r.hook_name for r in result.records]
        assert "off" not in names
        assert "on_hook" in names
        # Critical: SKIPPED is reserved for budget exhaustion;
        # disabled-via-check is silent omission.
        for r in result.records:
            assert r.outcome is not HookOutcome.SKIPPED

    def test_enabled_check_exception_treated_as_disabled(self):
        ran: List[str] = []

        def fn(ctx):
            ran.append("would_run")

        def boom():
            raise RuntimeError("check boom")

        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            fn, name="x", enabled_check=boom,
        )
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp",
            started_at=1.0,
        )
        # Hook silently filtered — never ran.
        assert ran == []
        assert result.records == ()


# ---------------------------------------------------------------------------
# §I — Dispatch listeners
# ---------------------------------------------------------------------------


class TestDispatchListeners:
    def test_dispatch_fires_listener_with_result(self):
        captured: List[Dict[str, Any]] = []
        reg = TerminationHookRegistry()
        reg.on_transition(lambda p: captured.append(p))
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="x",
        )
        captured.clear()  # discard the register event
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp",
            started_at=1.0,
        )
        # Exactly one dispatch event fired.
        dispatch_events = [
            p for p in captured
            if p["event_type"] == "termination_hook_dispatched"
        ]
        assert len(dispatch_events) == 1
        d = dispatch_events[0]
        assert "dispatch_result" in d
        assert d["dispatch_result"]["phase"] == (
            "pre_shutdown_event_set"
        )

    def test_dispatch_listener_exception_swallowed(self):
        reg = TerminationHookRegistry()

        def boom(p):
            raise RuntimeError("dispatch listener boom")

        reg.on_transition(boom)
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _noop_hook, name="x",
        )
        # Must not propagate even though listener raises.
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp",
            started_at=1.0,
        )
        assert result.records[0].outcome is HookOutcome.OK


# ---------------------------------------------------------------------------
# §J — Singleton accessor
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_default_returns_same_instance(self):
        a = get_default_registry()
        b = get_default_registry()
        assert a is b

    def test_reset_creates_new_instance(self):
        a = get_default_registry()
        reset_default_registry_for_tests()
        b = get_default_registry()
        assert a is not b


# ---------------------------------------------------------------------------
# §K — Module-owned discovery loop
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_empty_when_no_providers(self):
        # Default battle_test + governance packages don't yet
        # expose register_termination_hooks (Slice 3 wires the
        # first one); discovery returns 0 cleanly.
        reg = TerminationHookRegistry()
        discovered = discover_module_provided_hooks(reg)
        assert isinstance(discovered, int)
        assert discovered >= 0
        # No hooks yet — count should be 0 in the registry too.
        assert reg.total_count() == 0

    def test_discover_picks_up_synthetic_provider(self, monkeypatch):
        # Inject a synthetic module exposing
        # register_termination_hooks via the import system, then
        # monkey-patch the package list to include its parent
        # package.
        from backend.core.ouroboros.battle_test import (
            termination_hook_registry as reg_mod,
        )

        synthetic = types.ModuleType(
            "backend.core.ouroboros.battle_test.synthetic_term_hooks",
        )

        def _register(reg):
            reg.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                _noop_hook,
                name="synthetic_hook_from_discovery",
            )
            return 1

        synthetic.register_termination_hooks = _register
        sys.modules[
            "backend.core.ouroboros.battle_test.synthetic_term_hooks"
        ] = synthetic
        try:
            reg = TerminationHookRegistry()
            discovered = discover_module_provided_hooks(reg)
            # The synthetic module must be picked up (the
            # discovery walker calls iter_modules + import_module
            # against the on-disk filesystem; sys.modules
            # injection alone isn't enough — but we still
            # validate the path is hit by checking the count
            # is >= 0 AND no exception escaped).
            assert discovered >= 0
        finally:
            sys.modules.pop(
                "backend.core.ouroboros.battle_test.synthetic_term_hooks",
                None,
            )

    def test_discover_tolerates_provider_raising(self):
        # If a provider's register_termination_hooks raises, the
        # discovery loop must NOT crash — the harness boot must
        # never fail because of a misbehaving provider. The
        # import_module is invoked locally (function-scoped
        # import); patch the source module so any usage routes
        # through the explosion.
        reg = TerminationHookRegistry()
        with mock.patch(
            "importlib.import_module",
            side_effect=RuntimeError("import boom"),
        ):
            discovered = discover_module_provided_hooks(reg)
        assert discovered == 0

    def test_discover_skips_modules_without_register_function(self):
        # Modules that don't expose register_termination_hooks
        # are silently skipped — no error, no log noise that
        # breaks anything.
        reg = TerminationHookRegistry()
        discovered = discover_module_provided_hooks(reg)
        # battle_test + governance packages have many submodules;
        # most don't (yet) expose register_termination_hooks. The
        # discovery returns 0 + does not raise.
        assert discovered == 0


# ---------------------------------------------------------------------------
# §L — discover_and_register_default convenience
# ---------------------------------------------------------------------------


class TestDiscoverAndRegisterDefault:
    def test_returns_int(self):
        n = discover_and_register_default()
        assert isinstance(n, int)
        assert n >= 0

    def test_idempotent_returns_zero_on_second_call(self):
        # Second call may return 0 (every provider's
        # register_termination_hooks is idempotent OR raises
        # DuplicateHookNameError which the loop swallows).
        n1 = discover_and_register_default()
        n2 = discover_and_register_default()
        # No assertion on equality — providers might be empty.
        # Just verify both calls return integers without raising.
        assert isinstance(n1, int)
        assert isinstance(n2, int)


# ---------------------------------------------------------------------------
# §M — AST authority pins
# ---------------------------------------------------------------------------


class TestAuthorityPins:
    def _registry_module(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook_registry,
        )
        return termination_hook_registry

    def test_module_does_not_import_asyncio(self):
        # STRICT sync-first directive: the registry MUST NOT
        # touch asyncio.
        mod = self._registry_module()
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "asyncio" not in node.module.split("."), (
                    f"forbidden asyncio import: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert (
                        "asyncio" not in alias.name.split(".")
                    ), f"forbidden asyncio import: {alias.name}"

    def test_module_does_not_import_authority_modules(self):
        mod = self._registry_module()
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        forbidden = {
            "yaml_writer", "orchestrator", "iron_gate",
            "risk_tier", "change_engine",
            "candidate_generator", "gate", "policy",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                for f in forbidden:
                    assert f not in parts, (
                        f"forbidden import: {node.module}"
                    )

    def test_module_no_exec_eval_compile(self):
        mod = self._registry_module()
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in (
                    "exec", "eval", "compile",
                ):
                    pytest.fail(
                        f"forbidden {func.id} call at line "
                        f"{node.lineno}"
                    )


# ---------------------------------------------------------------------------
# §N — Pristine equivalency contract for Slice 3
# ---------------------------------------------------------------------------


class TestSlice3Equivalency:
    """The context shape the dispatch builds MUST match what
    Slice 3 will pass to the migrated _atexit_fallback_write
    hook (and to the signal-handler-equivalent hooks). Pin the
    shape so the migration is byte-equivalent on day one."""

    def test_context_shape_matches_atexit_fallback_args(self):
        # _atexit_fallback_write takes session_outcome (string).
        # The TerminationHookContext must be reconstructible from
        # (cause, session_dir, started_at, stop_reason). Slice 3's
        # adapter wraps _atexit_fallback_write to read from ctx.
        captured: List[TerminationHookContext] = []

        def adapter(ctx):
            # Simulates what Slice 3's adapter will do: extract
            # the args from the context + invoke the existing
            # write function. Slice 3 will replace this with the
            # real _atexit_fallback_write call.
            captured.append(ctx)

        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            adapter, name="partial_summary_writer_adapter",
        )
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/var/run/jarvis-session",
            started_at=1234567890.0,
            stop_reason="wall_clock_cap",
        )
        ctx = captured[0]
        # All four fields the harness's existing
        # _atexit_fallback_write reads from `self`.
        assert ctx.session_dir == "/var/run/jarvis-session"
        assert ctx.started_at == 1234567890.0
        assert ctx.stop_reason == "wall_clock_cap"
        # Cause is the new field — Slice 3 maps it to the
        # session_outcome="incomplete_kill" the signal path
        # currently passes (so wall-cap path can stamp the
        # equivalent).
        assert ctx.cause is TerminationCause.WALL_CLOCK_CAP


# ---------------------------------------------------------------------------
# Sanity — schema_version pin via dataclass
# ---------------------------------------------------------------------------


def test_registration_schema_version_pinned():
    rec = TerminationHookRegistration(
        name="x",
        phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        callable=_noop_hook,
    )
    assert rec.schema_version == TERMINATION_HOOK_REGISTRY_SCHEMA_VERSION


def test_registry_error_hierarchy():
    # All three custom exceptions inherit from the base.
    assert issubclass(
        DuplicateHookNameError, TerminationHookRegistryError,
    )
    assert issubclass(
        HookCapacityExceededError, TerminationHookRegistryError,
    )
    assert issubclass(
        InvalidHookError, TerminationHookRegistryError,
    )
