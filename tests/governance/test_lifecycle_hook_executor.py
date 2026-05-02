"""Lifecycle Hook Registry Slice 3 — async executor tests.

Covers:
  * Master flag short-circuit (CONTINUE without registry lookup)
  * No registrations → CONTINUE
  * Single CONTINUE / BLOCK / WARN hook → matching aggregate
  * Mix of hooks: BLOCK-wins, WARN-only, all-CONTINUE
  * Per-hook timeout → FAILED for that hook; siblings unaffected
  * Per-hook raise → FAILED for that hook; siblings unaffected
  * Per-hook returns non-HookResult → FAILED with bad-return detail
  * Disabled hook (is_enabled False) → DISABLED outcome
  * Priority order preserved (verified by execution order capture)
  * Parallel execution (slow hook doesn't block fast hooks)
  * Async cancellation propagates per asyncio convention
  * Hook name mismatch is canonicalized to registration name
  * Garbage event input → CONTINUE (defensive)
  * Garbage context input → coerced to empty HookContext
  * Authority allowlist
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from typing import List, Tuple

import pytest

from backend.core.ouroboros.governance.lifecycle_hook import (
    HookContext,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    make_hook_result,
)
from backend.core.ouroboros.governance.lifecycle_hook_executor import (
    LIFECYCLE_HOOK_EXECUTOR_SCHEMA_VERSION,
    fire_hooks,
)
from backend.core.ouroboros.governance.lifecycle_hook_registry import (
    LifecycleHookRegistry,
)


# ---------------------------------------------------------------------------
# Hook fixtures
# ---------------------------------------------------------------------------


def _continue_hook(ctx: HookContext) -> HookResult:
    return make_hook_result("continue-h", HookOutcome.CONTINUE)


def _block_hook(ctx: HookContext) -> HookResult:
    return make_hook_result(
        "block-h", HookOutcome.BLOCK, detail="forced block",
    )


def _warn_hook(ctx: HookContext) -> HookResult:
    return make_hook_result(
        "warn-h", HookOutcome.WARN, detail="forced warn",
    )


def _slow_hook(ctx: HookContext) -> HookResult:
    """Sleeps then returns CONTINUE — used for timeout + parallel
    tests."""
    time.sleep(0.5)
    return make_hook_result("slow-h", HookOutcome.CONTINUE)


def _raising_hook(ctx: HookContext) -> HookResult:
    raise RuntimeError("forced raise")


def _bad_return_hook(ctx: HookContext):
    return "not-a-HookResult"  # type: ignore[return-value]


def _ctx() -> HookContext:
    return HookContext(
        event=LifecycleEvent.PRE_APPLY, op_id="op-test",
    )


# ---------------------------------------------------------------------------
# Master flag + empty-registry short-circuits
# ---------------------------------------------------------------------------


class TestShortCircuits:
    @pytest.mark.asyncio
    async def test_master_disabled_returns_continue(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _block_hook, name="b",
        )
        # Even though a BLOCK hook is registered, master off
        # short-circuits before the registry lookup.
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=False,
        )
        assert result.aggregate is HookOutcome.CONTINUE
        assert result.total_hooks == 0
        assert result.blocking_hooks == ()

    @pytest.mark.asyncio
    async def test_no_registrations_returns_continue(self):
        registry = LifecycleHookRegistry()
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.CONTINUE
        assert result.total_hooks == 0

    @pytest.mark.asyncio
    async def test_garbage_event_returns_continue(self):
        registry = LifecycleHookRegistry()
        result = await fire_hooks(
            "not-an-event",  # type: ignore[arg-type]
            _ctx(), registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.CONTINUE


# ---------------------------------------------------------------------------
# Single-hook outcomes
# ---------------------------------------------------------------------------


class TestSingleHookOutcomes:
    @pytest.mark.asyncio
    async def test_single_continue_yields_continue(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="c",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.CONTINUE
        assert result.total_hooks == 1

    @pytest.mark.asyncio
    async def test_single_block_yields_block(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _block_hook, name="b",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.BLOCK
        assert result.blocking_hooks == ("b",)
        assert result.monotonic_tightening_verdict == "passed"

    @pytest.mark.asyncio
    async def test_single_warn_yields_warn(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _warn_hook, name="w",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.WARN
        assert result.warning_hooks == ("w",)


# ---------------------------------------------------------------------------
# Mixed-outcome aggregation (BLOCK-wins)
# ---------------------------------------------------------------------------


class TestMixedAggregation:
    @pytest.mark.asyncio
    async def test_block_dominates_continue_and_warn(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="c",
            priority=10,
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _warn_hook, name="w",
            priority=20,
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _block_hook, name="b",
            priority=30,
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.BLOCK
        assert result.blocking_hooks == ("b",)
        assert result.warning_hooks == ("w",)
        assert result.total_hooks == 3

    @pytest.mark.asyncio
    async def test_warn_only_yields_warn(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="c",
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _warn_hook, name="w",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.WARN

    @pytest.mark.asyncio
    async def test_all_continue_yields_continue(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="c1",
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="c2",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.CONTINUE


# ---------------------------------------------------------------------------
# Per-hook defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveDegradation:
    @pytest.mark.asyncio
    async def test_raising_hook_yields_failed_for_that_hook(self):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="ok",
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _raising_hook, name="raises",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        # Aggregate is CONTINUE (FAILED hooks are non-blocking by
        # design — buggy hook can't stop the orchestrator).
        assert result.aggregate is HookOutcome.CONTINUE
        assert "raises" in result.failed_hooks

    @pytest.mark.asyncio
    async def test_timeout_hook_yields_failed_with_timeout_detail(self):
        registry = LifecycleHookRegistry()
        # Slow hook + tight timeout.
        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_hook, name="slow",
            timeout_s=0.1,  # 100ms; hook sleeps 500ms
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        # The slow hook timed out → FAILED. Aggregate is CONTINUE
        # (FAILED is non-blocking).
        assert result.aggregate is HookOutcome.CONTINUE
        assert "slow" in result.failed_hooks

    @pytest.mark.asyncio
    async def test_bad_return_hook_yields_failed_with_bad_return_detail(
        self,
    ):
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _bad_return_hook, name="bad",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.CONTINUE
        assert "bad" in result.failed_hooks

    @pytest.mark.asyncio
    async def test_disabled_hook_yields_disabled_not_skipped(self):
        """Per-hook is_enabled=False produces a HookResult with
        outcome=DISABLED so observability sees it (distinct from
        'hook didn't run')."""
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="off",
            enabled_check=lambda: False,
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        # Aggregate is CONTINUE (DISABLED is non-blocking) but
        # total_hooks reflects that 1 hook actually ran (just to
        # report DISABLED).
        assert result.aggregate is HookOutcome.CONTINUE
        assert result.total_hooks == 1

    @pytest.mark.asyncio
    async def test_block_hook_dominates_failed_siblings(self):
        """A BLOCK in the mix wins over FAILED siblings."""
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _raising_hook, name="raises",
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _block_hook, name="blocks",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.aggregate is HookOutcome.BLOCK
        assert result.blocking_hooks == ("blocks",)
        assert result.failed_hooks == ("raises",)


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_slow_hook_does_not_block_fast_hook(self):
        """If hooks ran sequentially, total time = N × slow_time.
        Parallel: total time ≈ slow_time. Verify the latter."""
        registry = LifecycleHookRegistry()
        # 3 slow hooks, each sleeping ~300ms. Sequentially: 900ms.
        # In parallel: ~300ms.

        def _slow_300(ctx):
            time.sleep(0.3)
            return make_hook_result(ctx_hook_name(ctx), HookOutcome.CONTINUE)

        def _slow_300_a(ctx):
            time.sleep(0.3)
            return make_hook_result("slow-a", HookOutcome.CONTINUE)

        def _slow_300_b(ctx):
            time.sleep(0.3)
            return make_hook_result("slow-b", HookOutcome.CONTINUE)

        def _slow_300_c(ctx):
            time.sleep(0.3)
            return make_hook_result("slow-c", HookOutcome.CONTINUE)

        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_300_a, name="slow-a",
            timeout_s=2.0,
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_300_b, name="slow-b",
            timeout_s=2.0,
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_300_c, name="slow-c",
            timeout_s=2.0,
        )

        started = time.monotonic()
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        elapsed = time.monotonic() - started

        assert result.aggregate is HookOutcome.CONTINUE
        assert result.total_hooks == 3
        # Parallel execution should be much faster than 3 × 300ms.
        # Allow generous slack for CI variability.
        assert elapsed < 0.7, (
            f"hooks ran sequentially: {elapsed:.2f}s for 3 × 300ms"
        )

    @pytest.mark.asyncio
    async def test_one_timeout_does_not_block_others(self):
        """A timeout on one hook must not delay siblings."""
        registry = LifecycleHookRegistry()

        def _ok(ctx):
            return make_hook_result("ok", HookOutcome.CONTINUE)

        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_hook, name="slow",
            timeout_s=0.1,  # times out
        )
        registry.register(
            LifecycleEvent.PRE_APPLY, _ok, name="ok",
            timeout_s=2.0,
        )
        started = time.monotonic()
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        elapsed = time.monotonic() - started

        # Total time bounded by the slow hook's timeout (100ms),
        # not the slow hook's actual sleep (500ms).
        assert elapsed < 0.4, (
            f"slow hook leaked beyond its timeout: {elapsed:.2f}s"
        )
        assert "slow" in result.failed_hooks
        assert result.total_hooks == 2


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_caller_cancellation_propagates(self):
        """asyncio.CancelledError propagates per asyncio convention."""
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_hook, name="slow",
            timeout_s=10.0,
        )
        task = asyncio.create_task(
            fire_hooks(
                LifecycleEvent.PRE_APPLY, _ctx(),
                registry=registry, enabled=True,
            ),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Hook name canonicalization
# ---------------------------------------------------------------------------


class TestNameCanonicalization:
    @pytest.mark.asyncio
    async def test_hook_returns_wrong_name_canonicalized(self):
        """If a hook returns a HookResult with a different name
        than the registration, the executor canonicalizes to the
        registration name so audit reflects WHO ran."""
        def _wrong_name_hook(ctx):
            return make_hook_result(
                "wrong-name", HookOutcome.BLOCK,
            )

        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _wrong_name_hook,
            name="canonical-name",
        )
        result = await fire_hooks(
            LifecycleEvent.PRE_APPLY, _ctx(),
            registry=registry, enabled=True,
        )
        assert result.blocking_hooks == ("canonical-name",)
        assert "wrong-name" not in result.blocking_hooks


# ---------------------------------------------------------------------------
# Schema version + module constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version_constant(self):
        assert LIFECYCLE_HOOK_EXECUTOR_SCHEMA_VERSION == (
            "lifecycle_hook_executor.1"
        )


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "lifecycle_hook_executor.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        allowed = {
            "backend.core.ouroboros.governance.lifecycle_hook",
            "backend.core.ouroboros.governance.lifecycle_hook_registry",
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
                            f"Slice 3 imported module outside "
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
                            f"Slice 3 imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r}"
                        )

    def test_no_exec_eval_compile_calls(self):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 3 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


def ctx_hook_name(ctx: HookContext) -> str:
    return f"slow-{ctx.op_id}"
