"""Venom V4 — shared tool_name_pattern substrate + V1+V2 wiring
regression spine.

Pins per operator binding 2026-05-07:

  * compile_tool_name_pattern fail-fast at registration
    (invalid regex / oversized / wrong type / empty string
    raise)
  * matches_tool_name semantics: None → universal True;
    re.fullmatch otherwise; never raises on garbage input
  * V1 LifecycleHookRegistry.register accepts tool_name_pattern
    kwarg + compiles at registration + stores on
    HookRegistration.compiled_pattern
  * V2 PermissionRegistry.register accepts tool_name_pattern
    kwarg + compiles at registration + stores on
    PermissionRegistration.compiled_pattern
  * Bad regex raises domain-specific error at registration
    (InvalidHookError / InvalidPermissionCallbackError)
  * fire_hooks (lifecycle_hook_executor) consults
    for_event_filtered for ToolHookEvent → non-matched hooks
    NOT spawned as tasks (perf-spy proves)
  * evaluate_tool_permission filters via matches_tool_name BEFORE
    spawning tasks → non-matched callbacks NOT awaited
  * Universal registrations (no pattern) ALWAYS pass — pre-V4
    behavior byte-identical when no patterns registered
  * Overlapping patterns both fire (priority preserved among
    matched)
  * DENY-strongest still wins when only the matching callback
    denies
  * Master-flag-off unchanged (V1 + V2 short-circuit before
    pattern check)
  * Phase-boundary hooks (LifecycleEvent) ignore the filter
    (their HookContext doesn't carry tool_name)
  * AST pins enforce stdlib-only substrate + compile-once
    contract

Verifies (37 tests).
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


# ---------------------------------------------------------------------------
# Substrate — compile_tool_name_pattern
# ---------------------------------------------------------------------------


def test_compile_none_passthrough():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern,
    )
    assert compile_tool_name_pattern(None) is None


def test_compile_valid_regex():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern, CompiledToolNamePattern,
    )
    p = compile_tool_name_pattern(r"mcp_.*")
    assert isinstance(p, CompiledToolNamePattern)
    assert p.raw == r"mcp_.*"


def test_compile_empty_string_raises():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern,
        InvalidToolNamePatternError,
    )
    with pytest.raises(InvalidToolNamePatternError):
        compile_tool_name_pattern("")


def test_compile_non_string_raises():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern,
        InvalidToolNamePatternError,
    )
    for bad in (42, [], {}, b"bytes"):
        with pytest.raises(InvalidToolNamePatternError):
            compile_tool_name_pattern(bad)  # type: ignore


def test_compile_invalid_regex_raises():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern,
        InvalidToolNamePatternError,
    )
    with pytest.raises(InvalidToolNamePatternError):
        compile_tool_name_pattern(r"[unclosed")


def test_compile_oversized_pattern_raises(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_NAME_PATTERN_MAX_CHARS", "16",
    )
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern,
        InvalidToolNamePatternError,
    )
    with pytest.raises(InvalidToolNamePatternError):
        compile_tool_name_pattern("x" * 100)


def test_max_pattern_chars_clamps(monkeypatch):
    from backend.core.ouroboros.governance.tool_name_pattern import (
        max_pattern_chars,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_NAME_PATTERN_MAX_CHARS", "1",
    )
    # Below floor (16) clamps up
    assert max_pattern_chars() == 16
    monkeypatch.setenv(
        "JARVIS_TOOL_NAME_PATTERN_MAX_CHARS", "999999",
    )
    assert max_pattern_chars() == 4096


# ---------------------------------------------------------------------------
# Substrate — matches_tool_name
# ---------------------------------------------------------------------------


def test_matches_universal_true():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        matches_tool_name,
    )
    # None compiled → True for any input
    assert matches_tool_name(None, "anything") is True
    assert matches_tool_name(None, "") is True


def test_matches_fullmatch_semantics():
    """re.fullmatch — pattern must cover entire tool name.
    Otherwise ``web_*`` would accidentally match
    ``prefix_web_x``."""
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern, matches_tool_name,
    )
    p = compile_tool_name_pattern(r"web_.*")
    assert matches_tool_name(p, "web_search") is True
    assert matches_tool_name(p, "prefix_web_x") is False
    assert matches_tool_name(p, "WEB_search") is False


def test_matches_garbage_returns_false():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern, matches_tool_name,
    )
    p = compile_tool_name_pattern(r"x")
    assert matches_tool_name(p, None) is False  # type: ignore
    assert matches_tool_name(p, 42) is False  # type: ignore


def test_matches_alternation_pattern():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        compile_tool_name_pattern, matches_tool_name,
    )
    p = compile_tool_name_pattern(r"web_(search|fetch)")
    assert matches_tool_name(p, "web_search") is True
    assert matches_tool_name(p, "web_fetch") is True
    assert matches_tool_name(p, "web_other") is False


# ---------------------------------------------------------------------------
# V1 LifecycleHookRegistry — pattern wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_v1_registry():
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        reset_default_registry_for_tests,
    )
    reset_default_registry_for_tests()
    yield
    reset_default_registry_for_tests()


def test_v1_register_accepts_pattern(fresh_v1_registry):
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
            hook_name="x", outcome=HookOutcome.CONTINUE,
        ),
        name="mcp_only",
        tool_name_pattern=r"mcp_.*",
    )
    assert rec.compiled_pattern is not None
    assert rec.compiled_pattern.raw == r"mcp_.*"


def test_v1_register_rejects_bad_regex(fresh_v1_registry):
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        InvalidHookError, LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    with pytest.raises(InvalidHookError):
        reg.register(
            event=ToolHookEvent.PRE_TOOL_USE,
            hook=lambda ctx: make_hook_result(
                hook_name="x", outcome=HookOutcome.CONTINUE,
            ),
            name="bad",
            tool_name_pattern=r"[unclosed",
        )


def test_v1_for_event_filtered_universal_passes():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            hook_name="x", outcome=HookOutcome.CONTINUE,
        ),
        name="universal",
        # NO pattern — universal
    )
    matched = reg.for_event_filtered(
        ToolHookEvent.PRE_TOOL_USE, "any_tool_name",
    )
    assert len(matched) == 1
    assert matched[0].name == "universal"


def test_v1_for_event_filtered_skips_non_matching():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            hook_name="x", outcome=HookOutcome.CONTINUE,
        ),
        name="mcp_scoped",
        tool_name_pattern=r"mcp_.*",
    )
    matched = reg.for_event_filtered(
        ToolHookEvent.PRE_TOOL_USE, "read_file",
    )
    assert len(matched) == 0
    matched2 = reg.for_event_filtered(
        ToolHookEvent.PRE_TOOL_USE, "mcp_search",
    )
    assert len(matched2) == 1


def test_v1_for_event_filtered_preserves_priority():
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            hook_name="low", outcome=HookOutcome.CONTINUE,
        ),
        name="low_priority", priority=200,
        tool_name_pattern=r"mcp_.*",
    )
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            hook_name="high", outcome=HookOutcome.CONTINUE,
        ),
        name="high_priority", priority=10,
        tool_name_pattern=r"mcp_.*",
    )
    matched = reg.for_event_filtered(
        ToolHookEvent.PRE_TOOL_USE, "mcp_x",
    )
    assert [r.name for r in matched] == [
        "high_priority", "low_priority",
    ]


def test_v1_for_event_legacy_signature_unchanged():
    """The legacy for_event(event) signature returns ALL
    registrations regardless of pattern — used by phase-
    boundary callers that don't have a tool_name."""
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookOutcome, ToolHookEvent, make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        LifecycleHookRegistry,
    )
    reg = LifecycleHookRegistry()
    reg.register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=lambda ctx: make_hook_result(
            hook_name="x", outcome=HookOutcome.CONTINUE,
        ),
        name="scoped",
        tool_name_pattern=r"mcp_.*",
    )
    # Legacy call — returns the registration regardless
    legacy = reg.for_event(ToolHookEvent.PRE_TOOL_USE)
    assert len(legacy) == 1


def test_v1_dispatch_skips_non_matching_via_perf_spy(
    fresh_v1_registry, monkeypatch,
):
    """fire_hooks MUST NOT spawn tasks for non-matched hooks.
    Spy proves the callback's body is never invoked."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
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
    spy = MagicMock(return_value=make_hook_result(
        hook_name="never", outcome=HookOutcome.CONTINUE,
    ))
    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=spy,
        name="mcp_only",
        tool_name_pattern=r"mcp_.*",
    )
    # Dispatch with a tool_name that does NOT match
    ctx = HookContext(
        event=ToolHookEvent.PRE_TOOL_USE,
        op_id="op-1",
        payload={"tool_name": "read_file"},
    )
    asyncio.run(
        fire_hooks(ToolHookEvent.PRE_TOOL_USE, ctx),
    )
    # Non-matched → callback NEVER invoked (no task spawned)
    assert spy.call_count == 0


def test_v1_dispatch_fires_matching(
    fresh_v1_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_HOOKS_ENABLED", "1",
    )
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
    spy = MagicMock(return_value=make_hook_result(
        hook_name="audit", outcome=HookOutcome.CONTINUE,
    ))
    get_default_registry().register(
        event=ToolHookEvent.PRE_TOOL_USE,
        hook=spy,
        name="mcp_only",
        tool_name_pattern=r"mcp_.*",
    )
    ctx = HookContext(
        event=ToolHookEvent.PRE_TOOL_USE,
        op_id="op-1",
        payload={"tool_name": "mcp_search"},
    )
    asyncio.run(
        fire_hooks(ToolHookEvent.PRE_TOOL_USE, ctx),
    )
    assert spy.call_count == 1


def test_v1_phase_boundary_unchanged(
    fresh_v1_registry, monkeypatch,
):
    """Phase-boundary hooks (LifecycleEvent) ignore the filter
    — their HookContext doesn't carry a tool_name."""
    monkeypatch.setenv(
        "JARVIS_LIFECYCLE_HOOKS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.lifecycle_hook import (
        HookContext, HookOutcome, LifecycleEvent,
        make_hook_result,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_executor import (  # noqa: E501
        fire_hooks,
    )
    from backend.core.ouroboros.governance.lifecycle_hook_registry import (  # noqa: E501
        get_default_registry,
    )
    spy = MagicMock(return_value=make_hook_result(
        hook_name="phase", outcome=HookOutcome.CONTINUE,
    ))
    # Phase hook with NO pattern (universal)
    get_default_registry().register(
        event=LifecycleEvent.PRE_GENERATE,
        hook=spy,
        name="phase_universal",
    )
    ctx = HookContext(event=LifecycleEvent.PRE_GENERATE)
    asyncio.run(
        fire_hooks(LifecycleEvent.PRE_GENERATE, ctx),
    )
    assert spy.call_count == 1


# ---------------------------------------------------------------------------
# V2 PermissionRegistry — pattern wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_v2_registry():
    from backend.core.ouroboros.governance.tool_permission import (
        reset_default_registry_for_tests,
    )
    reset_default_registry_for_tests()
    yield
    reset_default_registry_for_tests()


def test_v2_register_accepts_pattern(fresh_v2_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, get_default_registry,
        make_permission_result,
    )
    rec = get_default_registry().register(
        lambda ctx: make_permission_result(
            callback_name="x",
            decision=ToolPermissionDecision.DEFER,
        ),
        name="mcp_only",
        tool_name_pattern=r"mcp_.*",
    )
    assert rec.compiled_pattern is not None
    assert rec.compiled_pattern.raw == r"mcp_.*"


def test_v2_register_rejects_bad_regex(fresh_v2_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        InvalidPermissionCallbackError,
        ToolPermissionDecision, get_default_registry,
        make_permission_result,
    )
    with pytest.raises(InvalidPermissionCallbackError):
        get_default_registry().register(
            lambda ctx: make_permission_result(
                callback_name="x",
                decision=ToolPermissionDecision.DEFER,
            ),
            name="bad",
            tool_name_pattern=r"[unclosed",
        )


def test_v2_dispatch_skips_non_matching_via_perf_spy(
    fresh_v2_registry, monkeypatch,
):
    """evaluate_tool_permission MUST NOT await non-matched
    callbacks. Spy proves zero invocation."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    spy = MagicMock(return_value=make_permission_result(
        callback_name="never",
        decision=ToolPermissionDecision.DENY,
    ))
    get_default_registry().register(
        spy, name="mcp_only", tool_name_pattern=r"mcp_.*",
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="read_file", op_id="op-1",
        ),
    )
    # Non-matched → callback NEVER invoked
    assert spy.call_count == 0
    # Result is DEFER with no_pattern_matched detail
    assert r.decision == ToolPermissionDecision.DEFER
    assert r.detail == "no_pattern_matched"


def test_v2_dispatch_fires_matching_pattern(
    fresh_v2_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    spy_count = []

    def deny_mcp(ctx):
        spy_count.append(ctx.tool_name)
        return make_permission_result(
            callback_name="deny_mcp",
            decision=ToolPermissionDecision.DENY,
        )

    get_default_registry().register(
        deny_mcp, name="deny_mcp",
        tool_name_pattern=r"mcp_.*",
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="mcp_search", op_id="op-1",
        ),
    )
    assert spy_count == ["mcp_search"]
    assert r.decision == ToolPermissionDecision.DENY


def test_v2_overlapping_patterns_both_fire(
    fresh_v2_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    fired: List[str] = []

    def cb_mcp(ctx):
        fired.append("cb_mcp")
        return make_permission_result(
            callback_name="cb_mcp",
            decision=ToolPermissionDecision.ALLOW,
        )

    def cb_search(ctx):
        fired.append("cb_search")
        return make_permission_result(
            callback_name="cb_search",
            decision=ToolPermissionDecision.ALLOW,
        )

    reg = get_default_registry()
    reg.register(
        cb_mcp, name="cb_mcp",
        tool_name_pattern=r"mcp_.*",
    )
    reg.register(
        cb_search, name="cb_search",
        tool_name_pattern=r".*_search",
    )
    asyncio.run(
        evaluate_tool_permission(
            tool_name="mcp_search", op_id="op-1",
        ),
    )
    # Both patterns match → both fire
    assert "cb_mcp" in fired
    assert "cb_search" in fired


def test_v2_deny_strongest_with_pattern_filter(
    fresh_v2_registry, monkeypatch,
):
    """DENY-strongest semantics still hold: when a matching
    callback denies, aggregate is DENY even if other matching
    callbacks ALLOW."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    reg = get_default_registry()
    reg.register(
        lambda ctx: make_permission_result(
            callback_name="allow_all",
            decision=ToolPermissionDecision.ALLOW,
        ),
        name="allow_all",
    )
    reg.register(
        lambda ctx: make_permission_result(
            callback_name="deny_mcp",
            decision=ToolPermissionDecision.DENY,
        ),
        name="deny_mcp",
        tool_name_pattern=r"mcp_.*",
    )
    # mcp_search → both fire; DENY wins
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="mcp_search", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DENY
    assert "deny_mcp" in r.deny_callbacks
    # read_file → only allow_all fires; ALLOW
    r2 = asyncio.run(
        evaluate_tool_permission(
            tool_name="read_file", op_id="op-1",
        ),
    )
    assert r2.decision == ToolPermissionDecision.ALLOW


def test_v2_master_off_unchanged(
    fresh_v2_registry, monkeypatch,
):
    """Master flag off → DEFER short-circuit BEFORE pattern
    evaluation; spy never fires."""
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    spy = MagicMock(return_value=make_permission_result(
        callback_name="never",
        decision=ToolPermissionDecision.DENY,
    ))
    get_default_registry().register(
        spy, name="never", tool_name_pattern=r".*",
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="anything", op_id="op-1",
        ),
    )
    assert spy.call_count == 0
    assert r.decision == ToolPermissionDecision.DEFER
    assert r.detail == "master_off"


def test_v2_universal_callback_byte_identical_pre_v4(
    fresh_v2_registry, monkeypatch,
):
    """When NO pattern is supplied (None / universal), the
    callback fires on every tool — pre-V4 behavior."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    fired: List[str] = []

    def universal(ctx):
        fired.append(ctx.tool_name)
        return make_permission_result(
            callback_name="universal",
            decision=ToolPermissionDecision.DEFER,
        )

    get_default_registry().register(
        universal, name="universal",
    )
    asyncio.run(
        evaluate_tool_permission(
            tool_name="bash", op_id="op-1",
        ),
    )
    asyncio.run(
        evaluate_tool_permission(
            tool_name="read_file", op_id="op-2",
        ),
    )
    assert fired == ["bash", "read_file"]


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_2():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "tool_name_pattern_authority_asymmetry",
        "tool_name_pattern_compile_once_contract",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tool_name_pattern.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_compile_once_pin_fires_on_dispatch_time_compile():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        register_shipped_invariants,
    )
    bad = '''
import re
def matches_tool_name(compiled, tool_name):
    # BAD — re.compile in dispatch path
    pattern = re.compile(r"mcp_.*")
    return pattern.match(tool_name) is not None
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "compile_once" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("matches_tool_name" in v for v in violations)


def test_authority_pin_fires_on_governance_import():
    from backend.core.ouroboros.governance.tool_name_pattern import (
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "authority_asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_substrate_public_api_stable():
    from backend.core.ouroboros.governance import tool_name_pattern
    expected = {
        "CompiledToolNamePattern",
        "InvalidToolNamePatternError",
        "TOOL_NAME_PATTERN_SCHEMA_VERSION",
        "compile_tool_name_pattern",
        "matches_tool_name",
        "max_pattern_chars",
        "pattern_raw",
        "register_shipped_invariants",
    }
    assert set(tool_name_pattern.__all__) == expected
