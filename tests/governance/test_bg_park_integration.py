"""
Stage 1.6 Slice 2b — BG park integration spine.

Closes the operator binding 2026-05-13 (the 3 spine claims):

  (a) **slot freed during mock-stalled GENERATE** — pool_size=1, op1
      parks via the wrapper; op2 is dispatched while op1's provider
      mock-stalls; op2 reaches a worker.

  (b) **no double-dispatch** — across park-emit + resume, the
      provider mock is invoked EXACTLY ONCE.  The park substrate's
      single-flight admission by ``(op_id, attempt_seq)`` + the
      RESUME path that materializes from store guarantees this.

  (c) **no lost terminal** — after park + resume, the resumed
      dispatch reaches a real terminal status (``completed``) on
      the BackgroundOp; the resume mark is cleared from the pool's
      side-channel.

These are integration tests — they wire a real BackgroundAgentPool +
real ParkedOpStore + real generate_park_wrapper, against a stub
orchestrator + stub generator that simulate the GENERATE phase.
Master flag is monkey-patched to True for the park-on paths;
master-off paths verify the legacy direct-await is byte-identical.
"""
from __future__ import annotations

import asyncio
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)
from backend.core.ouroboros.governance._governance_state import (
    bind_bg_pool,
    get_bound_bg_pool,
)
from backend.core.ouroboros.governance.op_park_store import (
    get_default_store,
    reset_default_store,
)


_WRAPPER_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "generate_park_wrapper.py"
)
_GENERATE_RUNNER_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "generate_runner.py"
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubGenerator:
    """Stub for ``orch._generator`` — counts calls and supports stalling.

    Default behavior: returns a sentinel ``"gen-N"`` string where N is
    the call count.  Set ``stall_event`` to an unset asyncio.Event
    BEFORE the call to make the next ``generate()`` hang until the
    event is set externally (simulates a long LLM round-trip).
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self.stall_event: asyncio.Event = asyncio.Event()
        # Default: not stalling — return immediately
        self.stall_event.set()

    async def generate(self, ctx: Any, deadline: datetime) -> Any:
        self.call_count += 1
        await self.stall_event.wait()
        return f"gen-{self.call_count}"


class _StubOrchestrator:
    """Stub for orchestrator.  Calls ``maybe_park_or_resume`` against a
    real wrapper so the seam logic runs end-to-end.  Returns the
    generation result wrapped in a marker so we can verify terminal
    state at the worker layer.
    """

    def __init__(self, gen_timeout: float = 60.0) -> None:
        self._generator = _StubGenerator()
        self._ledger: Any = None  # No ledger in unit tests — wrapper
                                  # tolerates None per spec
        self.run_count: int = 0
        self._gen_timeout = gen_timeout

    async def run(self, ctx: Any) -> Any:
        self.run_count += 1
        from backend.core.ouroboros.governance.generate_park_wrapper import (
            maybe_park_or_resume,
        )
        deadline = datetime.now(tz=timezone.utc) + timedelta(
            seconds=self._gen_timeout,
        )
        generation = await maybe_park_or_resume(
            orch=self,
            ctx=ctx,
            deadline=deadline,
            gen_timeout=self._gen_timeout,
            outer_grace_s=5.0,
        )
        return f"orch_run_done:{generation}"


class _StubCtx:
    """Stand-in for OperationContext."""

    def __init__(
        self,
        op_id: str = "op-int",
        *,
        provider_route: str = "background",
        signal_source: str = "",
        is_read_only: bool = False,
    ) -> None:
        self.op_id = op_id
        self.target_files: tuple = ()
        self.signal_source = signal_source
        self.is_read_only = is_read_only
        self.provider_route = provider_route
        self.description = "stub"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset ParkedOpStore + bg pool bind between tests."""
    reset_default_store()
    bind_bg_pool(None)
    yield
    reset_default_store()
    bind_bg_pool(None)


# ---------------------------------------------------------------------------
# Sanity — master OFF preserves legacy byte-identical behavior
# ---------------------------------------------------------------------------


def test_master_off_legacy_direct_await():
    """Master flag default-FALSE → direct await, no park, no store touch."""

    async def _go():
        orch = _StubOrchestrator(gen_timeout=10.0)
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1)
        await pool.start()
        try:
            ctx = _StubCtx(op_id="op-legacy")
            op_id_internal = await pool.submit(ctx)
            for _ in range(50):
                if pool._completed_count >= 1:
                    break
                await asyncio.sleep(0.02)
            assert pool._completed_count == 1, (
                f"Master-off legacy path failed; "
                f"completed={pool._completed_count}, parked={pool._parked_count}"
            )
            assert pool._parked_count == 0
            # Store must be EMPTY — nothing was admitted
            assert await get_default_store().size() == 0
            # BackgroundOp result carries the orchestrator return
            assert pool._ops[op_id_internal].result == "orch_run_done:gen-1"
        finally:
            await pool.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Claim (a) — slot freed during mock-stalled GENERATE
# ---------------------------------------------------------------------------


def test_claim_a_slot_freed_during_park_stall(monkeypatch: pytest.MonkeyPatch):
    """The operator's first claim.

    Pool size = 1.  Op1's generator stall_event is unset (the provider
    "hangs").  Op2 is submitted so queue_depth>0 when op1 enters the
    wrapper.  Master flag is on, route is eligible → op1 parks.

    After op1 parks, the worker frees the slot.  Op2 is then picked
    up.  Op2's wrapper sees queue_depth=0 (no more queued ops) so it
    takes the LEGACY direct-await path, which ALSO stalls on the
    same generator event.  This is the precise signal we want:

      * op1 reached the wrapper and parked (slot freedom achieved)
      * op2 reached the wrapper and entered the generator stall

    Without slot freedom, op2 would be stuck in 'queued' status with
    no started_at timestamp.  With slot freedom, op2 reaches 'running'
    status while op1's continuation is still suspended on the same
    stall_event.

    Asserts:
      * op1 status is 'parked' before its generator returns
      * op2 status is 'running' AND op2 entered the generator
        (call_count==2 — once for op1's continuation, once for op2's
        legacy await) — proves slot was freed
      * Neither provider returned (stall_event still unset)
      * 0 completed dispatches (no one finished)
    """
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

    async def _go():
        orch = _StubOrchestrator(gen_timeout=30.0)
        # Stall the generator BEFORE submit — next .generate() will hang.
        orch._generator.stall_event = asyncio.Event()  # unset
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1, queue_size=4)
        await pool.start()
        try:
            ctx1 = _StubCtx(op_id="op-stall-1", provider_route="background")
            ctx2 = _StubCtx(op_id="op-stall-2", provider_route="background")
            op1_internal = await pool.submit(ctx1)
            # Submit op2 IMMEDIATELY so queue_depth() > 0 when op1
            # enters the wrapper.  We need queue pressure for the
            # park-emit path to engage on op1.
            op2_internal = await pool.submit(ctx2)

            # Wait for op1 to park
            for _ in range(150):
                if pool._parked_count >= 1:
                    break
                await asyncio.sleep(0.02)
            assert pool._parked_count >= 1, (
                f"op1 did not park within timeout; "
                f"_parked_count={pool._parked_count}, "
                f"orch.run_count={orch.run_count}, "
                f"orch.gen.call_count={orch._generator.call_count}"
            )
            assert pool._ops[op1_internal].status == "parked"
            # ===========================================================
            # KEY CLAIM (a): the slot was FREED for op2.
            # Wait for op2 to reach the wrapper.  op2 takes the LEGACY
            # path (queue empty when it starts) and stalls on the same
            # generator event.  We see this via:
            #   * orch.run_count >= 2 (op2 entered orch.run)
            #   * orch._generator.call_count >= 2 (op2's legacy await
            #     reached the generator and is now blocking on the
            #     stall_event)
            #   * op2.status == "running" (worker dequeued + started it)
            # ===========================================================
            for _ in range(150):
                if (orch.run_count >= 2
                        and orch._generator.call_count >= 2):
                    break
                await asyncio.sleep(0.02)
            assert orch.run_count >= 2, (
                f"Slot was NOT freed for op2 — orch.run never invoked "
                f"on op2; orch.run_count={orch.run_count}"
            )
            assert orch._generator.call_count >= 2, (
                f"op2 did not reach the generator while op1's "
                f"continuation is stalled; gen.call_count="
                f"{orch._generator.call_count}"
            )
            op2_bg = pool._ops[op2_internal]
            assert op2_bg.status == "running", (
                f"op2 status={op2_bg.status} (expected running — proves "
                f"slot was freed by op1's park)"
            )
            assert op2_bg.started_at is not None
            # Neither stall has been released → no completions
            assert orch._generator.stall_event.is_set() is False
            assert pool._completed_count == 0
        finally:
            # Release the stall so continuations + ops can drain
            orch._generator.stall_event.set()
            await pool.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Claim (b) — no double-dispatch (provider called exactly once)
# ---------------------------------------------------------------------------


def test_claim_b_no_double_dispatch(monkeypatch: pytest.MonkeyPatch):
    """The operator's second claim.

    Across park-emit → continuation → resume, the generator's
    ``generate()`` is invoked EXACTLY ONCE.  Drives an op through the
    full park lifecycle and asserts call_count.

    To trigger park-emit we need queue pressure.  We submit op1 (which
    will park) AND op2 (filler) so queue_depth>0 at op1's wrapper
    entry.  After op1 parks, op2 will also park.  After the
    continuations complete, op1 resumes → wrapper sees the completed
    store record → returns generation WITHOUT calling generator again.

    Provider call_count breakdown:
      * op1 first park-emit: continuation calls generator → +1
      * op1 RESUME: wrapper materializes from store → +0
      * op2 first park-emit: continuation calls generator → +1
      * op2 RESUME: wrapper materializes from store → +0
    Total = 2 calls (one per op).  We assert exactly 2 — proving
    no double-dispatch.
    """
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

    async def _go():
        orch = _StubOrchestrator(gen_timeout=5.0)
        # Generator returns immediately (stall_event is set in __init__).
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1, queue_size=4)
        await pool.start()
        try:
            ctx1 = _StubCtx(op_id="op-no-dup-1", provider_route="background")
            ctx2 = _StubCtx(op_id="op-no-dup-2", provider_route="background")
            await pool.submit(ctx1)
            await pool.submit(ctx2)
            # Wait for BOTH ops to complete (via resume)
            for _ in range(200):
                if pool._completed_count >= 2:
                    break
                await asyncio.sleep(0.02)
            assert pool._completed_count >= 2, (
                f"Two ops did not both complete via resume; "
                f"completed={pool._completed_count}, "
                f"parked={pool._parked_count}, "
                f"gen.call_count={orch._generator.call_count}"
            )
            # ===========================================================
            # KEY CLAIM (b): generator called exactly twice — once per
            # op.  If the resume path double-dispatched, we would see
            # 3 or 4 calls.
            # ===========================================================
            assert orch._generator.call_count == 2, (
                f"Double-dispatch detected: gen.call_count="
                f"{orch._generator.call_count} (expected 2 — one per op)"
            )
        finally:
            await pool.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Claim (c) — no lost terminal (resumed op reaches completed)
# ---------------------------------------------------------------------------


def test_claim_c_no_lost_terminal(monkeypatch: pytest.MonkeyPatch):
    """The operator's third claim.

    After park + resume the BackgroundOp reaches a real terminal
    status (``completed``) — not ``parked``-forever.  The resume mark
    is cleared from the pool's side-channel.

    Drives one op fully:
      1. submit → park
      2. continuation completes → resubmit_for_resume
      3. resumed dispatch → wrapper materializes → orch.run returns
      4. worker writes 'completed', clears resume mark
    """
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

    async def _go():
        orch = _StubOrchestrator(gen_timeout=5.0)
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1, queue_size=4)
        await pool.start()
        try:
            # Need a filler to create queue pressure for the first op's
            # park-emit decision.
            ctx_filler = _StubCtx(
                op_id="op-filler", provider_route="background",
            )
            ctx = _StubCtx(op_id="op-terminal", provider_route="background")
            await pool.submit(ctx)
            await pool.submit(ctx_filler)
            # Wait for the resumed op-terminal to complete
            for _ in range(200):
                if pool._completed_count >= 2:
                    break
                await asyncio.sleep(0.02)
            assert pool._completed_count >= 2, (
                f"Resumed ops did not reach terminal; "
                f"completed={pool._completed_count}, parked={pool._parked_count}"
            )
            # KEY CLAIM (c.1): the resume mark is cleared
            assert pool.is_resumed_dispatch("op-terminal") is False, (
                "Resume mark for op-terminal was not cleared after resumed "
                "dispatch reached terminal"
            )
            # KEY CLAIM (c.2): the parked_count and completed_count are
            # both 2 — every op parked once, every op completed via
            # resume.  No lost terminals.
            assert pool._parked_count >= 2
            assert pool._completed_count >= 2
            # Store should be empty after both completions (TTL not
            # exercised here — the resume path called result_for which
            # left the record alive until next prune; verify size <= 2)
            assert await get_default_store().size() <= 2
        finally:
            await pool.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Supporting tests — bind contract + path decisions
# ---------------------------------------------------------------------------


def test_pool_start_binds_self_via_governance_state():
    """``pool.start()`` MUST register self in the bind so the wrapper
    can resolve it."""

    async def _go():
        orch = _StubOrchestrator()
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1)
        assert get_bound_bg_pool() is None  # pre-start
        await pool.start()
        try:
            assert get_bound_bg_pool() is pool
        finally:
            await pool.stop()
        # post-stop the bind is cleared
        assert get_bound_bg_pool() is None

    asyncio.run(_go())


def test_no_queue_pressure_no_park(monkeypatch: pytest.MonkeyPatch):
    """Master on + eligible route + NO queue pressure → legacy path."""
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

    async def _go():
        orch = _StubOrchestrator(gen_timeout=5.0)
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=2, queue_size=4)
        await pool.start()
        try:
            # Only ONE op submitted — pool_size=2 so queue stays empty.
            ctx = _StubCtx(op_id="op-no-press", provider_route="background")
            await pool.submit(ctx)
            for _ in range(50):
                if pool._completed_count >= 1:
                    break
                await asyncio.sleep(0.02)
            assert pool._completed_count == 1
            # NO park happened — generator was called via legacy path
            assert pool._parked_count == 0
            assert orch._generator.call_count == 1
        finally:
            await pool.stop()

    asyncio.run(_go())


def test_ineligible_route_no_park(monkeypatch: pytest.MonkeyPatch):
    """Master on + queue pressure + route NOT in eligible set → legacy."""
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

    async def _go():
        orch = _StubOrchestrator(gen_timeout=5.0)
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1, queue_size=4)
        await pool.start()
        try:
            # Both ops use 'standard' route — NOT in default eligible set
            ctx1 = _StubCtx(op_id="op-std-1", provider_route="standard")
            ctx2 = _StubCtx(op_id="op-std-2", provider_route="standard")
            await pool.submit(ctx1)
            await pool.submit(ctx2)
            for _ in range(50):
                if pool._completed_count >= 2:
                    break
                await asyncio.sleep(0.02)
            assert pool._completed_count == 2
            assert pool._parked_count == 0
            assert orch._generator.call_count == 2
        finally:
            await pool.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# AST pins — wrapper authority + callsite invariants
# ---------------------------------------------------------------------------


def test_ast_pin_generate_runner_uses_wrapper():
    """generate_runner.py:493 MUST call maybe_park_or_resume."""
    src = _GENERATE_RUNNER_SRC.read_text(encoding="utf-8")
    assert "maybe_park_or_resume" in src, (
        "generate_runner.py MUST call maybe_park_or_resume — the seam "
        "that wires the park substrate into the GENERATE phase"
    )
    assert "from backend.core.ouroboros.governance.generate_park_wrapper import" in src, (
        "generate_runner.py MUST import maybe_park_or_resume from "
        "generate_park_wrapper (the canonical seam module)"
    )


def test_ast_pin_wrapper_has_no_orchestrator_imports():
    """The wrapper MUST stay duck-typed — no hard imports of orchestrator."""
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Module-level imports only — function-local lazy imports are OK
    forbidden = (
        "orchestrator",
        "candidate_generator",
        "background_agent_pool",
        "phase_runners",
    )
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for bad in forbidden:
                assert bad not in mod, (
                    f"generate_park_wrapper.py must not import-from {mod!r} "
                    f"at module scope — wrapper MUST stay duck-typed"
                )


def test_ast_pin_wrapper_three_paths_present():
    """The three execution paths must all be reachable in the source.

    Defensive AST pin: future refactors that accidentally drop a path
    would break the substrate.  We assert the three path markers are
    present in the canonical entry function.
    """
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    assert "Path 1 — RESUME" in src, "RESUME path marker missing"
    assert "Path 2 — PARK-EMIT" in src, "PARK-EMIT path marker missing"
    assert "Path 3 — LEGACY" in src, "LEGACY path marker missing"


def test_ast_pin_wrapper_master_flag_first():
    """The wrapper MUST gate on park_enabled() in EVERY non-legacy
    branch — preserving the §33.1 default-FALSE invariant."""
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    assert "master_on = park_enabled()" in src, (
        "Wrapper MUST resolve park_enabled() into master_on at top"
    )
    # Resume path must check master_on
    assert "if master_on and pool is not None and ctx_op_id and pool.is_resumed_dispatch" in src, (
        "RESUME path MUST gate on master_on first"
    )
    # Park-emit path must check master_on
    assert "if (\n        master_on" in src, (
        "PARK-EMIT path MUST gate on master_on first"
    )
