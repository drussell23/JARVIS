"""Temporal Veto (Iron Gate) + Fast-Fail Load Shed.

The temporal impedance mismatch: RepairEngine enforces a 45s per-iter
client-side ``wait_for`` while the Slice-36 ladder could still elect the
op onto the BATCH plane — the caller kills the await at its bound, the
batch keeps cooking (billed), the iteration dies as provider_iter_timeout.

These tests pin the three-layer defense:
  1. predicate veto (deadline-aware top rung in _slice36_should_force_batch)
  2. fast-fail load shed (veto + RT-unusable → typed error in milliseconds)
  3. terminal chokepoint (the ONE _generate_via_batch entry refuses
     deadline-bound ops — covers rupture/429/503 fallthroughs + RT-disabled)
plus the FSM taxonomy (TEMPORAL_SHED: zero DW penalty, zero retry,
immediate Claude cascade) and the RepairEngine truthful-deadline fix.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import doubleword_provider as dwp
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackStateMachine,
    FailureMode,
    _is_outer_retry_eligible_mode,
)


def _ctx(route: str = "complex", **kw) -> SimpleNamespace:
    base = dict(
        op_id="op-veto-test", operation_id="op-veto-test",
        provider_route=route, task_complexity="complex",
        routing=None, cross_repo=False, target_files=(),
        is_read_only=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _deadline_s(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Layer 0 — the deadline math + veto predicate (env contract)
# ---------------------------------------------------------------------------

def test_remaining_budget_shapes():
    assert dwp._dw_remaining_budget_s(None) is None
    r = dwp._dw_remaining_budget_s(_deadline_s(45))
    assert r is not None and 43 < r <= 45.5
    # naive-UTC datetime (legacy callers)
    naive = datetime.utcnow() + timedelta(seconds=100)
    rn = dwp._dw_remaining_budget_s(naive)
    assert rn is not None and 98 < rn <= 100.5
    # duck-typed deadline object
    duck = SimpleNamespace(remaining_s=lambda: 33.0)
    assert dwp._dw_remaining_budget_s(duck) == 33.0
    # unrecognized shape → None, never raises
    assert dwp._dw_remaining_budget_s(object()) is None


def test_ceiling_is_adaptive_not_hardcoded(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_BATCH_SAFE_CEILING_S", raising=False)
    monkeypatch.delenv("JARVIS_DW_BATCH_TTFT_S", raising=False)
    monkeypatch.delenv("JARVIS_DW_BATCH_CEILING_SAFETY_MULT", raising=False)
    # default: floor 50 dominates (8s TTFT est × 3 = 24)
    assert dwp._dw_batch_safe_ceiling_s() == 50.0
    # a slow batch lane WIDENS its own exclusion zone with zero config
    monkeypatch.setenv("JARVIS_DW_BATCH_TTFT_S", "40")
    assert dwp._dw_batch_safe_ceiling_s() == 120.0
    # explicit floor override
    monkeypatch.delenv("JARVIS_DW_BATCH_TTFT_S", raising=False)
    monkeypatch.setenv("JARVIS_DW_BATCH_SAFE_CEILING_S", "75")
    assert dwp._dw_batch_safe_ceiling_s() == 75.0
    # explicit 0 disables the ceiling entirely
    monkeypatch.setenv("JARVIS_DW_BATCH_SAFE_CEILING_S", "0")
    assert dwp._dw_batch_safe_ceiling_s() == 0.0


def test_veto_predicate(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_TEMPORAL_VETO_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_DW_BATCH_SAFE_CEILING_S", raising=False)
    assert dwp._dw_temporal_veto(_ctx(), 45.0) is True       # 45 < 50
    assert dwp._dw_temporal_veto(_ctx(), 300.0) is False     # fat budget
    assert dwp._dw_temporal_veto(_ctx(), None) is False      # unbounded op
    # sla="strict" declares intent WITHOUT a threaded deadline
    assert dwp._dw_temporal_veto(_ctx(sla="strict"), None) is True
    # master off → never veto
    monkeypatch.setenv("JARVIS_DW_TEMPORAL_VETO_ENABLED", "false")
    assert dwp._dw_temporal_veto(_ctx(), 45.0) is False


# ---------------------------------------------------------------------------
# Layer 1 — the predicate veto beats every force-batch rung
# ---------------------------------------------------------------------------

def test_force_batch_vetoed_on_tight_budget(monkeypatch):
    """The legacy pure-DW static rung (Claude-disabled + complex route)
    historically returns True — the veto must beat it."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "1")
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", "false")
    monkeypatch.delenv("JARVIS_DW_TEMPORAL_VETO_ENABLED", raising=False)
    ctx = _ctx("complex")
    # fat budget: the ladder may elect batch (True historically)
    assert dwp._slice36_should_force_batch(
        ctx, model_id="m", remaining_budget_s=600.0) is True
    # tight budget: VETO — deterministic False, no matter the ladder
    assert dwp._slice36_should_force_batch(
        ctx, model_id="m", remaining_budget_s=45.0) is False
    # no budget info (legacy call shape) → byte-identical legacy behavior
    assert dwp._slice36_should_force_batch(ctx, model_id="m") is True


# ---------------------------------------------------------------------------
# Layer 2+3 — THE MANDATE INTEGRATION: 45s deadline, COMPLEX route,
# RT UNAVAILABLE → veto blocks batch, fast-fail shed, budget preserved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_fail_shed_when_rt_profile_unavailable(monkeypatch):
    """realtime_enabled=True but the transport profile marks the model
    RT-unavailable (403 entitlement class): the shed fires BEFORE any
    network attempt — milliseconds, not a 45s zombie await."""
    from backend.core.ouroboros.governance import dw_transport_profile as tp

    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "1")

    class _FakeProfile:
        def is_unavailable(self, model_id, transport):
            return transport == tp.TRANSPORT_REALTIME

    monkeypatch.setattr(tp, "get_transport_profile", lambda: _FakeProfile())

    dw = dwp.DoublewordProvider(
        api_key="test-key", base_url="http://127.0.0.1:9/v1",
        model="test-model", realtime_enabled=True)
    t0 = time.monotonic()
    with pytest.raises(dwp.TemporalBudgetShedError) as ei:
        await dw.generate(_ctx("complex"), _deadline_s(45))
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"shed took {elapsed:.1f}s — not a fast-fail"
    assert "temporal_load_shed" in str(ei.value)
    assert "rt_model_unavailable" in str(ei.value)
    assert isinstance(ei.value, dwp.DoublewordInfraError)  # rides the cascade
    assert ei.value.is_transient() is False                # never DW-retried


@pytest.mark.asyncio
async def test_shed_when_rt_disabled(monkeypatch):
    """realtime_enabled=False: the only DW lane is batch (E path). A 45s
    op must NOT enter the mail slot — the fast-fail layer sheds instantly
    (structurally BEFORE the chokepoint: budget preserved even sooner)."""
    monkeypatch.delenv("JARVIS_DW_TEMPORAL_VETO_ENABLED", raising=False)
    dw = dwp.DoublewordProvider(
        api_key="test-key", base_url="http://127.0.0.1:9/v1",
        model="test-model", realtime_enabled=False)
    t0 = time.monotonic()
    with pytest.raises(dwp.TemporalBudgetShedError) as ei:
        await dw.generate(_ctx("complex"), _deadline_s(45))
    assert time.monotonic() - t0 < 5.0
    assert "rt_disabled" in str(ei.value)


@pytest.mark.asyncio
async def test_chokepoint_sheds_the_503_fallthrough(monkeypatch):
    """The D″ seam: RT was USABLE (no fast-fail) but 503'd mid-op — the
    legacy fallthrough re-submits over BATCH in the same tick. Under the
    veto, the terminal chokepoint refuses the mail slot instead."""
    monkeypatch.delenv("JARVIS_DW_TEMPORAL_VETO_ENABLED", raising=False)
    # isolate from the REAL persisted ledgers on this machine: RT must
    # look fully usable so the fast-fail layer does NOT fire first
    monkeypatch.setattr(dwp, "_dw_streaming_warm_degraded", lambda: False)
    from backend.core.ouroboros.governance import dw_transport_profile as tp
    monkeypatch.setattr(
        tp, "get_transport_profile",
        lambda: SimpleNamespace(is_unavailable=lambda m, t: False))
    dw = dwp.DoublewordProvider(
        api_key="test-key", base_url="http://127.0.0.1:9/v1",
        model="test-model", realtime_enabled=True)

    async def _rt_503(context, deadline, **kw):
        raise dwp.DoublewordInfraError("overloaded", status_code=503)

    batch_entered = {"n": 0}

    async def _batch_spy(context, prompt_override=None):
        batch_entered["n"] += 1
        return "batch-reached"

    monkeypatch.setattr(dw, "_generate_realtime", _rt_503)
    monkeypatch.setattr(dw, "_generate_via_batch", _batch_spy)
    with pytest.raises(dwp.TemporalBudgetShedError) as ei:
        await dw.generate(_ctx("complex"), _deadline_s(45))
    assert "batch_chokepoint_rt_exhausted" in str(ei.value)
    assert batch_entered["n"] == 0, "mail slot was entered despite the veto"


@pytest.mark.asyncio
async def test_fat_budget_op_still_reaches_batch(monkeypatch):
    """Anti-overreach pin: an op with a FAT budget and RT disabled must
    still enter the batch plane (the discount is the point) — the veto
    only excludes deadline-bound ops."""
    dw = dwp.DoublewordProvider(
        api_key="test-key", base_url="http://127.0.0.1:9/v1",
        model="test-model", realtime_enabled=False)

    async def _fake_batch(context, prompt_override=None):
        return "batch-reached"

    monkeypatch.setattr(dw, "_generate_via_batch", _fake_batch)
    out = await dw.generate(_ctx("complex"), _deadline_s(600))
    assert out == "batch-reached"


# ---------------------------------------------------------------------------
# FSM taxonomy — a shed is a routing refusal, NOT a DW health fact
# ---------------------------------------------------------------------------

def test_shed_classifies_as_temporal_shed():
    exc = dwp.TemporalBudgetShedError(
        "rt_disabled", model_id="m", remaining_s=45.0, ceiling_s=50.0)
    assert FailbackStateMachine.classify_exception(exc) is FailureMode.TEMPORAL_SHED


def test_shed_is_never_outer_retry_eligible():
    assert _is_outer_retry_eligible_mode(FailureMode.TEMPORAL_SHED) is False


def test_shed_has_zero_recovery_penalty():
    from backend.core.ouroboros.governance.candidate_generator import (
        _RECOVERY_PARAMS,
    )
    params = _RECOVERY_PARAMS[FailureMode.TEMPORAL_SHED]
    assert params["base_s"] == 0.0 and params["max_s"] == 0.0


# ---------------------------------------------------------------------------
# THE MANDATE CASCADE TEST — CG routes the shed to the Claude fallback
# adapter well within the 45s budget, with ZERO DW penalty recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shed_cascades_to_claude_within_budget(monkeypatch):
    from backend.core.ouroboros.governance import provider_topology as pt

    monkeypatch.setattr(
        pt, "get_topology",
        lambda: SimpleNamespace(
            enabled=False,
            is_dw_blocked_for_route=lambda route: (False, "", ""),
        ),
    )
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    # env-isolation: this machine's real promotion ledger (12 records)
    # otherwise trips the Slice-23 multi_model_fleet sentinel activation
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "false")

    class _ShedDW:
        provider_name = "doubleword"
        is_available = True
        _realtime_enabled = True
        _model = "test-model"

        def __init__(self):
            self.calls = 0

        async def generate(self, context, deadline, **kw):
            self.calls += 1
            raise dwp.TemporalBudgetShedError(
                "rt_model_unavailable", model_id="test-model",
                remaining_s=45.0, ceiling_s=50.0)

    class _FakeClaude:
        provider_name = "claude"
        is_available = True

        def __init__(self):
            self.calls = 0

        async def generate(self, context, deadline, **kw):
            self.calls += 1
            return SimpleNamespace(
                candidates=({"fake": True},),
                provider_name="claude",
                generation_duration_s=0.1,
                total_output_tokens=1, cost_usd=0.0,
            )

    fake_dw = _ShedDW()
    fake_claude = _FakeClaude()
    cg = CandidateGenerator(
        primary=fake_dw, fallback=fake_claude, tier0=fake_dw)

    ctx = _ctx("complex")
    t0 = time.monotonic()
    result = await asyncio.wait_for(
        cg.generate(ctx, _deadline_s(45)), timeout=40.0)
    elapsed = time.monotonic() - t0

    assert fake_dw.calls == 1, "DW attempted exactly once (no shed retry)"
    assert fake_claude.calls >= 1, "Claude fallback adapter was never reached"
    assert result is not None and len(result.candidates) == 1
    assert result.provider_name == "claude"
    assert elapsed < 30.0, f"cascade took {elapsed:.1f}s of a 45s budget"
    # zero DW penalty: the FSM did not record a primary failure
    assert cg.fsm._consecutive_failures == 0
    assert cg.fsm._failure_mode is None


# ---------------------------------------------------------------------------
# Wiring pins — the veto is structurally where it claims to be
# ---------------------------------------------------------------------------

def test_wiring_pins():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    gov = root / "backend" / "core" / "ouroboros" / "governance"
    dw_src = (gov / "doubleword_provider.py").read_text()
    # veto rung is the FIRST evaluation in the predicate
    pred = dw_src.index("def _slice36_should_force_batch")
    veto_rung = dw_src.index("_dw_temporal_veto(context, remaining_budget_s)", pred)
    hedge_rung = dw_src.index("_dw_hedge_supersedes(context, model_id)", pred)
    assert veto_rung < hedge_rung
    # terminal chokepoint guards the ONE batch entry in dispatch
    choke = dw_src.index("batch_chokepoint_rt_exhausted")
    batch_call = dw_src.index(
        "result = await self._generate_via_batch(context, prompt_override)")
    assert choke < batch_call
    # RepairEngine passes the TRUTHFUL deadline (no veto logic at call site)
    rep_src = (gov / "repair_engine.py").read_text()
    assert "_provider_deadline = _now + timedelta(seconds=_effective_timeout_s)" in rep_src
    assert "ctx, _provider_deadline" in rep_src
    assert "_dw_temporal_veto" not in rep_src  # centralized — DRY holds
