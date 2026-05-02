"""Lifecycle Hook Registry Slice 5 — graduation regression spine.

Verifies the full Slices 1-4 stack composes end-to-end after the
master flag flips default-true and dynamic registration discovers
all 4 modules' contributions.

Coverage:
  * Master flag default-true post-graduation; explicit-false reverts
  * All 3 lifecycle hook flags discovered via FlagRegistry seed
  * All 8 lifecycle hook AST-pin invariants discovered + clean
  * discover_and_register_default convenience helper works
  * End-to-end through full stack: register hook → fire_hooks →
    aggregate → bridge → gate result with correct passed semantics
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.flag_registry import FlagRegistry
from backend.core.ouroboros.governance.flag_registry_seed import (
    seed_default_registry,
)
from backend.core.ouroboros.governance.lifecycle_hook import (
    HookContext,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    lifecycle_hooks_enabled,
    make_hook_result,
)
from backend.core.ouroboros.governance.lifecycle_hook_executor import (
    fire_hooks,
)
from backend.core.ouroboros.governance.lifecycle_hook_orchestrator_bridge import (
    gate_pre_apply,
)
from backend.core.ouroboros.governance.lifecycle_hook_registry import (
    LifecycleHookRegistry,
    discover_and_register_default,
    get_default_registry,
    reset_default_registry_for_tests,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_all,
)


# ---------------------------------------------------------------------------
# Master flag flip
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LIFECYCLE_HOOKS_ENABLED", raising=False,
        )
        assert lifecycle_hooks_enabled() is True

    def test_empty_string_is_default_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "")
        assert lifecycle_hooks_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "FALSE"],
    )
    def test_explicit_false_disables(self, monkeypatch, falsy: str):
        monkeypatch.setenv(
            "JARVIS_LIFECYCLE_HOOKS_ENABLED", falsy,
        )
        assert lifecycle_hooks_enabled() is False


# ---------------------------------------------------------------------------
# Dynamic flag discovery
# ---------------------------------------------------------------------------


class TestFlagDiscovery:
    def test_seed_discovers_all_3_lifecycle_hook_flags(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        hook_flags = [
            f for f in registry.list_all()
            if "LIFECYCLE_HOOKS" in f.name
        ]
        assert len(hook_flags) == 3

    def test_master_flag_default_is_true_in_registry(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec("JARVIS_LIFECYCLE_HOOKS_ENABLED")
        assert spec is not None
        assert spec.default is True

    def test_max_per_event_flag_present(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec(
            "JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT",
        )
        assert spec is not None
        assert spec.default == 16

    def test_default_timeout_flag_present(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec(
            "JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S",
        )
        assert spec is not None
        assert spec.default == 5.0


# ---------------------------------------------------------------------------
# Dynamic AST-pin discovery + clean validation
# ---------------------------------------------------------------------------


class TestInvariantDiscovery:
    def test_all_8_lifecycle_hook_invariants_discovered(self):
        invs = list_shipped_code_invariants()
        hook_invs = [
            i for i in invs
            if "lifecycle_hook" in i.invariant_name
        ]
        assert len(hook_invs) == 8

    def test_each_module_contributes_invariants(self):
        invs = list_shipped_code_invariants()
        names = {
            i.invariant_name for i in invs
            if "lifecycle_hook" in i.invariant_name
        }
        # Slice 1 lifecycle_hook: 3 invariants
        assert "lifecycle_hook_pure_stdlib" in names
        assert "lifecycle_hook_event_taxonomy_5_values" in names
        assert "lifecycle_hook_outcome_taxonomy_5_values" in names
        # Slice 2 lifecycle_hook_registry: 1 invariant
        assert "lifecycle_hook_registry_authority_allowlist" in names
        # Slice 3 lifecycle_hook_executor: 2 invariants
        assert "lifecycle_hook_executor_authority_allowlist" in names
        assert "lifecycle_hook_executor_fail_isolated" in names
        # Slice 4 lifecycle_hook_orchestrator_bridge: 2 invariants
        assert "lifecycle_hook_bridge_authority_allowlist" in names
        assert "lifecycle_hook_bridge_fail_open" in names

    def test_all_lifecycle_hook_invariants_validate_clean(self):
        violations = validate_all()
        hook_v = [
            v for v in violations
            if "lifecycle_hook" in v.invariant_name
        ]
        assert hook_v == [], (
            f"Lifecycle hook invariants drifted: "
            f"{[(v.invariant_name, v.detail) for v in hook_v]}"
        )


# ---------------------------------------------------------------------------
# Boot helper
# ---------------------------------------------------------------------------


class TestBootHelper:
    def test_discover_and_register_default_runs_cleanly(self):
        """No module currently exposes register_lifecycle_hooks
        (Slice 5b will start adding them). Returns 0 cleanly."""
        reset_default_registry_for_tests()
        count = discover_and_register_default()
        assert count == 0

    def test_discover_and_register_default_never_raises(self):
        try:
            discover_and_register_default()
        except Exception:
            pytest.fail("discover_and_register_default raised")


# ---------------------------------------------------------------------------
# End-to-end through full stack
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_e2e_block_propagates_through_full_stack(
        self, monkeypatch,
    ):
        """register hook → fire_hooks → aggregator → bridge → gate
        result. BLOCK at the hook propagates all the way to
        gate.passed=False with the hook's name in blocking_hooks."""
        # Master flag default-true post-graduation, but be explicit.
        monkeypatch.delenv(
            "JARVIS_LIFECYCLE_HOOKS_ENABLED", raising=False,
        )

        def _block(ctx: HookContext) -> HookResult:
            return make_hook_result(
                "e2e-blocker", HookOutcome.BLOCK,
                detail="grad block",
            )

        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _block, name="e2e-blocker",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply(
                "op-e2e", target_files=("foo.py",),
                diff_summary="end-to-end test",
            )
            assert gate.passed is False
            assert gate.aggregate is HookOutcome.BLOCK
            assert "e2e-blocker" in gate.blocking_hooks
            assert gate.is_tightening is True
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_e2e_continue_propagates_through_full_stack(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_LIFECYCLE_HOOKS_ENABLED", raising=False,
        )

        def _ok(ctx: HookContext) -> HookResult:
            return make_hook_result("e2e-ok", HookOutcome.CONTINUE)

        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _ok, name="e2e-ok",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply("op-e2e-ok")
            assert gate.passed is True
            assert gate.aggregate is HookOutcome.CONTINUE
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_e2e_explicit_false_reverts_to_passed_true(
        self, monkeypatch,
    ):
        """Backward-compat hot revert: explicit
        JARVIS_LIFECYCLE_HOOKS_ENABLED=false → fire_hooks
        short-circuits → gate returns passed=True even with
        BLOCK hooks registered."""
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "false")

        def _block(ctx: HookContext) -> HookResult:
            return make_hook_result(
                "would-block", HookOutcome.BLOCK,
            )

        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _block, name="would-block",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply("op-revert")
            # Master off → hot revert.
            assert gate.passed is True
            assert gate.aggregate is HookOutcome.CONTINUE
            assert gate.total_hooks == 0
        finally:
            reset_default_registry_for_tests()
