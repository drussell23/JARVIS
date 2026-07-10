"""Slice 194 — Race-Triage & Cross-Model Rotation Matrix.

The Slice 193 registry exposed a live gap: ``dispatches − victories = 2``
races died with NO winner (op-019eae7b: RT RuntimeError + structural parser
rejection) and the op kept re-walking the same dead model every ~2 minutes.
A Sovereign Organism does not watch its races die — it triages and rotates.

Pins:
  * ``hedge_races_abandoned`` — explicit charter counter in the mmap registry
    (no more derived-gap arithmetic).
  * ``hedged_race(on_abandoned=...)`` — fires EXACTLY when both arms fail,
    with per-arm exceptions; never fires on a win; a sink error never
    changes the race's raise behavior.
  * ``race_triage.triage_dual_failure`` — exception-signature analysis:
    dual vendor failures → hard model/endpoint blockage; an INTERNAL fault
    (our bug, Slice 185 taxonomy) or a cancelled arm NEVER blames the model.
  * Per-op blacklist rides the schema_drift_tracker's bounded storage
    (DriftType.DUAL_ARM_FAILURE) but is gated by JARVIS_RACE_TRIAGE_ENABLED
    (default TRUE, failure-path-only) — INDEPENDENT of the default-FALSE
    drift-rotation master, so the soak rotates without extra env flips.
  * Sentinel walker skips blacklisted models → the next iteration IS the
    next-highest-ranked catalog candidate (no blind retry, no 2-min stall).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_transport_hedge import hedged_race
from backend.core.ouroboros.governance.observability_registry import (
    HEDGE_RACES_ABANDONED,
    _reset_singleton_for_tests,
    get_observability_registry,
    record_hedge_abandoned,
)
from backend.core.ouroboros.governance.race_triage import (
    ArmFailureClass,
    classify_arm,
    is_blacklisted_for_op,
    is_budget_refusal_pair,
    race_triage_enabled,
    record_dual_arm_blacklist,
    triage_dual_failure,
)
from backend.core.ouroboros.governance.session_budget_authority import (
    SessionBudgetPreflightRefused,
)
from backend.core.ouroboros.governance.schema_drift_tracker import (
    DriftType,
    get_default_tracker,
    reset_default_tracker,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


def _vendor_rejection(msg: str, status_code: int = 422) -> ValueError:
    """A provider-layer structural rejection: per the Slice 185 taxonomy a
    ValueError is ambiguous-internal UNLESS it carries a vendor status_code
    (i.e. it came structured from the provider layer)."""
    exc = ValueError(msg)
    exc.status_code = status_code
    return exc


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "reg.bin"),
    )
    monkeypatch.delenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_RACE_TRIAGE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", raising=False)
    _reset_singleton_for_tests()
    reset_default_tracker()
    yield
    _reset_singleton_for_tests()
    reset_default_tracker()


# ===========================================================================
# A — the explicit abandoned counter (Phase 1)
# ===========================================================================

def test_abandoned_counter_preregistered_at_zero():
    assert get_observability_registry().snapshot()[HEDGE_RACES_ABANDONED] == 0


def test_record_hedge_abandoned_increments():
    record_hedge_abandoned()
    record_hedge_abandoned()
    assert get_observability_registry().get(HEDGE_RACES_ABANDONED) == 2


# ===========================================================================
# B — hedged_race on_abandoned hook (Phase 2)
# ===========================================================================

def test_dual_arm_failure_fires_on_abandoned_and_still_raises():
    seen = {}

    async def _fast():
        raise RuntimeError("rt stream severed")

    async def _stable():
        await asyncio.sleep(0.01)
        raise _vendor_rejection("batch candidate rejected: full_content too short")

    def _abandoned(fast_exc, stable_exc):
        seen["fast"] = fast_exc
        seen["stable"] = stable_exc

    async def _run():
        with pytest.raises(ValueError):
            await hedged_race(
                _fast, _stable,
                is_rupture=lambda e: isinstance(e, RuntimeError),
                on_abandoned=_abandoned,
            )
    asyncio.run(_run())
    assert isinstance(seen["fast"], RuntimeError)
    assert isinstance(seen["stable"], ValueError)


def test_winner_never_fires_on_abandoned():
    called = []

    async def _fast():
        return "rt-result"

    async def _stable():
        await asyncio.sleep(5)
        return "batch-result"

    async def _run():
        return await hedged_race(
            _fast, _stable, on_abandoned=lambda f, s: called.append(1),
        )
    assert asyncio.run(_run()) == "rt-result"
    assert called == []


def test_swallowed_rupture_with_stable_win_never_fires_on_abandoned():
    called = []

    async def _fast():
        raise RuntimeError("rupture")

    async def _stable():
        await asyncio.sleep(0.01)
        return "batch-result"

    async def _run():
        return await hedged_race(
            _fast, _stable,
            is_rupture=lambda e: True,
            on_abandoned=lambda f, s: called.append(1),
        )
    assert asyncio.run(_run()) == "batch-result"
    assert called == []


def test_on_abandoned_sink_error_never_changes_the_raise():
    async def _fast():
        raise RuntimeError("rt dead")

    async def _stable():
        raise _vendor_rejection("batch dead")

    def _bad_sink(f, s):
        raise OSError("sink exploded")

    async def _run():
        with pytest.raises((RuntimeError, ValueError)):
            await hedged_race(_fast, _stable, on_abandoned=_bad_sink)
    asyncio.run(_run())


# ===========================================================================
# C — the triage classifier (Phase 2)
# ===========================================================================

def test_classify_vendor_and_internal_arms():
    assert classify_arm(RuntimeError("severed")) is ArmFailureClass.VENDOR
    assert classify_arm(NameError("x undefined")) is ArmFailureClass.INTERNAL_FAULT
    assert classify_arm(asyncio.CancelledError()) is ArmFailureClass.CANCELLED
    assert classify_arm(None) is ArmFailureClass.ABSENT


def test_dual_vendor_failure_is_hard_blockage():
    """The live op-019eae7b shape: RT RuntimeError + structural rejection
    (provider-shaped, carries the vendor status_code)."""
    v = triage_dual_failure(
        RuntimeError("rt stream severed"),
        _vendor_rejection("full_content too short (1906 bytes vs original 4096)"),
    )
    assert v.hard_blockage is True
    assert v.fast_class is ArmFailureClass.VENDOR
    assert v.stable_class is ArmFailureClass.VENDOR


def test_bare_valueerror_is_ambiguous_internal_no_blame():
    """Slice 185 doctrine: a ValueError WITHOUT a vendor status_code is
    ambiguous (parse vs logic) → treated internal → never blacklists."""
    v = triage_dual_failure(RuntimeError("severed"), ValueError("bare"))
    assert v.hard_blockage is False


def test_internal_fault_never_blames_the_model():
    """Slice 185 doctrine: a NameError is OUR bug — never blacklist the model."""
    v = triage_dual_failure(NameError("_x undefined"), RuntimeError("severed"))
    assert v.hard_blockage is False
    assert "internal" in v.reason.lower()


def test_cancelled_or_absent_arm_is_not_a_blockage():
    v = triage_dual_failure(RuntimeError("severed"), None)
    assert v.hard_blockage is False
    v2 = triage_dual_failure(asyncio.CancelledError(), RuntimeError("severed"))
    assert v2.hard_blockage is False


# ===========================================================================
# D — the per-op blacklist (Phase 3)
# ===========================================================================

def test_blacklist_records_and_is_scoped_to_the_op():
    v = triage_dual_failure(RuntimeError("a"), _vendor_rejection("b"))
    assert record_dual_arm_blacklist("op-123", "qwen/qwen3.5-35b", v) is True
    assert is_blacklisted_for_op("op-123", "qwen/qwen3.5-35b") is True
    assert is_blacklisted_for_op("op-123", "deepseek-ai/deepseek-v4") is False
    assert is_blacklisted_for_op("op-OTHER", "qwen/qwen3.5-35b") is False


def test_blacklist_independent_of_drift_rotation_master(monkeypatch):
    """The drift-rotation master defaults FALSE — Slice 194's predicate must
    rotate anyway (its own master, failure-path-only default TRUE)."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "false")
    v = triage_dual_failure(RuntimeError("a"), _vendor_rejection("b"))
    record_dual_arm_blacklist("op-9", "m-1", v)
    assert is_blacklisted_for_op("op-9", "m-1") is True


def test_blacklist_event_lands_in_drift_audit_ledger():
    v = triage_dual_failure(RuntimeError("a"), _vendor_rejection("b"))
    record_dual_arm_blacklist("op-7", "m-7", v)
    events = get_default_tracker().events_for("op-7")
    assert any(e.drift_type is DriftType.DUAL_ARM_FAILURE for e in events)


def test_master_off_disables_blacklist(monkeypatch):
    monkeypatch.setenv("JARVIS_RACE_TRIAGE_ENABLED", "false")
    assert race_triage_enabled() is False
    v = triage_dual_failure(RuntimeError("a"), _vendor_rejection("b"))
    assert record_dual_arm_blacklist("op-5", "m-5", v) is False
    assert is_blacklisted_for_op("op-5", "m-5") is False


def test_soft_verdict_is_never_recorded():
    v = triage_dual_failure(NameError("our bug"), RuntimeError("x"))
    assert record_dual_arm_blacklist("op-6", "m-6", v) is False
    assert is_blacklisted_for_op("op-6", "m-6") is False


def _budget_refusal() -> SessionBudgetPreflightRefused:
    """A LOCAL hard-wallet refusal (session budget $0.00), matching the real
    keyword-only constructor at session_budget_authority.py:95-111."""
    return SessionBudgetPreflightRefused(
        provider="doubleword",
        estimated_cost_usd=0.10,
        session_remaining_usd=0.0,
    )


def test_dual_arm_budget_refusal_is_not_a_hard_blockage():
    """Slice 4 T2 — both arms failing on the LOCAL session-budget gate must NOT
    be a hard model blockage (Run #14: 27 spurious dual-arm blacklists → a
    phantom 79-minute global DW quarantine)."""
    v = triage_dual_failure(_budget_refusal(), _budget_refusal())
    assert v.hard_blockage is False
    assert "budget" in v.reason.lower()


def test_budget_refusal_pair_writes_no_blacklist():
    """The NOTE case: with both arms a budget refusal, the CONFIRMED blacklist
    path writes nothing — the drift registry the dispatch skip reads is
    untouched, so no model is rotated off for a local config gate."""
    op_id, model = "op-budget-1", "qwen/qwen3.5-35b-a3b-fp8"
    v = triage_dual_failure(_budget_refusal(), _budget_refusal())
    assert record_dual_arm_blacklist(op_id, model, v) is False
    # The registry is_blacklisted_for_op reads is untouched.
    assert is_blacklisted_for_op(op_id, model) is False
    assert not get_default_tracker().events_for(op_id)


def test_budget_refusal_pair_helper_variants():
    assert is_budget_refusal_pair(_budget_refusal(), _budget_refusal()) is True
    # A missing arm carries no counter-evidence.
    assert is_budget_refusal_pair(_budget_refusal(), None) is True
    # One real transport fault → not a pure budget refusal → still blockable.
    assert is_budget_refusal_pair(
        _budget_refusal(), RuntimeError("rt severed"),
    ) is False
    assert is_budget_refusal_pair(None, None) is False


def test_real_dual_vendor_failure_still_blacklists_after_t2():
    """Regression guard: the T2 budget carve-out must NOT weaken the genuine
    dual-vendor-outage path — two real transport faults still blacklist."""
    v = triage_dual_failure(RuntimeError("rt severed"), _vendor_rejection("b"))
    assert v.hard_blockage is True
    assert record_dual_arm_blacklist("op-real", "m-real", v) is True
    assert is_blacklisted_for_op("op-real", "m-real") is True


def test_predicate_default_enabled():
    assert race_triage_enabled() is True


# ===========================================================================
# E — end-to-end hot-swap simulation (Phase 4 acceptance)
# ===========================================================================

def test_synthetic_dual_arm_failure_counts_blacklists_and_rotates():
    """The user-pinned Slice 194 acceptance: a synthetic dual-arm failure on a
    target model → registry increments flawlessly, the model is blacklisted
    for the op, and a ranked walk hot-swaps to the next candidate."""
    op_id = "op-sim-194"
    failing_model = "qwen/qwen3.5-35b-a3b-fp8"
    ranked = [failing_model, "nvidia/nemotron-3", "deepseek-ai/deepseek-v4-pro"]

    async def _fast():
        raise RuntimeError("rt stream severed mid-generation")

    async def _stable():
        await asyncio.sleep(0.01)
        raise _vendor_rejection("batch candidate structurally rejected")

    def _abandoned(fast_exc, stable_exc):
        record_hedge_abandoned()
        verdict = triage_dual_failure(fast_exc, stable_exc)
        if verdict.hard_blockage:
            record_dual_arm_blacklist(op_id, failing_model, verdict)

    async def _run():
        with pytest.raises((RuntimeError, ValueError)):
            await hedged_race(
                _fast, _stable,
                is_rupture=lambda e: isinstance(e, RuntimeError),
                on_abandoned=_abandoned,
            )
    asyncio.run(_run())

    # 1 — the registry counted the abandoned race
    assert get_observability_registry().get(HEDGE_RACES_ABANDONED) == 1
    # 2 — the model is blacklisted for THIS op only
    assert is_blacklisted_for_op(op_id, failing_model) is True
    # 3 — a ranked walk rotates past the corpse to the next candidate
    survivors = [m for m in ranked if not is_blacklisted_for_op(op_id, m)]
    assert survivors[0] == "nvidia/nemotron-3"


# ===========================================================================
# F — wiring pins (source-pinned, repo precedent)
# ===========================================================================

def test_provider_hedge_block_wires_on_abandoned():
    src = (_GOV / "doubleword_provider.py").read_text(encoding="utf-8")
    assert "on_abandoned" in src
    assert "record_hedge_abandoned" in src
    assert "triage_dual_failure" in src


def test_sentinel_walker_skips_dual_arm_blacklisted_models():
    src = (_GOV / "candidate_generator.py").read_text(encoding="utf-8")
    assert "is_blacklisted_for_op" in src
    assert "skipped_dual_arm" in src


def test_authority_invariant_no_wide_imports():
    src = (_GOV / "race_triage.py").read_text(encoding="utf-8")
    for forbidden in (
        "from backend.core.ouroboros.governance.orchestrator",
        "iron_gate", "change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "semantic_guardian", "risk_tier",
    ):
        assert forbidden not in src, f"authority leak: {forbidden}"
