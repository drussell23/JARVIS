"""Slice 12AA — Per-Op Budget Reservation.

bt-2026-05-23-225740 (Slice 12Z validation soak) proved loop survival
across 6 consecutive clean soaks but the fixture op STILL hit
``session_budget_preflight_refused: claude_est=$0.5000 >
session_remaining=$0.4150``. Cost-shape audit of that soak + the prior
Slice 12X soak showed the fixture's cumulative Claude streaming chunks
totaled ~$0.51-$0.59 — matching Claude provider's internal
``_max_cost_per_op`` (~$0.585) almost exactly. The fixture needs that
much **contiguous runway**, but concurrent sensor calls + its own
earlier successful chunks together pushed total_spent past
(cap - foreground_reserve), starving the next fixture call.

Slice 12Y's background-spend ceiling kept sensors below the threshold
organically (0 refusals — they stayed under by themselves), but the
ceiling guarantees "the tier" has reserved budget, NOT that any single
foreground op has a contiguous slot.

# Slice 12AA — Per-op reservation closes the gap

When a foreground op enters the pipeline, the SBA reserves its
expected total runway (caller-supplied amount, typically the
provider's ``_max_cost_per_op`` — provider-derived, NOT a
hardcoded SWE-Bench value). Other ops see the reservation as
unavailable budget. The owning op spends from its own reservation
across multiple provider calls. Released at op terminal state.
Background ops can NEVER reserve; they use the Slice 12Y ceiling.

# Architecture

* :func:`acquire_reservation` — atomic per-op-id reserve via
  module-level ``_reservations`` dict guarded by
  ``_reservations_lock``.
* :func:`release_reservation` — idempotent pop, called by
  orchestrator's Slice 12Q terminal chokepoint.
* :func:`check_preflight` accepts optional ``op_id``. Computes
  ``effective_remaining = remaining - sum(other reservations)``.
  The owning op's reservation is excluded so it spends from its
  own runway. Slice 12Y ceiling + generic refusal both use
  ``effective_remaining``.
* Lazy acquire in Claude provider preflight — first call for each
  op_id auto-reserves ``_max_cost_per_op``. Idempotent.
* Release wired into orchestrator's ``_record_ledger`` Slice 12Q
  chokepoint.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import session_budget_authority
from backend.core.ouroboros.governance.session_budget_authority import (
    BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR,
    PER_OP_RESERVATION_ENABLED_ENV_VAR,
    Reservation,
    SessionBudgetPreflightRefused,
    _BACKGROUND_TIER_SIGNAL_SOURCES,
    _sum_other_reservations,
    acquire_reservation,
    check_preflight,
    get_reservations_snapshot,
    per_op_reservation_enabled,
    release_reservation,
    reset_for_tests,
    set_session_budget_provider,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    import os
    for var in (
        PER_OP_RESERVATION_ENABLED_ENV_VAR,
        BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR,
        "JARVIS_S2_SESSION_BUDGET_USD",
        "OUROBOROS_BATTLE_COST_CAP",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


class _FakeProvider:
    def __init__(self, total_spent: float, remaining: float):
        self.total_spent = total_spent
        self.remaining = remaining


# ──────────────────────────────────────────────────────────────────────
# Part 1 — Env knob + Reservation dataclass
# ──────────────────────────────────────────────────────────────────────


class TestPart1MasterSwitch:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv(
            PER_OP_RESERVATION_ENABLED_ENV_VAR, raising=False,
        )
        assert per_op_reservation_enabled() is True

    @pytest.mark.parametrize("raw, expected", [
        ("true", True), ("1", True), ("on", True), ("yes", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
        ("garbage", False),
    ])
    def test_truthy_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv(
            PER_OP_RESERVATION_ENABLED_ENV_VAR, raw,
        )
        assert per_op_reservation_enabled() is expected


class TestPart1ReservationShape:
    def test_frozen_dataclass(self):
        r = Reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            reserved_usd=0.50, acquired_at_monotonic=1.0,
        )
        with pytest.raises((AttributeError, Exception)):
            r.op_id = "op-2"  # type: ignore[misc]

    def test_carries_fields(self):
        r = Reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            reserved_usd=0.50, acquired_at_monotonic=1.0,
        )
        assert r.op_id == "op-1"
        assert r.signal_source == "swe_bench_pro"
        assert r.reserved_usd == 0.50


# ──────────────────────────────────────────────────────────────────────
# Part 2 — acquire_reservation
# ──────────────────────────────────────────────────────────────────────


class TestAcquireReservation:
    def test_foreground_succeeds(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="fg-op",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is True
        snap = get_reservations_snapshot()
        assert len(snap) == 1
        assert snap[0].reserved_usd == 0.60

    def test_background_rejected(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        for src in _BACKGROUND_TIER_SIGNAL_SOURCES:
            assert acquire_reservation(
                op_id=f"bg-{src}",
                signal_source=src,
                estimated_total_usd=0.01,
            ) is False
        assert len(get_reservations_snapshot()) == 0

    def test_zero_amount_rejected(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="op", signal_source="swe_bench_pro",
            estimated_total_usd=0.0,
        ) is False
        assert acquire_reservation(
            op_id="op", signal_source="swe_bench_pro",
            estimated_total_usd=-1.0,
        ) is False

    def test_no_room_rejected(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="hog", signal_source="swe_bench_pro",
            estimated_total_usd=0.80,
        ) is True
        assert acquire_reservation(
            op_id="hungry", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        ) is False
        # $0.15 fits comfortably (avoid float-precision edge at $0.20).
        assert acquire_reservation(
            op_id="hungry", signal_source="swe_bench_pro",
            estimated_total_usd=0.15,
        ) is True

    def test_reacquire_replaces(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.30,
        ) is True
        assert acquire_reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is True
        snap = get_reservations_snapshot()
        assert len(snap) == 1
        assert snap[0].reserved_usd == 0.60

    def test_master_disabled_rejects(self, monkeypatch):
        monkeypatch.setenv(
            PER_OP_RESERVATION_ENABLED_ENV_VAR, "false",
        )
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="op", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is False

    def test_no_provider_rejects(self):
        reset_for_tests()
        assert acquire_reservation(
            op_id="op", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is False


# ──────────────────────────────────────────────────────────────────────
# Part 3 — release_reservation
# ──────────────────────────────────────────────────────────────────────


class TestReleaseReservation:
    def test_releases_existing(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="op", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        )
        assert release_reservation("op") is True
        assert len(get_reservations_snapshot()) == 0

    def test_idempotent(self):
        assert release_reservation("missing") is False
        assert release_reservation("missing") is False

    def test_empty_op_id_rejected(self):
        assert release_reservation("") is False

    def test_released_runway_available_to_others(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="hog", signal_source="swe_bench_pro",
            estimated_total_usd=0.90,
        )
        assert acquire_reservation(
            op_id="hungry", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        ) is False
        release_reservation("hog")
        assert acquire_reservation(
            op_id="hungry", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        ) is True


# ──────────────────────────────────────────────────────────────────────
# Part 4 — check_preflight reservation composition (LOAD-BEARING)
# ──────────────────────────────────────────────────────────────────────


class TestPreflightReservationAware:
    def test_owning_op_spends_against_own_reservation(self):
        """The fixture-COMPLETE scenario."""
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="fixture",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is True
        # Simulate budget consumption — fixture's own reservation
        # protects its later spend.
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.50,
            signal_source="swe_bench_pro",
            op_id="fixture",
        )

    def test_other_op_cannot_consume_reserved_runway(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="fixture", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        )
        with pytest.raises(SessionBudgetPreflightRefused) as exc:
            check_preflight(
                provider_name="claude",
                estimated_cost_usd=0.50,
                signal_source="swe_bench_pro",
                op_id="other-fg",
            )
        assert "background_spend_ceiling" not in exc.value.reason

    def test_background_blocked_by_foreground_reservation(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="fixture", signal_source="swe_bench_pro",
            estimated_total_usd=0.80,
        )
        with pytest.raises(SessionBudgetPreflightRefused) as exc:
            check_preflight(
                provider_name="dw",
                estimated_cost_usd=0.01,
                signal_source="todo_scanner",
            )
        assert exc.value.reason == (
            "session_budget_preflight_refused:"
            "background_spend_ceiling"
        )

    def test_legacy_no_op_id_path(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="fixture", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        )
        # Legacy caller sees effective=0.40, $0.30 fits.
        check_preflight(
            provider_name="claude", estimated_cost_usd=0.30,
        )
        # $0.50 refused.
        with pytest.raises(SessionBudgetPreflightRefused):
            check_preflight(
                provider_name="claude", estimated_cost_usd=0.50,
            )


# ──────────────────────────────────────────────────────────────────────
# Part 5 — _sum_other_reservations helper
# ──────────────────────────────────────────────────────────────────────


class TestSumOtherReservations:
    def test_empty_returns_zero(self):
        assert _sum_other_reservations("anyone") == 0.0
        assert _sum_other_reservations(None) == 0.0

    def test_sums_others_excludes_self(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="A", signal_source="swe_bench_pro",
            estimated_total_usd=0.30,
        )
        acquire_reservation(
            op_id="B", signal_source="swe_bench_pro",
            estimated_total_usd=0.20,
        )
        assert _sum_other_reservations("A") == 0.20
        assert _sum_other_reservations("B") == 0.30
        assert _sum_other_reservations(None) == 0.50
        assert _sum_other_reservations("unknown") == 0.50


# ──────────────────────────────────────────────────────────────────────
# Part 6 — AST pins (lazy acquire + orchestrator release)
# ──────────────────────────────────────────────────────────────────────


class TestPart6ASTPins:
    def test_claude_provider_lazy_acquires(self):
        src = Path(
            "backend/core/ouroboros/governance/providers.py"
        ).read_text()
        idx = src.find("_sba_check_preflight(")
        assert idx > 0
        block = src[max(0, idx - 2000):idx]
        assert "acquire_reservation" in block or "_sba_acquire" in block, (
            "Claude provider preflight site does not lazy-acquire "
            "a reservation — fixture won't get contiguous runway"
        )
        assert "_max_cost_per_op" in block, (
            "Reservation amount no longer comes from provider's "
            "_max_cost_per_op — hardcoded sizing introduced"
        )

    def test_orchestrator_terminal_releases(self):
        src = Path(
            "backend/core/ouroboros/governance/orchestrator.py"
        ).read_text()
        assert "release_reservation" in src, (
            "Orchestrator does not release reservations at terminal"
        )
        assert "Slice 12AA" in src

    def test_preflight_signature_takes_op_id(self):
        sig = inspect.signature(check_preflight)
        assert "op_id" in sig.parameters
        assert sig.parameters["op_id"].default is None

    def test_preflight_uses_effective_remaining(self):
        src = Path(
            "backend/core/ouroboros/governance/session_budget_authority.py"
        ).read_text()
        assert "effective_remaining" in src
        assert "est > effective_remaining" in src


# ──────────────────────────────────────────────────────────────────────
# Public surface
# ──────────────────────────────────────────────────────────────────────


class TestPublicSurface:
    def test_exports_complete(self):
        for name in (
            "PER_OP_RESERVATION_ENABLED_ENV_VAR",
            "Reservation",
            "acquire_reservation",
            "release_reservation",
            "get_reservations_snapshot",
            "per_op_reservation_enabled",
        ):
            assert hasattr(session_budget_authority, name)
            assert name in session_budget_authority.__all__

    def test_reset_for_tests_clears_reservations(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="op", signal_source="swe_bench_pro",
            estimated_total_usd=0.30,
        )
        assert len(get_reservations_snapshot()) == 1
        reset_for_tests()
        assert len(get_reservations_snapshot()) == 0
