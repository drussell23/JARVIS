"""§37 Tier 2 #14 — Operation Modes regression spine.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (~32 tests):
  * Closed 4-value taxonomy (PLAN / ANALYZE / APPLY / AUTO)
  * Master flag default-FALSE per §33.1
  * resolve_mode_from_env: defaults, recognized values, garbage
  * current_mode resolution (ContextVar > env > default AUTO)
  * set_mode / reset_mode / async-context propagation
  * is_mutation_blocked: master-off / APPLY / AUTO / PLAN /
    ANALYZE × mutation-tool / read-tool combinations
  * Composes canonical scoped_tool_access._MUTATION_TOOLS
    (no parallel set; AST-pinned)
  * block_reason: empty when not blocked; informative when
    blocked
  * REPL dispatch: bare/status/help/set/unknown subcommand/
    unknown mode/parse-error
  * /mode set isolation (set in one context doesn't leak)
  * 4 AST pins clean (taxonomy / master-flag / authority /
    composes-mutation-set) + each fires on synthetic regression
  * tool_executor wiring: AST scan confirms helper call site
    BEFORE V2 permission check
  * tool_executor enforcement: PLAN mode + master ON + mutation
    tool → POLICY_DENIED at dispatch
  * tool_executor enforcement: APPLY mode + master ON + mutation
    tool → passes through
  * tool_executor enforcement: master OFF → byte-identical
    pre-slice (no block)
  * FlagRegistry seeds discoverable
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
        / "backend/core/ouroboros/governance/operation_mode.py"
    )


@pytest.fixture(autouse=True)
def _reset_active_mode():
    from backend.core.ouroboros.governance.operation_mode import (
        reset_active_mode_for_tests,
    )
    reset_active_mode_for_tests()
    yield
    reset_active_mode_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_4_values():
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode,
    )
    assert {m.name for m in OperationMode} == {
        "PLAN", "ANALYZE", "APPLY", "AUTO",
    }


def test_taxonomy_values_are_lowercase():
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode,
    )
    for m in OperationMode:
        assert m.value == m.name.lower()


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OPERATION_MODE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.operation_mode import (
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.operation_mode import (
        master_enabled,
    )
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(
            "JARVIS_OPERATION_MODE_ENABLED", v,
        )
        assert master_enabled() is True


# ---------------------------------------------------------------------------
# resolve_mode_from_env
# ---------------------------------------------------------------------------


def test_env_default_is_auto(monkeypatch):
    monkeypatch.delenv("JARVIS_OPERATION_MODE", raising=False)
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, resolve_mode_from_env,
    )
    assert resolve_mode_from_env() == OperationMode.AUTO


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        ("plan", "PLAN"),
        ("ANALYZE", "ANALYZE"),
        ("Apply", "APPLY"),
        ("auto", "AUTO"),
    ],
)
def test_env_recognized_values(
    monkeypatch, raw, expected_name,
):
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, resolve_mode_from_env,
    )
    monkeypatch.setenv("JARVIS_OPERATION_MODE", raw)
    assert (
        resolve_mode_from_env()
        == getattr(OperationMode, expected_name)
    )


def test_env_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATION_MODE", "garbage")
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, resolve_mode_from_env,
    )
    assert resolve_mode_from_env() == OperationMode.AUTO


# ---------------------------------------------------------------------------
# current_mode resolution
# ---------------------------------------------------------------------------


def test_current_mode_default_is_auto(monkeypatch):
    monkeypatch.delenv("JARVIS_OPERATION_MODE", raising=False)
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, current_mode,
    )
    assert current_mode() == OperationMode.AUTO


def test_set_mode_overrides_env(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATION_MODE", "auto")
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, current_mode, reset_mode, set_mode,
    )
    token = set_mode(OperationMode.PLAN)
    try:
        assert current_mode() == OperationMode.PLAN
    finally:
        reset_mode(token)
    # After reset, env wins.
    assert current_mode() == OperationMode.AUTO


def test_set_mode_async_propagates_to_child_task():
    """ContextVar inherits across asyncio.Task creation."""
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, current_mode, reset_mode, set_mode,
    )
    captured = []

    async def child():
        captured.append(current_mode())

    async def main():
        token = set_mode(OperationMode.PLAN)
        try:
            await asyncio.create_task(child())
        finally:
            reset_mode(token)

    asyncio.run(main())
    assert captured and captured[0] == OperationMode.PLAN


def test_reset_mode_swallows_invalid_token():
    from backend.core.ouroboros.governance.operation_mode import (
        reset_mode,
    )
    fake_token = MagicMock()
    # Must NOT raise.
    reset_mode(fake_token)


# ---------------------------------------------------------------------------
# is_mutation_blocked predicate
# ---------------------------------------------------------------------------


def test_blocked_false_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OPERATION_MODE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.PLAN)
    # Master off — never blocks even in PLAN mode.
    assert is_mutation_blocked("edit_file") is False


def test_blocked_true_in_plan_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.PLAN)
    assert is_mutation_blocked("edit_file") is True
    assert is_mutation_blocked("write_file") is True
    assert is_mutation_blocked("bash") is True


def test_blocked_true_in_analyze_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.ANALYZE)
    assert is_mutation_blocked("edit_file") is True


def test_blocked_false_in_apply_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.APPLY)
    assert is_mutation_blocked("edit_file") is False


def test_blocked_false_in_auto_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.AUTO)
    assert is_mutation_blocked("edit_file") is False


def test_read_only_tools_never_blocked(monkeypatch):
    """PLAN mode + master on + READ tool → passes through."""
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.PLAN)
    assert is_mutation_blocked("read_file") is False
    assert is_mutation_blocked("search_code") is False
    assert is_mutation_blocked("get_callers") is False


def test_blocked_false_for_empty_tool_name(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    set_mode(OperationMode.PLAN)
    assert is_mutation_blocked("") is False


def test_block_reason_empty_when_not_blocked():
    from backend.core.ouroboros.governance.operation_mode import (
        block_reason,
    )
    assert block_reason("read_file") == ""


def test_block_reason_informative_when_blocked(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, block_reason, set_mode,
    )
    set_mode(OperationMode.PLAN)
    reason = block_reason("edit_file")
    assert "OPERATION_MODE=plan" in reason
    assert "edit_file" in reason


# ---------------------------------------------------------------------------
# Composes canonical mutation set
# ---------------------------------------------------------------------------


def test_composes_canonical_mutation_set():
    """No parallel set — verify is_mutation_blocked uses
    scoped_tool_access._MUTATION_TOOLS by checking that tools
    NOT in canonical set are never blocked."""
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, is_mutation_blocked, set_mode,
    )
    from backend.core.ouroboros.governance.scoped_tool_access import (
        _MUTATION_TOOLS,
    )
    set_mode(OperationMode.PLAN)
    # Read-only tools that are NOT in the canonical mutation set
    # must never be blocked, regardless of mode.
    for tool in ("read_file", "search_code", "list_dir"):
        assert tool not in _MUTATION_TOOLS
        assert is_mutation_blocked(tool) is False


# ---------------------------------------------------------------------------
# REPL dispatch
# ---------------------------------------------------------------------------


def test_repl_unmatched_line():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/something_else")
    assert out.matched is False


def test_repl_bare_status():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode")
    assert out.matched is True
    assert out.ok is True
    assert "current_mode" in out.text


def test_repl_status_subcommand():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode status")
    assert out.ok is True
    assert "current_mode" in out.text


def test_repl_help():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode help")
    assert out.ok is True
    assert "/mode set" in out.text
    assert "plan" in out.text and "analyze" in out.text


def test_repl_set_valid_mode():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, current_mode,
    )
    out = dispatch_mode_command("/mode set plan")
    assert out.ok is True
    assert "plan" in out.text
    assert current_mode() == OperationMode.PLAN


def test_repl_set_unknown_mode():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode set bogus")
    assert out.ok is False
    assert "unknown mode" in out.text


def test_repl_set_missing_name():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode set")
    assert out.ok is False
    assert "missing mode name" in out.text


def test_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode delete")
    assert out.ok is False
    assert "unknown subcommand" in out.text


def test_repl_status_shows_enforcement_passive_when_master_off(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_OPERATION_MODE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    out = dispatch_mode_command("/mode status")
    assert "passive" in out.text


def test_repl_status_shows_enforcing_when_master_on_and_plan(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.mode_repl import (
        dispatch_mode_command,
    )
    dispatch_mode_command("/mode set plan")
    out = dispatch_mode_command("/mode status")
    assert "ENFORCING" in out.text


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "operation_mode_taxonomy_4_values_closed",
        "operation_mode_master_flag_default_false",
        "operation_mode_authority_asymmetry",
        "operation_mode_composes_canonical_mutation_set",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.operation_mode import (
        register_shipped_invariants,
    )
    source = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, source)
    assert violations == ()


def test_taxonomy_pin_fires_on_extra_value():
    from backend.core.ouroboros.governance.operation_mode import (
        register_shipped_invariants,
    )
    bad = '''
class OperationMode:
    PLAN = "plan"
    ANALYZE = "analyze"
    APPLY = "apply"
    AUTO = "auto"
    NUCLEAR = "nuclear"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "operation_mode_taxonomy_4_values_closed"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_master_flag_pin_fires_on_default_true():
    from backend.core.ouroboros.governance.operation_mode import (
        register_shipped_invariants,
    )
    bad = '''
def master_enabled() -> bool:
    raw = os.environ.get("JARVIS_OPERATION_MODE_ENABLED", "").strip().lower()
    if raw == "":
        return True
    return raw in ("1",)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "operation_mode_master_flag_default_false"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.operation_mode import (
        register_shipped_invariants,
    )
    bad = "from backend.core.ouroboros.governance.orchestrator import x"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "operation_mode_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_composes_mutation_set_pin_fires_on_parallel_set():
    from backend.core.ouroboros.governance.operation_mode import (
        register_shipped_invariants,
    )
    bad = '''
def is_mutation_blocked(tool_name):
    # BAD — parallel mutation set
    if tool_name in ("edit_file", "write_file"):
        return True
    return False
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "operation_mode_composes_canonical_mutation_set"
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
        tool_executor, "_maybe_block_by_operation_mode",
    )


def test_tool_executor_invokes_helper_in_dispatch():
    """AST scan: AsyncProcessToolBackend.execute_async invokes
    _maybe_block_by_operation_mode BEFORE
    _maybe_evaluate_tool_permission (V2)."""
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
    perm_lines = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                if func.id == "_maybe_block_by_operation_mode":
                    mode_lines.append(sub.lineno)
                if func.id == "_maybe_evaluate_tool_permission":
                    perm_lines.append(sub.lineno)
    assert mode_lines, (
        "execute_async MUST invoke "
        "_maybe_block_by_operation_mode"
    )
    assert perm_lines, (
        "_maybe_evaluate_tool_permission must still be present"
    )
    # Ordering: every mode-block call must come before every
    # V2 perm call.
    for m in mode_lines:
        for p in perm_lines:
            assert m < p, (
                f"mode block at line {m} must precede V2 "
                f"permission check at line {p}"
            )


def test_helper_returns_reason_when_plan_mode_master_on(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, set_mode,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_operation_mode,
    )
    set_mode(OperationMode.PLAN)
    call = MagicMock()
    call.name = "edit_file"
    reason = _maybe_block_by_operation_mode(call)
    assert reason is not None
    assert "edit_file" in reason


def test_helper_returns_none_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OPERATION_MODE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, set_mode,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_operation_mode,
    )
    set_mode(OperationMode.PLAN)
    call = MagicMock()
    call.name = "edit_file"
    assert _maybe_block_by_operation_mode(call) is None


def test_helper_returns_none_in_apply_mode(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, set_mode,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_operation_mode,
    )
    set_mode(OperationMode.APPLY)
    call = MagicMock()
    call.name = "edit_file"
    assert _maybe_block_by_operation_mode(call) is None


def test_helper_returns_none_for_read_only_tool(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATION_MODE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.operation_mode import (
        OperationMode, set_mode,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        _maybe_block_by_operation_mode,
    )
    set_mode(OperationMode.PLAN)
    call = MagicMock()
    call.name = "read_file"
    assert _maybe_block_by_operation_mode(call) is None


# ---------------------------------------------------------------------------
# FlagRegistry seeds + public API
# ---------------------------------------------------------------------------


def test_register_flags_seeds_two_knobs():
    from backend.core.ouroboros.governance.operation_mode import (
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 2
    names = {
        c.kwargs["name"] for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_OPERATION_MODE_ENABLED",
        "JARVIS_OPERATION_MODE",
    }


def test_register_flags_swallows_registry_errors():
    from backend.core.ouroboros.governance.operation_mode import (
        register_flags,
    )
    bad = MagicMock()
    bad.register.side_effect = TypeError("bad shape")
    register_flags(bad)


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        operation_mode as mod,
    )
    expected = {
        "OPERATION_MODE_SCHEMA_VERSION",
        "OperationMode",
        "block_reason",
        "current_mode",
        "is_mutation_blocked",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
        "reset_active_mode_for_tests",
        "reset_mode",
        "resolve_mode_from_env",
        "set_mode",
    }
    assert set(mod.__all__) == expected
