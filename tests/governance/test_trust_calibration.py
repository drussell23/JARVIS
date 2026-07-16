"""Trust Calibration — earned auto-apply envelope spine (autonomy Gap 4).

Proves the envelope is EARNED and ADAPTIVE, cage-safely and asymmetrically:
recency-weighted held-up-vs-reverted per scope; a fresh regression forces LOW +
a human-gate narrowing floor regardless of the historical rate; a high rate on
thin evidence stays UNKNOWN (volume floor); widening is DOUBLE opt-in +
default-inert + never touches the cage; and the narrowing floor composes through
the real risk_tier_floor.recommended_floor stack.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp.test_utils import make_mocked_request

import backend.core.ouroboros.governance.trust_calibration as tc
from backend.core.ouroboros.governance.trust_calibration import (
    ScopeTrust, TrustLevel,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier


# ---------------------------------------------------------------------------
# Calibration math — _compute (pure): recency-weighted, asymmetric, volume floor
# ---------------------------------------------------------------------------


_NOW = 1_800_000_000.0
_DAY = 86400.0


def test_all_held_up_over_volume_is_high(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_MIN_SAMPLE", "5")
    recs = [(True, _NOW - i * _DAY) for i in range(6)]   # 6 recent held-up
    st = tc._compute("svc", recs, _NOW)
    assert st.trust_level == TrustLevel.HIGH.value
    assert st.held_up == 6 and st.reverted == 0
    assert st.held_up_rate == 1.0


def test_high_rate_but_thin_evidence_is_unknown(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_MIN_SAMPLE", "5")
    recs = [(True, _NOW - _DAY), (True, _NOW - 2 * _DAY)]   # 2/2 held-up
    st = tc._compute("svc", recs, _NOW)
    # perfect rate but below the volume floor → NOT trusted.
    assert st.trust_level == TrustLevel.UNKNOWN.value
    assert st.held_up_rate == 1.0 and st.sample_count == 2


def test_fresh_regression_forces_low_regardless_of_rate(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_MIN_SAMPLE", "3")
    monkeypatch.setenv("JARVIS_TRUST_REGRESSION_WINDOW_S", str(int(7 * _DAY)))
    # 20 old successes + ONE revert yesterday → rate stays high but it's fresh-broken.
    recs = [(True, _NOW - (30 + i) * _DAY) for i in range(20)]
    recs.append((False, _NOW - 1 * _DAY))
    st = tc._compute("svc", recs, _NOW)
    assert st.recent_regression is True
    assert st.trust_level == TrustLevel.LOW.value   # asymmetric: fresh break wins


def test_old_regression_does_not_force_low(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_MIN_SAMPLE", "3")
    monkeypatch.setenv("JARVIS_TRUST_REGRESSION_WINDOW_S", str(int(7 * _DAY)))
    recs = [(True, _NOW - (1 + i) * _DAY) for i in range(10)]
    recs.append((False, _NOW - 90 * _DAY))   # a revert 90d ago — not fresh
    st = tc._compute("svc", recs, _NOW)
    assert st.recent_regression is False


def test_poor_rate_is_low(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_MIN_SAMPLE", "3")
    monkeypatch.setenv("JARVIS_TRUST_LOW_THRESHOLD", "0.5")
    monkeypatch.setenv("JARVIS_TRUST_REGRESSION_WINDOW_S", "1")  # nothing "fresh"
    recs = [(False, _NOW - (30 + i) * _DAY) for i in range(4)]   # 0/4 held up, old
    recs += [(True, _NOW - (30 + i) * _DAY) for i in range(1)]   # 1/5
    st = tc._compute("svc", recs, _NOW)
    assert st.trust_level == TrustLevel.LOW.value


# ---------------------------------------------------------------------------
# NARROWING — safety-forward, always active
# ---------------------------------------------------------------------------


def _stub_scope(monkeypatch, **kw):
    base = dict(scope="svc", trust_level=TrustLevel.UNKNOWN.value, held_up_rate=None,
                sample_count=0, held_up=0, reverted=0, recent_regression=False)
    base.update(kw)
    monkeypatch.setattr(tc, "scope_trust", lambda scope, **k: ScopeTrust(**{**base, "scope": scope}))


def test_narrowing_fresh_regression_gates_to_approval(monkeypatch):
    _stub_scope(monkeypatch, recent_regression=True, trust_level=TrustLevel.LOW.value)
    assert tc.trust_narrowing_tier("requirements") == "approval_required"


def test_narrowing_low_trust_gates_to_notify(monkeypatch):
    _stub_scope(monkeypatch, trust_level=TrustLevel.LOW.value, recent_regression=False)
    assert tc.trust_narrowing_tier("svc") == "notify_apply"


def test_no_narrowing_when_unknown_or_high(monkeypatch):
    _stub_scope(monkeypatch, trust_level=TrustLevel.UNKNOWN.value)
    assert tc.trust_narrowing_tier("svc") is None
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value)
    assert tc.trust_narrowing_tier("svc") is None


def test_narrowing_disabled_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_CALIBRATION_ENABLED", "false")
    _stub_scope(monkeypatch, recent_regression=True, trust_level=TrustLevel.LOW.value)
    assert tc.trust_narrowing_tier("svc") is None


def test_narrowing_composes_through_recommended_floor(monkeypatch):
    import backend.core.ouroboros.governance.risk_tier_floor as rtf
    _stub_scope(monkeypatch, trust_level=TrustLevel.LOW.value, recent_regression=True)
    floor = rtf.recommended_floor(target_files=["backend/gov/svc.py"])
    assert floor == "approval_required"   # the trust narrowing wins strictest


# ---------------------------------------------------------------------------
# WIDENING — double opt-in, default-inert, cage-excluded, evidence-gated
# ---------------------------------------------------------------------------


def _opt_in_widen(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_CALIBRATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRUST_WIDEN_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRUST_MAX_AUTO_TIER", "notify_apply")
    monkeypatch.setenv("JARVIS_TRUST_WIDEN_MIN_SAMPLE", "8")


def test_widen_default_inert(monkeypatch):
    monkeypatch.delenv("JARVIS_TRUST_WIDEN_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_TRUST_MAX_AUTO_TIER", raising=False)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=50)
    tier, why = tc.maybe_relax_tier(RiskTier.APPROVAL_REQUIRED, scope="svc", touches_cage=False)
    assert tier is RiskTier.APPROVAL_REQUIRED and why is None


def test_widen_when_opted_in_and_high_trust(monkeypatch):
    _opt_in_widen(monkeypatch)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=12,
                held_up_rate=0.95, recent_regression=False)
    tier, why = tc.maybe_relax_tier(RiskTier.APPROVAL_REQUIRED, scope="svc", touches_cage=False)
    assert tier is RiskTier.NOTIFY_APPLY   # Orange → Yellow (auto-apply, surfaced)
    assert why and "widened" in why


def test_widen_never_touches_cage(monkeypatch):
    _opt_in_widen(monkeypatch)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=50)
    tier, why = tc.maybe_relax_tier(RiskTier.APPROVAL_REQUIRED, scope="governance", touches_cage=True)
    assert tier is RiskTier.APPROVAL_REQUIRED and why == "cage_excluded"


def test_widen_blocked_below_volume(monkeypatch):
    _opt_in_widen(monkeypatch)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=4)  # < widen_min 8
    tier, _ = tc.maybe_relax_tier(RiskTier.APPROVAL_REQUIRED, scope="svc", touches_cage=False)
    assert tier is RiskTier.APPROVAL_REQUIRED


def test_widen_blocked_on_fresh_regression(monkeypatch):
    _opt_in_widen(monkeypatch)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=50,
                recent_regression=True)
    tier, _ = tc.maybe_relax_tier(RiskTier.APPROVAL_REQUIRED, scope="svc", touches_cage=False)
    assert tier is RiskTier.APPROVAL_REQUIRED


def test_widen_only_relaxes_approval_not_others(monkeypatch):
    _opt_in_widen(monkeypatch)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=50)
    # BLOCKED / NOTIFY_APPLY are not the human-gate tier → untouched.
    assert tc.maybe_relax_tier(RiskTier.BLOCKED, scope="svc", touches_cage=False)[0] is RiskTier.BLOCKED
    assert tc.maybe_relax_tier(RiskTier.NOTIFY_APPLY, scope="svc", touches_cage=False)[0] is RiskTier.NOTIFY_APPLY


def test_widen_blocked_when_ceiling_not_raised(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUST_CALIBRATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRUST_WIDEN_ENABLED", "true")
    monkeypatch.delenv("JARVIS_TRUST_MAX_AUTO_TIER", raising=False)  # ceiling unset
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=50)
    tier, _ = tc.maybe_relax_tier(RiskTier.APPROVAL_REQUIRED, scope="svc", touches_cage=False)
    assert tier is RiskTier.APPROVAL_REQUIRED   # widen enabled but no ceiling → inert


def test_relax_for_op_fail_closed_on_cage_check(monkeypatch):
    """A governance-self-mod op is cage-detected and never widened."""
    _opt_in_widen(monkeypatch)
    _stub_scope(monkeypatch, trust_level=TrustLevel.HIGH.value, sample_count=50)

    class _Ctx:
        target_files = ("backend/core/ouroboros/governance/orchestrator.py",)

    tier, why = tc.relax_tier_for_op(RiskTier.APPROVAL_REQUIRED, _Ctx())
    assert tier is RiskTier.APPROVAL_REQUIRED and why == "cage_excluded"


# ---------------------------------------------------------------------------
# Never-raises + endpoint
# ---------------------------------------------------------------------------


def test_scope_records_never_raises_on_bad_git(monkeypatch):
    import backend.core.ouroboros.governance.autonomy_metrics as am
    monkeypatch.setattr(am, "_run_git_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git")))
    tc.reset_cache_for_tests()
    rep = tc.trust_report(now=_NOW, force=True)
    assert rep == {}


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_TRUST_CALIBRATION_ENABLED", raising=False)
    assert tc.master_enabled() is True
    monkeypatch.setenv("JARVIS_TRUST_CALIBRATION_ENABLED", "0")
    assert tc.master_enabled() is False


def _req(path="/observability/trust"):
    r = make_mocked_request("GET", path, headers={})
    r._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return r


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _router():
    from backend.core.ouroboros.governance.ide_observability import IDEObservabilityRouter
    return IDEObservabilityRouter()


def test_endpoint_disabled_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = _run(_router()._handle_trust_calibration(_req()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.disabled"


def test_endpoint_substrate_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRUST_CALIBRATION_ENABLED", "false")
    resp = _run(_router()._handle_trust_calibration(_req()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.trust_disabled"


def test_endpoint_returns_snapshot(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRUST_CALIBRATION_ENABLED", "true")
    tc.reset_cache_for_tests()
    resp = _run(_router()._handle_trust_calibration(_req()))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["schema_version"] == tc.TRUST_CALIBRATION_SCHEMA_VERSION
    assert "envelope" in body and "scopes" in body
    assert body["envelope"]["widen_enabled"] is False   # default-inert surfaced
    assert resp.headers.get("Cache-Control") == "no-store"
