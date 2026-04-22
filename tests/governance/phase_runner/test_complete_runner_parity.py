"""Parity tests for :class:`COMPLETERunner` (Wave 2 (5) Slice 1).

The runner body is a verbatim transcription of the inline block at
``orchestrator.py`` line ~7073–7132. These tests pin the *observable
side-effect trace* so a graduation flip of
``JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED`` can't silently drift.

Parity contract (every test maps to one clause):

1. Serpent handle gets ``update_phase("COMPLETE")`` first, then ``stop(success=True)``.
2. ``ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")`` runs.
3. ``orch._stack.comm.emit_heartbeat`` is awaited with the complete-phase payload.
4. ``orch._record_canary_for_ctx(ctx, True, <latency>)`` is called with the t_apply delta.
5. ``orch._publish_outcome(ctx, OperationState.APPLIED)`` is awaited.
6. ``orch._persist_performance_record(ctx)`` is awaited.
7. ``orch._oracle_incremental_update(<resolved paths>)`` is awaited.
8. Optional ``_reasoning_narrator`` + ``_dialogue_store`` + ``_rsi_score_function``
   paths engage when set and are skipped when ``None``.
9. Every ``try/except: pass`` clause swallows exceptions — the runner
   never raises into the dispatcher.
10. Return value is
    ``PhaseResult(next_ctx=<COMPLETE ctx>, next_phase=None, status="ok", reason="complete")``.

Authority invariant: this test module imports nothing from
``candidate_generator`` / ``iron_gate`` / ``change_engine`` / ``gate``
/ ``policy`` / ``risk_tier``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.phase_runners.complete_runner import (
    COMPLETERunner,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSerpent:
    """Records update_phase/stop calls; matches real serpent's shape."""

    def __init__(self) -> None:
        self.updates: List[str] = []
        self.stopped: Optional[bool] = None
        self.update_should_raise = False
        self.stop_should_raise = False

    def update_phase(self, phase: str) -> None:
        self.updates.append(phase)
        if self.update_should_raise:
            raise RuntimeError("serpent update boom")

    async def stop(self, success: bool) -> None:
        self.stopped = success
        if self.stop_should_raise:
            raise RuntimeError("serpent stop boom")


class _FakeComm:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.should_raise = False

    async def emit_heartbeat(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.should_raise:
            raise RuntimeError("heartbeat boom")


class _FakeStack:
    def __init__(self, comm: _FakeComm) -> None:
        self.comm = comm


class _FakeNarrator:
    def __init__(self) -> None:
        self.outcomes: List[Tuple[str, bool, str]] = []
        self.narrated: List[str] = []
        self.should_raise = False

    def record_outcome(self, op_id: str, success: bool, note: str) -> None:
        self.outcomes.append((op_id, success, note))
        if self.should_raise:
            raise RuntimeError("narrator outcome boom")

    async def narrate_completion(self, op_id: str) -> None:
        self.narrated.append(op_id)
        if self.should_raise:
            raise RuntimeError("narrator narrate boom")


class _FakeDialogue:
    def __init__(self, op_id: str) -> None:
        self.op_id = op_id
        self.entries: List[Tuple[str, str]] = []

    def add_entry(self, phase: str, note: str) -> None:
        self.entries.append((phase, note))


class _FakeDialogueStore:
    def __init__(self, *, has_active: bool = True) -> None:
        self._active = has_active
        self.completed: List[Tuple[str, str]] = []
        self.dialogues: Dict[str, _FakeDialogue] = {}
        self.should_raise = False

    def get_active(self, op_id: str):
        if not self._active:
            return None
        d = self.dialogues.setdefault(op_id, _FakeDialogue(op_id))
        return d

    def complete_dialogue(self, op_id: str, outcome: str) -> None:
        self.completed.append((op_id, outcome))
        if self.should_raise:
            raise RuntimeError("dialogue boom")


@dataclass
class _FakeRsiScore:
    composite: float = 0.42


class _FakeRsiScoreFunction:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.should_raise = False

    def compute(self, **kwargs) -> _FakeRsiScore:
        self.calls.append(kwargs)
        if self.should_raise:
            raise RuntimeError("rsi boom")
        return _FakeRsiScore()


@dataclass
class _FakeOrchestrator:
    """Minimal surface the COMPLETERunner touches."""

    _stack: _FakeStack
    record_canary_calls: List[Tuple[Any, bool, float]] = field(default_factory=list)
    publish_outcome_calls: List[Tuple[Any, OperationState]] = field(default_factory=list)
    persist_performance_calls: List[Any] = field(default_factory=list)
    oracle_update_calls: List[List[Path]] = field(default_factory=list)
    _reasoning_narrator: Optional[_FakeNarrator] = None
    _dialogue_store: Optional[_FakeDialogueStore] = None
    _rsi_score_function: Optional[_FakeRsiScoreFunction] = None

    def _record_canary_for_ctx(self, ctx, success: bool, latency_s: float) -> None:
        self.record_canary_calls.append((ctx, success, latency_s))

    async def _publish_outcome(self, ctx, state: OperationState) -> None:
        self.publish_outcome_calls.append((ctx, state))

    async def _persist_performance_record(self, ctx) -> None:
        self.persist_performance_calls.append(ctx)

    async def _oracle_incremental_update(self, applied_files) -> None:
        self.oracle_update_calls.append(list(applied_files))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _verify_ctx(tmp_path: Path) -> OperationContext:
    """Advance a fresh ctx to VERIFY so ``advance(COMPLETE)`` is legal."""
    t1 = tmp_path / "a.py"
    t2 = tmp_path / "b.py"
    t1.write_text("x = 1\n", encoding="utf-8")
    t2.write_text("y = 2\n", encoding="utf-8")
    ctx = OperationContext.create(
        target_files=(str(t1), str(t2)),
        description="complete-runner parity",
    )
    # CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> APPLY -> VERIFY
    for nxt in (
        OperationPhase.ROUTE,
        OperationPhase.GENERATE,
        OperationPhase.VALIDATE,
        OperationPhase.GATE,
        OperationPhase.APPLY,
        OperationPhase.VERIFY,
    ):
        ctx = ctx.advance(nxt)
    return ctx


@pytest.fixture
def ctx(tmp_path: Path) -> OperationContext:
    return _verify_ctx(tmp_path)


@pytest.fixture
def orch() -> _FakeOrchestrator:
    return _FakeOrchestrator(_stack=_FakeStack(_FakeComm()))


# ---------------------------------------------------------------------------
# (1) Class attributes
# ---------------------------------------------------------------------------


def test_complete_runner_is_a_phase_runner():
    assert issubclass(COMPLETERunner, PhaseRunner)
    assert COMPLETERunner.phase is OperationPhase.COMPLETE


# ---------------------------------------------------------------------------
# (2) Happy path — full observable trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_trace(ctx, orch):
    serpent = _FakeSerpent()
    orch._reasoning_narrator = _FakeNarrator()
    orch._dialogue_store = _FakeDialogueStore()
    orch._rsi_score_function = _FakeRsiScoreFunction()

    runner = COMPLETERunner(orch, serpent, t_apply=0.0)
    result = await runner.run(ctx)

    # (10) Return shape
    assert isinstance(result, PhaseResult)
    assert result.status == "ok"
    assert result.next_phase is None
    assert result.reason == "complete"

    # (2) ctx advanced to COMPLETE with reason code
    assert result.next_ctx.phase is OperationPhase.COMPLETE
    assert result.next_ctx.terminal_reason_code == "complete"

    # (1) Serpent lifecycle order
    assert serpent.updates == ["COMPLETE"]
    assert serpent.stopped is True

    # (3) Heartbeat payload shape
    assert len(orch._stack.comm.calls) == 1
    hb = orch._stack.comm.calls[0]
    assert hb == {
        "op_id": result.next_ctx.op_id,
        "phase": "complete",
        "progress_pct": 100.0,
    }

    # (4) Canary call — success=True, latency>=0
    assert len(orch.record_canary_calls) == 1
    canary_ctx, canary_ok, canary_lat = orch.record_canary_calls[0]
    assert canary_ok is True
    assert canary_lat >= 0.0
    assert canary_ctx is result.next_ctx

    # (5) Publish outcome — APPLIED state
    assert orch.publish_outcome_calls == [(result.next_ctx, OperationState.APPLIED)]

    # (6) Persist performance record
    assert orch.persist_performance_calls == [result.next_ctx]

    # (7) Oracle update — resolved file paths
    assert len(orch.oracle_update_calls) == 1
    resolved = orch.oracle_update_calls[0]
    assert all(isinstance(p, Path) and p.is_absolute() for p in resolved)
    assert {p.name for p in resolved} == {"a.py", "b.py"}

    # (8a) Narrator engaged
    assert orch._reasoning_narrator.outcomes == [
        (result.next_ctx.op_id, True, "Applied successfully"),
    ]
    assert orch._reasoning_narrator.narrated == [result.next_ctx.op_id]

    # (8b) Dialogue store
    assert orch._dialogue_store.completed == [(result.next_ctx.op_id, "success")]
    entry = orch._dialogue_store.dialogues[result.next_ctx.op_id]
    assert entry.entries == [("COMPLETE", "Applied successfully")]

    # (8c) RSI score computed once
    assert len(orch._rsi_score_function.calls) == 1
    rsi_call = orch._rsi_score_function.calls[0]
    assert rsi_call["op_id"] == result.next_ctx.op_id


# ---------------------------------------------------------------------------
# (8) Optional orchestrator attributes default to None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_optional_paths_when_none(ctx, orch):
    """Narrator / Dialogue / RSI absent — runner must not reach for them."""
    assert orch._reasoning_narrator is None
    assert orch._dialogue_store is None
    assert orch._rsi_score_function is None

    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)

    assert result.status == "ok"
    assert result.next_ctx.phase is OperationPhase.COMPLETE


@pytest.mark.asyncio
async def test_none_serpent_does_not_crash(ctx, orch):
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    assert result.next_ctx.phase is OperationPhase.COMPLETE


# ---------------------------------------------------------------------------
# (9) Exception swallow invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_raise_is_swallowed(ctx, orch):
    orch._stack.comm.should_raise = True
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    # Heartbeat blew up, but every subsequent step still ran:
    assert orch.publish_outcome_calls, "publish_outcome must run after heartbeat raise"
    assert orch.persist_performance_calls
    assert orch.oracle_update_calls
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_narrator_raise_is_swallowed(ctx, orch):
    orch._reasoning_narrator = _FakeNarrator()
    orch._reasoning_narrator.should_raise = True
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_dialogue_raise_is_swallowed(ctx, orch):
    orch._dialogue_store = _FakeDialogueStore()
    orch._dialogue_store.should_raise = True
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_rsi_raise_is_swallowed(ctx, orch):
    orch._rsi_score_function = _FakeRsiScoreFunction()
    orch._rsi_score_function.should_raise = True
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_serpent_stop_raise_is_swallowed(ctx, orch):
    serpent = _FakeSerpent()
    serpent.stop_should_raise = True
    runner = COMPLETERunner(orch, serpent, t_apply=0.0)
    result = await runner.run(ctx)
    assert result.status == "ok"
    assert serpent.updates == ["COMPLETE"]


# ---------------------------------------------------------------------------
# (4) t_apply latency plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t_apply_latency_is_non_negative_delta(ctx, orch):
    """t_apply was recorded at APPLY start; latency = now - t_apply >= 0."""
    import time

    t_apply = time.monotonic() - 2.5
    runner = COMPLETERunner(orch, serpent=None, t_apply=t_apply)
    await runner.run(ctx)

    assert len(orch.record_canary_calls) == 1
    _c, _ok, latency = orch.record_canary_calls[0]
    # allow generous slack — threading/scheduling jitter
    assert 2.0 <= latency <= 30.0


# ---------------------------------------------------------------------------
# (2) Hash chain invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_chain_advances_on_complete(ctx, orch):
    before_hash = ctx.context_hash
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    assert result.next_ctx.previous_hash == before_hash
    assert result.next_ctx.context_hash != before_hash


# ---------------------------------------------------------------------------
# (8b) Dialogue get_active returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dialogue_no_active_still_completes(ctx, orch):
    orch._dialogue_store = _FakeDialogueStore(has_active=False)
    runner = COMPLETERunner(orch, serpent=None, t_apply=0.0)
    result = await runner.run(ctx)
    # add_entry never called (no active dialogue) but complete_dialogue must still fire.
    assert orch._dialogue_store.completed == [(result.next_ctx.op_id, "success")]
    assert orch._dialogue_store.dialogues == {}


__all__ = []
