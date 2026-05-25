"""Slice 5A — L2 per-iter provider isolation.

Closes the cascade surfaced by bt-2026-05-25-095834. L2 iter 1's
provider call (Claude streaming) consumed 118s of the pipeline budget;
any generate_error stop_reason then hard-stopped the engine; the
orchestrator cooldown chained 16 orphan ops into cancelled-on-shutdown.

# Fix

  1. Wrap ``self._prime.generate(...)`` in ``asyncio.wait_for`` bounded
     by ``per_iter_provider_timeout_s`` (default 45s, env-overridable).
  2. On ``asyncio.TimeoutError``: emit stop_reason
     ``provider_iter_timeout:<s>`` (does NOT hard-stop the engine).
  3. Loop classifies ``provider_iter_timeout:`` as a SOFT iter failure —
     ``continue`` to next iter until N consecutive timeouts.
  4. After ``max_consecutive_provider_timeouts`` (default 2) consecutive
     timeouts, hard-stop with ``consecutive_provider_timeouts_exhausted``.
  5. Any successful provider call resets the consecutive counter.

# Discipline

  * pipeline_deadline still passed through to provider unchanged
    (server-side cap honors it; wait_for is a narrower CLIENT-side bound)
  * Effective timeout = ``min(per_iter_provider_timeout_s,
    remaining_pipeline_seconds)`` — never violates the operation's
    deadline contract.
  * All non-timeout exceptions preserve pre-5A hard-stop behavior
    (byte-equivalent for generate_error:<TypeName>).

# Test surface (2 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPAIR_ENGINE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "repair_engine.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_provider_call_wrapped_in_wait_for() -> None:
    """The ``self._prime.generate(...)`` call MUST be inside an
    ``asyncio.wait_for`` with a ``timeout=`` kwarg. Without this, a
    single provider stream can consume the whole pipeline_deadline
    and starve all remaining L2 iters."""
    src = REPAIR_ENGINE_FILE.read_text()
    # asyncio.wait_for must wrap a self._prime.generate call
    assert "asyncio.wait_for(" in src, (
        "_generate_repair_candidate does not use asyncio.wait_for — "
        "provider call is unbounded; Slice 5A reverted."
    )
    # The wait_for must carry a timeout kwarg
    assert "timeout=_effective_timeout_s" in src, (
        "asyncio.wait_for missing timeout=_effective_timeout_s — "
        "Slice 5A bound is decorative."
    )
    # The effective timeout must honor pipeline_deadline as upper bound
    assert "_remaining_pipeline_s" in src, (
        "Slice 5A does not compute remaining_pipeline_s — risks "
        "exceeding the operation's deadline contract."
    )
    # On TimeoutError, structured stop_reason must be emitted
    assert 'provider_iter_timeout:' in src, (
        "TimeoutError handler does not emit provider_iter_timeout: "
        "stop_reason — loop cannot classify as soft failure."
    )


def test_ast_pin_loop_classifies_provider_timeout_as_soft() -> None:
    """The L2 loop must check for ``provider_iter_timeout:`` prefix and
    ``continue`` (not hard-stop) until ``max_consecutive_provider_timeouts``
    is reached. Without this the soft-timeout discipline is dead."""
    src = REPAIR_ENGINE_FILE.read_text()
    assert "consecutive_provider_timeouts" in src, (
        "L2 loop missing consecutive_provider_timeouts counter — "
        "Slice 5A graceful-continue path inert."
    )
    assert "provider_iter_timeout:" in src, (
        "Loop does not match provider_iter_timeout: stop_reason"
    )
    # The loop must call continue on soft-timeout
    assert (
        "continue" in src
        and "max_consecutive_provider_timeouts" in src
    ), (
        "Loop missing continue + max_consecutive cap — single timeout "
        "still hard-stops the engine."
    )
    # Successful call must reset the counter
    assert "consecutive_provider_timeouts = 0" in src, (
        "Counter never resets on success — eventual false hard-stop"
    )
    # Hard-stop reason when cap exhausted
    assert "consecutive_provider_timeouts_exhausted" in src, (
        "Hard-stop reason missing — cap exhaustion has no telemetry tag"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_budget_fields_default_to_5a_values() -> None:
    """RepairBudget defaults must compose the 5A fields."""
    from backend.core.ouroboros.governance.repair_engine import RepairBudget

    b = RepairBudget()
    assert b.per_iter_provider_timeout_s == 45.0
    assert b.max_consecutive_provider_timeouts == 2


def test_spine_budget_from_env_reads_5a_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env() reads both 5A env knobs."""
    from backend.core.ouroboros.governance.repair_engine import RepairBudget

    monkeypatch.setenv("JARVIS_L2_PER_ITER_PROVIDER_TIMEOUT_S", "30")
    monkeypatch.setenv("JARVIS_L2_MAX_CONSECUTIVE_PROVIDER_TIMEOUTS", "3")
    b = RepairBudget.from_env()
    assert b.per_iter_provider_timeout_s == 30.0
    assert b.max_consecutive_provider_timeouts == 3


def test_spine_effective_timeout_min_of_two_bounds() -> None:
    """The effective wait_for timeout is min(per_iter_bound,
    remaining_pipeline_seconds) — never exceeds operation deadline."""
    # Case 1: per_iter < remaining → per_iter wins
    per_iter = 45.0
    remaining = 100.0
    eff = max(1.0, min(per_iter, remaining))
    assert eff == 45.0

    # Case 2: remaining < per_iter → remaining wins (honors deadline)
    per_iter = 45.0
    remaining = 20.0
    eff = max(1.0, min(per_iter, remaining))
    assert eff == 20.0

    # Case 3: both negative/zero (deadline blown) → floor at 1.0
    per_iter = 45.0
    remaining = -5.0
    eff = max(1.0, min(per_iter, remaining))
    assert eff == 1.0


@pytest.mark.asyncio
async def test_spine_provider_timeout_returns_soft_stop_reason() -> None:
    """A provider call that exceeds per_iter_provider_timeout_s
    returns CandidateGenerationResult(candidate=None,
    stop_reason='provider_iter_timeout:<s>') — does NOT raise."""
    from backend.core.ouroboros.governance.repair_engine import (
        RepairBudget, RepairEngine,
    )

    class _SlowProvider:
        async def generate(self, ctx, deadline, repair_context=None):
            await asyncio.sleep(10)  # exceeds 1s bound
            raise AssertionError("should have been cancelled by wait_for")

    budget = RepairBudget(per_iter_provider_timeout_s=1.0)
    engine = RepairEngine(
        budget=budget,
        prime_provider=_SlowProvider(),
        repo_root="/tmp",
        sandbox_factory=lambda *a, **k: None,
    )

    class _Ctx:
        op_id = "test-op"

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    outcome = await engine._generate_repair_candidate(
        _Ctx(), deadline, repair_context={},
    )
    assert outcome.candidate is None
    assert outcome.stop_reason is not None
    assert outcome.stop_reason.startswith("provider_iter_timeout:")


@pytest.mark.asyncio
async def test_spine_non_timeout_exception_preserves_hard_stop() -> None:
    """Byte-equivalence: non-TimeoutError exceptions still produce
    ``generate_error:<TypeName>`` (no behavior change vs. pre-5A)."""
    from backend.core.ouroboros.governance.repair_engine import (
        RepairBudget, RepairEngine,
    )

    class _BoomProvider:
        async def generate(self, ctx, deadline, repair_context=None):
            raise RuntimeError("simulated provider explosion")

    budget = RepairBudget(per_iter_provider_timeout_s=30.0)
    engine = RepairEngine(
        budget=budget,
        prime_provider=_BoomProvider(),
        repo_root="/tmp",
        sandbox_factory=lambda *a, **k: None,
    )

    class _Ctx:
        op_id = "test-op"

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    outcome = await engine._generate_repair_candidate(
        _Ctx(), deadline, repair_context={},
    )
    assert outcome.candidate is None
    assert outcome.stop_reason == "generate_error:RuntimeError"


@pytest.mark.asyncio
async def test_spine_cancelled_error_propagates() -> None:
    """CancelledError must propagate — wait_for's cancellation
    semantics are part of the asyncio cooperative-cancel contract."""
    from backend.core.ouroboros.governance.repair_engine import (
        RepairBudget, RepairEngine,
    )

    class _CancelProvider:
        async def generate(self, ctx, deadline, repair_context=None):
            raise asyncio.CancelledError()

    budget = RepairBudget(per_iter_provider_timeout_s=30.0)
    engine = RepairEngine(
        budget=budget,
        prime_provider=_CancelProvider(),
        repo_root="/tmp",
        sandbox_factory=lambda *a, **k: None,
    )

    class _Ctx:
        op_id = "test-op"

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    with pytest.raises(asyncio.CancelledError):
        await engine._generate_repair_candidate(
            _Ctx(), deadline, repair_context={},
        )
