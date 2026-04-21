"""Slice 2 tests — InlinePromptController, BlessedShapeLedger, middleware.

Covers:
* controller Future-blocking, timeout → deny, state-machine invariants
* ledger bless / find_blessing / TTL / clear_op
* **double-ask matrix** — pinned tests for NOTIFY_APPLY / PLAN_APPROVAL /
  ORANGE_REVIEW / ASK_HUMAN blessings against tool × path × hash shapes
* middleware end-to-end with fake renderer + fake resolver
* autonomous-route coercion survives the middleware path
* broken renderer / resolver fail closed (§7)
* REPL dispatcher commands
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
    OpApprovedScope,
    RoutePosture,
    UpstreamPolicy,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    ApprovedScopeResolver,
    BlessedShape,
    BlessedShapeLedger,
    BlessingSource,
    InlineMiddlewareOutcome,
    InlinePermissionMiddleware,
    InlinePromptCapacityError,
    InlinePromptController,
    InlinePromptOutcome,
    InlinePromptRequest,
    InlinePromptRenderer,
    InlinePromptStateError,
    OutcomeSource,
    ResponseKind,
    STATE_ALLOWED,
    STATE_DENIED,
    STATE_EXPIRED,
    STATE_PAUSED,
    STATE_PENDING,
    _NullRenderer,
    posture_for_route,
    reset_default_singletons,
    tool_family,
)
from backend.core.ouroboros.governance.inline_permission_repl import (
    ConsoleInlineRenderer,
    InlineDispatchResult,
    dispatch_inline_command,
)


# ---------------------------------------------------------------------------
# Shared helpers / fakes
# ---------------------------------------------------------------------------


class FakeRenderer:
    """Records every render/dismiss for assertions."""

    def __init__(self) -> None:
        self.rendered: List[InlinePromptRequest] = []
        self.dismissed: List[Tuple[str, InlinePromptOutcome]] = []

    def render(self, request: InlinePromptRequest) -> None:
        self.rendered.append(request)

    def dismiss(
        self, prompt_id: str, outcome: InlinePromptOutcome,
    ) -> None:
        self.dismissed.append((prompt_id, outcome))


class RaisingRenderer:
    """Always raises on render — used to pin fail-closed behavior."""

    def render(self, request: InlinePromptRequest) -> None:
        raise RuntimeError("renderer exploded")

    def dismiss(
        self, prompt_id: str, outcome: InlinePromptOutcome,
    ) -> None:
        pass


class FakeScopeResolver(ApprovedScopeResolver):
    def __init__(self, scope: Optional[OpApprovedScope] = None) -> None:
        self._scope = scope or OpApprovedScope()

    def resolve(self, op_id: str) -> OpApprovedScope:
        _ = op_id
        return self._scope


@pytest.fixture(autouse=True)
def _clean_singletons():
    """Each test starts with fresh module-level state."""
    reset_default_singletons()
    yield
    reset_default_singletons()


# ===========================================================================
# Controller: state machine, Future, timeout, capacity
# ===========================================================================


def _make_request(
    *,
    prompt_id: str = "p-1",
    op_id: str = "op-1",
    tool: str = "edit_file",
    target: str = "backend/x.py",
    rule_id: str = "RULE_EDIT_OUT_OF_APPROVED",
) -> InlinePromptRequest:
    from backend.core.ouroboros.governance.inline_permission import (
        InlineGateVerdict,
    )
    return InlinePromptRequest(
        prompt_id=prompt_id,
        op_id=op_id,
        call_id=f"{op_id}:r0.0:{tool}",
        tool=tool,
        arg_fingerprint=target,
        arg_preview=target,
        target_path=target,
        verdict=InlineGateVerdict(
            decision=InlineDecision.ASK,
            rule_id=rule_id,
            reason="test reason",
        ),
    )


@pytest.mark.asyncio
async def test_controller_blocks_future_until_allow_once():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    fut = ctrl.request(_make_request())
    assert not fut.done()
    # Now resolve from another "thread" (here: soon on the same loop)
    asyncio.get_event_loop().call_soon(
        lambda: ctrl.allow_once("p-1", reviewer="repl", reason="looks fine"),
    )
    outcome = await asyncio.wait_for(fut, timeout=1.0)
    assert outcome.state == STATE_ALLOWED
    assert outcome.response is ResponseKind.ALLOW_ONCE
    assert outcome.reviewer == "repl"


@pytest.mark.asyncio
async def test_controller_deny_records_reason():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    fut = ctrl.request(_make_request())
    ctrl.deny("p-1", reviewer="repl", reason="touching wrong file")
    out = await fut
    assert out.state == STATE_DENIED
    assert out.operator_reason == "touching wrong file"


@pytest.mark.asyncio
async def test_controller_allow_always_sets_remembered_flag():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    fut = ctrl.request(_make_request())
    ctrl.allow_always("p-1", reviewer="repl")
    out = await fut
    assert out.state == STATE_ALLOWED
    assert out.response is ResponseKind.ALLOW_ALWAYS
    assert out.remembered is True


@pytest.mark.asyncio
async def test_controller_pause_distinct_from_deny():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    fut = ctrl.request(_make_request())
    ctrl.pause_op("p-1", reviewer="repl", reason="let me check")
    out = await fut
    assert out.state == STATE_PAUSED
    assert out.response is ResponseKind.PAUSE_OP
    assert not out.allowed


@pytest.mark.asyncio
async def test_controller_timeout_auto_denies():
    ctrl = InlinePromptController(default_timeout_s=0.05)
    fut = ctrl.request(_make_request())
    out = await asyncio.wait_for(fut, timeout=2.0)
    assert out.state == STATE_EXPIRED
    assert out.reviewer == "auto-timeout"


@pytest.mark.asyncio
async def test_controller_state_machine_terminal_is_sticky():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    ctrl.request(_make_request())
    ctrl.allow_once("p-1", reviewer="repl")
    with pytest.raises(InlinePromptStateError):
        ctrl.deny("p-1", reviewer="repl")


@pytest.mark.asyncio
async def test_controller_capacity_enforced():
    ctrl = InlinePromptController(max_pending=2, default_timeout_s=10.0)
    ctrl.request(_make_request(prompt_id="a"))
    ctrl.request(_make_request(prompt_id="b"))
    with pytest.raises(InlinePromptCapacityError):
        ctrl.request(_make_request(prompt_id="c"))


@pytest.mark.asyncio
async def test_controller_listener_fires_on_every_transition():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    events: List[str] = []
    ctrl.on_transition(lambda p: events.append(p["event_type"]))
    ctrl.request(_make_request())
    ctrl.allow_once("p-1", reviewer="repl")
    assert events[0] == "inline_prompt_pending"
    assert events[1] == "inline_prompt_allowed"


@pytest.mark.asyncio
async def test_controller_listener_exception_does_not_break_transition():
    ctrl = InlinePromptController(default_timeout_s=5.0)

    def _bad(_p: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    ctrl.on_transition(_bad)
    ctrl.request(_make_request())
    # Should still resolve cleanly despite listener raising
    out = ctrl.allow_once("p-1", reviewer="repl")
    assert out.state == STATE_ALLOWED


@pytest.mark.asyncio
async def test_controller_snapshot_projects_expected_fields():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    ctrl.request(_make_request())
    snap = ctrl.snapshot("p-1")
    assert snap is not None
    assert snap["state"] == STATE_PENDING
    assert snap["tool"] == "edit_file"
    assert snap["verdict_rule_id"] == "RULE_EDIT_OUT_OF_APPROVED"


@pytest.mark.asyncio
async def test_controller_history_bounded():
    ctrl = InlinePromptController(default_timeout_s=5.0)
    ctrl.request(_make_request(prompt_id="x"))
    ctrl.allow_once("x", reviewer="repl")
    h = ctrl.history()
    assert len(h) == 1
    assert h[0]["state"] == STATE_ALLOWED


# ===========================================================================
# BlessedShapeLedger + double-ask matrix
# ===========================================================================


def test_ledger_bless_and_find_by_path():
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    ledger.bless_notify_apply(
        op_id="op-1",
        approved_paths=frozenset({"backend/core/"}),
        candidate_hash="h0",
    )
    bless = ledger.find_blessing(
        op_id="op-1", tool="edit_file",
        target_path="backend/core/foo.py", arg_fingerprint="",
        candidate_hash="h0",
    )
    assert bless is not None
    assert bless.source is BlessingSource.NOTIFY_APPLY


def test_ledger_hash_divergence_prevents_blessing():
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    ledger.bless_notify_apply(
        op_id="op-1",
        approved_paths=frozenset({"backend/core/"}),
        candidate_hash="h0",
    )
    bless = ledger.find_blessing(
        op_id="op-1", tool="edit_file",
        target_path="backend/core/foo.py", arg_fingerprint="",
        candidate_hash="DIFFERENT",
    )
    assert bless is None, "hash mismatch must NOT match (divergence case)"


def test_ledger_plan_approval_blesses_multiple_families():
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    ledger.bless_plan_approval(
        op_id="op-1",
        approved_paths=frozenset({"backend/"}),
    )
    # edit_file, write_file, delete_file all covered
    for tool in ("edit_file", "write_file", "delete_file"):
        b = ledger.find_blessing(
            op_id="op-1", tool=tool,
            target_path="backend/x.py", arg_fingerprint="",
        )
        assert b is not None, f"{tool} should be covered by plan_approval"
    # bash is NOT covered by plan_approval (no blessed_commands)
    b = ledger.find_blessing(
        op_id="op-1", tool="bash",
        target_path="", arg_fingerprint="make build",
    )
    assert b is None, "plan_approval must not bless bash"


def test_ledger_orange_review_blesses_bash_when_command_listed():
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    ledger.bless_orange_review(
        op_id="op-1",
        approved_paths=frozenset({"*"}),
        candidate_hash="h1",
        blessed_commands=frozenset({"make build"}),
    )
    b = ledger.find_blessing(
        op_id="op-1", tool="bash",
        target_path="", arg_fingerprint="make build",
        candidate_hash="h1",
    )
    assert b is not None
    assert b.source is BlessingSource.ORANGE_REVIEW
    # Different bash command NOT blessed
    b2 = ledger.find_blessing(
        op_id="op-1", tool="bash",
        target_path="", arg_fingerprint="rm -rf /",
        candidate_hash="h1",
    )
    assert b2 is None


def test_ledger_ask_human_never_blesses_any_shape():
    """ASK_HUMAN is clarification, not authorization — pinned by contract."""
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    now = time.monotonic()
    ledger.bless("op-1", BlessedShape(
        source=BlessingSource.ASK_HUMAN,
        tool_families=frozenset(),  # empty → never matches
        approved_paths=frozenset(),
        blessed_commands=frozenset(),
        blessed_at_ts=now, expires_at_ts=now + 60.0,
    ))
    for tool in ("edit_file", "write_file", "delete_file", "bash"):
        b = ledger.find_blessing(
            op_id="op-1", tool=tool,
            target_path="backend/x.py",
            arg_fingerprint="make build",
        )
        assert b is None, f"ASK_HUMAN must not bless {tool}"


def test_ledger_expired_blessing_not_returned():
    ledger = BlessedShapeLedger()
    now = time.monotonic()
    ledger.bless("op-1", BlessedShape(
        source=BlessingSource.NOTIFY_APPLY,
        tool_families=frozenset({"edit"}),
        approved_paths=frozenset({"backend/"}),
        blessed_at_ts=now - 100.0,
        expires_at_ts=now - 1.0,  # already expired
    ))
    b = ledger.find_blessing(
        op_id="op-1", tool="edit_file",
        target_path="backend/x.py", arg_fingerprint="",
    )
    assert b is None


def test_ledger_clear_op_purges_all_shapes():
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    ledger.bless_notify_apply(
        op_id="op-1", approved_paths=frozenset({"a/"}), candidate_hash="h",
    )
    ledger.bless_plan_approval(
        op_id="op-1", approved_paths=frozenset({"b/"}),
    )
    assert len(ledger.snapshot("op-1")) == 2
    n = ledger.clear_op("op-1")
    assert n == 2
    assert ledger.snapshot("op-1") == []


def test_tool_family_covers_expected_mapping():
    assert tool_family("edit_file") == "edit"
    assert tool_family("write_file") == "write"
    assert tool_family("delete_file") == "delete"
    assert tool_family("apply_patch") == "edit"
    assert tool_family("bash") == "bash"
    # Unknown tool passes through
    assert tool_family("unknown_tool") == "unknown_tool"


# ===========================================================================
# Middleware — end-to-end
# ===========================================================================


def _mw(
    *,
    approved: Tuple[str, ...] = (),
    renderer: Optional[InlinePromptRenderer] = None,
    prompt_timeout_s: float = 5.0,
) -> Tuple[InlinePermissionMiddleware, FakeRenderer, BlessedShapeLedger, InlinePromptController]:
    r = renderer or FakeRenderer()
    ctrl = InlinePromptController(default_timeout_s=prompt_timeout_s)
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    mw = InlinePermissionMiddleware(
        controller=ctrl,
        ledger=ledger,
        renderer=r,
        scope_resolver=FakeScopeResolver(
            OpApprovedScope(approved_paths=approved),
        ),
    )
    return mw, r, ledger, ctrl  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_middleware_safe_tool_proceeds_no_prompt():
    mw, rend, _, ctrl = _mw(approved=("backend/",))
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="read_file", arg_fingerprint="backend/x.py",
        target_path="backend/x.py",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
    )
    assert outcome.proceed is True
    assert outcome.source is OutcomeSource.GATE_SAFE
    assert rend.rendered == []


@pytest.mark.asyncio
async def test_middleware_block_tool_denies_no_prompt():
    mw, rend, _, _ = _mw()
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="bash", arg_fingerprint="sudo apt update",
        target_path="",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.GATE_BLOCK
    assert rend.rendered == [], "BLOCK must NEVER render a prompt"


@pytest.mark.asyncio
async def test_middleware_upstream_block_mirrors_without_prompt():
    mw, rend, _, _ = _mw()
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="read_file", arg_fingerprint="x.py",
        target_path="x.py",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.BLOCKED,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.UPSTREAM_BLOCK
    assert rend.rendered == []


@pytest.mark.asyncio
async def test_middleware_ask_renders_then_awaits_operator():
    mw, rend, _, ctrl = _mw(approved=("tests/",))

    async def _answer():
        # Wait until prompt registered, then allow
        for _ in range(200):
            if ctrl.pending_count > 0:
                break
            await asyncio.sleep(0.001)
        pid = ctrl.pending_ids()[0]
        ctrl.allow_once(pid, reviewer="repl", reason="on second look, fine")

    answer_task = asyncio.create_task(_answer())
    try:
        outcome = await asyncio.wait_for(
            mw.check(
                op_id="op-1", call_id="c-1",
                tool="edit_file", arg_fingerprint="backend/other.py",
                target_path="backend/other.py",
                route=RoutePosture.INTERACTIVE,
                upstream_decision=UpstreamPolicy.NO_MATCH,
            ),
            timeout=3.0,
        )
    finally:
        answer_task.cancel()
    assert outcome.proceed is True
    assert outcome.source is OutcomeSource.OPERATOR_ALLOW_ONCE
    assert len(rend.rendered) == 1
    assert len(rend.dismissed) == 1


@pytest.mark.asyncio
async def test_middleware_ask_operator_deny():
    mw, _, _, ctrl = _mw(approved=("tests/",))

    async def _answer():
        for _ in range(200):
            if ctrl.pending_count:
                break
            await asyncio.sleep(0.001)
        ctrl.deny(ctrl.pending_ids()[0], reviewer="repl", reason="nope")

    asyncio.create_task(_answer())
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/other.py",
            target_path="backend/other.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=3.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.OPERATOR_DENY
    assert outcome.reason == "nope"


@pytest.mark.asyncio
async def test_middleware_ask_operator_pause_distinct_from_deny():
    mw, _, _, ctrl = _mw(approved=("tests/",))

    async def _answer():
        for _ in range(200):
            if ctrl.pending_count:
                break
            await asyncio.sleep(0.001)
        ctrl.pause_op(ctrl.pending_ids()[0], reviewer="repl", reason="halt")

    asyncio.create_task(_answer())
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/other.py",
            target_path="backend/other.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=3.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.OPERATOR_PAUSE
    assert outcome.response is ResponseKind.PAUSE_OP


@pytest.mark.asyncio
async def test_middleware_ask_timeout_auto_denies():
    mw, _, _, _ = _mw(approved=("tests/",), prompt_timeout_s=0.05)
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/other.py",
            target_path="backend/other.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=2.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.TIMEOUT_DENY


@pytest.mark.asyncio
async def test_middleware_autonomous_route_never_prompts():
    mw, rend, _, _ = _mw(approved=("tests/",))
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="edit_file", arg_fingerprint="backend/other.py",
        target_path="backend/other.py",
        route=RoutePosture.AUTONOMOUS,
        upstream_decision=UpstreamPolicy.NO_MATCH,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.AUTONOMOUS_COERCE
    assert rend.rendered == []


@pytest.mark.asyncio
async def test_middleware_allow_always_flags_remembered():
    mw, _, _, ctrl = _mw(approved=("tests/",))

    async def _answer():
        for _ in range(200):
            if ctrl.pending_count:
                break
            await asyncio.sleep(0.001)
        ctrl.allow_always(ctrl.pending_ids()[0], reviewer="repl")

    asyncio.create_task(_answer())
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/other.py",
            target_path="backend/other.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=3.0,
    )
    assert outcome.proceed is True
    assert outcome.source is OutcomeSource.OPERATOR_ALLOW_ALWAYS
    assert outcome.remembered is True


@pytest.mark.asyncio
async def test_middleware_renderer_failure_denies_fail_closed():
    """§7: broken renderer must never silently allow."""
    mw, _, _, _ = _mw(approved=("tests/",), renderer=RaisingRenderer())
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/other.py",
            target_path="backend/other.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=2.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.OPERATOR_DENY
    assert "renderer_failure" in outcome.reason


@pytest.mark.asyncio
async def test_middleware_null_renderer_does_not_crash():
    ctrl = InlinePromptController(default_timeout_s=0.05)
    mw = InlinePermissionMiddleware(
        controller=ctrl,
        ledger=BlessedShapeLedger(default_ttl_s=60.0),
        renderer=_NullRenderer(),
        scope_resolver=FakeScopeResolver(),
    )
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/other.py",
            target_path="backend/other.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=2.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.TIMEOUT_DENY


# ===========================================================================
# Double-ask matrix — the load-bearing behavioral lock (§6 additive)
# ===========================================================================


@pytest.mark.asyncio
async def test_double_ask_notify_apply_blessed_shape_skips_prompt():
    mw, rend, ledger, _ = _mw(approved=("tests/",))
    ledger.bless_notify_apply(
        op_id="op-1",
        approved_paths=frozenset({"backend/core/"}),
        candidate_hash="h-abc",
    )
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="edit_file", arg_fingerprint="backend/core/foo.py",
        target_path="backend/core/foo.py",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
        candidate_hash="h-abc",
    )
    assert outcome.proceed is True
    assert outcome.source is OutcomeSource.LEDGER_BLESSED
    assert outcome.blessing_source is BlessingSource.NOTIFY_APPLY
    assert rend.rendered == [], "blessed shape must NOT re-prompt"


@pytest.mark.asyncio
async def test_double_ask_notify_apply_path_divergence_reaches_prompt():
    """Risk-shape divergence: different path → inline ASK fires."""
    mw, _, ledger, ctrl = _mw(approved=("tests/",), prompt_timeout_s=0.05)
    ledger.bless_notify_apply(
        op_id="op-1",
        approved_paths=frozenset({"backend/core/"}),
        candidate_hash="h-abc",
    )
    # Target is OUTSIDE the blessed scope → blessing doesn't apply
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="docs/readme.md",
            target_path="docs/readme.md",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
            candidate_hash="h-abc",
        ),
        timeout=2.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.TIMEOUT_DENY
    _ = ctrl  # silence pyright


@pytest.mark.asyncio
async def test_double_ask_notify_apply_hash_divergence_reaches_prompt():
    """Risk-shape divergence: same path, different candidate hash → inline ASK fires."""
    mw, _, ledger, _ = _mw(approved=("tests/",), prompt_timeout_s=0.05)
    ledger.bless_notify_apply(
        op_id="op-1",
        approved_paths=frozenset({"backend/core/"}),
        candidate_hash="h-abc",
    )
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/core/foo.py",
            target_path="backend/core/foo.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
            candidate_hash="h-DIFFERENT",
        ),
        timeout=2.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.TIMEOUT_DENY


@pytest.mark.asyncio
async def test_double_ask_plan_approval_covers_file_tools_not_bash():
    mw, rend, ledger, ctrl = _mw(approved=("tests/",), prompt_timeout_s=0.05)
    ledger.bless_plan_approval(
        op_id="op-1",
        approved_paths=frozenset({"backend/"}),
    )
    # edit in-scope → blessed
    out1 = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="edit_file", arg_fingerprint="backend/x.py",
        target_path="backend/x.py",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
    )
    assert out1.source is OutcomeSource.LEDGER_BLESSED

    # bash NEVER blessed by plan_approval
    out2 = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-2",
            tool="bash", arg_fingerprint="make build",
            target_path="",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=2.0,
    )
    assert out2.source is OutcomeSource.TIMEOUT_DENY
    assert len(rend.rendered) == 1, "exactly one prompt (for bash), not two"
    _ = ctrl


@pytest.mark.asyncio
async def test_double_ask_ask_human_does_not_bless_any_shape():
    """ASK_HUMAN is clarification, not authorization (lock pinned by contract)."""
    mw, rend, ledger, _ = _mw(approved=("tests/",), prompt_timeout_s=0.05)
    now = time.monotonic()
    ledger.bless("op-1", BlessedShape(
        source=BlessingSource.ASK_HUMAN,
        tool_families=frozenset(),
        approved_paths=frozenset(),
        blessed_at_ts=now, expires_at_ts=now + 60.0,
    ))
    # Any tool call must STILL prompt (or time out) — not be blessed
    outcome = await asyncio.wait_for(
        mw.check(
            op_id="op-1", call_id="c-1",
            tool="edit_file", arg_fingerprint="backend/x.py",
            target_path="backend/x.py",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        ),
        timeout=2.0,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.TIMEOUT_DENY
    assert len(rend.rendered) == 1


@pytest.mark.asyncio
async def test_double_ask_orange_review_blesses_wildcard_tools():
    mw, _, ledger, _ = _mw(approved=("tests/",))
    ledger.bless_orange_review(
        op_id="op-1",
        approved_paths=frozenset({"backend/"}),
        candidate_hash="h-x",
        blessed_commands=frozenset({"make test"}),
    )
    # edit_file covered
    out = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="edit_file", arg_fingerprint="backend/x.py",
        target_path="backend/x.py",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
        candidate_hash="h-x",
    )
    assert out.source is OutcomeSource.LEDGER_BLESSED
    # bash with blessed command covered
    out2 = await mw.check(
        op_id="op-1", call_id="c-2",
        tool="bash", arg_fingerprint="make test",
        target_path="",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
        candidate_hash="h-x",
    )
    assert out2.source is OutcomeSource.LEDGER_BLESSED


@pytest.mark.asyncio
async def test_double_ask_blessing_does_not_override_upstream_block():
    """Even with a blessing, upstream BLOCK still mirrors."""
    mw, _, ledger, _ = _mw(approved=("tests/",))
    ledger.bless_orange_review(
        op_id="op-1",
        approved_paths=frozenset({"*"}),
        candidate_hash="h",
    )
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="edit_file", arg_fingerprint=".env",
        target_path=".env",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.BLOCKED,
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.UPSTREAM_BLOCK


@pytest.mark.asyncio
async def test_double_ask_blessing_does_not_override_gate_block():
    """Gate BLOCK (protected path) is irrevocable even when blessing would match shape."""
    mw, _, ledger, _ = _mw(approved=("tests/",))
    ledger.bless_orange_review(
        op_id="op-1",
        approved_paths=frozenset({"*"}),
        candidate_hash="h",
    )
    outcome = await mw.check(
        op_id="op-1", call_id="c-1",
        tool="edit_file", arg_fingerprint=".env.production",
        target_path=".env.production",  # protected-path BLOCK
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
        candidate_hash="h",
    )
    assert outcome.proceed is False
    assert outcome.source is OutcomeSource.GATE_BLOCK


# ===========================================================================
# Route posture helper
# ===========================================================================


def test_posture_maps_route_strings():
    assert posture_for_route("immediate") is RoutePosture.INTERACTIVE
    assert posture_for_route("standard") is RoutePosture.INTERACTIVE
    assert posture_for_route("complex") is RoutePosture.INTERACTIVE
    assert posture_for_route("background") is RoutePosture.AUTONOMOUS
    assert posture_for_route("speculative") is RoutePosture.AUTONOMOUS
    assert posture_for_route("") is RoutePosture.INTERACTIVE  # fail-safe default
    assert posture_for_route("BACKGROUND") is RoutePosture.AUTONOMOUS  # case-insensitive


# ===========================================================================
# REPL dispatcher
# ===========================================================================


def test_repl_unknown_command_falls_through():
    result = dispatch_inline_command("/plan mode on")
    assert result.matched is False


@pytest.mark.asyncio
async def test_repl_list_empty_when_no_prompts():
    result = dispatch_inline_command("/prompts")
    assert result.ok is True
    assert "no inline prompts pending" in result.text


@pytest.mark.asyncio
async def test_repl_list_shows_pending():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    ctrl.request(_make_request(prompt_id="abc123"))
    result = dispatch_inline_command("/prompts", controller=ctrl)
    assert result.ok is True
    assert "abc123" in result.text
    assert "edit_file" in result.text


@pytest.mark.asyncio
async def test_repl_show_detail():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    ctrl.request(_make_request(prompt_id="show1"))
    result = dispatch_inline_command("/prompts show show1", controller=ctrl)
    assert result.ok is True
    assert "op-1" in result.text
    assert "RULE_EDIT_OUT_OF_APPROVED" in result.text


@pytest.mark.asyncio
async def test_repl_allow_without_id_picks_oldest():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    fut = ctrl.request(_make_request(prompt_id="p1"))
    result = dispatch_inline_command("/allow", controller=ctrl)
    assert result.ok is True
    out = await fut
    assert out.state == STATE_ALLOWED
    assert out.response is ResponseKind.ALLOW_ONCE


@pytest.mark.asyncio
async def test_repl_always_marks_remembered():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    fut = ctrl.request(_make_request(prompt_id="p1"))
    result = dispatch_inline_command("/always", controller=ctrl)
    assert result.ok is True
    out = await fut
    assert out.response is ResponseKind.ALLOW_ALWAYS


@pytest.mark.asyncio
async def test_repl_deny_with_reason():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    fut = ctrl.request(_make_request(prompt_id="p1"))
    result = dispatch_inline_command(
        '/deny p1 "this is destructive"', controller=ctrl,
    )
    assert result.ok is True
    out = await fut
    assert out.state == STATE_DENIED
    assert out.operator_reason == "this is destructive"


@pytest.mark.asyncio
async def test_repl_pause_with_reason():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    fut = ctrl.request(_make_request(prompt_id="p1"))
    result = dispatch_inline_command("/pause let me check", controller=ctrl)
    assert result.ok is True
    out = await fut
    assert out.state == STATE_PAUSED
    assert "let me check" in out.operator_reason


@pytest.mark.asyncio
async def test_repl_allow_unknown_id_errors():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    result = dispatch_inline_command("/allow DOES_NOT_EXIST", controller=ctrl)
    assert result.ok is False


@pytest.mark.asyncio
async def test_repl_history_shows_resolved():
    ctrl = InlinePromptController(default_timeout_s=10.0)
    ctrl.request(_make_request(prompt_id="h1"))
    ctrl.allow_once("h1", reviewer="repl")
    result = dispatch_inline_command("/prompts history", controller=ctrl)
    assert result.ok is True
    assert "h1" in result.text


@pytest.mark.asyncio
async def test_repl_help_shows_commands():
    result = dispatch_inline_command("/prompts help")
    assert result.ok is True
    assert "/allow" in result.text
    assert "/deny" in result.text
    assert "/pause" in result.text


# ===========================================================================
# ConsoleInlineRenderer format_block — pure formatter, golden-ish
# ===========================================================================


def test_format_block_includes_required_fields():
    req = _make_request(prompt_id="X", target="backend/foo.py")
    block = ConsoleInlineRenderer.format_block(req)
    assert "edit_file" in block
    assert "backend/foo.py" in block
    assert "RULE_EDIT_OUT_OF_APPROVED" in block
    assert "/allow" in block
    assert "/deny" in block


def test_format_block_truncates_long_rationale():
    from backend.core.ouroboros.governance.inline_permission import (
        InlineGateVerdict,
    )
    req = InlinePromptRequest(
        prompt_id="X", op_id="op", call_id="c",
        tool="edit_file", arg_fingerprint="foo", arg_preview="foo",
        target_path="foo", rationale="X" * 1000,
        verdict=InlineGateVerdict(
            decision=InlineDecision.ASK,
            rule_id="R", reason="r",
        ),
    )
    block = ConsoleInlineRenderer.format_block(req)
    # rationale is truncated in the request itself by the middleware — this
    # formatter respects ``request.rationale`` verbatim but the block ≤ a
    # human-readable size
    assert len(block) < 2500


def test_console_renderer_prints_and_dismisses():
    lines: List[str] = []
    rend = ConsoleInlineRenderer(lines.append)
    req = _make_request(prompt_id="R1")
    rend.render(req)
    rend.dismiss("R1", InlinePromptOutcome(
        prompt_id="R1", state=STATE_ALLOWED,
        response=ResponseKind.ALLOW_ONCE,
        reviewer="repl", elapsed_s=0.5,
    ))
    assert any("edit_file" in line for line in lines)
    assert any("allowed" in line and "R1" in line for line in lines)


# ===========================================================================
# ToolExecutor integration: smoke test via direct helper import
# ===========================================================================


def test_tool_executor_helpers_extract_expected_shapes():
    from backend.core.ouroboros.governance.tool_executor import (
        ToolCall,
        _inline_extract_fingerprint,
        _inline_extract_target_path,
    )
    bash_tc = ToolCall(name="bash", arguments={"command": "rm -rf /tmp/foo"})
    edit_tc = ToolCall(
        name="edit_file",
        arguments={"file_path": "backend/x.py", "patch": "..."},
    )
    assert _inline_extract_fingerprint(bash_tc) == "rm -rf /tmp/foo"
    assert _inline_extract_target_path(bash_tc) == ""
    assert _inline_extract_fingerprint(edit_tc) == "backend/x.py"
    assert _inline_extract_target_path(edit_tc) == "backend/x.py"


def test_tool_executor_hook_disabled_by_default(monkeypatch):
    """§5 Slice 5 graduation: master switch default OFF until Slice 5 flips."""
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        inline_permission_enabled,
    )
    monkeypatch.delenv("JARVIS_INLINE_PERMISSION_ENABLED", raising=False)
    assert inline_permission_enabled() is False


def test_tool_executor_hook_respects_env_override(monkeypatch):
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        inline_permission_enabled,
    )
    monkeypatch.setenv("JARVIS_INLINE_PERMISSION_ENABLED", "true")
    assert inline_permission_enabled() is True
    monkeypatch.setenv("JARVIS_INLINE_PERMISSION_ENABLED", "false")
    assert inline_permission_enabled() is False
    # Also accepts junk as false (fail-closed on malformed env)
    monkeypatch.setenv("JARVIS_INLINE_PERMISSION_ENABLED", "maybe")
    assert inline_permission_enabled() is False


@pytest.mark.asyncio
async def test_tool_executor_maybe_hook_returns_none_when_disabled(monkeypatch):
    """ToolLoopCoordinator hook is a no-op when env flag is off."""
    monkeypatch.delenv("JARVIS_INLINE_PERMISSION_ENABLED", raising=False)
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyContext, ToolCall, ToolLoopCoordinator,
    )
    from pathlib import Path

    # Minimal fake backend + policy to satisfy the constructor.
    class _FakePolicy:
        def evaluate(self, call, ctx): ...
        def repo_root_for(self, repo): return Path(".")

    class _FakeBackend:
        async def execute_async(self, call, ctx, deadline): ...

    coord = ToolLoopCoordinator(
        backend=_FakeBackend(),  # type: ignore[arg-type]
        policy=_FakePolicy(),    # type: ignore[arg-type]
        max_rounds=1,
        tool_timeout_s=5.0,
    )
    tc = ToolCall(name="edit_file", arguments={"file_path": "x.py"})
    ctx = PolicyContext(
        repo="r", repo_root=Path("."), op_id="op-1",
        call_id="cid", round_index=0,
    )
    result = await coord._maybe_inline_permission_check(tc, ctx, "cid")
    assert result is None


@pytest.mark.asyncio
async def test_tool_executor_maybe_hook_denies_when_enabled_and_blocked(monkeypatch):
    """When enabled, gate BLOCK surfaces as (reason_code, detail) → outer DENY."""
    monkeypatch.setenv("JARVIS_INLINE_PERMISSION_ENABLED", "true")
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyContext, ToolCall, ToolLoopCoordinator,
    )
    from pathlib import Path

    class _FakePolicy:
        def evaluate(self, call, ctx): ...
        def repo_root_for(self, repo): return Path(".")

    class _FakeBackend:
        async def execute_async(self, call, ctx, deadline): ...

    coord = ToolLoopCoordinator(
        backend=_FakeBackend(),  # type: ignore[arg-type]
        policy=_FakePolicy(),    # type: ignore[arg-type]
        max_rounds=1, tool_timeout_s=5.0,
    )
    # bash + sudo → RULE_BASH_SUDO BLOCK
    tc = ToolCall(name="bash", arguments={"command": "sudo rm /x"})
    ctx = PolicyContext(
        repo="r", repo_root=Path("."), op_id="op-1",
        call_id="cid", round_index=0,
    )
    # Use an isolated middleware to avoid the default singleton's state
    override_ctrl = InlinePromptController(default_timeout_s=5.0)
    override_ledger = BlessedShapeLedger(default_ttl_s=60.0)
    mw = InlinePermissionMiddleware(
        controller=override_ctrl, ledger=override_ledger,
        renderer=FakeRenderer(),
        scope_resolver=FakeScopeResolver(),
    )
    coord.set_inline_middleware_override(mw)
    result = await coord._maybe_inline_permission_check(tc, ctx, "cid")
    assert result is not None
    reason_code, detail = result
    assert "gate_block" in reason_code
    assert "sudo" in detail.lower()
