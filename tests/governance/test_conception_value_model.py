"""Conception Value Model — expected-value ranker for self-conceived work (Gap 3).

Proves the model ranks conceived proposals by a composed expected value that is
evidence-grounded and Gap-4-aware: high strategic alignment + real substance +
EARNED trust + low cost ranks above the opposite; a fresh regression damps
feasibility; a proven-cosmetic band scores below the neutral prior; cost is
monotonic; every axis degrades to a neutral prior when its source is cold (a
fresh organism ranks by what it can see, never collapsing to zero); disabled is
inert; and nothing ever raises.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.conception_value_model as cvm


def _score(**kw):
    base = dict(
        description="x", target_files=["a.py"], estimated_cost_usd=0.0,
        align_fn=lambda d, r: (0.5, True),
        substance_fn=lambda s, t, r: (0.5, 0),
        feasibility_fn=lambda sc: (0.5, "unknown", False),
    )
    base.update(kw)
    return cvm.score_proposal(**base)


# ---------------------------------------------------------------------------
# Composition + ranking
# ---------------------------------------------------------------------------


def test_high_all_axes_ranks_above_low():
    hi = _score(align_fn=lambda d, r: (0.95, True),
                substance_fn=lambda s, t, r: (1.0, 3),
                feasibility_fn=lambda sc: (1.0, "high", False))
    lo = _score(align_fn=lambda d, r: (0.1, True),
                substance_fn=lambda s, t, r: (0.33, 1),
                feasibility_fn=lambda sc: (0.3, "low", False))
    assert hi.ev > lo.ev
    assert hi.ev > 0.8 and lo.ev < 0.4


def test_ev_is_weighted_mean_scaled_by_cost(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_W_ALIGN", "1")
    monkeypatch.setenv("JARVIS_CONCEPTION_W_SUBSTANCE", "1")
    monkeypatch.setenv("JARVIS_CONCEPTION_W_FEASIBILITY", "1")
    ev = _score(estimated_cost_usd=0.0,
                align_fn=lambda d, r: (0.6, True),
                substance_fn=lambda s, t, r: (0.6, 2),
                feasibility_fn=lambda sc: (0.6, "medium", False))
    assert abs(ev.ev - 0.6) < 1e-6   # equal weights, no cost → the mean


def test_rank_proposals_orders_desc():
    a = _score(feasibility_fn=lambda sc: (0.1, "low", False))
    b = _score(feasibility_fn=lambda sc: (0.9, "high", False))
    ranked = cvm.rank_proposals([("a", a), ("b", b)])
    assert [k for k, _ in ranked] == ["b", "a"]


# ---------------------------------------------------------------------------
# Gap-4 composition: earned trust is the feasibility axis
# ---------------------------------------------------------------------------


def test_fresh_regression_damps_feasibility(monkeypatch):
    """The damp lives IN the real feasibility axis (score_proposal trusts the
    axis value), so drive the axis directly via a stubbed scope_trust."""
    import backend.core.ouroboros.governance.trust_calibration as tc
    from backend.core.ouroboros.governance.trust_calibration import ScopeTrust

    monkeypatch.setenv("JARVIS_CONCEPTION_REGRESSION_DAMP", "0.5")

    def _st(recent):
        return lambda scope, **k: ScopeTrust(
            scope=scope, trust_level="high", held_up_rate=0.95, sample_count=20,
            held_up=19, reverted=1, recent_regression=recent, last_landed_unix=0.0)

    monkeypatch.setattr(tc, "scope_trust", _st(False))
    clean_v, _, clean_reg = cvm._feasibility_axis("svc")
    monkeypatch.setattr(tc, "scope_trust", _st(True))
    broke_v, _, broke_reg = cvm._feasibility_axis("svc")

    assert clean_v == 1.0 and clean_reg is False
    assert broke_v == 0.5 and broke_reg is True   # 1.0 × 0.5 damp
    assert broke_v < clean_v


def test_real_feasibility_axis_reads_trust_calibration(monkeypatch):
    """The default feasibility axis composes trust_calibration.scope_trust."""
    import backend.core.ouroboros.governance.trust_calibration as tc
    from backend.core.ouroboros.governance.trust_calibration import ScopeTrust

    monkeypatch.setattr(tc, "scope_trust", lambda scope, **k: ScopeTrust(
        scope=scope, trust_level="high", held_up_rate=0.95, sample_count=20,
        held_up=19, reverted=1, recent_regression=False, last_landed_unix=0.0))
    v, level, regressed = cvm._feasibility_axis("svc")
    assert level == "high" and v == 1.0 and regressed is False


# ---------------------------------------------------------------------------
# Substance: verifiable-evidence-first, cosmetic below neutral
# ---------------------------------------------------------------------------


def test_cosmetic_band_below_neutral_indeterminate_at_neutral():
    cosmetic = _score(substance_fn=lambda s, t, r: (cvm._BAND_AXIS[1], 1))
    indeterm = _score(substance_fn=lambda s, t, r: (cvm._BAND_AXIS[0], 0))
    assert cvm._BAND_AXIS[1] < cvm._NEUTRAL == cvm._BAND_AXIS[0]
    assert cosmetic.ev < indeterm.ev


def test_band_axis_monotonic():
    assert (cvm._BAND_AXIS[1] < cvm._BAND_AXIS[2] < cvm._BAND_AXIS[3])


# ---------------------------------------------------------------------------
# Cost efficiency
# ---------------------------------------------------------------------------


def test_cost_factor_monotonic_decreasing():
    assert cvm._cost_factor(0.0) == 1.0
    assert cvm._cost_factor(0.0) > cvm._cost_factor(0.1) > cvm._cost_factor(5.0)


def test_cheaper_proposal_wins_all_else_equal():
    cheap = _score(estimated_cost_usd=0.0)
    dear = _score(estimated_cost_usd=1.0)
    assert cheap.ev > dear.ev


# ---------------------------------------------------------------------------
# Cold-axis neutrality — a fresh organism ranks by what it can see
# ---------------------------------------------------------------------------


def test_cold_axes_degrade_to_neutral_not_zero():
    # all default seams, but force the alignment axis to report "cold".
    ev = _score(align_fn=lambda d, r: (cvm._NEUTRAL, False))
    assert ev.alignment_known is False
    assert 0.3 < ev.ev < 0.7   # neutral-ish, never collapsed to 0


def test_real_alignment_axis_cold_index_is_neutral_unknown():
    # With no built semantic index the real axis must report neutral+unknown.
    v, known = cvm._alignment_axis("some proposal text", None)
    assert v == cvm._NEUTRAL and known is False


def test_empty_description_alignment_unknown():
    v, known = cvm._alignment_axis("   ", None)
    assert v == cvm._NEUTRAL and known is False


# ---------------------------------------------------------------------------
# Disabled = inert, never-raise, adapters
# ---------------------------------------------------------------------------


def test_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_VALUE_MODEL_ENABLED", "false")
    ev = cvm.score_proposal(description="x", target_files=["a.py"], estimated_cost_usd=0.0)
    assert ev.rationale == "model_disabled"
    assert ev.ev == cvm._NEUTRAL   # neutral × cost_factor(0)=1


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_CONCEPTION_VALUE_MODEL_ENABLED", raising=False)
    assert cvm.master_enabled() is True
    monkeypatch.setenv("JARVIS_CONCEPTION_VALUE_MODEL_ENABLED", "0")
    assert cvm.master_enabled() is False


def test_score_blueprint_adapter():
    class _BP:
        description = "Add caching to the oracle lookup"
        title = "cache oracle"
        target_files = ("backend/core/ouroboros/oracle.py",)
        estimated_cost_usd = 0.03
    ev = cvm.score_blueprint(_BP())
    assert 0.0 <= ev.ev <= 1.0 and ev.scope

    class _Broken:  # unknown shape → neutral, never raises
        pass
    ev2 = cvm.score_blueprint(_Broken())
    assert 0.0 <= ev2.ev <= 1.0


def test_priority_hint_for_returns_ev_in_unit_range():
    h = cvm.priority_hint_for(description="x", target_files=["a.py"], estimated_cost_usd=0.1)
    assert 0.0 <= h <= 1.0


def test_never_raises_on_garbage():
    ev = cvm.score_proposal(description=None, target_files=None, estimated_cost_usd="oops")  # type: ignore[arg-type]
    assert 0.0 <= ev.ev <= 1.0


def test_rank_proposals_never_raises_on_bad_input():
    assert cvm.rank_proposals([("a", None)]) is not None  # type: ignore[list-item]


def test_snapshot_shape():
    snap = cvm.snapshot()
    assert snap["schema_version"] == cvm.CONCEPTION_VALUE_SCHEMA_VERSION
    assert set(snap["weights"]) == {"alignment", "substance", "feasibility"}
    assert "axes" in snap and "feasibility" in snap["axes"]


# ---------------------------------------------------------------------------
# Authority invariant + /observability/conception endpoint
# ---------------------------------------------------------------------------


def test_module_is_authority_free():
    """It composes read-only scorers; it must NOT import gate/policy/orchestrator."""
    import inspect
    src = inspect.getsource(cvm)
    banned = ("iron_gate", "risk_tier_floor", "semantic_guardian", "policy_engine",
              "orchestrator", "tool_executor", "scoped_tool_backend")
    for b in banned:
        assert f"import {b}" not in src and f"governance.{b}" not in src, b


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def _creq():
    from aiohttp.test_utils import make_mocked_request
    r = make_mocked_request("GET", "/observability/conception", headers={})
    r._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return r


def _crouter():
    from backend.core.ouroboros.governance.ide_observability import IDEObservabilityRouter
    return IDEObservabilityRouter()


def test_endpoint_disabled_master_off(monkeypatch):
    import json
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = _run(_crouter()._handle_conception_value(_creq()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.disabled"


def test_endpoint_substrate_off(monkeypatch):
    import json
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONCEPTION_VALUE_MODEL_ENABLED", "false")
    resp = _run(_crouter()._handle_conception_value(_creq()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.conception_disabled"


def test_endpoint_returns_snapshot(monkeypatch):
    import json
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONCEPTION_VALUE_MODEL_ENABLED", "true")
    resp = _run(_crouter()._handle_conception_value(_creq()))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["schema_version"] == cvm.CONCEPTION_VALUE_SCHEMA_VERSION
    assert body["enabled"] is True and "weights" in body
    assert resp.headers.get("Cache-Control") == "no-store"
