"""Sovereign Epistemic Context Matrix — FIX 1 regression guard.

The Information-Gain Governor's LR3 deadlock-override failure
(``GovernanceDeadlockError``, raised inside the Venom tool loop) MUST
reach the orchestrator's ``except GovernanceDeadlockError`` terminal catch
so it stamps ``terminal_reason_code="deadlock_override_failed"``.

Before this fix, every broad-catch failback site in ``candidate_generator``
swallowed it:

  * ``_try_primary_then_fallback`` reclassified the primary failure and
    cascaded into the Claude fallback.
  * ``_call_fallback`` (inner retry + outer catch) folded it into the
    ``all_providers_exhausted`` taxonomy.
  * ``_dispatch_via_sentinel``'s per-model rotation loop stored it in
    ``_attempt_exc`` and re-drove the cascade (the deadlock never carried an
    ``exhaustion_report`` so the "re-raise if instrumented" branch missed it,
    and its message matched none of the GENERATION_TIMEOUT / FSM_EXHAUSTED /
    INTERNAL_FAULT markers).

These tests pin that a ``GovernanceDeadlockError`` PROPAGATES UNCONVERTED
through the failback machinery rather than being reclassified.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance import candidate_generator as cgmod
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.tool_executor import GovernanceDeadlockError


def _make_generator(fallback_generate=None):
    primary = MagicMock()
    primary.health_probe = AsyncMock(return_value=False)
    primary.generate = AsyncMock(side_effect=RuntimeError("primary down"))
    fallback = MagicMock()
    fallback.generate = fallback_generate or AsyncMock(
        side_effect=RuntimeError("fallback should not be reached"),
    )
    gen = CandidateGenerator(
        primary=primary,
        fallback=fallback,
        primary_concurrency=1,
        fallback_concurrency=3,
    )
    gen.fsm._state = FailbackState.FALLBACK_ACTIVE
    return gen


def _make_context(op_id: str = "op-deadlock-test", route: str = "standard"):
    return OperationContext.create(
        target_files=("x.py",),
        description="test goal",
        op_id=op_id,
        provider_route=route,
    )


# ---------------------------------------------------------------------------
# Behavioral: the symbol is shared (same class object) so the orchestrator's
# except-clause WILL catch what candidate_generator re-raises.
# ---------------------------------------------------------------------------


def test_candidate_generator_uses_real_deadlock_class():
    """candidate_generator's GovernanceDeadlockError must BE the tool_executor
    class (not the fallback shim) so the orchestrator's except clause catches
    instances re-raised here."""
    assert cgmod.GovernanceDeadlockError is GovernanceDeadlockError
    assert issubclass(cgmod.GovernanceDeadlockError, RuntimeError)


# ---------------------------------------------------------------------------
# Behavioral Site B — _try_primary_then_fallback must NOT cascade a deadlock
# into the Claude fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_primary_then_fallback_propagates_deadlock(monkeypatch):
    gen = _make_generator()
    ctx = _make_context()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=120)

    async def _primary_raises(context, deadline, *, model_id=""):
        raise GovernanceDeadlockError("deadlock_override_failed")

    fallback_called = {"n": 0}

    async def _fallback_sentinel(context, deadline):
        fallback_called["n"] += 1
        from backend.core.ouroboros.governance.candidate_generator import (
            GenerationResult,
        )
        return GenerationResult(
            candidates=(), provider_name="claude-api", generation_duration_s=0.0,
        )

    monkeypatch.setattr(gen, "_call_primary", _primary_raises)
    monkeypatch.setattr(gen, "_call_fallback", _fallback_sentinel)

    with pytest.raises(GovernanceDeadlockError):
        await gen._try_primary_then_fallback(ctx, deadline)
    # The cascade to fallback MUST NOT have fired — a deadlock is terminal.
    assert fallback_called["n"] == 0


# ---------------------------------------------------------------------------
# Behavioral Site C/D — _call_fallback must NOT convert a deadlock into the
# all_providers_exhausted taxonomy.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_fallback_propagates_deadlock():
    async def _fb_gen(ctx, deadline):
        raise GovernanceDeadlockError("deadlock_override_failed")

    gen = _make_generator(AsyncMock(side_effect=_fb_gen))
    ctx = _make_context()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=120)

    with pytest.raises(GovernanceDeadlockError):
        await gen._call_fallback(ctx, deadline)


# ---------------------------------------------------------------------------
# Structural — every named broad-catch site has the re-raise guard, and the
# specific clause precedes the broad clause.
# ---------------------------------------------------------------------------


def test_candidate_generator_reraises_governance_deadlock_structural():
    src = inspect.getsource(cgmod)
    assert "GovernanceDeadlockError" in src
    assert "except GovernanceDeadlockError" in src
    # At least the 6 reachable failback sites (rotation, primary cascade,
    # fallback inner + outer, + 3 background wraps + topology). Count >= 6.
    assert src.count("except GovernanceDeadlockError") >= 6


def test_deadlock_guard_precedes_broad_catch_in_try_primary():
    """In _try_primary_then_fallback the specific clause must come BEFORE the
    broad ``except (Exception, asyncio.CancelledError)`` (Python evaluates
    clauses in order)."""
    src = inspect.getsource(cgmod.CandidateGenerator._try_primary_then_fallback)
    i_specific = src.find("except GovernanceDeadlockError")
    i_broad = src.find("except (Exception, asyncio.CancelledError)")
    assert i_specific != -1, "missing deadlock guard in _try_primary_then_fallback"
    assert i_broad != -1, "missing broad catch in _try_primary_then_fallback"
    assert i_specific < i_broad, "deadlock guard must precede the broad catch"


def test_deadlock_guard_precedes_broad_catch_in_call_fallback():
    src = inspect.getsource(cgmod.CandidateGenerator._call_fallback)
    # inner retry guard + outer guard both present
    assert src.count("except GovernanceDeadlockError") >= 2
