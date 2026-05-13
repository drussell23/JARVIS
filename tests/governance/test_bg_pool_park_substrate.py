"""
Stage 1.6 Slice 2a — BG pool park substrate spine.

This slice adds the worker-side handler for the park substrate (Slice 1)
plus the route-aware park policy.  No callsite is wired yet — that is
Slice 2b's job.  The spine here pins:

  * BackgroundOp gains ``resumed`` + ``park_attempt_seq`` fields with
    safe defaults (no caller-visible change for legacy submissions).
  * ``_VALID_STATUSES`` includes ``"parked"``; the status is non-
    terminal so observers awaiting result keep waiting until resume.
  * The BG worker loop catches ``ParkRequested`` raised by the
    orchestrator (simulated via a stub orchestrator) and:
        - sets ``op.status = "parked"``,
        - records the ``ParkSignal`` as ``op.result``,
        - increments ``_parked_count``,
        - releases the slot (finally block fires; ``task_done()`` called).
  * ``should_park_for_route`` is a pure function with deterministic
    decision-table semantics and CSV-overridable eligible route set.

The 3 operator-named claims for Slice 2 (slot freed during stall / no
double-dispatch / no lost terminal) require the GENERATE callsite to
be wired — they will land with Slice 2b under
``test_bg_park_integration.py``.  This file covers the substrate
contract that 2b depends on.
"""
from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
    BackgroundOp,
    _VALID_STATUSES,
    _ParkRequested_t,
)
from backend.core.ouroboros.governance.op_park_store import (
    should_park_for_route,
)
from backend.core.ouroboros.governance.park_signal import (
    ParkDescriptor,
    ParkRequested,
    ParkSignal,
)


_BG_POOL_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "background_agent_pool.py"
)
_OP_PARK_STORE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "op_park_store.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubCtx:
    """Minimal OperationContext stand-in for BG pool unit tests."""

    def __init__(
        self,
        op_id: str = "op-unit-test",
        *,
        target_files: tuple = (),
        signal_source: str = "",
        is_read_only: bool = False,
        provider_route: str = "background",
    ) -> None:
        self.op_id = op_id
        self.target_files = target_files
        self.signal_source = signal_source
        self.is_read_only = is_read_only
        self.provider_route = provider_route
        self.description = "stub"


class _RaisingParkOrchestrator:
    """Orchestrator stub that raises ParkRequested on run().

    Simulates the future Slice 2b wiring at the GENERATE callsite.
    """

    def __init__(self, signal: ParkSignal) -> None:
        self._signal = signal
        self.run_calls: int = 0

    async def run(self, ctx: Any) -> Any:
        self.run_calls += 1
        await asyncio.sleep(0)  # yield to honor cooperative cancellation
        raise ParkRequested(self._signal)


class _CompletingOrchestrator:
    """Orchestrator stub that returns a result on run() — for the
    no-regression smoke that the legacy path is unaffected."""

    def __init__(self, payload: Any = "ok") -> None:
        self._payload = payload
        self.run_calls: int = 0

    async def run(self, ctx: Any) -> Any:
        self.run_calls += 1
        await asyncio.sleep(0)
        return self._payload


def _make_signal(
    op_id: str = "op-unit",
    attempt: int = 1,
    kind: str = "generate",
) -> ParkSignal:
    return ParkSignal(
        op_id=op_id,
        token=f"{op_id}::attempt-{attempt}",
        attempt_seq=attempt,
        descriptor=ParkDescriptor(kind=kind, payload={}),
        park_started_at=0.0,
    )


# ---------------------------------------------------------------------------
# BackgroundOp surface pins
# ---------------------------------------------------------------------------


def test_background_op_has_resumed_field_default_false():
    op = BackgroundOp(op_id="op-x", goal="test")
    assert op.resumed is False, (
        "BackgroundOp.resumed MUST default False so legacy submissions "
        "behave identically to pre-1.6"
    )


def test_background_op_has_park_attempt_seq_field_default_zero():
    op = BackgroundOp(op_id="op-x", goal="test")
    assert op.park_attempt_seq == 0, (
        "BackgroundOp.park_attempt_seq MUST default 0 so legacy "
        "submissions don't appear as if they had parked"
    )


def test_background_op_accepts_parked_status():
    op = BackgroundOp(op_id="op-x", goal="test", status="parked")
    assert op.status == "parked"


def test_parked_is_not_in_is_terminal_tuple():
    """``parked`` MUST be non-terminal — resume completes the op later."""
    op = BackgroundOp(op_id="op-x", goal="test", status="parked")
    assert op.is_terminal is False, (
        "BackgroundOp.is_terminal must return False for status='parked' "
        "— resume dispatches the op to a real terminal status later"
    )


def test_parked_in_valid_statuses_set():
    assert "parked" in _VALID_STATUSES


# ---------------------------------------------------------------------------
# Lazy-import resolver
# ---------------------------------------------------------------------------


def test_park_requested_lazy_resolver_returns_canonical_class():
    cls = _ParkRequested_t()
    assert cls is ParkRequested, (
        "_ParkRequested_t() MUST return the canonical "
        "park_signal.ParkRequested class — single source of truth"
    )


def test_park_requested_resolver_caches_result():
    cls1 = _ParkRequested_t()
    cls2 = _ParkRequested_t()
    assert cls1 is cls2, "Resolver must cache the class object"


# ---------------------------------------------------------------------------
# ParkRequested exception contract
# ---------------------------------------------------------------------------


def test_park_requested_subclasses_base_exception_only():
    """ParkRequested MUST subclass BaseException but NOT Exception.

    This mirrors asyncio.CancelledError exactly.  If ParkRequested
    inherited from Exception it would be caught by the GENERATE
    retry-loop's ``except Exception as exc:`` at generate_runner.py:1210
    and routed to retry/failure instead of park-emit.
    """
    signal = _make_signal()
    exc = ParkRequested(signal)
    assert isinstance(exc, BaseException), (
        "ParkRequested must subclass BaseException for the worker "
        "except clause + worker-loop except chain to bind it"
    )
    assert not isinstance(exc, Exception), (
        "ParkRequested must NOT subclass Exception — would be caught by "
        "`except Exception:` clauses throughout the orchestrator (esp. "
        "generate_runner.py:1210). See ParkRequested docstring for why."
    )
    assert exc.signal is signal


def test_park_requested_message_carries_signal_metadata():
    signal = _make_signal(op_id="op-abc", attempt=3, kind="generate")
    exc = ParkRequested(signal)
    msg = str(exc)
    assert "op-abc" in msg
    assert "attempt=3" in msg
    assert "generate" in msg


# ---------------------------------------------------------------------------
# should_park_for_route — deterministic decision-table pin
# ---------------------------------------------------------------------------


def test_should_park_returns_false_when_master_off():
    """No env, no monkeypatch — master defaults FALSE per §33.1."""
    assert should_park_for_route(
        "background", queue_pressure=True,
    ) is False
    assert should_park_for_route(
        "complex", queue_pressure=True,
    ) is False


@pytest.mark.parametrize("route,pressure,resumed,expected", [
    # Master ON via env in body — see test_should_park_route_eligibility_table
    ("background", True, False, True),    # default-eligible + pressure
    ("complex", True, False, True),       # default-eligible + pressure
    ("background", False, False, False),  # no queue pressure → no park
    ("complex", False, False, False),     # ditto
    ("immediate", True, False, False),    # not eligible
    ("standard", True, False, False),     # not eligible
    ("speculative", True, False, False),  # not eligible
    ("BACKGROUND", True, False, True),    # case-insensitive
    ("background", True, True, False),    # is_resumed → never park
    ("complex", True, True, False),       # ditto
    ("", True, False, False),             # empty route
    ("unknown", True, False, False),      # unknown route → no
])
def test_should_park_route_eligibility_table(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    pressure: bool,
    resumed: bool,
    expected: bool,
):
    """Closed decision table — every cell pinned."""
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    result = should_park_for_route(
        route, queue_pressure=pressure, is_resumed=resumed,
    )
    assert result is expected, (
        f"should_park_for_route(route={route!r}, pressure={pressure}, "
        f"resumed={resumed}) returned {result}, expected {expected}"
    )


def test_should_park_route_csv_override(monkeypatch: pytest.MonkeyPatch):
    """Operators can extend the eligible-route set via env."""
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BG_PARK_ROUTES", "standard, complex")
    # standard now eligible, background no longer:
    assert should_park_for_route(
        "standard", queue_pressure=True,
    ) is True
    assert should_park_for_route(
        "background", queue_pressure=True,
    ) is False
    assert should_park_for_route(
        "complex", queue_pressure=True,
    ) is True  # still eligible (in override list)


def test_should_park_route_csv_override_empty_uses_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty CSV → fall back to default {background, complex}."""
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BG_PARK_ROUTES", "   ")
    assert should_park_for_route(
        "background", queue_pressure=True,
    ) is True


# ---------------------------------------------------------------------------
# BG worker integration — catches ParkRequested + frees slot
# ---------------------------------------------------------------------------


def test_worker_catches_park_requested_and_releases_slot():
    """The 3-claim-supporting integration smoke for Slice 2a.

    A stub orchestrator raises ParkRequested.  The worker MUST:
      * NOT propagate the exception to its outer try/finally
        (slot stays returned to the pool)
      * Set ``op.status = "parked"``
      * Stamp ``op.result`` with the ParkSignal
      * Increment ``_parked_count``
      * Free the slot for the next submission to be picked up
    """
    signal = _make_signal(op_id="op-park-smoke", attempt=1)
    orch = _RaisingParkOrchestrator(signal)

    async def _go():
        pool = BackgroundAgentPool(
            orchestrator=orch,
            pool_size=1,
            queue_size=2,
        )
        await pool.start()
        try:
            ctx = _StubCtx(op_id="op-park-smoke")
            op_id_internal = await pool.submit(ctx)
            # Wait for terminal state (parked is observable via
            # _parked_count; the result attribute is set in the
            # worker's except clause)
            for _ in range(50):
                if pool._parked_count >= 1:
                    break
                await asyncio.sleep(0.02)
            assert pool._parked_count == 1, (
                f"Worker did not park within timeout; "
                f"_parked_count={pool._parked_count}, "
                f"orch.run_calls={orch.run_calls}"
            )
            # Slot must be free — submit a second op and verify
            # the worker picks it up
            ctx2 = _StubCtx(op_id="op-park-smoke-second")
            await pool.submit(ctx2)
            # Replace orchestrator's behavior for the second op:
            # since it's the same _RaisingParkOrchestrator, it will
            # also park.  That's fine — we just need to prove the
            # slot was freed enough for a second dispatch.
            for _ in range(50):
                if pool._parked_count >= 2:
                    break
                await asyncio.sleep(0.02)
            assert pool._parked_count == 2, (
                f"Second submission was not picked up — slot was not "
                f"freed; _parked_count={pool._parked_count}"
            )
            # The first BackgroundOp should carry the signal
            bg_op = pool._ops[op_id_internal]
            assert bg_op.status == "parked"
            assert bg_op.result is signal
            # Completed/failed/cancelled counters stay at zero
            assert pool._completed_count == 0
            assert pool._failed_count == 0
            assert pool._cancelled_count == 0
        finally:
            await pool.stop()

    asyncio.run(_go())


def test_worker_park_count_in_health():
    """``health()`` MUST surface ``parked_count`` for observability."""
    orch = _CompletingOrchestrator()

    async def _go():
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1)
        await pool.start()
        try:
            status = pool.health()
            assert "parked_count" in status
            assert status["parked_count"] == 0
        finally:
            await pool.stop()

    asyncio.run(_go())


def test_legacy_completion_path_unaffected():
    """Regression: ops that DON'T raise ParkRequested still complete cleanly."""
    orch = _CompletingOrchestrator(payload="legacy-ok")

    async def _go():
        pool = BackgroundAgentPool(orchestrator=orch, pool_size=1)
        await pool.start()
        try:
            ctx = _StubCtx(op_id="op-legacy")
            op_id_internal = await pool.submit(ctx)
            for _ in range(50):
                if pool._completed_count >= 1:
                    break
                await asyncio.sleep(0.02)
            assert pool._completed_count == 1
            assert pool._parked_count == 0
            assert pool._ops[op_id_internal].status == "completed"
            assert pool._ops[op_id_internal].result == "legacy-ok"
        finally:
            await pool.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Authority + invariant AST pins
# ---------------------------------------------------------------------------


def test_ast_pin_bg_pool_lazy_imports_park_requested():
    """Worker MUST use lazy import — no module-level ``from park_signal``."""
    src = _BG_POOL_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Module-level ImportFrom statements (NOT inside functions)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert "park_signal" not in (node.module or ""), (
                f"background_agent_pool.py MUST NOT import-from park_signal "
                f"at module scope; got {node.module!r}. Use _ParkRequested_t() "
                f"lazy resolver instead — keeps import order acyclic."
            )


def test_ast_pin_bg_pool_uses_resolver_in_except_clause():
    """The except clause MUST use _ParkRequested_t() — single resolution seam."""
    src = _BG_POOL_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            handler_type = node.type
            if handler_type is None:
                continue
            # Pattern: `except _ParkRequested_t() as park_exc:`
            if isinstance(handler_type, ast.Call) \
                    and isinstance(handler_type.func, ast.Name) \
                    and handler_type.func.id == "_ParkRequested_t":
                found = True
                break
    assert found, (
        "background_agent_pool.py MUST have an except clause of the form "
        "`except _ParkRequested_t() as park_exc:` — proves the lazy "
        "resolver is wired into the worker hot path"
    )


def test_ast_pin_should_park_is_pure_function():
    """should_park_for_route MUST take pure inputs (no pool/ctx ref)."""
    src = _OP_PARK_STORE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "should_park_for_route":
            target = node
            break
    assert target is not None, (
        "op_park_store.py must define should_park_for_route"
    )
    # Arg names MUST be (provider_route, *, queue_pressure, is_resumed)
    # — pure scalars, no orchestrator/pool/ctx
    arg_names = [a.arg for a in target.args.args]
    kwonly_names = [a.arg for a in target.args.kwonlyargs]
    assert "provider_route" in arg_names, "Missing provider_route arg"
    assert "queue_pressure" in kwonly_names, (
        "queue_pressure MUST be keyword-only — operator pin"
    )
    assert "is_resumed" in kwonly_names, (
        "is_resumed MUST be keyword-only — operator pin"
    )


def test_ast_pin_park_requested_uses_signal_attribute():
    """ParkRequested.__init__ MUST stash the signal — worker reads .signal."""
    src = (
        Path(__file__).parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "park_signal.py"
    ).read_text(encoding="utf-8")
    assert "self.signal = signal" in src, (
        "ParkRequested.__init__ MUST assign self.signal = signal — "
        "the BG worker reads park_exc.signal to log + stamp op.result"
    )
