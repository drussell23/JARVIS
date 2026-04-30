"""Dynamic Provider Fallback — when the primary is in active backoff,
``_try_primary_then_fallback`` routes directly to the fallback without
re-attempting the failing primary.

Closes the failure mode observed at the end of Move 2 v5 soak
``bt-2026-04-30-065848``: Claude hit a sustained transport blackout
(14 client pool recycles in 5 minutes due to ConnectTimeout +
SSLWantReadError), but the dispatcher kept calling Claude on every
new IMMEDIATE/COMPLEX op because the existing FSM bookkeeping
(``FailbackStateMachine.record_primary_failure`` +
``should_attempt_primary``) was never consulted at the dispatch site.

Pins:
  * ``_try_primary_then_fallback`` reads
    ``self.fsm.should_attempt_primary()`` BEFORE invoking the primary.
  * When ``should_attempt_primary()`` returns False, the call routes
    directly to ``self._call_fallback`` and never touches primary.
  * When it returns True (no backoff, or ETA elapsed), normal
    behavior preserved — primary attempted first.
  * The dynamic-fallback log line carries the failure mode +
    consecutive failures + recovery_eta for operator observability.

Authority Invariant
-------------------
Tests import only from candidate_generator + stdlib. No orchestrator /
phase_runners / iron_gate imports.
"""
from __future__ import annotations

import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Bytes pin: dispatch consults should_attempt_primary
# -----------------------------------------------------------------------


def test_try_primary_then_fallback_consults_fsm_before_primary():
    """Bytes-pin: ``_try_primary_then_fallback`` MUST short-circuit to
    ``_call_fallback`` when ``should_attempt_primary()`` returns False.
    Without this guard, sustained primary failures cause repeated
    cost-burn on the failing provider."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()

    # Locate the function definition
    fn_idx = src.find("async def _try_primary_then_fallback(")
    assert fn_idx > 0
    # Find the next function definition to bound the search window
    next_def = src.find("    async def _call_primary(", fn_idx)
    assert next_def > fn_idx
    body = src[fn_idx:next_def]

    # The guard must read should_attempt_primary
    assert "self.fsm.should_attempt_primary()" in body, (
        "dynamic fallback guard not present in _try_primary_then_fallback"
    )
    # And it must early-return via _call_fallback BEFORE _call_primary
    short_circuit_idx = body.find(
        "if not self.fsm.should_attempt_primary():",
    )
    primary_call_idx = body.find("await self._call_primary(")
    assert short_circuit_idx > 0
    assert primary_call_idx > short_circuit_idx, (
        "should_attempt_primary check must come BEFORE _call_primary"
    )
    fallback_in_short_circuit = body.find(
        "return await self._call_fallback", short_circuit_idx,
    )
    # The short-circuit fallback return must precede the primary call
    assert 0 < fallback_in_short_circuit < primary_call_idx


def test_dynamic_fallback_log_carries_failure_mode_and_eta():
    """Operator observability: the dynamic-fallback log line must
    surface mode + consecutive_failures + recovery_eta so operators
    can diagnose 'why is the system suddenly using only DW?'"""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()
    # The log line must include all three signals
    assert "Dynamic fallback engaged" in src
    assert "consecutive_failures=" in src
    assert "recovery_eta=" in src


# -----------------------------------------------------------------------
# § B — Behavioral test against a real FSM
# -----------------------------------------------------------------------


def test_fsm_should_attempt_primary_after_failure_returns_false():
    """Verify the FSM contract our short-circuit relies on: after a
    primary failure, ``should_attempt_primary`` returns False until
    the recovery ETA has elapsed."""
    from backend.core.ouroboros.governance.candidate_generator import (
        FailbackStateMachine, FailureMode,
    )
    fsm = FailbackStateMachine()
    # Healthy state — should attempt primary
    assert fsm.should_attempt_primary() is True

    # Record a CONNECTION_ERROR failure (the v5 failure class)
    fsm.record_primary_failure(mode=FailureMode.CONNECTION_ERROR)
    # CONNECTION_ERROR base 120s — should NOT attempt primary right after
    assert fsm.should_attempt_primary() is False
    # FSM transitioned to FALLBACK_ACTIVE
    from backend.core.ouroboros.governance.candidate_generator import (
        FailbackState,
    )
    assert fsm.state is FailbackState.FALLBACK_ACTIVE
    # consecutive_failures incremented
    assert fsm._consecutive_failures == 1


def test_fsm_attempt_primary_returns_true_after_eta_elapses(monkeypatch):
    """After a TRANSIENT_TRANSPORT failure (5s base ETA), once the
    recovery ETA has elapsed, ``should_attempt_primary`` returns True
    again — the dynamic-fallback short-circuit naturally lifts.

    Determinism: we explicitly disable full-jitter so ``recovery_eta``
    returns a stable value across the two calls in this test (one to
    capture the threshold, one inside ``should_attempt_primary``).
    Without this, the random jitter on the second call can land just
    past the patched clock and false-fail the assertion. Issue
    diagnosed during UI Slice 3 (2026-04-30) — this test was
    intermittently failing in suite runs."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", "0")
    from backend.core.ouroboros.governance.candidate_generator import (
        FailbackStateMachine, FailureMode,
    )
    fsm = FailbackStateMachine()
    fsm.record_primary_failure(mode=FailureMode.TRANSIENT_TRANSPORT)
    assert fsm.should_attempt_primary() is False

    # Advance clock past recovery_eta (TRANSIENT_TRANSPORT base 5s,
    # cap 30s; first failure → ~5s ETA)
    eta = fsm.recovery_eta()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.candidate_generator.time.monotonic",
        lambda: eta + 1.0,
    )
    assert fsm.should_attempt_primary() is True


# -----------------------------------------------------------------------
# § C — End-to-end: stub _call_primary / _call_fallback
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_skips_primary_when_in_backoff(monkeypatch):
    """Drive ``_try_primary_then_fallback`` against a stubbed
    CandidateGenerator and verify it short-circuits to fallback when
    the FSM is in active backoff."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator, FailureMode,
    )
    from datetime import datetime, timedelta, timezone

    # Build a minimal CandidateGenerator instance via direct construction
    # of the smallest viable surface. We monkeypatch _call_primary and
    # _call_fallback to record which path was taken.
    cg = CandidateGenerator.__new__(CandidateGenerator)
    # Minimal attributes the function reads:
    from backend.core.ouroboros.governance.candidate_generator import (
        FailbackStateMachine,
    )
    cg.fsm = FailbackStateMachine()

    primary_calls = []
    fallback_calls = []

    async def stub_primary(ctx, deadline):
        primary_calls.append(ctx)
        class _R:
            pass
        return _R()

    async def stub_fallback(ctx, deadline):
        fallback_calls.append(ctx)
        class _R:
            pass
        return _R()

    cg._call_primary = stub_primary  # type: ignore[method-assign]
    cg._call_fallback = stub_fallback  # type: ignore[method-assign]

    class _FakeCtx:
        op_id = "op-test-001"
        provider_route = "immediate"

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=60)

    # Healthy FSM — primary attempted
    await cg._try_primary_then_fallback(_FakeCtx(), deadline)
    assert len(primary_calls) == 1
    assert len(fallback_calls) == 0

    # Trip the FSM with a CONNECTION_ERROR — primary should be skipped
    cg.fsm.record_primary_failure(mode=FailureMode.CONNECTION_ERROR)
    await cg._try_primary_then_fallback(_FakeCtx(), deadline)
    assert len(primary_calls) == 1, (
        "primary was called despite FSM being in backoff"
    )
    assert len(fallback_calls) == 1, (
        "fallback should have been called as the dynamic-fallback path"
    )


@pytest.mark.asyncio
async def test_dispatch_attempts_primary_when_healthy():
    """When FSM is in PRIMARY_READY, normal behavior preserved —
    primary is attempted first."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator, FailbackStateMachine,
    )
    from datetime import datetime, timedelta, timezone

    cg = CandidateGenerator.__new__(CandidateGenerator)
    cg.fsm = FailbackStateMachine()

    primary_calls = []

    async def stub_primary(ctx, deadline):
        primary_calls.append(ctx)
        class _R:
            pass
        return _R()

    async def stub_fallback(ctx, deadline):
        raise AssertionError("fallback called when primary should be tried")

    cg._call_primary = stub_primary  # type: ignore[method-assign]
    cg._call_fallback = stub_fallback  # type: ignore[method-assign]

    class _FakeCtx:
        op_id = "op-test-002"
        provider_route = "immediate"

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
    await cg._try_primary_then_fallback(_FakeCtx(), deadline)
    assert len(primary_calls) == 1


# -----------------------------------------------------------------------
# § D — Authority invariant
# -----------------------------------------------------------------------


def test_authority_invariant_no_orchestrator_imports():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "providers", "doubleword_provider", "policy",
    )
    for tok in forbidden:
        # candidate_generator import is allowed; others banned
        assert (
            f"from backend.core.ouroboros.governance.{tok} " not in src
        ), f"forbidden: {tok}"
