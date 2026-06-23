"""T3 — Graceful Semantic Pivot (Adaptive Epistemic Feedback Matrix).

Spec: docs/superpowers/specs/2026-06-22-epistemic-feedback-and-lane-escalation.md §1.3

When a sub-goal's repair is UNRESOLVABLE (the same ``failure_signature_hash``
persists after the temperature degenerates to the floor), the loop does NOT
deadlock: the repair engine surfaces an ``L2_PIVOT`` outcome carrying the
signature + stderr tail, ``_l2_hook`` returns ``("l2_pivot", ctx, sig, tail)``,
and ``_handle_l2_pivot``:

  * emits ``[SOVEREIGN YIELD: UNRESOLVABLE PATH]`` telemetry,
  * ``decompose_for_block(..., failure_hint=...)`` at the failure locus and
    re-injects via the SAME ``advance_orchestration`` seam → terminates
    ``decomposed`` (the pivot is PROGRESS), OR
  * if the op is already atomic → ``append_dlq(reason=
    "l2_unresolvable_awaiting_human")`` + soft-terminate.

DAG-preserving: only THIS sub-goal pivots; sibling ops are never touched.
Gated by ``epistemic_feedback_enabled()`` — OFF byte-identical (the engine
never emits L2_PIVOT, the directive never fires).
"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "orchestrator.py"
)
REPAIR_ENGINE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "repair_engine.py"
)
VALIDATE_RUNNER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "validate_runner.py"
)


# ======================================================================
# 1. The REAL pivot_verdict drives the trigger (signal-based, no caps)
# ======================================================================


def test_pivot_verdict_true_only_when_temp_at_floor_and_repeated() -> None:
    from backend.core.ouroboros.governance.epistemic_feedback import (
        pivot_verdict,
    )
    # Default JARVIS_EPISTEMIC_PIVOT_PASSES == 2.
    # temp NOT at floor -> never pivots regardless of repeat count.
    assert pivot_verdict(5, temp_at_floor=False) is False
    # temp at floor but not enough repeats -> no pivot.
    assert pivot_verdict(1, temp_at_floor=True) is False
    # temp at floor AND signature persisted >= 2 -> PIVOT.
    assert pivot_verdict(2, temp_at_floor=True) is True
    assert pivot_verdict(9, temp_at_floor=True) is True


def test_pivot_verdict_respects_env_passes(monkeypatch) -> None:
    from backend.core.ouroboros.governance import epistemic_feedback as ef
    monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "4")
    assert ef.pivot_verdict(3, temp_at_floor=True) is False
    assert ef.pivot_verdict(4, temp_at_floor=True) is True


# ======================================================================
# 2. repair_engine surfaces an L2_PIVOT carrying sig + stderr tail
# ======================================================================


def test_repair_result_carries_pivot_payload_fields() -> None:
    from backend.core.ouroboros.governance.repair_engine import RepairResult
    # Additive fields default empty -> legacy construction byte-identical.
    legacy = RepairResult(
        terminal="L2_STOPPED", candidate=None, stop_reason="x",
        summary={}, iterations=(),
    )
    assert legacy.failure_signature_hash == ""
    assert legacy.stderr_tail == ""
    # Pivot construction carries the payload.
    piv = RepairResult(
        terminal="L2_PIVOT", candidate=None, stop_reason="no_progress_streak",
        summary={"pivot": "unresolvable_path"}, iterations=(),
        failure_signature_hash="deadbeef", stderr_tail="Traceback: foo()",
    )
    assert piv.terminal == "L2_PIVOT"
    assert piv.failure_signature_hash == "deadbeef"
    assert "foo" in piv.stderr_tail


def test_ast_pin_run_inner_consults_pivot_verdict_before_stopped() -> None:
    """The divergence terminal-stop must consult the REAL pivot_verdict and
    return an L2_PIVOT before the legacy ``_stopped``. Structural pin so a
    refactor cannot silently sever the trigger."""
    src = REPAIR_ENGINE_FILE.read_text()
    assert "pivot_verdict" in src, "repair_engine must import pivot_verdict"
    assert "_pivoted(" in src, "repair_engine must have a _pivoted terminal"
    assert 'terminal="L2_PIVOT"' in src
    # The pivot decision must sit at the divergence (escape-disabled) stop.
    assert "_temp_at_floor" in src
    assert "UNRESOLVABLE PATH" in src


# ======================================================================
# 3. _l2_hook maps L2_PIVOT -> ("l2_pivot", ctx, sig, tail)
# ======================================================================


def test_ast_pin_l2_hook_emits_l2_pivot_directive() -> None:
    src = ORCHESTRATOR_FILE.read_text()
    assert 'elif l2_result.terminal == "L2_PIVOT":' in src, (
        "_l2_hook must branch on the L2_PIVOT terminal"
    )
    assert 'return ("l2_pivot", ctx, _pivot_sig, _pivot_tail)' in src, (
        "_l2_hook must return the l2_pivot directive with sig + tail"
    )


def test_all_three_consumers_dispatch_l2_pivot() -> None:
    """All _l2_hook directive consumers must route l2_pivot to the shared
    handler (orchestrator VALIDATE_RETRY @primary, orchestrator VERIFY, and
    the production validate_runner)."""
    orch = ORCHESTRATOR_FILE.read_text()
    vr = VALIDATE_RUNNER_FILE.read_text()
    assert orch.count('elif directive[0] == "l2_pivot":') == 2, (
        "both orchestrator consumers must dispatch l2_pivot"
    )
    assert orch.count("_handle_l2_pivot(") >= 3  # def + 2 callsites
    assert 'elif directive[0] == "l2_pivot":' in vr
    assert "orch._handle_l2_pivot(" in vr


# ======================================================================
# 4. decompose_for_block failure_hint biases the failure locus FIRST
# ======================================================================


@dataclass(frozen=True)
class _FakeScopedTarget:
    file_path: str
    symbol: str
    lineno: int = 0
    end_lineno: int = 0


@dataclass
class _FakeGoal:
    goal_id: str
    title: str
    description: str
    target_files: Tuple[str, ...]


def _scoper(symbols):
    def _scope(file_path, description):
        return tuple(
            _FakeScopedTarget(file_path=file_path, symbol=s) for s in symbols
        )
    return _scope


def test_decompose_failure_hint_scopes_locus_first() -> None:
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        decompose_for_block,
    )
    goal = _FakeGoal(
        goal_id="op-1", title="fix it", description="patch the module",
        target_files=("m.py",),
    )
    # 3 symbols; the failure trace implicates "beta".
    subs = decompose_for_block(
        goal, zero_coverage=False, scoper=_scoper(["alpha", "beta", "gamma"]),
        # one symbol per chunk so ordering is visible across sub-goals.
        compression_target=1,
        failure_hint={
            "signature_hash": "h",
            "stderr_tail": 'File "m.py", line 9, in beta\n    raise ValueError',
        },
    )
    # First mutation sub-goal's scoped symbol must be the failure locus.
    muts = [s for s in subs if s.scoped_symbols]
    assert muts, "expected at least one scoped mutation sub-goal"
    first_syms = muts[0].scoped_symbols
    assert any(r.endswith("::beta") for r in first_syms), (
        f"failure-locus symbol must be scoped FIRST, got {first_syms}"
    )


def test_decompose_failure_hint_failsoft_no_match_preserves_order() -> None:
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        decompose_for_block,
    )
    goal = _FakeGoal(
        goal_id="op-2", title="t", description="d", target_files=("m.py",),
    )
    base = decompose_for_block(
        goal, zero_coverage=False, scoper=_scoper(["alpha", "beta"]),
        compression_target=1,
    )
    hinted = decompose_for_block(
        goal, zero_coverage=False, scoper=_scoper(["alpha", "beta"]),
        compression_target=1,
        failure_hint={"stderr_tail": "no identifiers match here zzz"},
    )
    # No locus match -> byte-identical scoped order.
    base_syms = [s.scoped_symbols for s in base if s.scoped_symbols]
    hinted_syms = [s.scoped_symbols for s in hinted if s.scoped_symbols]
    assert base_syms == hinted_syms


def test_decompose_failure_hint_none_is_legacy() -> None:
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        decompose_for_block,
    )
    goal = _FakeGoal(
        goal_id="op-3", title="t", description="d", target_files=("m.py",),
    )
    a = decompose_for_block(goal, zero_coverage=False, scoper=_scoper(["x"]))
    b = decompose_for_block(
        goal, zero_coverage=False, scoper=_scoper(["x"]), failure_hint=None,
    )
    assert [s.scoped_symbols for s in a] == [s.scoped_symbols for s in b]


# ======================================================================
# 5. _handle_l2_pivot: decompose-further re-injects + terminates decomposed;
#    atomic -> DLQ + soft-terminate.   (exercise the real handler)
# ======================================================================


@dataclass(frozen=True)
class _FakePhaseR:
    name: str


@dataclass
class _FakeCtx:
    op_id: str
    description: str
    target_files: Tuple[str, ...]
    phase: _FakePhaseR
    terminal_reason_code: str = ""

    def advance(self, phase, *, terminal_reason_code=""):
        return _FakeCtx(
            op_id=self.op_id, description=self.description,
            target_files=self.target_files, phase=phase,
            terminal_reason_code=terminal_reason_code,
        )


def _make_orch():
    """Construct a bare orchestrator object whose _handle_l2_pivot can run
    without the full pipeline. We bind only the attributes the method reads."""
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
    orch = GovernedOrchestrator.__new__(GovernedOrchestrator)
    return orch


@dataclass
class _FakeReport:
    made_forward_progress: bool
    emitted_count: int = 0
    emitted_this_tick: int = 0
    diagnostic: str = ""


def test_handle_pivot_decomposes_and_terminates_decomposed(monkeypatch) -> None:
    import backend.core.ouroboros.governance.orchestrator as orch_mod
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        SubGoal, SubGoalKind,
    )

    captured = {}

    # Fake decompose returns a genuine further split (2 scoped sub-goals).
    def _fake_decompose(goal, *, zero_coverage, failure_hint=None, **kw):
        captured["failure_hint"] = failure_hint
        return (
            SubGoal(
                sub_goal_id="op-X::step-00", parent_goal_id="op-X",
                title="t", description="d", kind=SubGoalKind.ATOMIC,
                target_files=("m.py",), depends_on_sub_ids=(),
                estimated_complexity="moderate", boundary_crossed=False,
                scoped_symbols=("m.py::beta",),
            ),
            SubGoal(
                sub_goal_id="op-X::step-01", parent_goal_id="op-X",
                title="t", description="d", kind=SubGoalKind.ATOMIC,
                target_files=("m.py",), depends_on_sub_ids=(),
                estimated_complexity="moderate", boundary_crossed=False,
                scoped_symbols=("m.py::gamma",),
            ),
        )

    async def _fake_advance(plan, *, router=None):
        captured["plan"] = plan
        return _FakeReport(made_forward_progress=True, emitted_count=2,
                           emitted_this_tick=2)

    dlq_calls = []

    def _fake_dlq(env, *, reason, path=None):
        dlq_calls.append(reason)

    monkeypatch.setattr(orch_mod, "decompose_for_block", _fake_decompose)
    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.intake_dlq.append_dlq", _fake_dlq,
    )

    orch = _make_orch()
    orch._stack = None  # getattr(...) -> None router; advance is faked anyway
    # Make _record_ledger / _l2_escape_terminal harmless.
    async def _noop_ledger(*a, **k):
        return None
    monkeypatch.setattr(orch, "_record_ledger", _noop_ledger, raising=False)
    monkeypatch.setattr(
        orch, "_l2_escape_terminal",
        lambda p: _FakePhaseR("CANCELLED"), raising=False,
    )

    ctx = _FakeCtx(
        op_id="op-X", description="patch beta", target_files=("m.py",),
        phase=_FakePhaseR("VALIDATE_RETRY"),
    )
    out = asyncio.run(orch._handle_l2_pivot(ctx, "sig123", "in beta\nraise"))

    # The failure hint must have been threaded into decompose.
    assert captured["failure_hint"]["signature_hash"] == "sig123"
    assert "beta" in captured["failure_hint"]["stderr_tail"]
    # Re-injected (advance got the plan) and terminated 'decomposed'.
    assert "plan" in captured
    assert out.terminal_reason_code == "decomposed"
    # NOT routed to DLQ — this was a genuine split.
    assert dlq_calls == []


def test_handle_pivot_atomic_routes_to_dlq(monkeypatch) -> None:
    import backend.core.ouroboros.governance.orchestrator as orch_mod
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        SubGoal, SubGoalKind,
    )

    # Atomic: decompose returns a single whole-op fallback (no scoped_symbols,
    # id mirrors the parent) -> _pivot_is_atomic True -> DLQ.
    def _fake_decompose(goal, *, zero_coverage, failure_hint=None, **kw):
        return (
            SubGoal(
                sub_goal_id="op-Y::step-00", parent_goal_id="op-Y",
                title="t", description="d", kind=SubGoalKind.ATOMIC,
                target_files=("m.py",), depends_on_sub_ids=(),
                estimated_complexity="moderate", boundary_crossed=False,
                scoped_symbols=(),  # no narrower scope -> atomic
            ),
        )

    advance_called = {"hit": False}

    async def _fake_advance(plan, *, router=None):
        advance_called["hit"] = True
        return _FakeReport(made_forward_progress=True)

    dlq_calls = []

    def _fake_dlq(env, *, reason, path=None):
        dlq_calls.append((reason, env.get("failure_signature_hash")))

    monkeypatch.setattr(orch_mod, "decompose_for_block", _fake_decompose)
    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.intake_dlq.append_dlq", _fake_dlq,
    )

    orch = _make_orch()
    orch._stack = None
    async def _noop_ledger(*a, **k):
        return None
    monkeypatch.setattr(orch, "_record_ledger", _noop_ledger, raising=False)
    monkeypatch.setattr(
        orch, "_l2_escape_terminal",
        lambda p: _FakePhaseR("CANCELLED"), raising=False,
    )

    ctx = _FakeCtx(
        op_id="op-Y", description="atomic fix", target_files=("m.py",),
        phase=_FakePhaseR("VALIDATE_RETRY"),
    )
    out = asyncio.run(orch._handle_l2_pivot(ctx, "sigZ", "no match"))

    # Atomic -> DLQ flagged for human, NOT re-injected.
    assert advance_called["hit"] is False
    assert dlq_calls == [("l2_unresolvable_awaiting_human", "sigZ")]
    assert out.terminal_reason_code == "l2_unresolvable_awaiting_human"


def test_pivot_is_atomic_classifier() -> None:
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        SubGoal, SubGoalKind,
    )
    _f = GovernedOrchestrator._pivot_is_atomic
    # empty -> atomic
    assert _f((), "op") is True
    # single unscoped whole-op fallback -> atomic
    one = SubGoal(
        sub_goal_id="op::step-00", parent_goal_id="op", title="t",
        description="d", kind=SubGoalKind.ATOMIC, target_files=("m.py",),
        depends_on_sub_ids=(), estimated_complexity="moderate",
        boundary_crossed=False, scoped_symbols=(),
    )
    assert _f((one,), "op") is True
    # single SCOPED sub-goal -> genuine split (NOT atomic)
    scoped = SubGoal(
        sub_goal_id="op::step-00", parent_goal_id="op", title="t",
        description="d", kind=SubGoalKind.ATOMIC, target_files=("m.py",),
        depends_on_sub_ids=(), estimated_complexity="moderate",
        boundary_crossed=False, scoped_symbols=("m.py::foo",),
    )
    assert _f((scoped,), "op") is False
    # two sub-goals -> NOT atomic
    assert _f((one, scoped), "op") is False


# ======================================================================
# 6. OFF byte-identical: feedback disabled -> repair never pivots
# ======================================================================


def test_off_byte_identical_no_pivot_when_disabled(monkeypatch) -> None:
    """With JARVIS_EPISTEMIC_FEEDBACK_ENABLED=false the repair-engine pivot
    gate is short-circuited; the legacy _stopped path is taken. We assert the
    gating predicate is honored at the source level + at runtime."""
    monkeypatch.setenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", "false")
    from backend.core.ouroboros.governance.epistemic_feedback import (
        epistemic_feedback_enabled, pivot_verdict,
    )
    assert epistemic_feedback_enabled() is False
    # pivot_verdict itself is signal-pure; the GATE is epistemic_feedback_enabled.
    # Source-level: the divergence stop guards pivot behind _efe().
    src = REPAIR_ENGINE_FILE.read_text()
    # The pivot branch must be nested under an epistemic_feedback_enabled() guard.
    assert "if _efe():" in src
    assert "_efe()" in src and "epistemic_feedback_enabled as _efe" in src


# ======================================================================
# 7. DAG-preserving: pivot handler never references sibling ops
# ======================================================================


def test_pivot_handler_is_dag_preserving_single_op() -> None:
    """_handle_l2_pivot operates ONLY on the passed ctx — it never enumerates
    or mutates sibling ops. Pin: the method body references no sibling/op-list
    iteration primitives."""
    src = ORCHESTRATOR_FILE.read_text()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_l2_pivot":
            fn = node
            break
    assert fn is not None, "_handle_l2_pivot not found"
    body_src = ast.get_source_segment(src, fn) or ""
    # Must reference the single ctx + decompose seam, NOT a sibling sweep.
    assert "decompose_for_block" in body_src
    assert "advance_orchestration" in body_src
    # No broad op-table iteration inside the handler.
    for forbidden in ("for op in", "_active_file_ops", "self._ops.values"):
        assert forbidden not in body_src, (
            f"pivot handler must not sweep siblings ({forbidden!r})"
        )
