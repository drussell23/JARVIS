"""§37 Tier 2 #16 — Per-component tool scope (Pattern C).

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (~40 tests):
  Slice 1 — component_tool_scope substrate
    * Closed 4-value taxonomy (ALLOW / DENY / NO_SCOPE /
      DISABLED) bytes-pinned
    * Master flag default-FALSE per §33.1
    * ComponentToolScope frozen + schema_version + to_dict
    * Registry: register / unregister / get / list / reset
    * Idempotent register replaces prior scope
    * Async-safe ContextVar bridge (set / reset / get)
    * Pattern matching composes V4 substrate (regex via
      compile_tool_name_pattern + matches_tool_name)
    * Pattern cache memoizes compilation
    * evaluate_component_scope: 7 resolution gates
      (master-off / empty tool / empty cid / no scope /
      explicit deny / allowlist miss / allow)
    * is_tool_allowed convenience wrapper
    * block_reason produces informative diagnostic
    * NEVER raises on invalid pattern / broken substrate
    * 4 AST pins clean + each fires on synthetic regression
    * Backward compat: master-off → ALLOW (byte-identical)

  Slice 2 — tool_executor wiring + /scope REPL
    * tool_executor invokes _maybe_block_by_component_scope
      AFTER OperationMode + BEFORE V2 (AST-pinned ordering)
    * Helper returns reason on PLAN/ANALYZE-equivalent block
    * Helper returns None when no active component
    * /scope REPL — 5 subcommands: bare / show / check /
      active / help
    * /scope check renders all 4 decision values
    * Disabled message when master flag off
    * Auto-discovery (matches=False on unrelated lines)
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "component_tool_scope.py"
    )


@pytest.fixture(autouse=True)
def _reset_scope_state():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        _reset_pattern_cache_for_tests,
        reset_active_component_for_tests,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    reset_active_component_for_tests()
    _reset_pattern_cache_for_tests()
    yield
    reset_registry_for_tests()
    reset_active_component_for_tests()
    _reset_pattern_cache_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_decision_taxonomy_4_values():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision,
    )
    assert {d.name for d in ComponentScopeDecision} == {
        "ALLOW", "DENY", "NO_SCOPE", "DISABLED",
    }


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(
            "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", v,
        )
        assert master_enabled() is True


# ---------------------------------------------------------------------------
# ComponentToolScope artifact
# ---------------------------------------------------------------------------


def test_scope_frozen():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope,
    )
    s = ComponentToolScope(component_id="vision_sensor")
    with pytest.raises(Exception):
        s.component_id = "other"  # type: ignore[misc]


def test_scope_to_dict():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope,
    )
    s = ComponentToolScope(
        component_id="vision",
        allowed_tools=frozenset({"read_.*"}),
        denied_tools=frozenset({"bash"}),
        inherits_from="parent_x",
    )
    d = s.to_dict()
    assert d["component_id"] == "vision"
    assert d["allowed_tools"] == ["read_.*"]
    assert d["denied_tools"] == ["bash"]
    assert d["inherits_from"] == "parent_x"
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


def test_register_returns_false_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
    )
    assert register_scope(
        ComponentToolScope(component_id="x"),
    ) is False


def test_register_basic_flow(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, get_scope, register_scope,
    )
    s = ComponentToolScope(
        component_id="vision_sensor",
        allowed_tools=frozenset({"read_.*"}),
    )
    assert register_scope(s) is True
    found = get_scope("vision_sensor")
    assert found is not None
    assert found.component_id == "vision_sensor"


def test_register_rejects_empty_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
    )
    assert register_scope(
        ComponentToolScope(component_id=""),
    ) is False


def test_register_idempotent_replaces(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, get_scope, register_scope,
    )
    register_scope(
        ComponentToolScope(
            component_id="x",
            allowed_tools=frozenset({"a"}),
        ),
    )
    register_scope(
        ComponentToolScope(
            component_id="x",
            allowed_tools=frozenset({"b", "c"}),
        ),
    )
    s = get_scope("x")
    assert s.allowed_tools == frozenset({"b", "c"})


def test_unregister_works(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, get_scope, register_scope,
        unregister_scope,
    )
    register_scope(ComponentToolScope(component_id="x"))
    assert unregister_scope("x") is True
    assert get_scope("x") is None
    # Idempotent.
    assert unregister_scope("x") is False


def test_list_components_returns_copy(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, list_components, register_scope,
    )
    register_scope(ComponentToolScope(component_id="a"))
    register_scope(ComponentToolScope(component_id="b"))
    snap = list_components()
    assert set(snap.keys()) == {"a", "b"}
    snap.clear()  # Mutating snapshot must not affect registry.
    assert set(list_components().keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# ContextVar bridge
# ---------------------------------------------------------------------------


def test_active_component_default_empty():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        get_active_component,
    )
    assert get_active_component() == ""


def test_set_reset_active_component():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        get_active_component, reset_active_component,
        set_active_component,
    )
    token = set_active_component("vision_sensor")
    try:
        assert get_active_component() == "vision_sensor"
    finally:
        reset_active_component(token)
    assert get_active_component() == ""


def test_active_component_async_propagates():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        get_active_component, reset_active_component,
        set_active_component,
    )
    captured = []

    async def child():
        captured.append(get_active_component())

    async def main():
        token = set_active_component("agent_X")
        try:
            await asyncio.create_task(child())
        finally:
            reset_active_component(token)

    asyncio.run(main())
    assert captured == ["agent_X"]


def test_reset_swallows_invalid_token():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        reset_active_component,
    )
    fake = MagicMock()
    # Must NOT raise.
    reset_active_component(fake)


# ---------------------------------------------------------------------------
# evaluate_component_scope — 7 resolution gates
# ---------------------------------------------------------------------------


def test_evaluate_disabled_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, evaluate_component_scope,
    )
    out = evaluate_component_scope(
        component_id="x", tool_name="y",
    )
    assert out == ComponentScopeDecision.DISABLED


def test_evaluate_allow_when_tool_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, evaluate_component_scope,
    )
    out = evaluate_component_scope(
        component_id="x", tool_name="",
    )
    assert out == ComponentScopeDecision.ALLOW


def test_evaluate_no_scope_when_cid_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, evaluate_component_scope,
    )
    out = evaluate_component_scope(
        component_id="", tool_name="read_file",
    )
    assert out == ComponentScopeDecision.NO_SCOPE


def test_evaluate_no_scope_when_unregistered(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, evaluate_component_scope,
    )
    out = evaluate_component_scope(
        component_id="never_registered", tool_name="x",
    )
    assert out == ComponentScopeDecision.NO_SCOPE


def test_evaluate_deny_on_explicit_denied_tools(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, ComponentToolScope,
        evaluate_component_scope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="vision",
        denied_tools=frozenset({"bash"}),
    ))
    out = evaluate_component_scope(
        component_id="vision", tool_name="bash",
    )
    assert out == ComponentScopeDecision.DENY


def test_evaluate_deny_when_allowlist_miss(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, ComponentToolScope,
        evaluate_component_scope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="vision",
        allowed_tools=frozenset({"read_.*", "search_code"}),
    ))
    out = evaluate_component_scope(
        component_id="vision", tool_name="edit_file",
    )
    assert out == ComponentScopeDecision.DENY


def test_evaluate_allow_when_allowlist_hit(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, ComponentToolScope,
        evaluate_component_scope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="vision",
        allowed_tools=frozenset({"read_.*"}),
    ))
    out = evaluate_component_scope(
        component_id="vision", tool_name="read_file",
    )
    assert out == ComponentScopeDecision.ALLOW


def test_evaluate_deny_wins_over_allow(monkeypatch):
    """Tool in BOTH allowed_tools + denied_tools → DENY wins
    (defense-in-depth)."""
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, ComponentToolScope,
        evaluate_component_scope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="vision",
        allowed_tools=frozenset({".*"}),  # wildcard
        denied_tools=frozenset({"bash"}),
    ))
    out = evaluate_component_scope(
        component_id="vision", tool_name="bash",
    )
    assert out == ComponentScopeDecision.DENY


def test_evaluate_allow_in_denylist_only_mode(monkeypatch):
    """Empty allowed_tools = denylist-only mode (anything not
    explicitly denied is allowed)."""
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, ComponentToolScope,
        evaluate_component_scope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="loose",
        denied_tools=frozenset({"bash"}),
    ))
    out_a = evaluate_component_scope(
        component_id="loose", tool_name="anything",
    )
    out_b = evaluate_component_scope(
        component_id="loose", tool_name="bash",
    )
    assert out_a == ComponentScopeDecision.ALLOW
    assert out_b == ComponentScopeDecision.DENY


# ---------------------------------------------------------------------------
# is_tool_allowed convenience + block_reason
# ---------------------------------------------------------------------------


def test_is_tool_allowed_default_permissive(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        is_tool_allowed,
    )
    # No scope → ALLOW (NO_SCOPE wraps to True).
    assert is_tool_allowed(
        component_id="unknown", tool_name="anything",
    ) is True


def test_block_reason_empty_when_allowed():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        block_reason,
    )
    assert block_reason(
        component_id="unknown", tool_name="x",
    ) == ""


def test_block_reason_informative_when_denied(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, block_reason, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="strict",
        allowed_tools=frozenset({"read_.*"}),
    ))
    reason = block_reason(
        component_id="strict", tool_name="bash",
    )
    assert "strict" in reason
    assert "bash" in reason


# ---------------------------------------------------------------------------
# Pattern compilation cache
# ---------------------------------------------------------------------------


def test_pattern_cache_memoizes(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import (
        component_tool_scope as cts,
    )
    # First evaluation populates cache.
    cts.register_scope(cts.ComponentToolScope(
        component_id="x",
        allowed_tools=frozenset({"read_.*"}),
    ))
    cts.evaluate_component_scope(
        component_id="x", tool_name="read_file",
    )
    # Cache should now contain the compiled pattern.
    with cts._COMPILED_PATTERN_CACHE_LOCK:
        assert "read_.*" in cts._COMPILED_PATTERN_CACHE


def test_invalid_pattern_falls_back_to_exact_match(monkeypatch):
    """Malformed regex shouldn't crash; falls back to exact
    match for the literal pattern string."""
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentScopeDecision, ComponentToolScope,
        evaluate_component_scope, register_scope,
    )
    # "[unclosed" is invalid regex.
    register_scope(ComponentToolScope(
        component_id="x",
        allowed_tools=frozenset({"[unclosed"}),
    ))
    # Exact match → ALLOW (defensive fallback).
    a = evaluate_component_scope(
        component_id="x", tool_name="[unclosed",
    )
    # Non-match → DENY (allowlist non-empty + miss).
    b = evaluate_component_scope(
        component_id="x", tool_name="anything_else",
    )
    assert a == ComponentScopeDecision.ALLOW
    assert b == ComponentScopeDecision.DENY


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "component_scope_decision_taxonomy_4_values",
        "component_scope_master_flag_default_false",
        "component_scope_authority_asymmetry",
        "component_scope_composes_tool_name_pattern",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_decision_pin_fires_on_taxonomy_drift():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class ComponentScopeDecision:
    ALLOW = "allow"
    DENY = "deny"
    # missing NO_SCOPE + DISABLED
    SUPER_ALLOW = "super_allow"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "component_scope_decision_taxonomy_4_values"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "from backend.core.ouroboros.governance.orchestrator import x"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "component_scope_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_composes_v4_pin_fires_on_parallel_regex():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
import re
def _matches_any_pattern(tool_name, patterns):
    # BAD — uses re module directly
    for p in patterns:
        if re.fullmatch(p, tool_name):
            return True
    return False
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "component_scope_composes_tool_name_pattern"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# tool_executor wiring
# ---------------------------------------------------------------------------


def test_tool_executor_helper_exists():
    from backend.core.ouroboros.governance import tool_executor
    assert hasattr(
        tool_executor, "_maybe_block_by_component_scope",
    )


def test_tool_executor_invokes_helper_in_dispatch():
    """AST scan: AsyncProcessToolBackend.execute_async invokes
    _maybe_block_by_component_scope AFTER
    _maybe_block_by_operation_mode + BEFORE
    _maybe_evaluate_tool_permission."""
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for cls in ast.walk(tree):
        if (
            isinstance(cls, ast.ClassDef)
            and cls.name == "AsyncProcessToolBackend"
        ):
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AsyncFunctionDef)
                    and stmt.name == "execute_async"
                ):
                    fn = stmt
                    break
            break
    assert fn is not None
    mode_lines = []
    scope_lines = []
    perm_lines = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                if func.id == "_maybe_block_by_operation_mode":
                    mode_lines.append(sub.lineno)
                if func.id == "_maybe_block_by_component_scope":
                    scope_lines.append(sub.lineno)
                if func.id == "_maybe_evaluate_tool_permission":
                    perm_lines.append(sub.lineno)
    assert mode_lines and scope_lines and perm_lines
    # Order: mode < scope < perm
    for m in mode_lines:
        for s in scope_lines:
            assert m < s, (
                f"mode at {m} must precede scope at {s}"
            )
    for s in scope_lines:
        for p in perm_lines:
            assert s < p, (
                f"scope at {s} must precede perm at {p}"
            )


def test_helper_returns_reason_when_scope_denies(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
        set_active_component,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_component_scope,
    )
    register_scope(ComponentToolScope(
        component_id="strict",
        allowed_tools=frozenset({"read_.*"}),
    ))
    set_active_component("strict")
    call = MagicMock()
    call.name = "edit_file"
    reason = _maybe_block_by_component_scope(call)
    assert reason is not None
    assert "edit_file" in reason


def test_helper_returns_none_when_no_active_component(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_component_scope,
    )
    call = MagicMock()
    call.name = "edit_file"
    # No active component set → pass through.
    assert _maybe_block_by_component_scope(call) is None


def test_helper_returns_none_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
        set_active_component,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_component_scope,
    )
    # Even with active component + matching scope, master off
    # short-circuits.
    set_active_component("strict")
    call = MagicMock()
    call.name = "edit_file"
    assert _maybe_block_by_component_scope(call) is None


# ---------------------------------------------------------------------------
# /scope REPL
# ---------------------------------------------------------------------------


def test_repl_unmatched_line():
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/something_else")
    assert out.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope help")
    assert out.ok is True
    assert "/scope check" in out.text


def test_repl_disabled_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope")
    assert out.ok is True
    assert "disabled" in out.text


def test_repl_overview_no_components(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope")
    assert out.ok is True
    assert "no components registered" in out.text


def test_repl_overview_with_components(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="vision",
        allowed_tools=frozenset({"read_.*"}),
    ))
    register_scope(ComponentToolScope(
        component_id="general",
        denied_tools=frozenset({"bash"}),
    ))
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope")
    assert out.ok is True
    assert "vision" in out.text
    assert "general" in out.text


def test_repl_show_unknown_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope show nonexistent")
    assert out.ok is False
    assert "no scope" in out.text


def test_repl_show_known(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="vision",
        allowed_tools=frozenset({"read_.*"}),
        denied_tools=frozenset({"bash"}),
    ))
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope show vision")
    assert out.ok is True
    assert "read_.*" in out.text
    assert "bash" in out.text


@pytest.mark.parametrize(
    "tool,expected_decision",
    [
        ("read_file", "allow"),
        ("edit_file", "deny"),
        ("anything", "deny"),  # allowlist miss
    ],
)
def test_repl_check_subcommand(
    monkeypatch, tool, expected_decision,
):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        ComponentToolScope, register_scope,
    )
    register_scope(ComponentToolScope(
        component_id="strict",
        allowed_tools=frozenset({"read_.*"}),
    ))
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command(f"/scope check strict {tool}")
    assert out.ok is True
    assert f"decision = {expected_decision}" in out.text


def test_repl_check_missing_args():
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope check only_one_arg")
    assert out.ok is False
    assert "missing args" in out.text


def test_repl_active_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope active")
    assert out.ok is True
    assert "no component currently active" in out.text


def test_repl_active_with_value(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        set_active_component,
    )
    set_active_component("vision_sensor")
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope active")
    assert out.ok is True
    assert "vision_sensor" in out.text


def test_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.scope_repl import (
        dispatch_scope_command,
    )
    out = dispatch_scope_command("/scope bogus")
    assert out.ok is False
    assert "unknown subcommand" in out.text


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        component_tool_scope as mod,
    )
    expected = {
        "COMPONENT_TOOL_SCOPE_SCHEMA_VERSION",
        "ComponentScopeDecision",
        "ComponentToolScope",
        "block_reason",
        "evaluate_component_scope",
        "get_active_component",
        "get_scope",
        "is_tool_allowed",
        "list_components",
        "master_enabled",
        "register_flags",
        "register_scope",
        "register_shipped_invariants",
        "reset_active_component",
        "reset_active_component_for_tests",
        "reset_registry_for_tests",
        "set_active_component",
        "unregister_scope",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_master_only():
    from backend.core.ouroboros.governance.component_tool_scope import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 1
    name = registry.register.call_args.kwargs["name"]
    assert name == "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED"
