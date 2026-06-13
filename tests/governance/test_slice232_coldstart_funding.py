"""Slice 232 (RC1) — cold-start provider-funding signal.

The live soak (Slice 231 validation) proved the budget synthesizer fires
correctly BUT its 240s generation-timeout lift never engaged
(``gen_timeout lifted count: 0``) for GOAL-001 — the very op it was built for.

Root cause: a cold-start TIMING gap. The Claude breaker boots CLOSED (the
global persist ENABLE flag is off), so at the ROUTE/gen-timeout moment the
collector reads ``claude_available=True``; the breaker only trips OPEN *during*
GENERATE on the first 402 — too late for the lift, which is decided before
dispatch. The persisted ``.jarvis/claude_breaker_state.json`` already records a
fresh economic OPEN; the collector just wasn't reading it.

Fix: the collector derives Claude funding from the AUTHORITATIVE persisted
breaker state at op #1 — reusing the existing
``claude_circuit_breaker._read_breaker_state`` reader (TTL-aware, fail-soft) —
INDEPENDENT of the persist enable flag. In-memory OPEN keeps precedence;
missing/stale/corrupt persisted state degrades gracefully to "available" (the
live breaker still trips on the first 402).
"""
from __future__ import annotations

import time

import backend.core.ouroboros.governance.claude_circuit_breaker as CB
from backend.core.ouroboros.governance.claude_circuit_breaker import CircuitState
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthRecord,
    SurfaceVerdict,
)
from backend.core.ouroboros.governance.provider_availability import (
    collect_provider_availability,
)
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    synthesize_generation_timeout,
)


class _Breaker:
    """In-memory breaker double (read-only surface the collector uses)."""

    def __init__(self, state, *, economic=0, transport=0):
        self._state = state
        self._e = economic
        self._t = transport

    @property
    def state(self):
        return self._state

    def snapshot(self):
        return {
            "state": self._state.value,
            "consecutive_economic_failures": self._e,
            "consecutive_transport_failures": self._t,
        }


class _Ledger:
    def __init__(self, verdict=SurfaceVerdict.HEALTHY):
        self._v = verdict

    def verdict_for(self, surface):
        return SurfaceHealthRecord(surface=surface, verdict=self._v)


def _collect(breaker, *, persisted_reader):
    return collect_provider_availability(
        breaker=breaker,
        ledger=_Ledger(),
        claude_disabled=False,
        breaker_enabled=True,
        persisted_reader=persisted_reader,
    )


class TestColdStartFundingSignal:
    def test_coldboot_persisted_open_marks_claude_unavailable(self):
        # In-memory breaker CLOSED (cold boot, persist-flag off) but the
        # persisted source of truth shows a fresh economic OPEN → unavailable.
        snap = _collect(_Breaker(CircuitState.CLOSED), persisted_reader=lambda: 1)
        assert snap.claude_available is False
        assert "economic" in snap.claude_reason and "persisted" in snap.claude_reason

    def test_coldboot_no_persisted_state_is_available(self):
        # Nothing persisted (or stale → reader returns None) → legacy available.
        snap = _collect(_Breaker(CircuitState.CLOSED), persisted_reader=lambda: None)
        assert snap.claude_available is True
        assert snap.claude_reason == "closed"

    def test_inmemory_open_takes_precedence_over_persisted(self):
        # A live OPEN is authoritative; the persisted consult must not override
        # or be needed when the in-memory breaker already says OPEN.
        snap = _collect(
            _Breaker(CircuitState.OPEN, economic=1), persisted_reader=lambda: None,
        )
        assert snap.claude_available is False
        assert snap.claude_reason == "breaker_open_economic"

    def test_structural_disable_takes_precedence_over_persisted(self):
        snap = collect_provider_availability(
            breaker=_Breaker(CircuitState.CLOSED),
            ledger=_Ledger(),
            claude_disabled=True,
            breaker_enabled=True,
            persisted_reader=lambda: 1,
        )
        assert snap.claude_available is False
        assert snap.claude_reason == "structurally_disabled"

    def test_persisted_reader_exception_degrades_gracefully(self):
        # Corrupt/unreadable persisted state → no crash, no signal → available
        # (the live breaker still trips on the first 402). Never a hard fail.
        def _boom():
            raise RuntimeError("corrupt persisted state")

        snap = _collect(_Breaker(CircuitState.CLOSED), persisted_reader=_boom)
        assert snap.claude_available is True

    def test_default_reader_is_the_real_persistence_layer(self):
        # No injected reader → the collector must consult the REAL
        # _read_breaker_state (no duplicate path). Smoke: it doesn't crash and
        # returns a well-formed snapshot regardless of on-disk state.
        snap = collect_provider_availability(
            breaker=_Breaker(CircuitState.CLOSED),
            ledger=_Ledger(),
            claude_disabled=False,
            breaker_enabled=True,
        )
        assert isinstance(snap.claude_available, bool)


class TestEconomicRecordAlwaysMaintained:
    """The funding-memory WRITE must be flag-independent too, else the persisted
    record goes stale (the 90h-stale host file we observed) and the collector's
    cold-start signal is never populated. The §33.1 gate keeps guarding the
    behavior-changing part — auto-boot-OPEN restore — which stays gated.
    """

    def test_economic_trip_persists_record_even_with_persist_flag_off(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED", raising=False)
        monkeypatch.setenv("JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED", "1")
        p = str(tmp_path / "breaker.json")
        b = CB.ClaudeCircuitBreaker(persist_path=p)
        b.record_economic_exhaustion("credit balance too low")
        # Record IS written despite persist-enable off → cold-start signal lives.
        assert CB._read_breaker_state(path=p, now_wall=time.time(), ttl_s=86400) == 1

    def test_restore_stays_gated_when_persist_flag_off(self, tmp_path, monkeypatch):
        # §33.1 invariant preserved: a fresh persisted OPEN must NOT auto-boot the
        # breaker OPEN when the persist flag is off — only the record is kept.
        monkeypatch.delenv("JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED", raising=False)
        p = str(tmp_path / "breaker.json")
        CB._write_breaker_state("open", 3, reason="economic", path=p, now_wall=time.time())
        b = CB.ClaudeCircuitBreaker(persist_path=p)
        assert b.state is CircuitState.CLOSED

    def test_recovery_clears_record_even_with_persist_flag_off(
        self, tmp_path, monkeypatch,
    ):
        # A refunded lane must clear the record so the collector stops reporting
        # down — also flag-independent (else stale-down lingers until TTL).
        monkeypatch.delenv("JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED", raising=False)
        monkeypatch.setenv("JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED", "1")
        p = str(tmp_path / "breaker.json")
        b = CB.ClaudeCircuitBreaker(persist_path=p)
        b.record_economic_exhaustion("credit balance too low")
        assert CB._read_breaker_state(path=p, now_wall=time.time(), ttl_s=86400) == 1
        b.record_success()  # lane recovered
        assert CB._read_breaker_state(path=p, now_wall=time.time(), ttl_s=86400) is None


class TestColdStartLiftEndToEnd:
    """The money test: with the cold-start signal populated, the 240s lift now
    fires for a GOAL-001-shaped op from op #1 (was 0 in the soak)."""

    def test_persisted_open_makes_lift_fire_for_immediate_toolloop(self):
        snap = _collect(_Breaker(CircuitState.CLOSED), persisted_reader=lambda: 1)
        out = synthesize_generation_timeout(
            ProviderRoute.IMMEDIATE, 120.0, snap,
            tool_loop_demanded=True, elevated_timeout_s=240.0,
        )
        assert out == 240.0

    def test_no_persisted_signal_keeps_base_until_live_trip(self):
        # Cold boot, nothing persisted → no lift yet (claude appears available);
        # the live trip path handles it once the breaker opens.
        snap = _collect(_Breaker(CircuitState.CLOSED), persisted_reader=lambda: None)
        out = synthesize_generation_timeout(
            ProviderRoute.IMMEDIATE, 120.0, snap,
            tool_loop_demanded=True, elevated_timeout_s=240.0,
        )
        assert out == 120.0
