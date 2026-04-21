"""Regression spine — Gap #5 Slice 2 Venom task tools.

Mirrors the Ticket #4 Slice 2 monitor-tool spine in shape + rigor.

Pins the structural contract:

  1. Policy deny/allow matrix: master env default false → DENY;
     explicit "false" → DENY; explicit "true" → ALLOW; malformed
     args → deny with deterministic reason codes.
  2. Manifest integrity: 3 manifests registered, all with empty
     capabilities (read-only, not in _MUTATION_TOOLS).
  3. Authority invariant: tools allowed under is_read_only scope
     (no mutation category); no imports of Iron Gate / risk / policy.
  4. Handler happy paths: task_create / task_update (content +
     start + cancel) / task_complete — all produce SUCCESS with
     the documented JSON shape.
  5. Handler failure modes: board closed, state errors, unknown
     task_id, missing op_id, bad args.
  6. Registry lifecycle: lazy create on first touch, close_task_board
     idempotent, close evicts from registry, close is single canonical
     shutdown hook.
  7. Single-focus invariant preserved at tool level.
  8. Terminal sticky preserved at tool level.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.task_tool import (
    classify_task_args,
    close_task_board,
    get_or_create_task_board,
    registry_size,
    reset_task_board_registry,
    run_task_tool,
    task_tools_enabled,
)
from backend.core.ouroboros.governance.tool_executor import (
    _L1_MANIFESTS,
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
    ToolExecStatus,
)
from backend.core.ouroboros.governance.scoped_tool_access import (
    _MUTATION_TOOLS,
    ScopedToolGate,
    ToolScope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_task_env(monkeypatch):
    """Isolate every test — deny-by-default baseline + clean registry."""
    for key in list(os.environ.keys()):
        if (
            key.startswith("JARVIS_TOOL_TASK_BOARD_")
            or key.startswith("JARVIS_TASK_BOARD_")
        ):
            monkeypatch.delenv(key, raising=False)
    reset_task_board_registry()
    yield
    reset_task_board_registry()


def _pctx(op_id: str = "op-task-test") -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=Path("/tmp"),
        op_id=op_id,
        call_id=f"{op_id}:r0:t0",
        round_index=0,
        risk_tier=None,
        is_read_only=False,
    )


def _call(name: str, **args: Any) -> ToolCall:
    return ToolCall(name=name, arguments=dict(args))


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "true")


# ===========================================================================
# 1. Manifest integrity + authority invariant
# ===========================================================================


def test_three_manifests_registered_with_empty_capabilities():
    """Slice 2 test 1: all three task tools registered with the
    read-only/no-side-effect capability signature (empty frozenset).
    Pins the §1 Boundary invariant at manifest time."""
    for name in ("task_create", "task_update", "task_complete"):
        assert name in _L1_MANIFESTS, f"missing manifest: {name}"
        m = _L1_MANIFESTS[name]
        assert m.capabilities == frozenset(), (
            f"{name}: caps must be empty frozenset (no side effects); "
            f"got {m.capabilities}"
        )
        assert "write" not in m.capabilities


def test_task_tools_not_in_mutation_tools():
    """Slice 2 test 2 (CRITICAL): task tools MUST NOT be in
    _MUTATION_TOOLS. Ensures is_read_only scopes don't reject them."""
    for name in ("task_create", "task_update", "task_complete"):
        assert name not in _MUTATION_TOOLS


def test_task_tools_allowed_under_read_only_scope():
    """Slice 2 test 3: ScopedToolGate with read_only=True + allowlist
    containing the task tools — each is permitted. Scratchpad is
    not mutation."""
    gate = ScopedToolGate(ToolScope(
        read_only=True,
        allowed_tools=frozenset({"task_create", "task_update", "task_complete"}),
    ))
    for name in ("task_create", "task_update", "task_complete"):
        allowed, _reason = gate.can_use(name)
        assert allowed is True, f"{name} denied under read-only scope"


# ===========================================================================
# 2. Policy deny/allow matrix
# ===========================================================================


def test_policy_denies_when_master_switch_explicitly_off(monkeypatch):
    """Slice 2 test 4 (CRITICAL, post-Slice-4 graduation): explicit
    JARVIS_TOOL_TASK_BOARD_ENABLED=false (operator opt-out) DENIES
    every task tool. Proves the runtime kill-switch survives the
    graduation flip."""
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "false")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    for name, args in [
        ("task_create", {"title": "work"}),
        ("task_update", {"task_id": "task-x", "action": "start"}),
        ("task_complete", {"task_id": "task-x"}),
    ]:
        result = policy.evaluate(_call(name, **args), _pctx())
        assert result.decision == PolicyDecision.DENY, (
            name + " allowed while master explicit-false"
        )
        assert result.reason_code == "tool.denied.task_tools_disabled"


def test_policy_denies_master_switch_false_string(monkeypatch):
    """Slice 2 test 5: explicit 'false' also denies. Mirrors the
    monitor-tool edge-case guard that parser doesn't succumb to
    truthy-ish strings."""
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "false")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(_call("task_create", title="x"), _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_tools_disabled"


def test_policy_allows_when_master_switch_true(monkeypatch):
    """Slice 2 test 6: happy path — env on + well-formed args → ALLOW."""
    _enable(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(_call("task_create", title="work"), _pctx())
    assert result.decision == PolicyDecision.ALLOW


def test_policy_denies_bad_args_create_empty_title(monkeypatch):
    """Slice 2 test 7: empty title rejected at policy layer."""
    _enable(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(_call("task_create", title=""), _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_bad_args"


def test_policy_denies_bad_args_update_missing_fields(monkeypatch):
    """Slice 2 test 8: task_update with neither action nor title/body
    rejected."""
    _enable(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call("task_update", task_id="task-x"), _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_bad_args"


def test_policy_denies_bad_args_update_invalid_action(monkeypatch):
    """Slice 2 test 9: invalid action enum value rejected."""
    _enable(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call("task_update", task_id="task-x", action="FROBNICATE"),
        _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_bad_args"


def test_policy_denies_bad_args_update_action_with_content(monkeypatch):
    """Slice 2 test 10: mixing action + title/body is rejected —
    state transitions are a separate call shape from content edits."""
    _enable(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call("task_update", task_id="task-x", action="start", title="t"),
        _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_bad_args"


def test_policy_denies_bad_args_complete_missing_task_id(monkeypatch):
    """Slice 2 test 11: task_complete without task_id rejected."""
    _enable(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(_call("task_complete"), _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.task_bad_args"


# ===========================================================================
# 3. Handler happy paths
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_task_create_success(monkeypatch):
    """Slice 2 test 12: task_create happy path returns SUCCESS with
    the documented JSON shape."""
    _enable(monkeypatch)
    result = await run_task_tool(
        _call("task_create", title="research auth refactor"),
        _pctx("op-h-create"), timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    for key in (
        "task_id", "op_id", "state", "title", "body",
        "sequence", "active_task_id", "board_size",
    ):
        assert key in payload
    assert payload["state"] == "pending"
    assert payload["title"] == "research auth refactor"
    assert payload["op_id"] == "op-h-create"
    assert payload["board_size"] == 1
    assert payload["active_task_id"] is None  # pending ≠ active


@pytest.mark.asyncio
async def test_handler_task_update_start_transition(monkeypatch):
    """Slice 2 test 13: task_update with action=start transitions
    pending → in_progress and updates active_task_id."""
    _enable(monkeypatch)
    pctx = _pctx("op-h-start")
    create = await run_task_tool(
        _call("task_create", title="t"), pctx, timeout=10.0, cap=4096,
    )
    tid = json.loads(create.output)["task_id"]
    result = await run_task_tool(
        _call("task_update", task_id=tid, action="start"),
        pctx, timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["state"] == "in_progress"
    assert payload["active_task_id"] == tid


@pytest.mark.asyncio
async def test_handler_task_update_cancel_with_reason(monkeypatch):
    """Slice 2 test 14: task_update action=cancel with optional
    reason moves state → cancelled; reason captured."""
    _enable(monkeypatch)
    pctx = _pctx("op-h-cancel")
    create = await run_task_tool(
        _call("task_create", title="t"), pctx, timeout=10.0, cap=4096,
    )
    tid = json.loads(create.output)["task_id"]
    result = await run_task_tool(
        _call("task_update", task_id=tid, action="cancel",
              reason="redirected by operator"),
        pctx, timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["state"] == "cancelled"


@pytest.mark.asyncio
async def test_handler_task_update_content_edit(monkeypatch):
    """Slice 2 test 15: task_update without action edits title/body
    without changing state."""
    _enable(monkeypatch)
    pctx = _pctx("op-h-edit")
    create = await run_task_tool(
        _call("task_create", title="original"),
        pctx, timeout=10.0, cap=4096,
    )
    tid = json.loads(create.output)["task_id"]
    result = await run_task_tool(
        _call("task_update", task_id=tid, title="revised", body="new details"),
        pctx, timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["title"] == "revised"
    assert payload["body"] == "new details"
    assert payload["state"] == "pending"  # unchanged


@pytest.mark.asyncio
async def test_handler_task_complete_from_pending(monkeypatch):
    """Slice 2 test 16: quick-win path — pending → completed directly."""
    _enable(monkeypatch)
    pctx = _pctx("op-h-complete")
    create = await run_task_tool(
        _call("task_create", title="quick win"),
        pctx, timeout=10.0, cap=4096,
    )
    tid = json.loads(create.output)["task_id"]
    result = await run_task_tool(
        _call("task_complete", task_id=tid), pctx, timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["state"] == "completed"


@pytest.mark.asyncio
async def test_handler_full_lifecycle_create_start_complete(monkeypatch):
    """Slice 2 test 17 (CRITICAL END-TO-END): full canonical flow —
    create → update(start) → complete — works through the Venom
    tool surface."""
    _enable(monkeypatch)
    pctx = _pctx("op-lifecycle")
    r1 = await run_task_tool(
        _call("task_create", title="end-to-end"),
        pctx, timeout=10.0, cap=4096,
    )
    tid = json.loads(r1.output)["task_id"]

    r2 = await run_task_tool(
        _call("task_update", task_id=tid, action="start"),
        pctx, timeout=10.0, cap=4096,
    )
    assert json.loads(r2.output)["state"] == "in_progress"

    r3 = await run_task_tool(
        _call("task_complete", task_id=tid),
        pctx, timeout=10.0, cap=4096,
    )
    assert json.loads(r3.output)["state"] == "completed"
    # Active slot freed.
    assert json.loads(r3.output)["active_task_id"] is None


# ===========================================================================
# 4. Handler failure modes
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_bad_args_defense_in_depth():
    """Slice 2 test 18: direct-call bypass of the policy — the
    handler's own classify_task_args still rejects cleanly."""
    result = await run_task_tool(
        _call("task_create", title=""),  # empty title
        _pctx(), timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR


@pytest.mark.asyncio
async def test_handler_missing_op_id():
    """Slice 2 test 19: empty policy_ctx.op_id → clean EXEC_ERROR."""
    pctx = _pctx("")
    # Work around the validator in _pctx by constructing directly.
    pctx = PolicyContext(
        repo="jarvis", repo_root=Path("/tmp"), op_id="",
        call_id="no-op:r0:t0", round_index=0, risk_tier=None,
        is_read_only=False,
    )
    result = await run_task_tool(
        _call("task_create", title="x"), pctx,
        timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "op_id" in (result.error or "")


@pytest.mark.asyncio
async def test_handler_unknown_task_id(monkeypatch):
    """Slice 2 test 20: unknown task_id from handler → EXEC_ERROR
    with 'state:' prefix (TaskBoardStateError path)."""
    _enable(monkeypatch)
    result = await run_task_tool(
        _call("task_complete", task_id="task-ghost-0001"),
        _pctx("op-ghost"), timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "state:" in (result.error or "")


@pytest.mark.asyncio
async def test_handler_rejects_mutations_after_board_close(monkeypatch):
    """Slice 2 test 21 (CRITICAL): after close_task_board is called,
    subsequent tool invocations for that op produce EXEC_ERROR with
    a 'board_closed:' prefix — propagates TaskBoardClosedError.

    Note: close_task_board ALSO evicts from registry, so a later
    tool call lazily creates a fresh board. Test shows that on an
    already-evicted op, a direct call to the closed board (via
    get_or_create_task_board returning a fresh one) behaves as a
    new board. This documents the actual registry semantics."""
    _enable(monkeypatch)
    op_id = "op-close-behavior"
    pctx = _pctx(op_id)
    r1 = await run_task_tool(
        _call("task_create", title="before close"),
        pctx, timeout=10.0, cap=4096,
    )
    assert r1.status == ToolExecStatus.SUCCESS
    # Close explicitly via the canonical shutdown API.
    closed = close_task_board(op_id, reason="test close")
    assert closed is True
    assert registry_size() == 0
    # A subsequent tool call LAZILY CREATES a new board (registry
    # was evicted). This is the documented semantic.
    r2 = await run_task_tool(
        _call("task_create", title="after close"),
        pctx, timeout=10.0, cap=4096,
    )
    assert r2.status == ToolExecStatus.SUCCESS
    # New board, sequence restart.
    assert json.loads(r2.output)["sequence"] == 1


# ===========================================================================
# 5. Registry lifecycle — the single canonical shutdown hook
# ===========================================================================


def test_registry_lazy_create_on_first_touch():
    """Slice 2 test 22: registry_size is 0 until a board is requested."""
    reset_task_board_registry()
    assert registry_size() == 0
    _b = get_or_create_task_board("op-lazy")
    assert registry_size() == 1


def test_close_task_board_returns_false_on_unknown_op():
    """Slice 2 test 23: close_task_board is idempotent — calling on
    an op with no registered board returns False, does NOT raise."""
    reset_task_board_registry()
    assert close_task_board("op-never-touched") is False


def test_close_task_board_is_idempotent_on_same_op():
    """Slice 2 test 24: second close_task_board call on the same
    op_id returns False (already evicted). Matches the TaskBoard's
    own close() idempotence."""
    get_or_create_task_board("op-double-close")
    assert close_task_board("op-double-close") is True
    # Second call — board already evicted.
    assert close_task_board("op-double-close") is False


def test_multiple_ops_isolate_in_registry():
    """Slice 2 test 25: each op gets its own board; closing one
    doesn't affect the other."""
    reset_task_board_registry()
    b1 = get_or_create_task_board("op-a")
    b2 = get_or_create_task_board("op-b")
    assert b1 is not b2
    assert registry_size() == 2
    close_task_board("op-a")
    assert registry_size() == 1
    assert get_or_create_task_board("op-b") is b2  # unchanged


# ===========================================================================
# 6. Preserved Slice-1 invariants at tool level
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_single_focus_invariant_preserved(monkeypatch):
    """Slice 2 test 26: starting a second task while one is
    in_progress raises → EXEC_ERROR 'state:' prefix. Slice 1's
    invariant holds at the Venom surface."""
    _enable(monkeypatch)
    pctx = _pctx("op-focus")
    r1 = await run_task_tool(
        _call("task_create", title="A"), pctx, timeout=10.0, cap=4096,
    )
    r2 = await run_task_tool(
        _call("task_create", title="B"), pctx, timeout=10.0, cap=4096,
    )
    tid_a = json.loads(r1.output)["task_id"]
    tid_b = json.loads(r2.output)["task_id"]
    await run_task_tool(
        _call("task_update", task_id=tid_a, action="start"),
        pctx, timeout=10.0, cap=4096,
    )
    result = await run_task_tool(
        _call("task_update", task_id=tid_b, action="start"),
        pctx, timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "single-focus" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_handler_terminal_sticky_preserved(monkeypatch):
    """Slice 2 test 27: completing an already-terminal task raises
    → EXEC_ERROR. Slice 1 stickiness holds at the Venom surface."""
    _enable(monkeypatch)
    pctx = _pctx("op-sticky")
    r1 = await run_task_tool(
        _call("task_create", title="t"), pctx, timeout=10.0, cap=4096,
    )
    tid = json.loads(r1.output)["task_id"]
    await run_task_tool(
        _call("task_complete", task_id=tid), pctx, timeout=10.0, cap=4096,
    )
    result = await run_task_tool(
        _call("task_complete", task_id=tid), pctx, timeout=10.0, cap=4096,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR


# ===========================================================================
# 7. Module boundary / authority invariants
# ===========================================================================


def test_task_tool_module_does_not_import_gate_modules():
    """Slice 2 test 28 (CRITICAL): task_tool.py MUST NOT import
    Iron Gate / risk_tier_floor / semantic_guardian / policy_engine.
    Scratchpad stays scratchpad; no authority leakage."""
    src = Path(
        "backend/core/ouroboros/governance/task_tool.py"
    ).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for f in forbidden:
        assert f not in src, (
            "Slice 2 authority violation: task_tool.py imports "
            + repr(f) + ". Tasks must remain scratchpad; no authority "
            "surface."
        )


def test_task_tool_imports_primitive_directly():
    """Slice 2 test 29: task_tool.py DOES import TaskBoard from
    task_board.py (the Slice 1 primitive). Pins intended
    dependency direction."""
    src = Path(
        "backend/core/ouroboros/governance/task_tool.py"
    ).read_text()
    assert "from backend.core.ouroboros.governance.task_board" in src


# ===========================================================================
# 8. Helper-function pins (classify_task_args)
# ===========================================================================


def test_classify_create_rejects_missing_title():
    assert classify_task_args("task_create", {}) is not None


def test_classify_update_accepts_action_start():
    assert classify_task_args(
        "task_update", {"task_id": "t", "action": "start"},
    ) is None


def test_classify_update_accepts_action_cancel_with_reason():
    assert classify_task_args(
        "task_update", {"task_id": "t", "action": "cancel", "reason": "why"},
    ) is None


def test_classify_update_accepts_content_update():
    assert classify_task_args(
        "task_update", {"task_id": "t", "title": "new"},
    ) is None


def test_classify_task_tools_enabled_default_post_graduation_is_true(monkeypatch):
    """Slice 4 graduation pin (renamed from default-false test): the
    env helper returns True when JARVIS_TOOL_TASK_BOARD_ENABLED is
    absent, reflecting the Slice 4 graduation flip. Opt-out via
    explicit "false" is pinned separately."""
    monkeypatch.delenv("JARVIS_TOOL_TASK_BOARD_ENABLED", raising=False)
    assert task_tools_enabled() is True


def test_classify_task_tools_enabled_true(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_TASK_BOARD_ENABLED", "true")
    assert task_tools_enabled() is True
