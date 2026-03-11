"""
Acceptance tests for Phase 2A: VALIDATE gates APPLY via TestRunner.

Covers AC1–AC3, AC5, AC5b, AC6 from the Phase 2A design doc.
(AC4 BlockedPathError/symlink rejection is deferred to a later task.)
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.test_runner import (
    AdapterResult,
    MultiAdapterResult,
    TestResult,
    _route,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── shared helpers ────────────────────────────────────────────────────────


def _test_result(passed: bool) -> TestResult:
    return TestResult(
        passed=passed,
        total=1 if passed else 0,
        failed=0 if passed else 1,
        failed_tests=() if passed else ("test_foo::test_bar",),
        duration_seconds=0.1,
        stdout="",
        flake_suspected=False,
    )


def _adapter(name: str, passed: bool, fc: str = "none") -> AdapterResult:
    return AdapterResult(
        adapter=name,
        passed=passed,
        failure_class=fc if not passed else "none",
        test_result=_test_result(passed),
        duration_s=0.1,
    )


def _multi(passed: bool, failure_class: str = "none", adapters=("python",)) -> MultiAdapterResult:
    """Build a MultiAdapterResult.

    failure_class is applied to failing adapters; ignored when passed=True.
    """
    adapter_fc = failure_class if not passed else "none"
    ar = tuple(_adapter(a, passed, adapter_fc) for a in adapters)
    dominant = next((r for r in ar if not r.passed), None)
    return MultiAdapterResult(
        adapter_results=ar,
        passed=passed,
        dominant_failure=dominant,
        total_duration_s=0.1,
    )


def _make_orch(runner: MagicMock) -> tuple[GovernedOrchestrator, MagicMock]:
    """Build a GovernedOrchestrator with a mocked governance stack."""
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock(return_value=True)

    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    # can_write returns (allowed, reason) — always allow to reach APPLY
    mock_stack.can_write.return_value = (True, "")

    mock_generator = MagicMock()
    mock_generator.generate = AsyncMock(
        return_value=MagicMock(
            candidates=({"file_path": "backend/core/foo.py", "full_content": "x = 1\n"},),
            provider_name="test",
            generation_duration_s=0.1,
            is_noop=False,
        )
    )

    config = OrchestratorConfig(project_root=REPO_ROOT, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack,
        generator=mock_generator,
        approval_provider=MagicMock(),
        config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger


def _ctx() -> OperationContext:
    """OperationContext with a generous pipeline_deadline."""
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="acceptance test op",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


def _route_files(changed_files: tuple[Path, ...]) -> frozenset[str]:
    """Return the set of required adapter names for the given changed files."""
    return _route(changed_files, REPO_ROOT)


# ── AC1: TestRunner is always called ─────────────────────────────────────


async def test_ac1_testrunner_called_on_non_trivial_op():
    """AC1: VALIDATE calls validation_runner.run() for a valid (non-syntax-error) candidate."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    orch, _ = _make_orch(runner)

    await orch.run(_ctx())

    runner.run.assert_called_once()


async def test_ac1_testrunner_not_called_on_syntax_error():
    """AC1 edge: SyntaxError candidate short-circuits before runner — runner NOT called."""
    runner = MagicMock()
    runner.run = AsyncMock()

    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock(return_value=True)

    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")

    mock_generator = MagicMock()
    mock_generator.generate = AsyncMock(
        return_value=MagicMock(
            candidates=({"file_path": "backend/core/foo.py", "full_content": "def broken(:\n    pass"},),
            provider_name="test",
            generation_duration_s=0.1,
            is_noop=False,
        )
    )

    config = OrchestratorConfig(project_root=REPO_ROOT, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack,
        generator=mock_generator,
        approval_provider=MagicMock(),
        config=config,
        validation_runner=runner,
    )
    await orch.run(_ctx())
    runner.run.assert_not_called()


# ── AC2: APPLY unreachable after VALIDATE failure ─────────────────────────


async def test_ac2_apply_unreachable_on_test_failure():
    """AC2: test failure during VALIDATE → terminal phase is CANCELLED, never APPLY."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="test"))
    orch, _ = _make_orch(runner)

    terminal = await orch.run(_ctx())

    assert terminal.phase != OperationPhase.APPLY
    assert terminal.phase == OperationPhase.CANCELLED


async def test_ac2_apply_unreachable_on_budget_exhaustion():
    """AC2: budget exhausted (remaining_s <= 0) → CANCELLED before runner is called, never APPLY."""
    runner = MagicMock()
    runner.run = AsyncMock()  # never reached — budget check fires first
    orch, _ = _make_orch(runner)

    # Use an already-expired deadline so the budget check fires immediately
    expired_ctx = OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="budget test",
        pipeline_deadline=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
    )
    terminal = await orch.run(expired_ctx)

    assert terminal.phase != OperationPhase.APPLY
    assert terminal.phase == OperationPhase.CANCELLED
    runner.run.assert_not_called()


# ── AC3: op_id continuity ────────────────────────────────────────────────


async def test_ac3_op_id_identical_in_all_ledger_entries():
    """AC3: All ledger entries share the same op_id as the initial context."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(runner)

    ctx = _ctx()
    await orch.run(ctx)

    assert ledger.append.call_count > 0
    for call in ledger.append.call_args_list:
        entry: LedgerEntry = call.args[0]
        assert entry.op_id == ctx.op_id, (
            f"Ledger entry op_id mismatch: {entry.op_id!r} != {ctx.op_id!r}"
        )


async def test_ac3_runner_receives_same_op_id():
    """AC3: validation_runner.run() is called with op_id matching the operation context."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    orch, _ = _make_orch(runner)

    ctx = _ctx()
    await orch.run(ctx)

    call_kwargs = runner.run.call_args
    assert call_kwargs is not None
    passed_op_id = call_kwargs.kwargs.get("op_id")
    assert passed_op_id == ctx.op_id


# ── AC5: infra → POSTMORTEM ───────────────────────────────────────────────


async def test_ac5_infra_failure_terminal_phase_is_postmortem():
    """AC5: infra failure during VALIDATE → terminal phase = POSTMORTEM."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="infra"))
    orch, _ = _make_orch(runner)

    terminal = await orch.run(_ctx())

    assert terminal.phase == OperationPhase.POSTMORTEM


async def test_ac5_infra_failure_ledger_has_infra_reason():
    """AC5: POSTMORTEM ledger entry contains an 'infra' reason."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="infra"))
    orch, ledger = _make_orch(runner)

    await orch.run(_ctx())

    reasons = []
    for call in ledger.append.call_args_list:
        entry: LedgerEntry = call.args[0]
        if entry.state == OperationState.FAILED:
            reasons.append(entry.data.get("reason", ""))
    assert any("infra" in r for r in reasons), (
        f"No infra reason in any FAILED ledger entry. Reasons found: {reasons}"
    )


# ── AC5b: POSTMORTEM ledger entry has failure_class ──────────────────────


async def test_ac5b_postmortem_ledger_has_failure_class():
    """AC5b: POSTMORTEM ledger entry contains failure_class='infra'."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="infra"))
    orch, ledger = _make_orch(runner)

    await orch.run(_ctx())

    for call in ledger.append.call_args_list:
        entry: LedgerEntry = call.args[0]
        if entry.state == OperationState.FAILED:
            assert entry.data.get("failure_class") == "infra", (
                f"Expected failure_class='infra' in FAILED entry data, got: {entry.data}"
            )
            return
    pytest.fail("No FAILED ledger entry found after infra failure")


# ── AC6: Deterministic adapter routing ───────────────────────────────────


def test_ac6_mlforge_routes_to_python_and_cpp():
    """AC6: mlforge/** → both python and cpp adapters required."""
    changed = (REPO_ROOT / "mlforge" / "kernels.cpp",)
    required = _route_files(changed)
    assert "python" in required
    assert "cpp" in required


def test_ac6_bindings_routes_to_python_and_cpp():
    """AC6: bindings/** → both python and cpp adapters required."""
    changed = (REPO_ROOT / "bindings" / "wrapper.pyx",)
    required = _route_files(changed)
    assert "python" in required
    assert "cpp" in required


def test_ac6_reactor_core_routes_to_python_only():
    """AC6: reactor_core/** → python only."""
    changed = (REPO_ROOT / "reactor_core" / "model.py",)
    required = _route_files(changed)
    assert "python" in required
    assert "cpp" not in required


def test_ac6_tests_routes_to_python_only():
    """AC6: tests/** → python only."""
    changed = (REPO_ROOT / "tests" / "test_foo.py",)
    required = _route_files(changed)
    assert "python" in required
    assert "cpp" not in required
