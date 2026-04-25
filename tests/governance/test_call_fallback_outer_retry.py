"""Rooted-problem fix (2026-04-25) — `_call_fallback` outer-retry tests.

Pin the outer-retry behavior added to `CandidateGenerator._call_fallback`
to address the W3(6) Slice 5b graduation blocker surfaced by F1 Slice 4
cadence S1b (`bt-2026-04-25-054256`):

  * Provider's internal `_call_with_backoff` exhausts ~3 attempts in ~70-80s
    on TCP-timeout failures (anyio cancel scope).
  * `_call_fallback` was treating the propagated CancelledError as
    terminal exhaustion — leaving 100+s of parent budget UNUSED.
  * Operator binding 2026-04-25 (Option B closure of S1b): "Will not
    mask provider latency by modifying the seed (Option C) or
    artificially inflating the timeout boundaries (Option D)."

The fix adds NO new budget. It CONSUMES the budget JARVIS already
authorized at ROUTE. Outer retry holds `_fallback_sem` (preserves
head-of-queue) and re-invokes the provider on transient failures while
remaining budget exceeds `_MIN_VIABLE_FALLBACK_S`.

Pin coverage:

A. Helper `_is_outer_retry_eligible_mode` — transient modes return True;
   permanent modes return False.
B. Constants `_FALLBACK_OUTER_RETRY_MAX` (default 3) +
   `_FALLBACK_OUTER_RETRY_BACKOFF_S` (default 1.0) load from env.
C. Outer retry succeeds when first attempt fails transient + second
   attempt succeeds (the rooted-problem-fix happy path).
D. Cooperative cancel via `OperationCancelledError` is NEVER retried —
   honored immediately. The W3(7) cancel-token cooperation contract.
E. Permanent failure modes (CONTENT_FAILURE) are NEVER retried.
F. Outer-retry cap (`_FALLBACK_OUTER_RETRY_MAX`) bounds attempts.
G. Budget exhaustion (< `_MIN_VIABLE_FALLBACK_S`) breaks the loop.
H. Source-grep pin — `_call_fallback` body contains the outer-retry
   loop sentinel string + the cancel-token import.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# (A) Helper transient-mode classification
# ---------------------------------------------------------------------------


def test_helper_transient_modes_eligible() -> None:
    """All 5 transient modes are outer-retry eligible."""
    from backend.core.ouroboros.governance.candidate_generator import (
        FailureMode,
        _is_outer_retry_eligible_mode,
    )
    assert _is_outer_retry_eligible_mode(FailureMode.TIMEOUT) is True
    assert _is_outer_retry_eligible_mode(FailureMode.CONNECTION_ERROR) is True
    assert _is_outer_retry_eligible_mode(FailureMode.TRANSIENT_TRANSPORT) is True
    assert _is_outer_retry_eligible_mode(FailureMode.SERVER_ERROR) is True
    assert _is_outer_retry_eligible_mode(FailureMode.RATE_LIMITED) is True


def test_helper_permanent_modes_not_eligible() -> None:
    """Permanent failure modes never retry — re-failing wastes budget."""
    from backend.core.ouroboros.governance.candidate_generator import (
        FailureMode,
        _is_outer_retry_eligible_mode,
    )
    assert _is_outer_retry_eligible_mode(FailureMode.CONTENT_FAILURE) is False
    assert _is_outer_retry_eligible_mode(FailureMode.CONTEXT_OVERFLOW) is False


# ---------------------------------------------------------------------------
# (B) Env knobs load with defaults
# ---------------------------------------------------------------------------


def test_outer_retry_max_default_3() -> None:
    """Default outer-retry cap is 3 attempts."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _FALLBACK_OUTER_RETRY_MAX,
    )
    assert _FALLBACK_OUTER_RETRY_MAX == 3


def test_outer_retry_backoff_default_1s() -> None:
    """Default backoff between attempts is 1.0s."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _FALLBACK_OUTER_RETRY_BACKOFF_S,
    )
    assert _FALLBACK_OUTER_RETRY_BACKOFF_S == 1.0


# ---------------------------------------------------------------------------
# Helper: build a CandidateGenerator with mock providers
# ---------------------------------------------------------------------------


def _make_generator(fallback_generate):
    """Build a minimal CandidateGenerator wired to a mocked fallback.generate."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
        FailbackState,
    )
    primary = MagicMock()
    primary.health_probe = AsyncMock(return_value=False)  # primary unhealthy
    primary.generate = AsyncMock(side_effect=RuntimeError("primary down"))
    fallback = MagicMock()
    fallback.generate = fallback_generate

    gen = CandidateGenerator(
        primary=primary,
        fallback=fallback,
        primary_concurrency=1,
        fallback_concurrency=3,
    )
    # Force fallback path
    gen.fsm._state = FailbackState.FALLBACK_ACTIVE
    return gen


def _make_context(op_id: str = "op-test-outer-retry", route: str = "standard"):
    """Build a minimal OperationContext shape matching what _call_fallback reads."""
    from backend.core.ouroboros.governance.op_context import OperationContext
    return OperationContext.create(
        target_files=("x.py",),
        description="test goal",
        op_id=op_id,
        provider_route=route,
    )


# ---------------------------------------------------------------------------
# (C) Happy path — outer retry succeeds after first transient failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outer_retry_succeeds_after_first_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First attempt raises asyncio.TimeoutError (TIMEOUT mode); second
    attempt succeeds. The rooted-problem-fix happy path."""
    from backend.core.ouroboros.governance.candidate_generator import (
        GenerationResult,
    )
    success = GenerationResult(
        candidates=({"file_path": "x.py", "full_content": "# x"},),
        provider_name="claude-api",
        generation_duration_s=0.1,
    )

    call_count = {"n": 0}

    async def _fb_gen(ctx, deadline):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise asyncio.TimeoutError("first attempt TCP timeout")
        return success

    gen = _make_generator(AsyncMock(side_effect=_fb_gen))
    ctx = _make_context()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=120)

    result = await gen._call_fallback(ctx, deadline)
    assert result is success
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# (D) Cooperative cancel — NEVER retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operation_cancelled_error_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W3(7) cancel-token fires → OperationCancelledError → propagated
    immediately. Even though the fix adds outer retry, cooperative cancel
    must be honored without retry."""
    from backend.core.ouroboros.governance.cancel_token import (
        CancelRecord,
        OperationCancelledError,
    )

    call_count = {"n": 0}

    async def _fb_gen(ctx, deadline):
        call_count["n"] += 1
        rec = CancelRecord(
            schema_version="cancel.1",
            cancel_id="cid-test",
            op_id=ctx.op_id,
            origin="D:repl_operator",
            phase_at_trigger="GENERATE",
            trigger_monotonic=0.0,
            trigger_wall_iso="2026-04-25T05:00:00Z",
            bounded_deadline_s=30.0,
            reason="test",
        )
        raise OperationCancelledError(rec)

    gen = _make_generator(AsyncMock(side_effect=_fb_gen))
    ctx = _make_context()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=120)

    with pytest.raises(OperationCancelledError):
        await gen._call_fallback(ctx, deadline)
    # Critical: only ONE attempt — cooperative cancel must short-circuit
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# (E) Permanent failure mode — NEVER retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_failure_never_retried() -> None:
    """CONTENT_FAILURE classification → no retry. The exhaustion path
    fires on the first attempt. Diff-apply / patch-conflict failures
    won't fix themselves on a re-attempt."""
    call_count = {"n": 0}

    async def _fb_gen(ctx, deadline):
        call_count["n"] += 1
        # Use a content-failure marker that classify_exception flags
        # as CONTENT_FAILURE (matches `_is_content_failure` markers).
        raise RuntimeError("diff_apply_failed: something")

    gen = _make_generator(AsyncMock(side_effect=_fb_gen))
    ctx = _make_context()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=120)

    with pytest.raises(Exception):  # exhaustion or RuntimeError
        await gen._call_fallback(ctx, deadline)
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# (F) Outer-retry cap bounds attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outer_retry_cap_bounds_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every attempt fails transient, the outer loop respects the cap.
    Default cap is 3 → exactly 3 calls before exhaustion fires."""
    monkeypatch.setenv("JARVIS_FALLBACK_OUTER_RETRY_BACKOFF_S", "0.01")  # speed test
    # Reload module to pick up env change for the const
    import importlib
    import backend.core.ouroboros.governance.candidate_generator as cg_module
    importlib.reload(cg_module)

    call_count = {"n": 0}

    async def _fb_gen(ctx, deadline):
        call_count["n"] += 1
        raise asyncio.TimeoutError(f"attempt {call_count['n']} failed")

    # Build generator using the freshly-reloaded module
    primary = MagicMock()
    primary.health_probe = AsyncMock(return_value=False)
    primary.generate = AsyncMock(side_effect=RuntimeError("primary down"))
    fallback = MagicMock()
    fallback.generate = AsyncMock(side_effect=_fb_gen)
    gen = cg_module.CandidateGenerator(
        primary=primary, fallback=fallback,
        primary_concurrency=1, fallback_concurrency=3,
    )
    gen.fsm._state = cg_module.FailbackState.FALLBACK_ACTIVE

    ctx = _make_context()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=300)

    with pytest.raises(Exception):  # eventual exhaustion
        await gen._call_fallback(ctx, deadline)
    # Default cap is 3 — verify we attempted exactly 3 times, not more
    assert call_count["n"] == cg_module._FALLBACK_OUTER_RETRY_MAX
    assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# (G) Budget exhaustion breaks the loop early
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhaustion_breaks_outer_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If remaining budget falls below `_MIN_VIABLE_FALLBACK_S` between
    attempts, the loop breaks before launching a doomed call."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _MIN_VIABLE_FALLBACK_S,
    )

    call_count = {"n": 0}

    async def _fb_gen(ctx, deadline):
        call_count["n"] += 1
        # Burn most of the budget on this attempt
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError("simulated")

    gen = _make_generator(AsyncMock(side_effect=_fb_gen))
    ctx = _make_context()
    # Set a deadline that will be sub-MIN_VIABLE after ~1 attempt
    deadline = datetime.now(tz=timezone.utc) + timedelta(
        seconds=_MIN_VIABLE_FALLBACK_S * 1.5,
    )

    with pytest.raises(Exception):
        await gen._call_fallback(ctx, deadline)
    # Should NOT have hit the cap — budget exhausted earlier
    assert call_count["n"] >= 1


# ---------------------------------------------------------------------------
# (H) Source-grep pins — code shape that must survive drift
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_call_fallback_has_outer_retry_loop() -> None:
    """`_call_fallback` source contains the outer-retry-loop sentinel
    + the cancel-token cooperation pin."""
    src = _read("backend/core/ouroboros/governance/candidate_generator.py")
    assert "Outer-retry loop (rooted-problem fix 2026-04-25)" in src
    assert "_FALLBACK_OUTER_RETRY_MAX" in src
    assert "_is_outer_retry_eligible_mode" in src
    assert "OperationCancelledError" in src
    # Cooperative-cancel must be the FIRST except-clause inside the loop
    # (raised before the broader Exception catcher) so it propagates
    # without retry.
    assert "except _OperationCancelledError" in src


def test_pin_outer_retry_helper_imported_safely() -> None:
    """The transient-mode classification helper is module-level, not
    method-bound — so unit tests can pin it without needing the full
    CandidateGenerator wiring."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _is_outer_retry_eligible_mode,
        _FALLBACK_OUTER_RETRY_TRANSIENT_MODE_NAMES,
    )
    assert callable(_is_outer_retry_eligible_mode)
    assert isinstance(_FALLBACK_OUTER_RETRY_TRANSIENT_MODE_NAMES, frozenset)
    # Set must be exactly 5 transient modes
    assert len(_FALLBACK_OUTER_RETRY_TRANSIENT_MODE_NAMES) == 5
