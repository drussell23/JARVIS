"""Slice 231 — Telemetry-Driven Budget Synthesizer (Dynamic Allocation Kernel).

Root fix: ``route_budget_profile`` was a STATIC lookup that allocated the funded
DW primary ``max_dw_wait_s: 0.0`` on the IMMEDIATE route. When the premium
fallback lane (Claude) is economically unavailable (out of credits → breaker
OPEN), the op died at budget allocation (``deadline_exhausted_pre_fallback``)
before ever reaching the hardened DW agentic path.

This suite pins the three operating states demanded by the operator:

  * State-0 (steady state): Claude funded/available → legacy-identical short
    budget window (self-heal, byte-identical to pre-Slice-231 behavior).
  * State-1 (quota exhaustion): Claude breaker OPEN (economic) → an IMMEDIATE
    tool-loop op dynamically claims the un-throttled 180s DW window.
  * State-2 (fail-soft baseline): a telemetry sensing exception inside
    ``collect_provider_availability`` drops back to legacy-safe defaults
    without stalling dispatch.

Plus: full truth-table for ``synthesize_budget_profile``, byte-identical
rollback parity, collector purity (no breaker mutation / probe consumption),
and the master-flag gate.
"""
from __future__ import annotations

import types

import pytest

from backend.core.ouroboros.governance.claude_circuit_breaker import CircuitState
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthRecord,
    SurfaceKind,
    SurfaceVerdict,
)
from backend.core.ouroboros.governance.provider_availability import (
    ProviderAvailabilitySnapshot,
    collect_provider_availability,
)
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    UrgencyRouter,
    budget_synthesis_enabled,
    synthesize_budget_profile,
    synthesize_generation_timeout,
)

_MASTER_FLAG = "JARVIS_BUDGET_SYNTHESIS_ENABLED"

# Legacy constants the synthesizer must reproduce byte-for-byte when Claude is
# available or the master flag is off.
_LEGACY_IMMEDIATE = {"tier0_fraction": 0.0, "tier1_reserve_s": 0.0, "max_dw_wait_s": 0.0}


# --------------------------------------------------------------------------- #
# Test doubles — injected so the collector is exercised with zero global state.
# --------------------------------------------------------------------------- #
class _FakeBreaker:
    """Records any access so tests can prove the collector is side-effect-free."""

    def __init__(self, state, *, economic=0, transport=0, raise_on_state=False):
        self._state = state
        self._economic = economic
        self._transport = transport
        self._raise_on_state = raise_on_state
        self.calls: list[str] = []

    @property
    def state(self):
        self.calls.append("state")
        if self._raise_on_state:
            raise RuntimeError("simulated telemetry sensing failure")
        return self._state

    def snapshot(self):
        self.calls.append("snapshot")
        return {
            "state": self._state.value,
            "consecutive_economic_failures": self._economic,
            "consecutive_transport_failures": self._transport,
        }

    # ---- mutating methods that MUST NEVER be called by a read-only collector
    def should_allow_request(self):  # pragma: no cover - asserted not called
        self.calls.append("should_allow_request")
        return True

    def record_economic_exhaustion(self, *a, **k):  # pragma: no cover
        self.calls.append("record_economic_exhaustion")

    def record_transport_exhaustion(self, *a, **k):  # pragma: no cover
        self.calls.append("record_transport_exhaustion")

    def reset(self):  # pragma: no cover
        self.calls.append("reset")


class _FakeLedger:
    def __init__(self, record):
        self._record = record

    def verdict_for(self, surface):
        assert surface is SurfaceKind.DIRECT_STREAMING
        return self._record


def _dw_record(verdict):
    return SurfaceHealthRecord(
        surface=SurfaceKind.DIRECT_STREAMING, verdict=verdict,
    )


def _healthy_ledger():
    return _FakeLedger(_dw_record(SurfaceVerdict.HEALTHY))


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Default-TRUE master flag, hard-pinned for determinism."""
    monkeypatch.setenv(_MASTER_FLAG, "true")
    yield


# --------------------------------------------------------------------------- #
# collect_provider_availability — the read-only telemetry snapshot client.
# --------------------------------------------------------------------------- #
class TestCollectProviderAvailability:
    def test_state0_steady_state_claude_available_dw_healthy(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.CLOSED),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.claude_available is True
        assert snap.claude_reason == "closed"
        assert snap.dw_healthy is True
        assert snap.dw_reason == "healthy"

    def test_state1_economic_open_marks_claude_unavailable_economic(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, economic=1),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.claude_available is False
        assert snap.claude_reason == "breaker_open_economic"
        assert snap.dw_healthy is True

    def test_transport_open_is_labelled_distinctly(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, transport=3),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.claude_available is False
        assert snap.claude_reason == "breaker_open_transport"

    def test_half_open_probing_is_unavailable(self):
        # Slice 162 lesson: a HALF_OPEN lane is probing, NOT available — an
        # IMMEDIATE op committed to it just exhausts.
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.HALF_OPEN),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.claude_available is False
        assert snap.claude_reason == "half_open_probing"

    def test_structural_disable_overrides_breaker(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.CLOSED),
            ledger=_healthy_ledger(),
            claude_disabled=True,
            breaker_enabled=True,
        )
        assert snap.claude_available is False
        assert snap.claude_reason == "structurally_disabled"

    def test_breaker_disabled_treated_as_available(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, economic=1),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=False,
        )
        # Breaker not authoritative → assume available (legacy behavior).
        assert snap.claude_available is True
        assert snap.claude_reason == "breaker_disabled"

    def test_dw_transport_degraded_marks_unhealthy(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, economic=1),
            ledger=_FakeLedger(_dw_record(SurfaceVerdict.TRANSPORT_DEGRADED)),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.dw_healthy is False
        assert snap.dw_reason == "transport_degraded"

    def test_dw_upstream_degraded_still_usable(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, economic=1),
            ledger=_FakeLedger(_dw_record(SurfaceVerdict.UPSTREAM_DEGRADED)),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.dw_healthy is True
        assert snap.dw_reason == "upstream_degraded"

    def test_dw_no_record_is_legacy_safe_healthy(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, economic=1),
            ledger=_FakeLedger(None),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.dw_healthy is True
        assert snap.dw_reason == "unknown"

    def test_state2_fail_soft_on_sensing_exception(self):
        # Telemetry source raises → conservative legacy-safe defaults, never raise.
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, raise_on_state=True),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert snap.claude_available is True
        assert snap.dw_healthy is True
        assert "fail_soft" in snap.claude_reason

    def test_collector_is_side_effect_free(self):
        # Purity invariant: the collector reads .state / .snapshot only — it must
        # NEVER call should_allow_request() (consumes a HALF_OPEN probe slot) or
        # any mutating method.
        breaker = _FakeBreaker(CircuitState.OPEN, economic=1)
        collect_provider_availability(
            breaker=breaker,
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        forbidden = {
            "should_allow_request",
            "record_economic_exhaustion",
            "record_transport_exhaustion",
            "reset",
        }
        assert forbidden.isdisjoint(set(breaker.calls)), breaker.calls

    def test_snapshot_is_immutable(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.CLOSED),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        with pytest.raises((AttributeError, Exception)):
            snap.claude_available = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# synthesize_budget_profile — the pure allocation kernel.
# --------------------------------------------------------------------------- #
def _snap(*, claude_available, dw_healthy=True):
    return ProviderAvailabilitySnapshot(
        claude_available=claude_available,
        claude_reason="closed" if claude_available else "breaker_open_economic",
        dw_healthy=dw_healthy,
        dw_reason="healthy" if dw_healthy else "transport_degraded",
    )


class TestSynthesizeBudgetProfile:
    def test_immediate_claude_available_is_legacy_reflex(self):
        # Self-heal: Claude funded → unchanged Claude-direct reflex.
        out = synthesize_budget_profile(
            ProviderRoute.IMMEDIATE,
            _snap(claude_available=True),
            _LEGACY_IMMEDIATE,
            tool_loop_demanded=True,
        )
        assert out == _LEGACY_IMMEDIATE

    def test_state1_immediate_tool_loop_claims_180s_dw_window(self):
        out = synthesize_budget_profile(
            ProviderRoute.IMMEDIATE,
            _snap(claude_available=False),
            _LEGACY_IMMEDIATE,
            tool_loop_demanded=True,
        )
        assert out["max_dw_wait_s"] == 180.0
        assert out["tier0_fraction"] == 1.0
        assert out["tier1_reserve_s"] == 0.0

    def test_immediate_non_tool_loop_gets_reflex_window_on_dw(self):
        out = synthesize_budget_profile(
            ProviderRoute.IMMEDIATE,
            _snap(claude_available=False),
            _LEGACY_IMMEDIATE,
            tool_loop_demanded=False,
        )
        assert out["max_dw_wait_s"] == 60.0
        assert out["tier0_fraction"] == 1.0

    def test_immediate_dw_degraded_clamps_to_floor(self):
        out = synthesize_budget_profile(
            ProviderRoute.IMMEDIATE,
            _snap(claude_available=False, dw_healthy=False),
            _LEGACY_IMMEDIATE,
            tool_loop_demanded=True,
        )
        # DW is our only funded lane but degraded — DW-primary, clamped window.
        assert out["tier0_fraction"] == 1.0
        assert out["max_dw_wait_s"] == 60.0

    def test_standard_folds_claude_reserve_into_dw_when_claude_down(self):
        legacy = {"tier0_fraction": 0.65, "tier1_reserve_s": 25.0, "max_dw_wait_s": 90.0}
        out = synthesize_budget_profile(
            ProviderRoute.STANDARD,
            _snap(claude_available=False),
            legacy,
            tool_loop_demanded=False,
        )
        assert out["tier1_reserve_s"] == 0.0
        assert out["tier0_fraction"] == 1.0
        assert out["max_dw_wait_s"] == 115.0  # 90 + 25 reserved seconds folded in

    def test_complex_folds_reserve_when_claude_down(self):
        legacy = {"tier0_fraction": 0.80, "tier1_reserve_s": 20.0, "max_dw_wait_s": 120.0}
        out = synthesize_budget_profile(
            ProviderRoute.COMPLEX,
            _snap(claude_available=False),
            legacy,
            tool_loop_demanded=True,
        )
        assert out["max_dw_wait_s"] == 140.0
        assert out["tier1_reserve_s"] == 0.0

    def test_background_route_untouched_even_when_claude_down(self):
        legacy = {"tier0_fraction": 1.0, "tier1_reserve_s": 0.0, "max_dw_wait_s": 180.0}
        out = synthesize_budget_profile(
            ProviderRoute.BACKGROUND,
            _snap(claude_available=False),
            legacy,
            tool_loop_demanded=False,
        )
        assert out == legacy

    def test_synthesis_is_deterministic(self):
        args = (ProviderRoute.IMMEDIATE, _snap(claude_available=False), _LEGACY_IMMEDIATE)
        a = synthesize_budget_profile(*args, tool_loop_demanded=True)
        b = synthesize_budget_profile(*args, tool_loop_demanded=True)
        assert a == b


# --------------------------------------------------------------------------- #
# route_budget_profile — backward-compatible integration + rollback parity.
# --------------------------------------------------------------------------- #
_ALL_ROUTES = [
    ProviderRoute.IMMEDIATE,
    ProviderRoute.STANDARD,
    ProviderRoute.COMPLEX,
    ProviderRoute.BACKGROUND,
    ProviderRoute.SPECULATIVE,
]


class TestRouteBudgetProfileIntegration:
    @pytest.mark.parametrize("route", _ALL_ROUTES)
    def test_legacy_parity_snapshot_none_equals_one_arg(self, route):
        # snapshot=None must be byte-identical to the historical 1-arg call.
        assert UrgencyRouter.route_budget_profile(route) == \
            UrgencyRouter.route_budget_profile(route, None)

    @pytest.mark.parametrize("route", _ALL_ROUTES)
    def test_flag_off_is_byte_identical_rollback(self, route, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "false")
        assert budget_synthesis_enabled() is False
        down = _snap(claude_available=False)
        # Even with a Claude-down snapshot, flag OFF returns the legacy profile.
        assert UrgencyRouter.route_budget_profile(route, down, tool_loop_demanded=True) == \
            UrgencyRouter.route_budget_profile(route)

    def test_flag_on_immediate_claude_down_funds_dw(self):
        down = _snap(claude_available=False)
        out = UrgencyRouter.route_budget_profile(
            ProviderRoute.IMMEDIATE, down, tool_loop_demanded=True,
        )
        assert out["max_dw_wait_s"] == 180.0
        assert out != _LEGACY_IMMEDIATE  # the whole point: not {0,0,0} anymore

    def test_flag_on_immediate_claude_up_self_heals(self):
        up = _snap(claude_available=True)
        out = UrgencyRouter.route_budget_profile(
            ProviderRoute.IMMEDIATE, up, tool_loop_demanded=True,
        )
        assert out == _LEGACY_IMMEDIATE


# --------------------------------------------------------------------------- #
# End-to-end: collector → synthesizer (the live wiring contract).
# --------------------------------------------------------------------------- #
class TestSynthesizeGenerationTimeout:
    """The load-bearing dispatch lever: the per-route generation deadline. The
    budget_profile is observability-only; THIS is what actually gives the DW
    reroute time to finish tool-loop work when Claude is down."""

    _IMMEDIATE_BASE = 120.0
    _COMPLEX = 240.0

    def test_claude_up_keeps_base_timeout(self):
        out = synthesize_generation_timeout(
            "immediate", self._IMMEDIATE_BASE, _snap(claude_available=True),
            tool_loop_demanded=True, elevated_timeout_s=self._COMPLEX,
        )
        assert out == self._IMMEDIATE_BASE

    def test_claude_down_tool_loop_lifts_immediate_to_complex_window(self):
        out = synthesize_generation_timeout(
            "immediate", self._IMMEDIATE_BASE, _snap(claude_available=False),
            tool_loop_demanded=True, elevated_timeout_s=self._COMPLEX,
        )
        assert out == self._COMPLEX  # 120s reflex → 240s COMPLEX window

    def test_claude_down_no_tool_loop_keeps_base(self):
        out = synthesize_generation_timeout(
            "immediate", self._IMMEDIATE_BASE, _snap(claude_available=False),
            tool_loop_demanded=False, elevated_timeout_s=self._COMPLEX,
        )
        assert out == self._IMMEDIATE_BASE

    def test_lift_never_shrinks_a_larger_base(self):
        # If base already exceeds the elevated window, never reduce it.
        out = synthesize_generation_timeout(
            "immediate", 300.0, _snap(claude_available=False),
            tool_loop_demanded=True, elevated_timeout_s=self._COMPLEX,
        )
        assert out == 300.0

    def test_non_immediate_route_unchanged(self):
        out = synthesize_generation_timeout(
            "standard", 220.0, _snap(claude_available=False),
            tool_loop_demanded=True, elevated_timeout_s=self._COMPLEX,
        )
        assert out == 220.0

    def test_accepts_enum_route(self):
        out = synthesize_generation_timeout(
            ProviderRoute.IMMEDIATE, self._IMMEDIATE_BASE,
            _snap(claude_available=False),
            tool_loop_demanded=True, elevated_timeout_s=self._COMPLEX,
        )
        assert out == self._COMPLEX

    def test_missing_elevated_target_is_safe(self):
        out = synthesize_generation_timeout(
            "immediate", self._IMMEDIATE_BASE, _snap(claude_available=False),
            tool_loop_demanded=True, elevated_timeout_s=None,
        )
        assert out == self._IMMEDIATE_BASE


class TestContextBudgetProfileSeam:
    """The ROUTE-phase wiring seam both stamp sites call: it builds the live
    snapshot + derives the tool-loop predicate from the op's complexity."""

    def _patch_collect(self, monkeypatch, snapshot):
        import backend.core.ouroboros.governance.provider_availability as pa
        monkeypatch.setattr(
            pa, "collect_provider_availability", lambda **k: snapshot,
        )

    def test_claude_down_tool_loop_complexity_funds_180s(self, monkeypatch):
        self._patch_collect(monkeypatch, _snap(claude_available=False))
        ctx = types.SimpleNamespace(task_complexity="moderate")
        out = UrgencyRouter.context_budget_profile(ProviderRoute.IMMEDIATE, ctx)
        assert out["max_dw_wait_s"] == 180.0
        assert out["tier0_fraction"] == 1.0

    def test_claude_down_trivial_complexity_is_reflex_window(self, monkeypatch):
        # trivial ops are exempt from the exploration floor → no tool loop → 60s.
        self._patch_collect(monkeypatch, _snap(claude_available=False))
        ctx = types.SimpleNamespace(task_complexity="trivial")
        out = UrgencyRouter.context_budget_profile(ProviderRoute.IMMEDIATE, ctx)
        assert out["max_dw_wait_s"] == 60.0

    def test_claude_up_self_heals_via_seam(self, monkeypatch):
        self._patch_collect(monkeypatch, _snap(claude_available=True))
        ctx = types.SimpleNamespace(task_complexity="moderate")
        out = UrgencyRouter.context_budget_profile(ProviderRoute.IMMEDIATE, ctx)
        assert out == _LEGACY_IMMEDIATE

    def test_flag_off_seam_returns_legacy(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "false")
        ctx = types.SimpleNamespace(task_complexity="moderate")
        out = UrgencyRouter.context_budget_profile(ProviderRoute.IMMEDIATE, ctx)
        assert out == _LEGACY_IMMEDIATE

    def test_seam_is_fail_soft_on_collector_error(self, monkeypatch):
        import backend.core.ouroboros.governance.provider_availability as pa

        def _boom(**k):
            raise RuntimeError("collector blew up")

        monkeypatch.setattr(pa, "collect_provider_availability", _boom)
        ctx = types.SimpleNamespace(task_complexity="moderate")
        # Wiring errors must never crash routing — fall back to legacy.
        out = UrgencyRouter.context_budget_profile(ProviderRoute.IMMEDIATE, ctx)
        assert out == _LEGACY_IMMEDIATE


class TestEndToEndCollectThenSynthesize:
    def test_economic_outage_yields_funded_dw_window_no_deadline_starvation(self):
        snap = collect_provider_availability(
            breaker=_FakeBreaker(CircuitState.OPEN, economic=1),
            ledger=_healthy_ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        out = UrgencyRouter.route_budget_profile(
            ProviderRoute.IMMEDIATE, snap, tool_loop_demanded=True,
        )
        # The funded primary now receives a real execution window — the op can
        # reach the DW agentic path instead of deadline_exhausted_pre_fallback.
        assert out["max_dw_wait_s"] >= 180.0
        assert out["tier0_fraction"] == 1.0
