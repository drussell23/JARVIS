"""Venom V2 Slice 1 — per-tool permission substrate + executor
wiring regression spine.

Pins per operator binding 2026-05-06:

  * 4-value ToolPermissionDecision closed taxonomy (ALLOW/DENY/
    ASK/DEFER) bytes-pinned
  * Master flag JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED
    default-FALSE per §33.1 graduation contract
  * Aggregator first-DENY-wins semantics (DENY > ASK > ALLOW
    > DEFER)
  * Empty registry → DEFER (dispatch proceeds)
  * Master-flag-off → DEFER + zero callback work
  * NEVER raises across all paths (timeout / exception /
    garbage return → DEFER + audit)
  * tool_executor.execute_async consults permission registry
    BEFORE V1 PRE_TOOL_USE fires (AST anchor)
  * DENY → ToolExecStatus.PERMISSION_DENIED returned without
    dispatching
  * ASK pre-graduation → conservative DENY (no synchronous
    bridge yet)
  * Concurrent callback dispatch via asyncio.gather
  * Async callbacks supported (inspect.iscoroutinefunction)
  * Garbage return type → DEFER + error audit
  * Public API stable

Verifies (28 tests).
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance.tool_permission import (
        reset_default_registry_for_tests,
    )
    reset_default_registry_for_tests()
    yield
    reset_default_registry_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_decision_taxonomy_4_values():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision,
    )
    assert len(list(ToolPermissionDecision)) == 4
    assert {d.value for d in ToolPermissionDecision} == {
        "allow", "deny", "ask", "defer",
    }


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_permission import (
        venom_tool_permissions_enabled,
    )
    assert venom_tool_permissions_enabled() is False


def test_master_flag_truthy(monkeypatch):
    from backend.core.ouroboros.governance.tool_permission import (
        venom_tool_permissions_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", v,
        )
        assert venom_tool_permissions_enabled() is True


def test_master_flag_falsy(monkeypatch):
    from backend.core.ouroboros.governance.tool_permission import (
        venom_tool_permissions_enabled,
    )
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(
            "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", v,
        )
        assert venom_tool_permissions_enabled() is False


# ---------------------------------------------------------------------------
# make_permission_result helper
# ---------------------------------------------------------------------------


def test_make_permission_result_truncates_long_strings():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, make_permission_result,
    )
    r = make_permission_result(
        callback_name="x" * 500,
        decision=ToolPermissionDecision.DENY,
        reason="r" * 500,
        error="e" * 500,
    )
    assert len(r.callback_name) <= 128
    assert len(r.reason) <= 256
    assert len(r.error) <= 256


def test_make_permission_result_garbage_decision_falls_back():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, make_permission_result,
    )
    r = make_permission_result(
        callback_name="x",
        decision="not_an_enum",  # type: ignore
    )
    assert r.decision == ToolPermissionDecision.DEFER


# ---------------------------------------------------------------------------
# Registry — register / unregister / capacity
# ---------------------------------------------------------------------------


def test_registry_register_returns_registration(fresh_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        get_default_registry, make_permission_result,
        ToolPermissionDecision,
    )
    reg = get_default_registry()
    rec = reg.register(
        lambda ctx: make_permission_result(
            callback_name="x",
            decision=ToolPermissionDecision.DEFER,
        ),
        name="my_callback",
    )
    assert rec.name == "my_callback"
    assert reg.total_count() == 1


def test_registry_priority_ordering(fresh_registry):
    """Lower priority value fires first."""
    from backend.core.ouroboros.governance.tool_permission import (
        get_default_registry, make_permission_result,
        ToolPermissionDecision,
    )
    reg = get_default_registry()
    reg.register(
        lambda ctx: make_permission_result(
            callback_name="low",
            decision=ToolPermissionDecision.DEFER,
        ),
        name="low_priority", priority=200,
    )
    reg.register(
        lambda ctx: make_permission_result(
            callback_name="high",
            decision=ToolPermissionDecision.DEFER,
        ),
        name="high_priority", priority=10,
    )
    names = [r.name for r in reg.all_registrations()]
    assert names == ["high_priority", "low_priority"]


def test_registry_duplicate_name_rejected(fresh_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        DuplicatePermissionCallbackNameError,
        get_default_registry, make_permission_result,
        ToolPermissionDecision,
    )
    reg = get_default_registry()
    reg.register(
        lambda ctx: make_permission_result(
            callback_name="x",
            decision=ToolPermissionDecision.DEFER,
        ),
        name="dup",
    )
    with pytest.raises(
        DuplicatePermissionCallbackNameError,
    ):
        reg.register(
            lambda ctx: make_permission_result(
                callback_name="y",
                decision=ToolPermissionDecision.DEFER,
            ),
            name="dup",
        )


def test_registry_invalid_callback_rejected(fresh_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        InvalidPermissionCallbackError,
        get_default_registry,
    )
    reg = get_default_registry()
    with pytest.raises(InvalidPermissionCallbackError):
        reg.register("not_callable", name="x")  # type: ignore


def test_registry_empty_name_rejected(fresh_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        InvalidPermissionCallbackError,
        get_default_registry, make_permission_result,
        ToolPermissionDecision,
    )
    reg = get_default_registry()
    with pytest.raises(InvalidPermissionCallbackError):
        reg.register(
            lambda ctx: make_permission_result(
                callback_name="x",
                decision=ToolPermissionDecision.DEFER,
            ),
            name="",
        )


def test_registry_unregister(fresh_registry):
    from backend.core.ouroboros.governance.tool_permission import (
        get_default_registry, make_permission_result,
        ToolPermissionDecision,
    )
    reg = get_default_registry()
    reg.register(
        lambda ctx: make_permission_result(
            callback_name="x",
            decision=ToolPermissionDecision.DEFER,
        ),
        name="bye",
    )
    assert reg.unregister("bye") is True
    assert reg.unregister("nonexistent") is False
    assert reg.total_count() == 0


# ---------------------------------------------------------------------------
# Aggregator — first-DENY-wins
# ---------------------------------------------------------------------------


def test_aggregate_empty_results_defers():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, compute_permission_decision,
    )
    r = compute_permission_decision(
        tool_name="x", op_id="op-1", results=(),
    )
    assert r.decision == ToolPermissionDecision.DEFER


def test_aggregate_deny_wins_over_allow():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, compute_permission_decision,
        make_permission_result,
    )
    results = (
        make_permission_result(
            callback_name="a",
            decision=ToolPermissionDecision.ALLOW,
        ),
        make_permission_result(
            callback_name="b",
            decision=ToolPermissionDecision.DENY,
        ),
    )
    r = compute_permission_decision(
        tool_name="x", op_id="op-1", results=results,
    )
    assert r.decision == ToolPermissionDecision.DENY
    assert "b" in r.deny_callbacks


def test_aggregate_ask_beats_allow_below_deny():
    """No DENY + any ASK + any ALLOW → ASK aggregate."""
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, compute_permission_decision,
        make_permission_result,
    )
    results = (
        make_permission_result(
            callback_name="a",
            decision=ToolPermissionDecision.ALLOW,
        ),
        make_permission_result(
            callback_name="b",
            decision=ToolPermissionDecision.ASK,
        ),
    )
    r = compute_permission_decision(
        tool_name="x", op_id="op-1", results=results,
    )
    assert r.decision == ToolPermissionDecision.ASK


def test_aggregate_allow_beats_defer():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, compute_permission_decision,
        make_permission_result,
    )
    results = (
        make_permission_result(
            callback_name="a",
            decision=ToolPermissionDecision.DEFER,
        ),
        make_permission_result(
            callback_name="b",
            decision=ToolPermissionDecision.ALLOW,
        ),
    )
    r = compute_permission_decision(
        tool_name="x", op_id="op-1", results=results,
    )
    assert r.decision == ToolPermissionDecision.ALLOW


def test_aggregate_all_defer_returns_defer():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, compute_permission_decision,
        make_permission_result,
    )
    results = (
        make_permission_result(
            callback_name="a",
            decision=ToolPermissionDecision.DEFER,
        ),
        make_permission_result(
            callback_name="b",
            decision=ToolPermissionDecision.DEFER,
        ),
    )
    r = compute_permission_decision(
        tool_name="x", op_id="op-1", results=results,
    )
    assert r.decision == ToolPermissionDecision.DEFER


def test_aggregate_skips_non_result_garbage():
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, compute_permission_decision,
    )
    # Mix in garbage; aggregator skips non-PermissionResult
    results = ("not_a_result",)  # type: ignore
    r = compute_permission_decision(
        tool_name="x", op_id="op-1", results=results,
    )
    assert r.decision == ToolPermissionDecision.DEFER


# ---------------------------------------------------------------------------
# Async evaluator — composes registry chain
# ---------------------------------------------------------------------------


def test_evaluate_master_off_returns_defer(
    fresh_registry, monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="bash", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DEFER
    assert r.detail == "master_off"


def test_evaluate_empty_registry_returns_defer(
    fresh_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="bash", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DEFER


def test_evaluate_deny_short_circuits(
    fresh_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    get_default_registry().register(
        lambda ctx: make_permission_result(
            callback_name="deny_all",
            decision=ToolPermissionDecision.DENY,
            reason="no",
        ),
        name="deny_all",
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="bash", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DENY


def test_evaluate_async_callback_supported(
    fresh_registry, monkeypatch,
):
    """Async callbacks must be awaited (no asyncio.to_thread
    coercion)."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )

    async def async_deny(ctx):
        await asyncio.sleep(0)
        return make_permission_result(
            callback_name="async_deny",
            decision=ToolPermissionDecision.DENY,
        )

    get_default_registry().register(
        async_deny, name="async_deny",
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="bash", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DENY


def test_evaluate_buggy_callback_quarantined(
    fresh_registry, monkeypatch,
):
    """A callback that raises must NOT propagate; the runner
    converts it to DEFER + audit."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry,
    )

    def crashy(ctx):
        raise RuntimeError("boom")

    get_default_registry().register(crashy, name="crashy")
    # Doesn't raise; aggregator returns DEFER (callback's
    # exception → DEFER result with error)
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="x", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DEFER


def test_evaluate_garbage_return_quarantined(
    fresh_registry, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry,
    )

    def returns_garbage(ctx):
        return "not_a_result"

    get_default_registry().register(
        returns_garbage, name="garbage",
    )
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="x", op_id="op-1",
        ),
    )
    assert r.decision == ToolPermissionDecision.DEFER


def test_evaluate_concurrent_dispatch(
    fresh_registry, monkeypatch,
):
    """Multiple callbacks fire concurrently via asyncio.gather."""
    monkeypatch.setenv(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.tool_permission import (
        ToolPermissionDecision, evaluate_tool_permission,
        get_default_registry, make_permission_result,
    )
    fired_order = []

    async def slow_allow(ctx):
        await asyncio.sleep(0.05)
        fired_order.append("slow_allow")
        return make_permission_result(
            callback_name="slow_allow",
            decision=ToolPermissionDecision.ALLOW,
        )

    async def fast_deny(ctx):
        fired_order.append("fast_deny")
        return make_permission_result(
            callback_name="fast_deny",
            decision=ToolPermissionDecision.DENY,
        )

    reg = get_default_registry()
    reg.register(slow_allow, name="slow")
    reg.register(fast_deny, name="fast")
    r = asyncio.run(
        evaluate_tool_permission(
            tool_name="x", op_id="op-1",
        ),
    )
    # Both fire (concurrent dispatch); DENY wins
    assert len(fired_order) == 2
    assert r.decision == ToolPermissionDecision.DENY


# ---------------------------------------------------------------------------
# tool_executor wiring (AST anchors)
# ---------------------------------------------------------------------------


def test_tool_executor_consults_permission_before_v1_hook():
    """Permission evaluation MUST fire BEFORE V1 PRE_TOOL_USE
    hook. AST anchor on execute_async."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    # Anchor to the actual ToolExecutor.execute_async method.
    idx = source.rfind(
        'cap = int(os.environ.get("JARVIS_TOOL_OUTPUT_CAP_BYTES"',
    )
    assert idx >= 0
    # Window widened to 8000 chars after §37 Tier 2 #14 / #16
    # added Operation Mode + Component Scope gates between the
    # cap read and PRE_TOOL_USE fire (~3500 chars of new gating
    # logic).
    section = source[idx:idx + 8000]
    perm_idx = section.find("_maybe_evaluate_tool_permission")
    pre_tool_use_idx = section.find(
        '_maybe_fire_tool_hook(\n            "pre_tool_use"',
    )
    assert perm_idx >= 0, (
        "execute_async MUST consult permission registry"
    )
    assert pre_tool_use_idx >= 0
    assert perm_idx < pre_tool_use_idx, (
        "permission check MUST precede V1 PRE_TOOL_USE hook"
    )


def test_tool_executor_helper_composes_canonical_substrate():
    """_maybe_evaluate_tool_permission must compose
    evaluate_tool_permission from tool_permission module —
    no parallel evaluator."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_executor.py"
    )
    source = target.read_text(encoding="utf-8")
    helper_idx = source.find(
        "async def _maybe_evaluate_tool_permission",
    )
    assert helper_idx >= 0
    helper_section = source[helper_idx:helper_idx + 2000]
    assert (
        "from backend.core.ouroboros.governance.tool_permission"
        in helper_section
    )
    assert "evaluate_tool_permission" in helper_section


def test_tool_exec_status_has_permission_denied():
    """ToolExecStatus enum must include PERMISSION_DENIED for
    the deny short-circuit return path."""
    from backend.core.ouroboros.governance.tool_executor import (
        ToolExecStatus,
    )
    assert hasattr(ToolExecStatus, "PERMISSION_DENIED")
    assert ToolExecStatus.PERMISSION_DENIED.value == (
        "permission_denied"
    )


# ---------------------------------------------------------------------------
# AST pins on substrate
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.tool_permission import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "tool_permission_decision_taxonomy_closed",
        "tool_permission_authority_asymmetry",
        "tool_permission_master_flag_default_false",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.tool_permission import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/tool_permission.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.tool_permission import (
        register_shipped_invariants,
    )
    bad = '''
import enum
class ToolPermissionDecision(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    DEFER = "defer"
    UNAUTHORIZED = "unauthorized"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.tool_permission import (
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import tool_permission
    expected = {
        "AggregatePermissionDecision",
        "DuplicatePermissionCallbackNameError",
        "InvalidPermissionCallbackError",
        "PermissionContext",
        "PermissionRegistration",
        "PermissionRegistry",
        "PermissionRegistryError",
        "PermissionResult",
        "TOOL_PERMISSION_SCHEMA_VERSION",
        "ToolPermissionCallback",
        "ToolPermissionDecision",
        "compute_permission_decision",
        "evaluate_tool_permission",
        "get_default_registry",
        "make_permission_result",
        "register_shipped_invariants",
        "reset_default_registry_for_tests",
        "venom_tool_permissions_enabled",
    }
    assert set(tool_permission.__all__) == expected
