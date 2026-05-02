"""Lifecycle Hook Registry Slice 2 — sync registry tests.

Covers:
  * Construction (default + explicit max_per_event clamp)
  * register() happy path + priority insertion ordering
  * register() validation: non-event / non-callable / empty name
    / duplicate name / capacity exceeded all raise explicit
    typed exceptions
  * unregister() removes; returns False on unknown; NEVER raises
  * for_event() returns priority-ordered tuple
  * snapshot / snapshot_all / names introspection
  * on_transition listener fires on register + unregister
  * Listener exceptions swallowed (don't block registrations)
  * Singleton accessor + reset
  * Thread-safety smoke (multi-thread concurrent register)
  * HookRegistration.is_enabled gate evaluation
  * discover_module_provided_hooks loop runs cleanly when no
    modules provide hooks
  * AST-walked authority allowlist
"""
from __future__ import annotations

import ast
import pathlib
import threading
import time
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.lifecycle_hook import (
    HookContext,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    make_hook_result,
)
from backend.core.ouroboros.governance.lifecycle_hook_registry import (
    DuplicateHookNameError,
    HookCapacityExceededError,
    HookRegistration,
    InvalidHookError,
    LIFECYCLE_HOOK_REGISTRY_SCHEMA_VERSION,
    LifecycleHookRegistry,
    discover_module_provided_hooks,
    get_default_registry,
    reset_default_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Hook fixtures
# ---------------------------------------------------------------------------


def _continue_hook(ctx: HookContext) -> HookResult:
    return make_hook_result("continue-h", HookOutcome.CONTINUE)


def _block_hook(ctx: HookContext) -> HookResult:
    return make_hook_result(
        "block-h", HookOutcome.BLOCK, detail="test block",
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_max_per_event_uses_env(self):
        r = LifecycleHookRegistry()
        assert r.max_per_event == 16  # default

    def test_explicit_max_per_event_clamped_floor(self):
        r = LifecycleHookRegistry(max_per_event=0)
        assert r.max_per_event == 1

    def test_explicit_max_per_event_clamped_ceiling(self):
        r = LifecycleHookRegistry(max_per_event=9999)
        assert r.max_per_event == 256

    def test_garbage_max_per_event_uses_default(self):
        r = LifecycleHookRegistry(
            max_per_event="not-a-number",  # type: ignore[arg-type]
        )
        assert r.max_per_event == 16

    def test_initially_empty(self):
        r = LifecycleHookRegistry()
        assert r.total_count() == 0
        for ev in LifecycleEvent:
            assert r.count_for_event(ev) == 0
            assert r.for_event(ev) == ()


# ---------------------------------------------------------------------------
# register() validation
# ---------------------------------------------------------------------------


class TestRegisterValidation:
    def test_non_event_raises_invalid_hook(self):
        r = LifecycleHookRegistry()
        with pytest.raises(InvalidHookError):
            r.register(
                "not-an-event",  # type: ignore[arg-type]
                _continue_hook, name="x",
            )

    def test_non_callable_raises_invalid_hook(self):
        r = LifecycleHookRegistry()
        with pytest.raises(InvalidHookError):
            r.register(
                LifecycleEvent.PRE_APPLY,
                "not-callable",  # type: ignore[arg-type]
                name="x",
            )

    def test_empty_name_raises_invalid_hook(self):
        r = LifecycleHookRegistry()
        with pytest.raises(InvalidHookError):
            r.register(
                LifecycleEvent.PRE_APPLY, _continue_hook, name="",
            )

    def test_whitespace_name_raises_invalid_hook(self):
        r = LifecycleHookRegistry()
        with pytest.raises(InvalidHookError):
            r.register(
                LifecycleEvent.PRE_APPLY, _continue_hook, name="   ",
            )

    def test_duplicate_name_raises_duplicate_error(self):
        r = LifecycleHookRegistry()
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="dup",
        )
        with pytest.raises(DuplicateHookNameError):
            r.register(
                LifecycleEvent.POST_APPLY, _continue_hook, name="dup",
            )

    def test_capacity_exceeded_raises_capacity_error(self):
        r = LifecycleHookRegistry(max_per_event=2)
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h2",
        )
        with pytest.raises(HookCapacityExceededError):
            r.register(
                LifecycleEvent.PRE_APPLY, _continue_hook, name="h3",
            )

    def test_capacity_per_event_independent(self):
        """Capacity is per-event, not total."""
        r = LifecycleHookRegistry(max_per_event=2)
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="h1")
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="h2")
        # Different event has its own capacity.
        r.register(LifecycleEvent.POST_APPLY, _continue_hook, name="h3")
        assert r.total_count() == 3


# ---------------------------------------------------------------------------
# register() happy path
# ---------------------------------------------------------------------------


class TestRegisterHappyPath:
    def test_register_returns_registration(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert isinstance(reg, HookRegistration)
        assert reg.name == "h1"
        assert reg.event is LifecycleEvent.PRE_APPLY

    def test_register_truncates_long_name(self):
        r = LifecycleHookRegistry()
        long_name = "x" * 500
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name=long_name,
        )
        assert len(reg.name) == 128

    def test_register_default_priority_is_100(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert reg.priority == 100

    def test_register_default_timeout_uses_env(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert reg.timeout_s == 5.0  # env default

    def test_register_custom_timeout_clamped(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
            timeout_s=999.0,
        )
        assert reg.timeout_s == 60.0  # ceiling

    def test_register_garbage_priority_defaults_to_100(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
            priority="not-a-number",  # type: ignore[arg-type]
        )
        assert reg.priority == 100

    def test_register_increments_counts(self):
        r = LifecycleHookRegistry()
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="h1")
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="h2")
        r.register(LifecycleEvent.POST_APPLY, _continue_hook, name="h3")
        assert r.total_count() == 3
        assert r.count_for_event(LifecycleEvent.PRE_APPLY) == 2
        assert r.count_for_event(LifecycleEvent.POST_APPLY) == 1


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    def test_for_event_returns_priority_ordered_tuple(self):
        r = LifecycleHookRegistry()
        # Register in opposite-of-priority order; for_event must
        # return them in priority order.
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="last", priority=300,
        )
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="first", priority=10,
        )
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="middle", priority=100,
        )
        regs = r.for_event(LifecycleEvent.PRE_APPLY)
        assert [reg.name for reg in regs] == ["first", "middle", "last"]

    def test_for_event_stable_for_equal_priority(self):
        """Registrations with the same priority preserve insertion
        order."""
        r = LifecycleHookRegistry()
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="a", priority=100,
        )
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="b", priority=100,
        )
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="c", priority=100,
        )
        regs = r.for_event(LifecycleEvent.PRE_APPLY)
        assert [reg.name for reg in regs] == ["a", "b", "c"]

    def test_for_event_unknown_event_returns_empty(self):
        r = LifecycleHookRegistry()
        assert r.for_event(LifecycleEvent.POST_VERIFY) == ()

    def test_for_event_garbage_returns_empty(self):
        r = LifecycleHookRegistry()
        assert r.for_event("not-an-event") == ()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# unregister()
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_unregister_removes_known_hook(self):
        r = LifecycleHookRegistry()
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert r.unregister("h1") is True
        assert r.total_count() == 0
        assert r.count_for_event(LifecycleEvent.PRE_APPLY) == 0

    def test_unregister_unknown_returns_false(self):
        r = LifecycleHookRegistry()
        assert r.unregister("never-registered") is False

    def test_unregister_empty_name_returns_false(self):
        r = LifecycleHookRegistry()
        assert r.unregister("") is False
        assert r.unregister("   ") is False

    def test_unregister_after_register_allows_reregister(self):
        r = LifecycleHookRegistry()
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        r.unregister("h1")
        # Should be free to register again with same name.
        r.register(
            LifecycleEvent.POST_APPLY, _continue_hook, name="h1",
        )
        assert r.total_count() == 1

    def test_unregister_never_raises(self):
        r = LifecycleHookRegistry()
        for bad in [None, 42, object()]:
            try:
                r.unregister(bad)  # type: ignore[arg-type]
            except Exception:
                pytest.fail(f"unregister raised on {bad!r}")


# ---------------------------------------------------------------------------
# Snapshot + names introspection
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_returns_projection(self):
        r = LifecycleHookRegistry()
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook,
            name="h1", priority=50,
        )
        snap = r.snapshot("h1")
        assert snap is not None
        assert snap["name"] == "h1"
        assert snap["event"] == "pre_apply"
        assert snap["priority"] == 50
        assert snap["has_enabled_check"] is False
        # Callable + enabled_check NOT in projection.
        assert "callable" not in snap
        assert "enabled_check" not in snap

    def test_snapshot_unknown_returns_none(self):
        r = LifecycleHookRegistry()
        assert r.snapshot("never-registered") is None

    def test_snapshot_all_alphabetical(self):
        r = LifecycleHookRegistry()
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="zebra")
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="alpha")
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="middle")
        names = [s["name"] for s in r.snapshot_all()]
        assert names == ["alpha", "middle", "zebra"]

    def test_names_alphabetical(self):
        r = LifecycleHookRegistry()
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="b")
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="a")
        assert r.names() == ("a", "b")


# ---------------------------------------------------------------------------
# Listener pattern
# ---------------------------------------------------------------------------


class TestListeners:
    def test_listener_fires_on_register(self):
        r = LifecycleHookRegistry()
        events = []
        unsub = r.on_transition(lambda p: events.append(p))
        r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert len(events) == 1
        assert events[0]["event_type"] == "hook_registered"
        assert events[0]["projection"]["name"] == "h1"
        unsub()

    def test_listener_fires_on_unregister(self):
        r = LifecycleHookRegistry()
        events = []
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="h1")
        unsub = r.on_transition(lambda p: events.append(p))
        r.unregister("h1")
        assert len(events) == 1
        assert events[0]["event_type"] == "hook_unregistered"
        unsub()

    def test_unsub_stops_further_events(self):
        r = LifecycleHookRegistry()
        events = []
        unsub = r.on_transition(lambda p: events.append(p))
        unsub()
        r.register(LifecycleEvent.PRE_APPLY, _continue_hook, name="h1")
        assert events == []

    def test_broken_listener_does_not_block_register(self):
        """Listener exception swallowed — registration must succeed."""
        r = LifecycleHookRegistry()

        def _broken(payload):
            raise RuntimeError("boom")

        r.on_transition(_broken)
        # Must NOT raise.
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert reg.name == "h1"
        assert r.total_count() == 1


# ---------------------------------------------------------------------------
# HookRegistration.is_enabled
# ---------------------------------------------------------------------------


class TestRegistrationEnabledCheck:
    def test_default_no_check_is_enabled_true(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
        )
        assert reg.is_enabled() is True

    def test_check_returns_true_enables(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
            enabled_check=lambda: True,
        )
        assert reg.is_enabled() is True

    def test_check_returns_false_disables(self):
        r = LifecycleHookRegistry()
        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
            enabled_check=lambda: False,
        )
        assert reg.is_enabled() is False

    def test_check_raises_treated_as_disabled(self):
        r = LifecycleHookRegistry()

        def _broken_check() -> bool:
            raise RuntimeError("boom")

        reg = r.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="h1",
            enabled_check=_broken_check,
        )
        assert reg.is_enabled() is False


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


class TestSingletonAccessor:
    def test_get_default_returns_registry(self):
        reset_default_registry_for_tests()
        r = get_default_registry()
        assert isinstance(r, LifecycleHookRegistry)

    def test_singleton_stable_across_calls(self):
        reset_default_registry_for_tests()
        r1 = get_default_registry()
        r2 = get_default_registry()
        assert r1 is r2

    def test_reset_drops_singleton(self):
        r1 = get_default_registry()
        reset_default_registry_for_tests()
        r2 = get_default_registry()
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Thread safety smoke
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_registers_all_succeed(self):
        r = LifecycleHookRegistry(max_per_event=256)
        N = 50
        errors: list = []

        def _register(i: int) -> None:
            try:
                r.register(
                    LifecycleEvent.PRE_APPLY, _continue_hook,
                    name=f"hook-{i}",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append((i, exc))

        threads = [
            threading.Thread(target=_register, args=(i,))
            for i in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert r.total_count() == N


# ---------------------------------------------------------------------------
# discover_module_provided_hooks
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_runs_cleanly_when_no_modules_provide_hooks(self):
        """No module currently exposes register_lifecycle_hooks
        (Slice 4 will start adding them). Discovery should run
        without error and return 0."""
        r = LifecycleHookRegistry(max_per_event=256)
        count = discover_module_provided_hooks(r)
        assert count == 0
        # Registry stays empty.
        assert r.total_count() == 0

    def test_discover_never_raises(self):
        r = LifecycleHookRegistry()
        # Passing garbage — even if a hypothetical provider called
        # registry.register with bad inputs, the discovery loop's
        # try/except per-module catches.
        try:
            discover_module_provided_hooks(r)
        except Exception:
            pytest.fail("discover raised")


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert LIFECYCLE_HOOK_REGISTRY_SCHEMA_VERSION == (
            "lifecycle_hook_registry.1"
        )


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "lifecycle_hook_registry.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        """Slice 2 may import:
          * Slice 1 lifecycle_hook (primitive)
        Module-owned register_* exempt (registration-contract)."""
        allowed = {
            "backend.core.ouroboros.governance.lifecycle_hook",
        }
        tree = ast.parse(self._source())
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or (
                    "governance" in module and module
                ):
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    if module not in allowed:
                        raise AssertionError(
                            f"Slice 2 imported module outside "
                            f"allowlist: {module!r} at line {lineno}"
                        )

    def test_no_orchestrator_tier_imports(self):
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for ban in banned_substrings:
                    if ban in module:
                        raise AssertionError(
                            f"Slice 2 imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r}"
                        )

    def test_no_exec_eval_compile_calls(self):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 2 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )
