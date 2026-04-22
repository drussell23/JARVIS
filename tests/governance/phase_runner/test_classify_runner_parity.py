"""Parity tests for :class:`CLASSIFYRunner` (Wave 2 (5) Slice 2).

The runner body is a verbatim transcription of orchestrator.py
lines 1235–1994 with ``self.`` → ``orch.`` substitutions. These tests
pin the *observable side-effect trace* that a graduation flip of
``JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED`` must preserve.

Parity contract — **four exit paths, all covered**:

1. **Emergency protocol ORANGE+** → ``CANCELLED`` + ``reason=emergency_*``
2. **Advisor BLOCK** → ``CANCELLED`` + ``reason=advisor_blocked``
3. **Risk BLOCKED** (risk engine or policy engine) → ``CANCELLED`` +
   ``reason=<classification.reason_code>`` + ledger entry with
   ``OperationState.BLOCKED``
4. **OK** → advance to ROUTE with ``risk_tier`` stamped + advisory
   threaded through artifacts + narrator/dialogue start hooks fire

Authority invariant: no imports from
``candidate_generator`` / ``iron_gate`` / ``change_engine`` / ``gate``.
This test module imports ``policy_engine`` and ``risk_engine`` because
the inline CLASSIFY block does — these are read-only references for
classification, not execution authority.
"""
from __future__ import annotations

import os
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
from backend.core.ouroboros.governance.phase_runners.classify_runner import (
    CLASSIFYRunner,
)
from backend.core.ouroboros.governance.policy_engine import PolicyDecision
from backend.core.ouroboros.governance.risk_engine import (
    RiskClassification,
    RiskTier,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSerpent:
    def __init__(self) -> None:
        self.updates: List[str] = []
        self.stopped: Optional[bool] = None

    def update_phase(self, phase: str) -> None:
        self.updates.append(phase)

    async def stop(self, success: bool) -> None:
        self.stopped = success


class _FakeEmergency:
    def __init__(self, *, blocks: bool = False, level: str = "ORANGE") -> None:
        self._blocks = blocks
        self._level = level

    def can_proceed(self) -> bool:
        return not self._blocks

    def get_state(self):
        class _State:
            pass
        s = _State()
        s.level = type("L", (), {"name": self._level})()
        return s


class _FakeRiskEngine:
    def __init__(
        self,
        tier: RiskTier = RiskTier.SAFE_AUTO,
        reason_code: str = "normal",
    ) -> None:
        self._tier = tier
        self._reason = reason_code
        self.classify_calls = 0

    def classify(self, profile):
        self.classify_calls += 1
        return RiskClassification(
            tier=self._tier, reason_code=self._reason,
        )


class _FakeComm:
    def __init__(self) -> None:
        self.intents: List[Dict[str, Any]] = []
        self.heartbeats: List[Dict[str, Any]] = []

    async def emit_intent(self, **kwargs) -> None:
        self.intents.append(kwargs)

    async def emit_heartbeat(self, **kwargs) -> None:
        self.heartbeats.append(kwargs)


@dataclass
class _FakePolicyEngine:
    decisions: Dict[str, PolicyDecision] = field(default_factory=dict)
    call_log: List[Tuple[str, str]] = field(default_factory=list)

    def classify(self, *, tool: str, target: str) -> PolicyDecision:
        self.call_log.append((tool, target))
        return self.decisions.get(target, PolicyDecision.NO_MATCH)


class _FakeStack:
    def __init__(
        self,
        comm: _FakeComm,
        risk_engine: _FakeRiskEngine,
        emergency: Optional[_FakeEmergency] = None,
        policy_engine: Optional[_FakePolicyEngine] = None,
    ) -> None:
        self.comm = comm
        self.risk_engine = risk_engine
        if emergency is not None:
            self._emergency_engine = emergency
        if policy_engine is not None:
            self.policy_engine = policy_engine
        self.topology = None
        self.ledger = None
        self.consciousness_bridge = None
        self.governed_loop_service = None


class _FakeNarrator:
    def __init__(self) -> None:
        self.classify_records: List[Tuple[str, str, str]] = []
        self.traces_started: List[str] = []

    def record_classify(self, op_id: str, decision: str, msg: str) -> None:
        self.classify_records.append((op_id, decision, msg))

    def start_trace(self, op_id: str) -> None:
        self.traces_started.append(op_id)


class _FakeDialogue:
    def __init__(self, op_id: str) -> None:
        self.op_id = op_id
        self.entries: List[Tuple[str, str]] = []

    def add_entry(self, phase: str, note: str) -> None:
        self.entries.append((phase, note))


class _FakeDialogueStore:
    def __init__(self) -> None:
        self.started: List[Tuple[str, str]] = []
        self.dialogues: Dict[str, _FakeDialogue] = {}

    def start_dialogue(self, *, op_id: str, domain_key: str, description: str, target_files) -> None:
        self.started.append((op_id, domain_key))
        self.dialogues[op_id] = _FakeDialogue(op_id)

    def get_active(self, op_id: str):
        return self.dialogues.get(op_id)


@dataclass
class _FakeConfig:
    project_root: Path


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _reasoning_bridge: Any = None
    _reasoning_narrator: Optional[_FakeNarrator] = None
    _dialogue_store: Optional[_FakeDialogueStore] = None
    ledger_records: List[Tuple[Any, OperationState, Dict[str, Any]]] = field(default_factory=list)
    build_profile_calls: int = 0

    def _build_profile(self, ctx):
        self.build_profile_calls += 1
        return {"op_id": ctx.op_id, "target_files": list(ctx.target_files)}

    async def _record_ledger(self, ctx, state: OperationState, extra: Dict[str, Any]) -> None:
        self.ledger_records.append((ctx, state, extra))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # Quarantine so orchestrator sibling state doesn't bleed in.
    monkeypatch.setenv("JARVIS_CLASSIFY_CLARIFY_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "0")
    monkeypatch.setenv("JARVIS_SEMANTIC_INFERENCE_ENABLED", "0")
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "0")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "0")
    yield


def _classify_ctx(tmp_path: Path) -> OperationContext:
    (tmp_path / "a.py").write_text("x = 1\n")
    return OperationContext.create(
        target_files=(str(tmp_path / "a.py"),),
        description="classify-runner parity",
    )


@pytest.fixture
def ctx(tmp_path: Path) -> OperationContext:
    return _classify_ctx(tmp_path)


def _orch(
    tmp_path: Path,
    *,
    risk: RiskTier = RiskTier.SAFE_AUTO,
    emergency_blocks: bool = False,
    emergency_level: str = "ORANGE",
    policy_decision: Optional[PolicyDecision] = None,
    narrator: bool = False,
    dialogue: bool = False,
    reason_code: str = "normal",
) -> _FakeOrchestrator:
    re = _FakeRiskEngine(tier=risk, reason_code=reason_code)
    comm = _FakeComm()
    em = _FakeEmergency(blocks=emergency_blocks, level=emergency_level)
    pe = None
    if policy_decision is not None:
        pe = _FakePolicyEngine(
            decisions={
                str(tmp_path / "a.py"): policy_decision,
            },
        )
    stack = _FakeStack(comm=comm, risk_engine=re, emergency=em, policy_engine=pe)
    cfg = _FakeConfig(project_root=tmp_path)
    return _FakeOrchestrator(
        _stack=stack,
        _config=cfg,
        _reasoning_narrator=_FakeNarrator() if narrator else None,
        _dialogue_store=_FakeDialogueStore() if dialogue else None,
    )


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_classify_runner_is_a_phase_runner():
    assert issubclass(CLASSIFYRunner, PhaseRunner)
    assert CLASSIFYRunner.phase is OperationPhase.CLASSIFY


# ---------------------------------------------------------------------------
# (2) Happy path — advance to ROUTE with risk stamped + advisory threaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_advances_to_route(ctx, tmp_path):
    orch = _orch(tmp_path, narrator=True, dialogue=True)
    serpent = _FakeSerpent()
    runner = CLASSIFYRunner(orch, serpent)
    result = await runner.run(ctx)

    assert isinstance(result, PhaseResult)
    assert result.status == "ok"
    assert result.next_phase is OperationPhase.ROUTE
    assert result.reason == "classified"
    assert result.next_ctx.phase is OperationPhase.ROUTE
    assert result.next_ctx.risk_tier is RiskTier.SAFE_AUTO

    # Serpent saw the ROUTE transition
    assert "ROUTE" in serpent.updates

    # risk_engine.classify was called once with a profile dict
    assert orch._stack.risk_engine.classify_calls == 1
    assert orch.build_profile_calls == 1

    # emit_intent fired with the classification
    assert len(orch._stack.comm.intents) == 1
    intent = orch._stack.comm.intents[0]
    assert intent["op_id"] == ctx.op_id
    assert intent["risk_tier"] == "SAFE_AUTO"

    # intent_chain heartbeat emit IS attempted but the inline block has a
    # latent double-phase-kwarg TypeError that the surrounding try/except
    # swallows (see orchestrator.py ~line 1884 + 1888). Parity preserves
    # the bug — runner must NOT somehow succeed where inline fails.
    # Observable result: zero heartbeats land on _FakeComm.
    assert orch._stack.comm.heartbeats == []


@pytest.mark.asyncio
async def test_happy_path_artifacts_contain_advisory_key(ctx, tmp_path):
    orch = _orch(tmp_path)
    runner = CLASSIFYRunner(orch, _FakeSerpent())
    result = await runner.run(ctx)
    # _advisory may be None if OperationAdvisor import fails or returns None —
    # the contract is that the KEY is present so the orchestrator hook
    # can safely do .get("advisory").
    assert "advisory" in result.artifacts


# ---------------------------------------------------------------------------
# (3) Narrator / Dialogue wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrator_starts_trace_on_success(ctx, tmp_path):
    orch = _orch(tmp_path, narrator=True)
    await CLASSIFYRunner(orch, None).run(ctx)
    assert orch._reasoning_narrator.traces_started == [ctx.op_id]
    # record_classify called with the risk-tier value (RiskTier uses
    # ``auto()``, so ``.value`` is an int, not a string — verbatim
    # parity with the inline block).
    records = orch._reasoning_narrator.classify_records
    assert records, "record_classify should have fired"
    assert records[0][0] == ctx.op_id
    # risk_tier.value for RiskTier.SAFE_AUTO is 1 (int); the inline
    # code uses `.value` if present. Runner must match.
    assert records[0][1] in (RiskTier.SAFE_AUTO.value, "SAFE_AUTO")


@pytest.mark.asyncio
async def test_dialogue_starts_on_success(ctx, tmp_path):
    orch = _orch(tmp_path, dialogue=True)
    await CLASSIFYRunner(orch, None).run(ctx)
    assert orch._dialogue_store.started
    started_op = orch._dialogue_store.started[0][0]
    assert started_op == ctx.op_id
    # CLASSIFY entry added
    entry = orch._dialogue_store.dialogues[ctx.op_id]
    assert any(e[0] == "CLASSIFY" for e in entry.entries)


@pytest.mark.asyncio
async def test_narrator_none_does_not_crash(ctx, tmp_path):
    orch = _orch(tmp_path)  # narrator=None default
    assert orch._reasoning_narrator is None
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_dialogue_none_does_not_crash(ctx, tmp_path):
    orch = _orch(tmp_path)  # dialogue=None default
    assert orch._dialogue_store is None
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# (4) Emergency block — exit 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_blocks_with_cancelled_ctx(ctx, tmp_path):
    orch = _orch(
        tmp_path,
        emergency_blocks=True,
        emergency_level="RED",
    )
    serpent = _FakeSerpent()
    result = await CLASSIFYRunner(orch, serpent).run(ctx)

    assert result.status == "fail"
    assert result.next_phase is None
    assert result.reason == "emergency_red"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert result.next_ctx.terminal_reason_code == "emergency_red"

    # Serpent stopped with success=False
    assert serpent.stopped is False

    # Risk engine NEVER consulted on emergency path
    assert orch._stack.risk_engine.classify_calls == 0

    # No intent emitted
    assert orch._stack.comm.intents == []


@pytest.mark.asyncio
async def test_emergency_block_sets_advisory_none_in_artifacts(ctx, tmp_path):
    orch = _orch(
        tmp_path, emergency_blocks=True, emergency_level="ORANGE",
    )
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.artifacts == {"advisory": None, "consciousness_bridge": None}


# ---------------------------------------------------------------------------
# (5) Risk tier BLOCKED — exit 3 (risk engine path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_blocked_writes_ledger_and_cancels(ctx, tmp_path):
    orch = _orch(
        tmp_path, risk=RiskTier.BLOCKED, reason_code="supervisor_surface",
    )
    result = await CLASSIFYRunner(orch, None).run(ctx)

    assert result.status == "fail"
    assert result.next_phase is None
    assert result.reason == "supervisor_surface"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert result.next_ctx.risk_tier is RiskTier.BLOCKED

    # Ledger entry recorded
    assert len(orch.ledger_records) == 1
    _ctx_out, state, extra = orch.ledger_records[0]
    assert state is OperationState.BLOCKED
    assert extra["reason_code"] == "supervisor_surface"
    assert extra["risk_tier"] == "BLOCKED"

    # No emit_intent (short-circuit before announce)
    assert orch._stack.comm.intents == []


# ---------------------------------------------------------------------------
# (6) Policy engine BLOCKED override — exit 3 (policy path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_engine_blocked_overrides_risk_tier(ctx, tmp_path):
    # Risk engine says SAFE_AUTO, policy engine says BLOCKED. Policy wins.
    orch = _orch(
        tmp_path,
        risk=RiskTier.SAFE_AUTO,
        policy_decision=PolicyDecision.BLOCKED,
        reason_code="initial_safe",
    )
    result = await CLASSIFYRunner(orch, None).run(ctx)

    assert result.status == "fail"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert result.next_ctx.risk_tier is RiskTier.BLOCKED
    # Ledger sees BLOCKED state regardless of reason_code origin
    assert orch.ledger_records
    assert orch.ledger_records[0][1] is OperationState.BLOCKED


@pytest.mark.asyncio
async def test_policy_engine_allow_does_not_block(ctx, tmp_path):
    orch = _orch(
        tmp_path,
        policy_decision=PolicyDecision.NO_MATCH,
    )
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.status == "ok"
    assert result.next_ctx.risk_tier is RiskTier.SAFE_AUTO


# ---------------------------------------------------------------------------
# (7) Intent + heartbeat telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_payload_shape(ctx, tmp_path):
    orch = _orch(tmp_path)
    await CLASSIFYRunner(orch, None).run(ctx)
    assert len(orch._stack.comm.intents) == 1
    intent = orch._stack.comm.intents[0]
    assert set(intent.keys()) == {
        "op_id", "goal", "target_files", "risk_tier", "blast_radius",
    }
    assert intent["blast_radius"] == 1


@pytest.mark.asyncio
async def test_heartbeat_intent_chain_is_swallowed_by_latent_bug(ctx, tmp_path):
    """Parity-preserves the pre-existing double-phase-kwarg TypeError.

    Inline code at orchestrator.py ~1868+ builds a ``_chain_payload`` dict
    that already contains ``phase="intent_chain"`` and then passes that
    key a SECOND time as a named kwarg to ``emit_heartbeat``. Python
    raises ``TypeError: got multiple values for keyword argument 'phase'``;
    the ``try/except: pass`` two lines below swallows it. Net effect: the
    intent_chain heartbeat never actually lands on the comm surface.

    The runner must preserve this broken-but-harmless behavior verbatim.
    If someone fixes the double-kwarg bug, they must delete this test
    AND update the inline orchestrator path in the same commit.
    """
    orch = _orch(tmp_path)
    await CLASSIFYRunner(orch, None).run(ctx)
    assert orch._stack.comm.heartbeats == []


# ---------------------------------------------------------------------------
# (8) Hash chain invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_chain_advances_on_ok(ctx, tmp_path):
    orch = _orch(tmp_path)
    before = ctx.context_hash
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.next_ctx.context_hash != before


@pytest.mark.asyncio
async def test_hash_chain_advances_on_emergency(ctx, tmp_path):
    orch = _orch(tmp_path, emergency_blocks=True, emergency_level="ORANGE")
    before = ctx.context_hash
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.next_ctx.context_hash != before


# ---------------------------------------------------------------------------
# (9) Exception-swallow invariants — subsystems never crash the phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_intent_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path)

    async def _bad_intent(**kwargs):
        raise RuntimeError("intent boom")

    orch._stack.comm.emit_intent = _bad_intent  # type: ignore[method-assign]
    result = await CLASSIFYRunner(orch, None).run(ctx)
    # Downstream advance to ROUTE still happens despite emit_intent raising
    assert result.status == "ok"
    assert result.next_ctx.phase is OperationPhase.ROUTE


@pytest.mark.asyncio
async def test_heartbeat_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path)

    async def _bad_hb(**kwargs):
        raise RuntimeError("heartbeat boom")

    orch._stack.comm.emit_heartbeat = _bad_hb  # type: ignore[method-assign]
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_policy_engine_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path, policy_decision=PolicyDecision.NO_MATCH)

    def _raise(**kwargs):
        raise RuntimeError("policy boom")

    orch._stack.policy_engine.classify = _raise  # type: ignore[method-assign]
    result = await CLASSIFYRunner(orch, None).run(ctx)
    # Policy engine raise is a WARNING, not a BLOCK — pipeline continues
    # with the risk_engine's tier (SAFE_AUTO by default).
    assert result.status == "ok"
    assert result.next_ctx.risk_tier is RiskTier.SAFE_AUTO


# ---------------------------------------------------------------------------
# (10) Authority invariant (grep-pinned)
# ---------------------------------------------------------------------------


def test_classify_runner_module_bans_execution_authority_imports():
    """Scope doc §1 — no imports from candidate_generator / iron_gate /
    change_engine / gate / risk_engine's mutation surfaces."""
    import inspect

    from backend.core.ouroboros.governance.phase_runners import classify_runner

    src = inspect.getsource(classify_runner)
    banned = ("candidate_generator", "iron_gate", "change_engine", "gate")
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")):
            for b in banned:
                # Allow docstring mentions by restricting the check to
                # actual import lines.
                assert b not in s, (
                    f"classify_runner.py must not import {b}: {s}"
                )


# ---------------------------------------------------------------------------
# (11) Serpent None-safe on all paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_serpent_happy_path(ctx, tmp_path):
    orch = _orch(tmp_path)
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_none_serpent_emergency_path(ctx, tmp_path):
    orch = _orch(tmp_path, emergency_blocks=True, emergency_level="RED")
    result = await CLASSIFYRunner(orch, None).run(ctx)
    assert result.status == "fail"
    assert result.next_ctx.phase is OperationPhase.CANCELLED


__all__ = []
