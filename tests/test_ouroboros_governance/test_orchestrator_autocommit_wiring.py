"""Regression spine — GovernedOrchestrator._run_pipeline invokes
AutoCommitter on the real (non-noop) applied+verified live path.

Background (bt-iso-1783042643 investigation)
----------------------------------------------
A live A1 soak session showed 23 ops reaching
``LEDGER_TERMINAL state=applied written=True`` with zero AutoCommitter
log lines all run. The initial hypothesis was that APPLY/VERIFY had
been extracted to a ``PhaseRunner`` (the "live path = extracted
phase-runners, not orchestrator inline" lesson from the Anti-Venom
arc) and the orchestrator's inline committer call site
(``orchestrator.py`` ~line 10746) was dead code on the live path.

That hypothesis FALSIFIES: ``phase_dispatcher.py``'s own
``DISPATCHER_INTERNAL_ONLY_PHASES`` invariant documents APPLY/VERIFY/
VISUAL_VERIFY as *deliberately* NOT PhaseRunner-served -- the
orchestrator's ``_run_pipeline`` inline machinery (ChangeEngine apply,
scoped post-apply verify, the Phase 8b auto-commit block) remains the
sole live path. Cross-checked against ``summary.json`` for every
"applied" op in the evidence session: all 23 carry
``files_changed=0`` / ``terminal_reason_code=read_only_complete`` --
i.e. every terminal-applied op that run was a read-only no-op
self-audit canary (the model reported no change needed), not a real
APPLY. Fleet-wide, zero of 52 ``bt-iso-*`` sessions to date contain a
single ``ChangeEngine`` invocation. AutoCommitter correctly never
fired: there was nothing to commit.

The REAL gap this file closes: the only test that could have proven
the live wiring (``TestHappyPath.test_happy_path_safe_auto`` in
``test_orchestrator.py``) was itself failing at baseline (29/45 tests
in that file), because the shared ``_mock_stack()`` fixture never
configured ``stack.governed_loop_service._cancel_token_registry`` --
W3(7) Slice 2's cancel-check treated the unconfigured
``MagicMock().is_cancelled`` as truthy and short-circuited every
dispatched op straight to POSTMORTEM before GATE/APPLY ever ran. That
fixture gap is fixed alongside this file (see
``test_orchestrator.py::_mock_stack``). This file adds the missing
behavioral proof that a genuinely applied+verified op reaches the
Phase 8b commit block.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.auto_committer import CommitResult
from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator

from tests.test_ouroboros_governance.test_orchestrator import (
    _default_config,
    _make_context,
    _mock_generator,
    _mock_stack,
)


_AUTO_COMMITTER_PATH = (
    "backend.core.ouroboros.governance.auto_committer.AutoCommitter"
)


@pytest.fixture(autouse=True)
def _disable_iron_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same scoped rationale as ``test_orchestrator.py``'s fixture of the
    same name: these mock candidates carry no Venom tool-execution
    records, so the post-GENERATE Iron Gates (exploration-first,
    ASCII-strictness) would fail every op before APPLY. Not a copy of
    production logic -- pure pipeline-state-machine test setup."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    monkeypatch.setenv("JARVIS_ASCII_GATE", "false")


@pytest.mark.asyncio
class TestAutoCommitterLivePathWiring:
    """Proves ``_run_pipeline`` invokes AutoCommitter.commit() for a
    real applied+verified op -- the live path, not the (falsified)
    extracted-phase-runner path."""

    async def test_committer_invoked_after_apply_and_verify_succeed(
        self,
    ) -> None:
        stack = _mock_stack()
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context(op_id="op-livepath-001")

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )

        with patch(_AUTO_COMMITTER_PATH) as mock_committer_cls:
            mock_instance = mock_committer_cls.return_value
            mock_instance.commit = AsyncMock(
                return_value=CommitResult(
                    committed=True,
                    commit_hash="deadbeef",
                    commit_message="fix: test commit",
                ),
            )

            result = await orch.run(ctx)

        # The op actually reached the real terminal success state --
        # not a noop/read-only short-circuit.
        assert result.phase is OperationPhase.COMPLETE
        stack.change_engine.execute.assert_called_once()

        # AutoCommitter was constructed against the configured repo
        # root (the live orchestrator.py:~10746 call site) and its
        # commit() coroutine was awaited exactly once with this op's
        # identity threaded through -- proving the live path, not a
        # dead extracted-runner copy, drives the commit.
        mock_committer_cls.assert_called_once_with(
            repo_root=config.project_root,
        )
        mock_instance.commit.assert_awaited_once()
        _, call_kwargs = mock_instance.commit.call_args
        assert call_kwargs["op_id"] == ctx.op_id
        assert call_kwargs["target_files"] == ctx.target_files

    async def test_committer_failure_never_disturbs_terminal_transition(
        self,
    ) -> None:
        """Fail-soft contract: a committer exception must not alter
        the FSM's terminal outcome. The change is already applied on
        disk -- an auto-commit failure is logged, not propagated."""
        stack = _mock_stack()
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context(op_id="op-livepath-002")

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )

        with patch(_AUTO_COMMITTER_PATH) as mock_committer_cls:
            mock_instance = mock_committer_cls.return_value
            mock_instance.commit = AsyncMock(
                side_effect=RuntimeError("git commit exploded"),
            )

            result = await orch.run(ctx)

        # Terminal state is unaffected by the committer blowing up --
        # the orchestrator's try/except around Phase 8b swallows it.
        assert result.phase is OperationPhase.COMPLETE
        mock_instance.commit.assert_awaited_once()
