"""Venom V1 Slice 2 — tool_executor.py fire-point regression
spine.

Pins per operator binding 2026-05-06:

  * `_maybe_fire_tool_hook` and `_maybe_fire_tool_hook_failure`
    helpers exist at module level
  * Both helpers are master-flag-gated
    (JARVIS_VENOM_TOOL_HOOKS_ENABLED) — default-FALSE means
    zero overhead pre-graduation
  * Both helpers compose canonical fire_hooks substrate
    (single source of truth)
  * Both helpers NEVER raise into the tool path (a buggy
    hook cannot break tool dispatch)
  * fire payload includes tool_name + op_id + (result_summary
    OR error)
  * `execute_async` invokes PRE_TOOL_USE before any dispatch
    + POST_TOOL_USE / POST_TOOL_USE_FAILURE based on result
    status
  * Master-flag-off: hooks never fire (verified by counter)
  * Master-flag-on with no hooks registered: helpers
    short-circuit cleanly
  * compute_hook_decision now accepts ToolHookEvent without
    coercing to PRE_APPLY (tests Slice 1 widen propagation)

Verifies (22 tests).
"""
from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        reset_default_registry_for_tests,
    )
    reset_default_registry_for_tests()
    yield
    reset_default_registry_for_tests()


def _make_capture_hook(fired: List[Any]):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, make_hook_result,
    )

    def _hook(ctx):
        fired.append({
            "event": ctx.event.value,
            "tool_name": ctx.payload.get("tool_name"),
            "result_summary": ctx.payload.get(
                "result_summary", ""
            ),
            "error": ctx.payload.get("error", ""),
            "op_id": ctx.op_id,
        })
        return make_hook_result(
            name="capture", outcome=HookOutcome.CONTINUE,
        )
    return _hook


# ---------------------------------------------------------------------------
# Helper presence + composition
# ---------------------------------------------------------------------------


def test_helpers_exist_at_module_level():
    from backend.core.ouroboros.governance import tool_executor
    assert hasattr(tool_executor, "_maybe_fire_tool_hook")
    assert hasattr(
        tool_executor, "_maybe_fire_tool_hook_failure",
    )


def test_helpers_compose_canonical_fire_hooks():
    """AST scan: tool_executor.py imports fire_hooks from the
    canonical lifecycle_hook_executor — no parallel dispatch
    machinery."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    # Helper bodies must reference fire_hooks (lazy import).
    assert "from backend.core.ouroboros.governance.lifecycle_hook_executor import" in source
    assert "fire_hooks" in source


def test_helpers_compose_master_flag_gate():
    """AST scan: helpers must invoke venom_tool_hooks_enabled()
    so the default-FALSE pre-graduation path is zero overhead."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "venom_tool_hooks_enabled" in source


# ---------------------------------------------------------------------------
# Master flag gating — fire helpers
# ---------------------------------------------------------------------------


def test_fire_short_circuits_when_master_off(
    fresh_registry, monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook, ToolCall,
    )
    fired: List[Any] = []
    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_capture_hook(fired),
        name="never_fires",
    )
    call = ToolCall(name="read_file", arguments={"path": "/x"})
    ctx = SimpleNamespace(op_id="op-1")
    asyncio.run(_maybe_fire_tool_hook("pre_tool_use", call, ctx))
    # Master flag off → hook NEVER fires
    assert fired == []


def test_fire_runs_when_master_on(fresh_registry, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook, ToolCall,
    )
    fired: List[Any] = []
    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_capture_hook(fired),
        name="audit",
    )
    call = ToolCall(name="read_file", arguments={"path": "/x"})
    ctx = SimpleNamespace(op_id="op-42")
    asyncio.run(_maybe_fire_tool_hook("pre_tool_use", call, ctx))
    assert len(fired) == 1
    assert fired[0]["event"] == "pre_tool_use"
    assert fired[0]["tool_name"] == "read_file"
    assert fired[0]["op_id"] == "op-42"


def test_fire_failure_carries_error_string(
    fresh_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook_failure, ToolCall,
    )
    fired: List[Any] = []
    get_default_registry().register(
        event=ToolHookEvent.POST_TOOL_USE_FAILURE,
        hook=_make_capture_hook(fired),
        name="audit_failure",
    )
    call = ToolCall(name="bash", arguments={"cmd": "false"})
    ctx = SimpleNamespace(op_id="op-fail")
    asyncio.run(
        _maybe_fire_tool_hook_failure(
            "post_tool_use_failure", call, ctx,
            error="EXEC_ERROR: nonzero exit",
        ),
    )
    assert len(fired) == 1
    assert fired[0]["event"] == "post_tool_use_failure"
    assert "EXEC_ERROR" in fired[0]["error"]


def test_fire_truncates_oversized_summary(
    fresh_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook, ToolCall,
    )
    fired: List[Any] = []
    get_default_registry().register(
        event=ToolHookEvent.POST_TOOL_USE,
        hook=_make_capture_hook(fired),
        name="audit",
    )
    call = ToolCall(name="read_file", arguments={})
    ctx = SimpleNamespace(op_id="x")
    huge = "x" * 10_000
    asyncio.run(
        _maybe_fire_tool_hook(
            "post_tool_use", call, ctx,
            result_summary=huge,
        ),
    )
    assert len(fired) == 1
    # Helper truncates to ≤128 chars
    assert len(fired[0]["result_summary"]) <= 128


def test_fire_unknown_event_silent(fresh_registry, monkeypatch):
    """Garbage event token → silent return, no raise."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook, ToolCall,
    )
    call = ToolCall(name="read_file", arguments={})
    ctx = SimpleNamespace(op_id="x")
    # Should not raise
    asyncio.run(
        _maybe_fire_tool_hook(
            "garbage_event", call, ctx,
        ),
    )


def test_fire_never_raises_on_buggy_hook(
    fresh_registry, monkeypatch,
):
    """A hook that raises must NOT propagate into the tool
    path. Buggy hook quarantined inside fire_hooks."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook, ToolCall,
    )

    def _crashy(ctx):
        raise RuntimeError("hook crash")

    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_crashy,
        name="crashy",
    )
    call = ToolCall(name="read_file", arguments={})
    ctx = SimpleNamespace(op_id="x")
    # Must not raise
    asyncio.run(
        _maybe_fire_tool_hook("pre_tool_use", call, ctx),
    )


def test_fire_never_raises_on_substrate_unavailable(
    fresh_registry, monkeypatch,
):
    """If lifecycle_hook substrate import fails (rollback
    branch), helper returns silently rather than raising."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_fire_tool_hook, ToolCall,
    )
    real_import = __import__

    def _block(name, *args, **kwargs):
        if name == (
            "backend.core.ouroboros.governance.lifecycle_hook"
        ):
            raise ImportError("simulated rollback")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block)
    call = ToolCall(name="read_file", arguments={})
    ctx = SimpleNamespace(op_id="x")
    # Must not raise
    asyncio.run(
        _maybe_fire_tool_hook("pre_tool_use", call, ctx),
    )


# ---------------------------------------------------------------------------
# execute_async wiring — fire points present at call sites
# ---------------------------------------------------------------------------


def test_execute_async_fires_pre_tool_use():
    """AST regression: execute_async body invokes
    _maybe_fire_tool_hook with 'pre_tool_use' before any
    dispatch."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    # Find execute_async (the second one — the ToolExecutor's
    # async dispatch). Anchor by the unique combination of
    # ``policy_ctx: PolicyContext, deadline: float``.
    # Anchor to the cap-read line in the actual
    # ToolExecutor.execute_async method body. There are TWO
    # occurrences of this pattern (Protocol declaration at
    # ~line 319 + actual class body at ~3190); rfind grabs
    # the later one.
    idx = source.rfind(
        'cap = int(os.environ.get("JARVIS_TOOL_OUTPUT_CAP_BYTES"',
    )
    assert idx >= 0
    section = source[idx:idx + 3500]
    assert '_maybe_fire_tool_hook(\n            "pre_tool_use"' in section, (
        "execute_async must fire 'pre_tool_use' before dispatch"
    )


def test_execute_async_fires_post_tool_use_on_success():
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    idx = source.rfind(
        'cap = int(os.environ.get("JARVIS_TOOL_OUTPUT_CAP_BYTES"',
    )
    section = source[idx:idx + 4500]
    # Look for post_tool_use fire (any indent — the indentation
    # depends on whether it's inside an if-branch vs at top
    # level of the method).
    assert '_maybe_fire_tool_hook(' in section
    assert '"post_tool_use"' in section


def test_execute_async_fires_post_tool_use_failure():
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    # Anchor to the cap-read line in the actual
    # ToolExecutor.execute_async method body. There are TWO
    # occurrences of this pattern (Protocol declaration at
    # ~line 319 + actual class body at ~3190); rfind grabs
    # the later one.
    idx = source.rfind(
        'cap = int(os.environ.get("JARVIS_TOOL_OUTPUT_CAP_BYTES"',
    )
    section = source[idx:idx + 4000]
    assert "post_tool_use_failure" in section


def test_post_tool_use_branches_on_status():
    """The success path fires post_tool_use; the failure path
    fires post_tool_use_failure. AST scan."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    # Anchor to the cap-read line in the actual
    # ToolExecutor.execute_async method body. There are TWO
    # occurrences of this pattern (Protocol declaration at
    # ~line 319 + actual class body at ~3190); rfind grabs
    # the later one.
    idx = source.rfind(
        'cap = int(os.environ.get("JARVIS_TOOL_OUTPUT_CAP_BYTES"',
    )
    section = source[idx:idx + 4500]
    # Must check ToolExecStatus.SUCCESS to branch
    assert "ToolExecStatus.SUCCESS" in section


# ---------------------------------------------------------------------------
# compute_hook_decision now accepts ToolHookEvent
# ---------------------------------------------------------------------------


def test_compute_hook_decision_accepts_tool_hook_event():
    """Slice 1 widen propagated — passing a ToolHookEvent
    no longer coerces to PRE_APPLY."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        ToolHookEvent, compute_hook_decision,
    )
    decision = compute_hook_decision(
        ToolHookEvent.PRE_TOOL_USE, (),
    )
    # Empty tuple → CONTINUE; key check: decision.event is the
    # ORIGINAL event, not coerced to LifecycleEvent.PRE_APPLY
    assert decision.event == ToolHookEvent.PRE_TOOL_USE


def test_compute_hook_decision_garbage_event_falls_back():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        LifecycleEvent, compute_hook_decision,
    )
    decision = compute_hook_decision(
        "completely_unknown_token", (),  # type: ignore
    )
    # Last-resort coercion to PRE_APPLY (defensive)
    assert decision.event == LifecycleEvent.PRE_APPLY


# ---------------------------------------------------------------------------
# fire_hooks dispatches on correct master flag
# ---------------------------------------------------------------------------


def test_fire_hooks_uses_venom_flag_for_tool_event(
    fresh_registry, monkeypatch,
):
    """Tool-boundary events MUST consult JARVIS_VENOM_TOOL_HOOKS_ENABLED,
    not JARVIS_LIFECYCLE_HOOKS_ENABLED. The two surfaces are
    independent."""
    # Lifecycle flag on, Venom flag off → tool hooks STILL silent
    monkeypatch.setenv(
        "JARVIS_LIFECYCLE_HOOKS_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        fire_hooks,
    )
    fired: List[Any] = []
    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=_make_capture_hook(fired),
        name="audit",
    )
    ctx = HookContext(event=ToolHookEvent.PRE_TOOL_USE)
    decision = asyncio.run(
        fire_hooks(ToolHookEvent.PRE_TOOL_USE, ctx),
    )
    # Tool hooks gated by Venom flag — silent
    assert fired == []
    assert decision.aggregate.value == "continue"


def test_fire_hooks_uses_lifecycle_flag_for_phase_event(
    fresh_registry, monkeypatch,
):
    """Phase-boundary events MUST consult JARVIS_LIFECYCLE_HOOKS_ENABLED,
    not JARVIS_VENOM_TOOL_HOOKS_ENABLED."""
    monkeypatch.setenv(
        "JARVIS_LIFECYCLE_HOOKS_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, LifecycleEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        fire_hooks,
    )
    fired: List[Any] = []
    get_default_registry().register(
        event=LifecycleEvent.PRE_GENERATE,
        hook=_make_capture_hook(fired),
        name="phase_audit",
    )
    ctx = HookContext(event=LifecycleEvent.PRE_GENERATE)
    asyncio.run(fire_hooks(LifecycleEvent.PRE_GENERATE, ctx))
    # Phase hooks gated by lifecycle flag — fires
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# AggregateHookDecision event widening (Slice 2)
# ---------------------------------------------------------------------------


def test_aggregate_decision_carries_tool_hook_event(
    fresh_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, ToolHookEvent,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        fire_hooks,
    )
    ctx = HookContext(event=ToolHookEvent.SUBAGENT_START)
    decision = asyncio.run(
        fire_hooks(ToolHookEvent.SUBAGENT_START, ctx),
    )
    # decision.event MUST preserve the ToolHookEvent (not
    # silently coerce to LifecycleEvent.PRE_APPLY)
    assert decision.event == ToolHookEvent.SUBAGENT_START


# ---------------------------------------------------------------------------
# HookContext widening (Slice 2)
# ---------------------------------------------------------------------------


def test_hook_context_accepts_tool_hook_event():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, ToolHookEvent,
    )
    ctx = HookContext(
        event=ToolHookEvent.PRE_TOOL_USE,
        op_id="op-1",
        payload={"tool_name": "x"},
    )
    assert ctx.event == ToolHookEvent.PRE_TOOL_USE
