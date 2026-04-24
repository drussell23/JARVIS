"""Tier 3 Reflex tests — PLAN phase budget caps (item B from F1 Slice 4 S5 triage).

Scope: Manifesto §5 Tier 3 enforcement extended to PLAN phase added 2026-04-24
after F1 Slice 4 S5 (bt-2026-04-24-220418) exposed a 227.2s PLAN-phase stall.
The seed op `op-019dc186-3ae2` consumed 227s in `CandidateGenerator.plan()`
(DW Connection error → Claude fallback ConnectTimeout chain) before falling
through to GENERATE without plan — and the BG worker pool ceiling (360s)
killed the op before GENERATE could run.

Pinned contract:

1. PLAN primary path applies `_TIER3_REFLEX_HARD_CAP_S` (default 30s) the same
   way GENERATE Tier-0 does.
2. PLAN fallback path applies a tighter cap `_PLAN_FALLBACK_MAX_TIMEOUT_S`
   (default 60s, half of GENERATE's 120s) — PLAN's structured plan.1 JSON
   is short, doesn't need the full Claude reserve.
3. Cap binds only when remaining > cap (preserves tight-budget invariants).
4. The cap is observable via `[CandidateGenerator] Plan Tier3_cap_active`
   INFO log when it binds.
5. Both caps are env-tunable via `OUROBOROS_TIER3_REFLEX_HARD_CAP_S` and
   `OUROBOROS_PLAN_FALLBACK_MAX_TIMEOUT_S`.

Implementation note: tests measure caps via the `deadline_arg` passed into
the mock provider's `plan()` (deadline-based) and via captured timeouts on
`asyncio.wait_for` patched through pytest's `monkeypatch` fixture, which
guarantees teardown even under cross-test pollution from earlier suites.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.core.ouroboros.governance.candidate_generator as cgmod
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
    _PLAN_FALLBACK_MAX_TIMEOUT_S,
    _TIER3_REFLEX_HARD_CAP_S,
)


# ---------------------------------------------------------------------------
# (1) Module-level defaults
# ---------------------------------------------------------------------------


def test_plan_primary_cap_reuses_tier3_default_30s():
    """PLAN primary path reuses the GENERATE Tier 3 cap (30s default)."""
    assert _TIER3_REFLEX_HARD_CAP_S == 30.0


def test_plan_fallback_cap_default_is_60s():
    """PLAN fallback cap is half the GENERATE fallback cap (120s)."""
    assert _PLAN_FALLBACK_MAX_TIMEOUT_S == 60.0


def test_plan_fallback_cap_tighter_than_generate_fallback():
    """PLAN fallback cap MUST be tighter than the GENERATE fallback cap."""
    generate_fallback_cap = CandidateGenerator._FALLBACK_MAX_TIMEOUT_S
    assert _PLAN_FALLBACK_MAX_TIMEOUT_S < generate_fallback_cap


# ---------------------------------------------------------------------------
# (2) Cap binding via deadline introspection (pollution-safe)
#
# We capture the deadline_arg passed to the mock provider's plan(); the
# distance from now() to that deadline is the upper bound on the timeout
# the wait_for actually used. No asyncio.wait_for patching needed.
# ---------------------------------------------------------------------------


def _make_cg(
    primary_plan: AsyncMock | None = None,
    fallback_plan: AsyncMock | None = None,
) -> CandidateGenerator:
    """Build a CandidateGenerator with MagicMock parents (proven test pattern).

    Parent must be MagicMock not AsyncMock — assigning `.plan = AsyncMock(...)`
    on an AsyncMock parent has subtle child-mock-shadowing behavior that
    breaks side_effect propagation when called via the wrapped attribute.
    The same pattern is used by test_candidate_generator.py (lines 685-720).
    """
    primary = MagicMock()
    primary.provider_name = "primary"
    primary.plan = primary_plan if primary_plan is not None else AsyncMock(return_value="ok")
    primary.generate = AsyncMock()
    primary.health_probe = AsyncMock(return_value=True)

    fallback = MagicMock()
    fallback.provider_name = "fallback"
    fallback.plan = fallback_plan if fallback_plan is not None else AsyncMock(return_value="ok")
    fallback.generate = AsyncMock()
    fallback.health_probe = AsyncMock(return_value=True)

    return CandidateGenerator(primary=primary, fallback=fallback)


def _make_recording_plan(
    record: List[Tuple[str, datetime]], result: str = "ok"
) -> AsyncMock:
    """AsyncMock that records (prompt, deadline) on each call."""
    async def _side(prompt, deadline_arg):
        record.append((prompt, deadline_arg))
        return result
    return AsyncMock(side_effect=_side)


def _make_failing_plan(
    record: List[Tuple[str, datetime]], exc: Exception
) -> AsyncMock:
    async def _side(prompt, deadline_arg):
        record.append((prompt, deadline_arg))
        raise exc
    return AsyncMock(side_effect=_side)


@pytest.mark.asyncio
async def test_plan_primary_cap_binds_via_wait_for_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When remaining > 30s + slack, primary timeout = 30s and we log it.

    Spies on asyncio.wait_for via monkeypatch (auto-restored on teardown,
    pollution-proof against earlier suites).
    """
    primary_calls: List[Tuple[str, datetime]] = []
    cg = _make_cg(primary_plan=_make_recording_plan(primary_calls))
    cg.fsm._state = FailbackState.PRIMARY_READY

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=120)

    captured: List[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(cgmod.asyncio, "wait_for", _spy)
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.candidate_generator",
    )

    result = await cg.plan("test prompt", deadline)

    assert result == "ok"
    assert len(primary_calls) == 1, "primary path must have run"
    assert len(captured) == 1
    assert captured[0] == pytest.approx(30.0, abs=0.5)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("Plan Tier3_cap_active" in m for m in msgs), \
        f"expected Tier3_cap_active log, got: {msgs}"


@pytest.mark.asyncio
async def test_plan_primary_cap_does_not_bind_when_remaining_below_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remaining ≤ 30s → primary gets raw remaining, not 30s cap."""
    primary_calls: List[Tuple[str, datetime]] = []
    cg = _make_cg(primary_plan=_make_recording_plan(primary_calls))
    cg.fsm._state = FailbackState.PRIMARY_READY

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=15)

    captured: List[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(cgmod.asyncio, "wait_for", _spy)

    await cg.plan("test prompt", deadline)

    assert len(primary_calls) == 1
    assert len(captured) == 1
    assert 0 < captured[0] <= 15.5


@pytest.mark.asyncio
async def test_plan_fallback_cap_binds_at_60s_when_state_is_fallback_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FALLBACK_ACTIVE state: fallback path uses _PLAN_FALLBACK_MAX_TIMEOUT_S=60s."""
    fallback_calls: List[Tuple[str, datetime]] = []
    cg = _make_cg(fallback_plan=_make_recording_plan(fallback_calls))
    cg.fsm.record_primary_failure()
    assert cg.fsm.state is FailbackState.FALLBACK_ACTIVE

    # 200s remaining — without PLAN cap would be min(200, 90 floor, 120 cap)=120.
    # With PLAN cap, must be 60s.
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=200)

    captured: List[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(cgmod.asyncio, "wait_for", _spy)

    result = await cg.plan("test prompt", deadline)

    assert result == "ok"
    assert len(fallback_calls) == 1, "fallback path must have run"
    assert len(captured) == 1
    assert captured[0] == pytest.approx(60.0, abs=0.5)


@pytest.mark.asyncio
async def test_plan_fallback_after_primary_failure_also_capped_at_60s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary fails → fallback path STILL hits the 60s PLAN cap (not 120s)."""
    primary_calls: List[Tuple[str, datetime]] = []
    fallback_calls: List[Tuple[str, datetime]] = []
    cg = _make_cg(
        primary_plan=_make_failing_plan(primary_calls, ConnectionError("DW down")),
        fallback_plan=_make_recording_plan(fallback_calls),
    )
    cg.fsm._state = FailbackState.PRIMARY_READY

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=200)

    captured: List[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(cgmod.asyncio, "wait_for", _spy)

    result = await cg.plan("test prompt", deadline)

    assert result == "ok"
    assert len(primary_calls) == 1, "primary must have been attempted"
    assert len(fallback_calls) == 1, "fallback must have been attempted"
    assert len(captured) == 2
    assert captured[0] == pytest.approx(30.0, abs=0.5)
    assert captured[1] == pytest.approx(60.0, abs=0.5)


# ---------------------------------------------------------------------------
# (3) S5 scenario regression — exact reproduction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_scenario_post_patch_capped_at_90s_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin for F1 Slice 4 S5 (bt-2026-04-24-220418).

    Pre-patch: PLAN burned 227s (DW raw remaining ≈90s + Claude raw 120s).
    Post-patch: PLAN burns at most 30s (DW Tier 3) + 60s (Claude PLAN cap)
    = 90s worst case, leaving the BG worker pool ceiling (360s) with at
    least 270s for GENERATE.
    """
    primary_calls: List[Tuple[str, datetime]] = []
    fallback_calls: List[Tuple[str, datetime]] = []
    cg = _make_cg(
        primary_plan=_make_failing_plan(
            primary_calls, ConnectionError("DW Connection error")
        ),
        fallback_plan=_make_failing_plan(
            fallback_calls, ConnectionError("Claude ConnectTimeout")
        ),
    )
    cg.fsm._state = FailbackState.PRIMARY_READY

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=360)

    captured: List[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(cgmod.asyncio, "wait_for", _spy)

    with pytest.raises(ConnectionError):
        await cg.plan("test prompt", deadline)

    assert len(captured) == 2, f"expected 2 wait_for calls, got {len(captured)}"
    assert captured[0] == pytest.approx(30.0, abs=0.5)
    assert captured[1] == pytest.approx(60.0, abs=0.5)
    assert sum(captured) <= 90.5, (
        f"PLAN worst-case budget {sum(captured)}s exceeds 90s S5 contract"
    )


# ---------------------------------------------------------------------------
# (4) Source-grep pins — patch survives drift
# ---------------------------------------------------------------------------


def test_plan_primary_path_passes_capped_budget_not_raw_remaining():
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()
    assert "primary_budget = min(remaining, _TIER3_REFLEX_HARD_CAP_S)" in src
    assert "timeout=primary_budget," in src


def test_plan_fallback_path_uses_plan_specific_cap_constant():
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()
    assert "remaining = min(_budget_target, _PLAN_FALLBACK_MAX_TIMEOUT_S)" in src


def test_plan_fallback_cap_env_override_documented():
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()
    assert "OUROBOROS_PLAN_FALLBACK_MAX_TIMEOUT_S" in src
