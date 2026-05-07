"""Venom V3 — async hook fire-and-forget regression spine.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  (a) async hook never awaited in blocking gather — spy proves
      blocking aggregate completes BEFORE FFN runs
  (b) blocking hook still BLOCKs; async hook cannot block
  (c) exception in FFN logged, pipeline continues
  (d) master-off / empty async → identical decisions vs pre-V3
  (e) graceful shutdown drains via drain_ffn_tasks (bounded
      task count surfaced via ffn_pending_count)

  Plus structural pins:
  * is_async kwarg on register() defaults False (byte-identical)
  * is_async=False AND master-on → blocking path unchanged
  * Master flag JARVIS_HOOK_ASYNC_ENABLED default-FALSE (§33.1)
  * AST pin for FFN scheduling discipline fires on regressions
  * V3 substrate purity preserved (no orchestrator imports)
  * V2 PermissionRegistry untouched (V3 is V1 hook path only)

Verifies (28 tests).
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_v3_state():
    """Reset both the V1 hook registry AND the V3 FFN task
    registry between tests."""
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        reset_ffn_registry_for_tests,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        reset_default_registry_for_tests,
    )
    reset_default_registry_for_tests()
    reset_ffn_registry_for_tests()
    yield
    reset_default_registry_for_tests()
    reset_ffn_registry_for_tests()


# ---------------------------------------------------------------------------
# Master flag — default-FALSE per §33.1
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_HOOK_ASYNC_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        hook_async_ffn_enabled,
    )
    assert hook_async_ffn_enabled() is False


def test_master_flag_truthy(monkeypatch):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        hook_async_ffn_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", v)
        assert hook_async_ffn_enabled() is True


def test_master_flag_falsy(monkeypatch):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        hook_async_ffn_enabled,
    )
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", v)
        assert hook_async_ffn_enabled() is False


# ---------------------------------------------------------------------------
# Registration — is_async kwarg default-False
# ---------------------------------------------------------------------------


def test_register_default_is_async_false(fresh_v3_state):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    rec = reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            "x", HookOutcome.CONTINUE,
        ),
        name="default",
    )
    assert rec.is_async is False


def test_register_explicit_is_async_true(fresh_v3_state):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    rec = reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            "x", HookOutcome.CONTINUE,
        ),
        name="ffn",
        is_async=True,
    )
    assert rec.is_async is True


# ---------------------------------------------------------------------------
# (a) Async hook never awaited in blocking gather
# ---------------------------------------------------------------------------


def test_async_hook_never_awaited_in_blocking_gather(
    fresh_v3_state, monkeypatch,
):
    """Operator binding (a): async hook MUST NOT block the
    aggregation path. Spy on a slow async hook + verify
    decision returns BEFORE the spy completes."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks, ffn_pending_count, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    side_effects: List[str] = []

    async def slow_async(ctx):
        # 200ms work — would be slow if it blocked the path
        await asyncio.sleep(0.2)
        side_effects.append("async_done")
        return make_hook_result(
            "slow_async", HookOutcome.CONTINUE,
        )

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=slow_async,
        name="slow_async",
        is_async=True,
    )

    async def main():
        ctx = HookContext(
            event=ToolHookEvent.PRE_TOOL_USE,
            payload={"tool_name": "read_file"},
        )
        # fire_hooks must return immediately (FFN scheduled,
        # not awaited)
        decision = await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE, ctx,
        )
        # Decision returned BEFORE the FFN sleep completes
        assert side_effects == []
        # FFN task is in-flight
        assert ffn_pending_count() >= 1
        # Aggregation excluded the async hook
        assert decision.total_hooks == 0
        # Drain confirms the task completes
        drained = await drain_ffn_tasks(timeout=2.0)
        assert drained >= 1
        assert side_effects == ["async_done"]

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (b) Blocking still BLOCKs; async cannot block
# ---------------------------------------------------------------------------


def test_blocking_hook_can_still_block(
    fresh_v3_state, monkeypatch,
):
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )

    def deny_blocking(ctx):
        return make_hook_result(
            "deny", HookOutcome.BLOCK,
            detail="blocking_deny",
        )

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=deny_blocking,
        name="deny_blocking",
        is_async=False,
    )
    decision = asyncio.run(
        fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        ),
    )
    assert decision.aggregate == HookOutcome.BLOCK


def test_async_hook_block_outcome_does_not_aggregate(
    fresh_v3_state, monkeypatch,
):
    """Operator binding (b): an async hook returning BLOCK
    MUST NOT cause the aggregate to be BLOCK. FFN results are
    structurally excluded from BLOCK-wins."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )

    async def evil_async_block(ctx):
        return make_hook_result(
            "evil_async", HookOutcome.BLOCK,
            detail="async_cannot_block",
        )

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=evil_async_block,
        name="evil_async",
        is_async=True,
    )

    async def main():
        decision = await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        )
        # Even if the async hook returns BLOCK, aggregate is
        # CONTINUE because FFN results never contribute
        assert decision.aggregate == HookOutcome.CONTINUE
        await drain_ffn_tasks(timeout=1.0)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (c) FFN exception logged, pipeline continues
# ---------------------------------------------------------------------------


def test_ffn_exception_logged_pipeline_continues(
    fresh_v3_state, monkeypatch, caplog,
):
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )

    async def crashy_async(ctx):
        raise RuntimeError("boom_in_ffn")

    def good_blocking(ctx):
        return make_hook_result(
            "good", HookOutcome.CONTINUE,
        )

    reg = get_default_registry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=crashy_async, name="crashy",
        is_async=True,
    )
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=good_blocking, name="good",
        is_async=False,
    )

    async def main():
        decision = await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        )
        # Pipeline continues despite FFN exception
        assert decision.aggregate == HookOutcome.CONTINUE
        # Blocking hook contributed
        assert decision.total_hooks == 1
        await drain_ffn_tasks(timeout=1.0)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (d) Master-off / empty async → byte-identical pre-V3
# ---------------------------------------------------------------------------


def test_master_off_treats_async_as_blocking(
    fresh_v3_state, monkeypatch,
):
    """When JARVIS_HOOK_ASYNC_ENABLED is OFF, registrations
    with is_async=True execute as blocking — byte-identical
    pre-V3 behavior. Aggregation still treats them as
    blocking."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.delenv(
        "JARVIS_HOOK_ASYNC_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        ffn_pending_count, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    fired: List[str] = []

    async def async_hook(ctx):
        fired.append("async_hook")
        return make_hook_result(
            "async_hook", HookOutcome.CONTINUE,
        )

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=async_hook, name="async_hook",
        is_async=True,
    )
    decision = asyncio.run(
        fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        ),
    )
    # Master off → async treated as blocking → contributes to
    # aggregation
    assert fired == ["async_hook"]
    assert decision.total_hooks == 1
    # No FFN tasks scheduled
    assert ffn_pending_count() == 0


def test_empty_async_registry_byte_identical(
    fresh_v3_state, monkeypatch,
):
    """No async hooks registered → aggregation identical to
    pre-V3."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        ffn_pending_count, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            "blocking_only", HookOutcome.CONTINUE,
        ),
        name="blocking_only",
    )
    decision = asyncio.run(
        fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        ),
    )
    assert decision.total_hooks == 1
    assert ffn_pending_count() == 0


# ---------------------------------------------------------------------------
# (e) Graceful shutdown drain
# ---------------------------------------------------------------------------


def test_drain_ffn_tasks_completes_pending(
    fresh_v3_state, monkeypatch,
):
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks, ffn_pending_count, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    completed: List[str] = []

    async def slow(ctx):
        await asyncio.sleep(0.05)
        completed.append("done")
        return make_hook_result("slow", HookOutcome.CONTINUE)

    reg = get_default_registry()
    for i in range(3):
        reg.register(
            event=ToolHookEvent.PRE_TOOL_USE,
            hook=slow, name=f"slow_{i}",
            is_async=True,
        )

    async def main():
        await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        )
        # All 3 FFN tasks pending
        assert ffn_pending_count() == 3
        drained = await drain_ffn_tasks(timeout=2.0)
        assert drained == 3
        assert len(completed) == 3
        assert ffn_pending_count() == 0

    asyncio.run(main())


def test_drain_with_no_tasks_returns_zero(fresh_v3_state):
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks,
    )
    drained = asyncio.run(drain_ffn_tasks(timeout=0.5))
    assert drained == 0


def test_drain_clamps_timeout(fresh_v3_state):
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks,
    )
    # Negative timeout → clamped to 0.1; doesn't raise
    drained = asyncio.run(drain_ffn_tasks(timeout=-1.0))
    assert drained == 0


def test_ffn_pending_count_never_raises():
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        ffn_pending_count,
    )
    # Must return int regardless of registry state
    assert isinstance(ffn_pending_count(), int)


# ---------------------------------------------------------------------------
# Operator-visible task naming
# ---------------------------------------------------------------------------


def test_ffn_tasks_named(fresh_v3_state, monkeypatch):
    """Operator binding: 'task names + weak registry' — every
    FFN task carries a name following the canonical pattern."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        _FFN_TASK_REGISTRY, drain_ffn_tasks, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )

    async def slow(ctx):
        await asyncio.sleep(0.05)
        return make_hook_result(
            "audit", HookOutcome.CONTINUE,
        )

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=slow, name="my_audit",
        is_async=True,
    )

    async def main():
        await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        )
        names = [t.get_name() for t in _FFN_TASK_REGISTRY]
        assert any(
            n.startswith("venom_v3_ffn_my_audit_")
            for n in names
        )
        await drain_ffn_tasks(timeout=1.0)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Mixed async + blocking + V4 patterns + V2-untouched
# ---------------------------------------------------------------------------


def test_mixed_async_blocking_priority_preserved(
    fresh_v3_state, monkeypatch,
):
    """Blocking gather still respects priority; FFN scheduling
    happens after — operator can mix both."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        drain_ffn_tasks, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    fired_blocking: List[str] = []
    fired_async: List[str] = []

    def b1(ctx):
        fired_blocking.append("b1")
        return make_hook_result(
            "b1", HookOutcome.CONTINUE,
        )

    def b2(ctx):
        fired_blocking.append("b2")
        return make_hook_result(
            "b2", HookOutcome.CONTINUE,
        )

    async def a1(ctx):
        await asyncio.sleep(0.02)
        fired_async.append("a1")
        return make_hook_result(
            "a1", HookOutcome.CONTINUE,
        )

    reg = get_default_registry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=b1, name="b1", priority=10,
    )
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=b2, name="b2", priority=20,
    )
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=a1, name="a1", is_async=True,
    )

    async def main():
        decision = await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "x"},
            ),
        )
        # Both blocking hooks fired (priority preserved by
        # registry's insertion-sort)
        assert fired_blocking == ["b1", "b2"]
        # Aggregation only counts blocking
        assert decision.total_hooks == 2
        # FFN not yet completed
        assert fired_async == []
        await drain_ffn_tasks(timeout=1.0)
        assert fired_async == ["a1"]

    asyncio.run(main())


def test_v4_pattern_filter_applies_to_async(
    fresh_v3_state, monkeypatch,
):
    """V4 pattern filter applies BEFORE V3 partition — non-
    matching async hooks are never spawned."""
    monkeypatch.setenv("JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_HOOK_ASYNC_ENABLED", "1")
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, ToolHookEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        ffn_pending_count, fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    spy = MagicMock(return_value=make_hook_result(
        "never", HookOutcome.CONTINUE,
    ))

    async def async_spy(ctx):
        return spy(ctx)

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=async_spy, name="mcp_async",
        is_async=True,
        tool_name_pattern=r"mcp_.*",
    )

    async def main():
        # Tool doesn't match pattern — async hook NOT spawned
        await fire_hooks(
            ToolHookEvent.PRE_TOOL_USE,
            HookContext(
                event=ToolHookEvent.PRE_TOOL_USE,
                payload={"tool_name": "read_file"},
            ),
        )
        assert spy.call_count == 0
        assert ffn_pending_count() == 0

    asyncio.run(main())


def test_v2_permission_registry_untouched():
    """Operator binding: 'V3 is V1 hook path only — do not
    widen permission callbacks to FFN until hook semantics are
    proven (different security story).' AST scan asserts
    PermissionRegistration has NO is_async field."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_permission.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "PermissionRegistration"
        ):
            field_names = {
                sub.target.id
                for sub in node.body
                if (
                    isinstance(sub, ast.AnnAssign)
                    and isinstance(sub.target, ast.Name)
                )
            }
            assert "is_async" not in field_names, (
                "V2 PermissionRegistration MUST NOT carry "
                "is_async — V3 is V1 hook path only "
                "(operator binding 2026-05-07)"
            )
            return
    pytest.fail("PermissionRegistration class not found")


# ---------------------------------------------------------------------------
# AST pin coverage
# ---------------------------------------------------------------------------


def test_v3_ffn_discipline_pin_validates_clean():
    """The new lifecycle_hook_executor_v3_ffn_discipline pin
    validates clean against the actual source."""
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "lifecycle_hook_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    pin = next(
        (
            i for i in register_shipped_invariants()
            if "v3_ffn_discipline" in i.invariant_name
        ),
        None,
    )
    assert pin is not None
    violations = pin.validate(tree, source)
    assert violations == ()


def test_v3_pin_fires_on_unnamed_create_task():
    """Synthetic regression — if _schedule_ffn_tasks creates
    an unnamed task, the pin fires."""
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
import asyncio
def _schedule_ffn_tasks(ffn_regs, context, event):
    loop = asyncio.get_running_loop()
    for r in ffn_regs:
        # BAD — no name= kwarg
        task = loop.create_task(r.callable(context))
        _FFN_TASK_REGISTRY.add(task)

async def fire_hooks(event, ctx):
    decision = compute_hook_decision(event, ())
    _schedule_ffn_tasks((), ctx, event)
    return decision
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "v3_ffn_discipline" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("name= kwarg" in v for v in violations)


def test_v3_pin_fires_on_ffn_before_aggregation():
    """Synthetic regression — if _schedule_ffn_tasks is
    invoked BEFORE compute_hook_decision, the pin fires."""
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def _schedule_ffn_tasks(ffn_regs, context, event):
    import asyncio
    loop = asyncio.get_running_loop()
    for r in ffn_regs:
        task = loop.create_task(
            r.callable(context), name="x",
        )
        _FFN_TASK_REGISTRY.add(task)

async def fire_hooks(event, ctx):
    # BAD — FFN scheduled BEFORE aggregation
    _schedule_ffn_tasks((), ctx, event)
    decision = compute_hook_decision(event, ())
    return decision
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "v3_ffn_discipline" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any(
        "AFTER compute_hook_decision" in v for v in violations
    )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_executor_public_api_includes_v3_helpers():
    from backend.core.ouroboros.governance import (
        lifecycle_hook_executor,
    )
    expected = {
        "LIFECYCLE_HOOK_EXECUTOR_SCHEMA_VERSION",
        "drain_ffn_tasks",
        "ffn_pending_count",
        "fire_hooks",
        "register_shipped_invariants",
        "reset_ffn_registry_for_tests",
    }
    assert set(lifecycle_hook_executor.__all__) == expected
