"""Unified Observability — BG ops register into central ``_active_ops``.

Closes the architectural blind-spot diagnosed at the end of Move 2 v4:
``BackgroundAgentPool`` workers ran ops directly via ``orchestrator.run``,
bypassing the ``_active_ops`` set the harness ``ActivityMonitor`` watches.
The result: long BG GENERATEs were invisible to staleness checks, the
idle watchdog never got poked, and 3600s after session start the
``IdleWatchdog`` fired — even though tokens were actively streaming.

Pins:
  * ``BackgroundAgentPool.__init__`` accepts the two hooks
    (``on_op_active_register`` / ``on_op_active_unregister``).
  * Both hooks default to ``None`` — older callers (tests) keep working.
  * Worker pickup calls ``on_op_active_register(ctx.op_id)`` with the
    OperationContext's op_id (NOT the pool-internal slot id).
  * Worker finally-block calls ``on_op_active_unregister(ctx.op_id)``
    on success / failure / cancel / rupture — no dangling state.
  * Hook exceptions are swallowed and logged — provider failure can't
    leak via observability hook.
  * ``GovernedLoopService`` constructs the pool with hooks bound to
    its own ``_active_ops`` set and creates a minimal
    ``LoopRuntimeContext`` so the ActivityMonitor staleness check has
    something to read and Phase-Aware Heartbeats target the right ctx.

Authority Invariant
-------------------
Tests import only from the modules under test plus stdlib. No
orchestrator / phase_runners / iron_gate imports.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — BackgroundAgentPool accepts the hooks
# -----------------------------------------------------------------------


def test_pool_accepts_active_register_hooks():
    """Bytes-pin: BG pool __init__ accepts on_op_active_register and
    on_op_active_unregister kwargs. Both default to None so older
    constructions stay green."""
    from backend.core.ouroboros.governance.background_agent_pool import (
        BackgroundAgentPool,
    )
    import inspect
    sig = inspect.signature(BackgroundAgentPool.__init__)
    assert "on_op_active_register" in sig.parameters
    assert "on_op_active_unregister" in sig.parameters
    assert sig.parameters["on_op_active_register"].default is None
    assert sig.parameters["on_op_active_unregister"].default is None


def test_pool_stores_hooks_when_provided():
    from backend.core.ouroboros.governance.background_agent_pool import (
        BackgroundAgentPool,
    )

    def reg(op_id: str) -> None:
        pass

    def unreg(op_id: str) -> None:
        pass

    pool = BackgroundAgentPool(
        orchestrator=None,  # type: ignore[arg-type]
        on_op_active_register=reg,
        on_op_active_unregister=unreg,
    )
    assert pool._on_op_active_register is reg
    assert pool._on_op_active_unregister is unreg


# -----------------------------------------------------------------------
# § B — Bytes pins on the worker loop
# -----------------------------------------------------------------------


def _bg_pool_src() -> str:
    return pathlib.Path(
        "backend/core/ouroboros/governance/background_agent_pool.py"
    ).read_text()


def test_worker_calls_register_hook_on_pickup():
    """Worker must call ``self._on_op_active_register(ctx_op_id)``
    after dequeuing an op. Bytes-pin so a refactor that drops the call
    is caught."""
    src = _bg_pool_src()
    assert "self._on_op_active_register(_ctx_op_id)" in src


def test_worker_calls_unregister_hook_in_finally():
    """Worker must call unregister inside the finally block so cancel
    / failure / rupture all clean up. Bytes-pin."""
    src = _bg_pool_src()
    assert "self._on_op_active_unregister(" in src
    # The unregister must live in a finally block — search for the
    # finally clause and the unregister call within reasonable proximity.
    finally_idx = src.find("finally:\n                    op.completed_at")
    assert finally_idx > 0, "expected unified-cleanup finally block"
    unregister_idx = src.find(
        "self._on_op_active_unregister(", finally_idx,
    )
    assert unregister_idx > 0
    # Must be within ~30 lines of the finally
    assert (
        src[finally_idx:unregister_idx].count("\n") < 30
    ), "unregister too far from finally — cleanup may not always run"


def test_register_uses_context_op_id_not_pool_slot():
    """The hook receives ``ctx.op_id`` (the orchestrator's id), not
    ``op.op_id`` (pool-internal slot). Otherwise the harness's tracker
    would be keyed differently than the orchestrator and stream-tick
    heartbeats wouldn't reach the right fsm_ctx."""
    src = _bg_pool_src()
    # The variable that holds the context op_id
    assert '_ctx_op_id = str(getattr(op.context, "op_id", "") or "")' in src


def test_register_failure_is_swallowed():
    """Hook exceptions never propagate — a misbehaving observability
    consumer cannot kill a BG worker. Bytes-pin the try/except."""
    src = _bg_pool_src()
    assert (
        "self._on_op_active_register(_ctx_op_id)" in src
    )
    # Find the call and verify there's an except block within a few
    # lines — the existing pattern uses ``except Exception as _exc``.
    reg_idx = src.find("self._on_op_active_register(_ctx_op_id)")
    nearby = src[reg_idx: reg_idx + 400]
    assert "except Exception" in nearby


# -----------------------------------------------------------------------
# § C — End-to-end with a fake worker iteration
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_unregister_round_trip_via_real_worker():
    """Drive a real ``_worker_loop`` iteration and verify the hooks fire
    in the right order around a successful op."""
    from backend.core.ouroboros.governance.background_agent_pool import (
        BackgroundAgentPool, BackgroundOp,
    )

    events: list = []

    def reg(op_id: str) -> None:
        events.append(("register", op_id))

    def unreg(op_id: str) -> None:
        events.append(("unregister", op_id))

    class _FakeOrchestrator:
        async def run(self, ctx):
            events.append(("orch_run", ctx.op_id))
            class _Result:
                terminal_phase = type("P", (), {"name": "COMPLETE"})
                reason_code = "ok"
            return _Result()

    class _FakeCtx:
        op_id = "op-fake-bg-001"
        target_files = []
        is_read_only = False

    pool = BackgroundAgentPool(
        orchestrator=_FakeOrchestrator(),  # type: ignore[arg-type]
        pool_size=1,
        queue_size=4,
        on_op_active_register=reg,
        on_op_active_unregister=unreg,
    )

    # Submit + run one iteration of the worker loop in isolation
    await pool.start()
    try:
        await pool.submit(_FakeCtx())  # type: ignore[arg-type]
        # Wait for the worker to drain the single op (with a timeout)
        for _ in range(50):
            await asyncio.sleep(0.05)
            if any(e[0] == "unregister" for e in events):
                break
    finally:
        await pool.stop()

    # The hooks must have fired around the orchestrator call
    op_ids = [e[1] for e in events]
    kinds = [e[0] for e in events]
    assert "register" in kinds, f"no register event: {events}"
    assert "unregister" in kinds, f"no unregister event: {events}"
    # Every op_id must be the context's, not the pool slot id
    for opid in op_ids:
        assert opid == "op-fake-bg-001"
    # Order: register happens before unregister
    reg_idx = kinds.index("register")
    unreg_idx = kinds.index("unregister")
    assert reg_idx < unreg_idx


@pytest.mark.asyncio
async def test_unregister_fires_even_when_orchestrator_raises():
    """The finally-block guarantee: unregister fires on failure too."""
    from backend.core.ouroboros.governance.background_agent_pool import (
        BackgroundAgentPool,
    )

    events: list = []

    def reg(op_id: str) -> None:
        events.append(("register", op_id))

    def unreg(op_id: str) -> None:
        events.append(("unregister", op_id))

    class _FailingOrchestrator:
        async def run(self, ctx):
            raise RuntimeError("simulated provider failure")

    class _FakeCtx:
        op_id = "op-fake-bg-fail"
        target_files = []
        is_read_only = False

    pool = BackgroundAgentPool(
        orchestrator=_FailingOrchestrator(),  # type: ignore[arg-type]
        pool_size=1,
        queue_size=4,
        on_op_active_register=reg,
        on_op_active_unregister=unreg,
    )

    await pool.start()
    try:
        await pool.submit(_FakeCtx())  # type: ignore[arg-type]
        for _ in range(50):
            await asyncio.sleep(0.05)
            if any(e[0] == "unregister" for e in events):
                break
    finally:
        await pool.stop()

    kinds = [e[0] for e in events]
    assert "register" in kinds
    assert "unregister" in kinds, "unregister did not fire on failure path"


# -----------------------------------------------------------------------
# § D — GLS wires the hooks (bytes pin)
# -----------------------------------------------------------------------


def test_gls_constructs_pool_with_unified_hooks():
    """Bytes-pin: governed_loop_service.py must construct the pool with
    on_op_active_register + on_op_active_unregister bound to functions
    that touch ``self._active_ops`` and ``self._fsm_contexts``."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/governed_loop_service.py"
    ).read_text()
    assert "on_op_active_register=_bg_register_active" in src
    assert "on_op_active_unregister=_bg_unregister_active" in src
    assert "self._active_ops.add(op_id)" in src
    assert "self._fsm_contexts[op_id] = LoopRuntimeContext(" in src
    assert "self._active_ops.discard(op_id)" in src
    assert "self._fsm_contexts.pop(op_id, None)" in src


# -----------------------------------------------------------------------
# § E — Authority invariant
# -----------------------------------------------------------------------


def test_authority_invariant_no_orchestrator_imports():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "candidate_generator", "policy",
    )
    for tok in forbidden:
        assert f"import {tok}" not in src, f"forbidden import: {tok}"
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden submodule import: {tok}"
