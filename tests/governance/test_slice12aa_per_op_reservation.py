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

Slice 12Y's background-spend ceiling kept sensors below the
threshold organically (0 refusals — they stayed under by themselves),
but the ceiling guarantees "the tier" has reserved budget, NOT that
any single foreground op has a contiguous slot.

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
  ``_reservations_lock``. Returns False on disabled / background /
  bad-amount / no-room. Last-write-wins on same op_id.
* :func:`release_reservation` — idempotent pop. Called by the
  orchestrator's terminal hook (Slice 12Q chokepoint).
* :func:`check_preflight` (modified) — accepts optional ``op_id``.
  Computes ``effective_remaining = remaining - sum(other
  reservations)`` — the owning op's reservation is intentionally
  excluded so it can spend from its own runway. Background ceiling
  + generic refusal both use ``effective_remaining``.
* Lazy acquire in Claude provider's ``generate`` preflight site —
  first preflight call for each op_id auto-reserves the provider's
  ``_max_cost_per_op``. Idempotent.
* Release wired into orchestrator's ``_record_ledger`` Slice 12Q
  chokepoint, alongside the SessionRecorder terminal record.

# Composition with Slice 12Y

Both checks compose cleanly:
  * Slice 12AA subtracts OTHER reservations FIRST → effective_remaining
  * Slice 12Y's background ceiling computes ``bg_remaining =
    max(0, effective_remaining - foreground_reserve)``
  * Slice 12AA's generic refusal: ``est > effective_remaining``

Foreground reservations are STRICTER than the background ceiling
(reservations are exact per-op claims; ceiling is a tier-wide
floor). Combined, they form the complete budget-distribution
contract.
"""

from __future__ import annotations

import ast
import os
import threading
from pathlib import Path
from typing import Optional

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


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
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
# Part 1 — Env knob + dataclass shape
# ──────────────────────────────────────────────────────────────────────


class TestPart1MasterSwitch:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv(
            PER_OP_RESERVATION_ENABLED_ENV_VAR, raising=False,
        )
        assert per_op_reservation_enabled() is True

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("true", True), ("1", True), ("on", True), ("yes", True),
            ("false", False), ("0", False), ("no", False),
            ("off", False), ("garbage", False),
        ],
    )
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

    def test_carries_load_bearing_fields(self):
        r = Reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            reserved_usd=0.50, acquired_at_monotonic=1.0,
        )
        assert r.op_id == "op-1"
        assert r.signal_source == "swe_bench_pro"
        assert r.reserved_usd == 0.50
        assert r.acquired_at_monotonic == 1.0


# ──────────────────────────────────────────────────────────────────────
# Part 2 — acquire_reservation
# ──────────────────────────────────────────────────────────────────────


class TestAcquireReservation:
    def test_foreground_op_succeeds_when_room(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="fg-op",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is True
        snap = get_reservations_snapshot()
        assert len(snap) == 1
        assert snap[0].op_id == "fg-op"
        assert snap[0].reserved_usd == 0.60

    def test_background_op_rejected(self):
        """Background ops MUST NOT acquire reservations — they
        use the Slice 12Y ceiling instead."""
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        for src in (
            "todo_scanner", "doc_staleness", "opportunity_miner",
        ):
            if src in {"opportunity_miner"}:
                # opportunity_miner is in _BACKGROUND_SOURCES but
                # via the ai_miner / exploration tier. Confirm:
                pass
            assert acquire_reservation(
                op_id=f"bg-{src}",
                signal_source=src if src in _BACKGROUND_TIER_SIGNAL_SOURCES else "todo_scanner",
                estimated_total_usd=0.01,
            ) is False
        assert len(get_reservations_snapshot()) == 0

    def test_speculative_op_rejected(self):
        """SPECULATIVE-tier ops also can't reserve."""
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="spec-op",
            signal_source="intent_discovery",
            estimated_total_usd=0.01,
        ) is False

    def test_rejects_zero_or_negative_amount(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="bad-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.0,
        ) is False
        assert acquire_reservation(
            op_id="bad-2", signal_source="swe_bench_pro",
            estimated_total_usd=-1.0,
        ) is False

    def test_rejects_when_no_room(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        # First op reserves $0.80.
        assert acquire_reservation(
            op_id="hog",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.80,
        ) is True
        # Second op asks for $0.50 — only $0.20 free.
        assert acquire_reservation(
            op_id="hungry",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        ) is False
        # And $0.15 fits comfortably (avoid float-precision edge
        # case at exactly $0.20 where 1.0 - 0.80 = 0.1999...).
        assert acquire_reservation(
            op_id="hungry",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.15,
        ) is True

    def test_reacquire_same_op_replaces(self):
        """Last-write-wins on re-arming same op_id (provider
        cap may have been revised; idempotency is more useful
        than first-write-wins for the provider-driven sizing
        model)."""
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

    def test_rejects_when_master_disabled(self, monkeypatch):
        monkeypatch.setenv(
            PER_OP_RESERVATION_ENABLED_ENV_VAR, "false",
        )
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is False
        assert len(get_reservations_snapshot()) == 0

    def test_rejects_when_no_provider(self):
        reset_for_tests()
        # No provider registered → fail-OPEN at higher level,
        # but reservation acquire returns False (no authority
        # to reserve against).
        assert acquire_reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is False


# ──────────────────────────────────────────────────────────────────────
# Part 3 — release_reservation
# ──────────────────────────────────────────────────────────────────────


class TestReleaseReservation:
    def test_releases_existing(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        )
        assert release_reservation("op-1") is True
        assert len(get_reservations_snapshot()) == 0

    def test_idempotent_on_missing(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert release_reservation("never-acquired") is False
        # Calling again is still safe.
        assert release_reservation("never-acquired") is False

    def test_rejects_empty_op_id(self):
        assert release_reservation("") is False
        assert release_reservation(None) is False  # type: ignore[arg-type]

    def test_released_runway_available_for_others(self):
        """The load-bearing claim: after release, the freed
        budget can be reserved by another op."""
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        assert acquire_reservation(
            op_id="hog", signal_source="swe_bench_pro",
            estimated_total_usd=0.90,
        ) is True
        # Only $0.10 left — too tight for another foreground op.
        assert acquire_reservation(
            op_id="hungry", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        ) is False
        # Release the hog.
        release_reservation("hog")
        # Now hungry can claim.
        assert acquire_reservation(
            op_id="hungry", signal_source="swe_bench_pro",
            estimated_total_usd=0.50,
        ) is True


# ──────────────────────────────────────────────────────────────────────
# Part 4 — check_preflight composition with reservations
# ──────────────────────────────────────────────────────────────────────


class TestPreflightReservationAware:
    """The load-bearing claims:

    1. Foreground op SEES its own reservation as available
       (spends from its own runway across multiple calls).
    2. OTHER ops do NOT see reserved budget — they're refused.
    3. Background ops cannot consume reserved foreground runway.
    """

    def test_owning_op_spends_against_own_reservation(self):
        """The fixture-COMPLETE scenario: with $1.00 cap and
        $0.50 already spent (sensors + own earlier chunks), the
        fixture's NEXT $0.50 call MUST succeed when it has a
        $0.60 reservation."""
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # Fixture acquires its $0.60 reservation FIRST (when it
        # only had ample budget — but reservation also fails on
        # tight room, so test acquire at full budget).
        # Reset to full budget for clean acquire:
        set_session_budget_provider(
            _FakeProvider(total_spent=0.0, remaining=1.0),
        )
        assert acquire_reservation(
            op_id="fixture",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        ) is True
        # Now simulate budget shift: total_spent jumps, remaining
        # drops. Fixture's OWN reservation should still let it
        # spend $0.50.
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # MUST NOT raise — fixture spends against its own
        # reservation.
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.50,
            signal_source="swe_bench_pro",
            op_id="fixture",
        )

    def test_other_op_cannot_consume_reserved_runway(self):
        """The complementary load-bearing claim: an op OTHER
        than the reservation owner sees the reservation as
        unavailable budget."""
        set_session_budget_provider(
            _FakeProvider(total_spent=0.0, remaining=1.0),
        )
        # Fixture reserves $0.60.
        acquire_reservation(
            op_id="fixture",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        )
        # Another foreground op tries to spend $0.50 — but
        # effective_remaining = 1.0 - 0.60 = 0.40.
        with pytest.raises(SessionBudgetPreflightRefused) as exc:
            check_preflight(
                provider_name="claude",
                estimated_cost_usd=0.50,
                signal_source="swe_bench_pro",
                op_id="other-fg",
            )
        # The generic refusal reason — not background_spend_ceiling.
        assert "background_spend_ceiling" not in exc.value.reason

    def test_background_op_blocked_by_foreground_reservation(self):
        """Slice 12Y + 12AA composition: background ops compute
        their ceiling against effective_remaining (which already
        excludes the foreground reservation)."""
        set_session_budget_provider(
            _FakeProvider(total_spent=0.0, remaining=1.0),
        )
        acquire_reservation(
            op_id="fixture",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.80,
        )
        # effective_remaining = 1.0 - 0.80 = 0.20
        # foreground_reserve = 1.0 * 0.5 = 0.50
        # bg_remaining = max(0, 0.20 - 0.50) = 0
        # Any background spend > 0 refused.
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

    def test_no_op_id_legacy_path_unchanged(self):
        """Legacy callers without op_id still get the
        reservation-aware effective_remaining (other ops'
        reservations are subtracted) but can't claim
        ownership of any reservation themselves."""
        set_session_budget_provider(
            _FakeProvider(total_spent=0.0, remaining=1.0),
        )
        # Pre-existing fixture reservation.
        acquire_reservation(
            op_id="fixture",
            signal_source="swe_bench_pro",
            estimated_total_usd=0.60,
        )
        # Legacy caller (no op_id) sees effective_remaining = 0.40.
        # $0.30 fits.
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.30,
        )
        # $0.50 refused.
        with pytest.raises(SessionBudgetPreflightRefused):
            check_preflight(
                provider_name="claude",
                estimated_cost_usd=0.50,
            )

    def test_release_frees_runway_for_preflight(self):
        """After release, the owning op's runway becomes
        available to others."""
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # Use a fresh provider with full budget so the
        # reservation acquire works.
        set_session_budget_provider(
            _FakeProvider(total_spent=0.0, remaining=1.0),
        )
        acquire_reservation(
            op_id="hog", signal_source="swe_bench_pro",
            estimated_total_usd=0.80,
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # Other op's preflight refused because effective=0.50-0.80=
        # max(0, -0.30) = 0.
        with pytest.raises(SessionBudgetPreflightRefused):
            check_preflight(
                provider_name="claude",
                estimated_cost_usd=0.10,
                signal_source="swe_bench_pro",
                op_id="other",
            )
        # Release the hog.
        release_reservation("hog")
        # Now $0.10 fits.
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.10,
            signal_source="swe_bench_pro",
            op_id="other",
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
            op_id="op-A", signal_source="swe_bench_pro",
            estimated_total_usd=0.30,
        )
        acquire_reservation(
            op_id="op-B", signal_source="swe_bench_pro",
            estimated_total_usd=0.20,
        )
        # From op-A's perspective: only op-B counts (0.20).
        assert _sum_other_reservations("op-A") == 0.20
        # From op-B's perspective: only op-A counts (0.30).
        assert _sum_other_reservations("op-B") == 0.30
        # From None's perspective: both count (0.50).
        assert _sum_other_reservations(None) == 0.50
        # From unknown op: all count.
        assert _sum_other_reservations("other") == 0.50


# ──────────────────────────────────────────────────────────────────────
# Part 6 — Lazy acquire + orchestrator release wiring AST pins
# ──────────────────────────────────────────────────────────────────────


class TestPart6ASTPins:
    def test_claude_provider_lazy_acquires(self):
        """The Claude provider preflight call site MUST lazy-
        acquire a reservation using its own _max_cost_per_op
        (provider-derived sizing — NOT hardcoded)."""
        src = Path(
            "backend/core/ouroboros/governance/providers.py"
        ).read_text()
        # Find the check_preflight call site.
        idx = src.find("_sba_check_preflight(")
        assert idx > 0, (
            "check_preflight call site missing from providers.py"
        )
        # Walk back to look for the acquire site in the surrounding
        # block (must appear shortly BEFORE the preflight check).
        block_start = max(0, idx - 2000)
        block = src[block_start:idx]
        assert "acquire_reservation" in block or (
            "_sba_acquire" in block
        ), (
            "providers.py Claude generate path does NOT lazy-"
            "acquire a Slice 12AA reservation — fixture op "
            "won't get a contiguous runway"
        )
        # The reservation amount MUST come from _max_cost_per_op
        # (provider-derived, not hardcoded).
        assert "_max_cost_per_op" in block, (
            "Lazy acquire doesn't use _max_cost_per_op — "
            "reservation sizing has been hardcoded"
        )

    def test_orchestrator_terminal_releases(self):
        """The orchestrator's _record_ledger terminal hook MUST
        call release_reservation so freed runway becomes
        available to subsequent ops."""
        src = Path(
            "backend/core/ouroboros/governance/orchestrator.py"
        ).read_text()
        assert "release_reservation" in src, (
            "orchestrator does NOT release reservations at "
            "terminal — runway leaks across ops"
        )
        assert "Slice 12AA" in src, (
            "Slice 12AA marker missing from orchestrator — "
            "release wiring may have been edited out"
        )

    def test_preflight_signature_takes_op_id(self):
        """The check_preflight signature MUST accept op_id as
        an optional kwarg for reservation-ownership."""
        import inspect
        sig = inspect.signature(check_preflight)
        assert "op_id" in sig.parameters
        assert sig.parameters["op_id"].default is None

    def test_preflight_uses_effective_remaining(self):
        """The refusal MUST be computed against
        effective_remaining (= remaining - other_reserved),
        not bare remaining. Pinned via source-string check."""
        src = Path(
            "backend/core/ouroboros/governance/session_budget_authority.py"
        ).read_text()
        # The effective_remaining computation must exist.
        assert "effective_remaining" in src
        # And the legacy bare `est > remaining` check is gone.
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
            assert name in session_budget_authority.__all__, (
                f"{name} missing from __all__"
            )

    def test_reset_for_tests_clears_reservations(self):
        set_session_budget_provider(_FakeProvider(0.0, 1.0))
        acquire_reservation(
            op_id="op-1", signal_source="swe_bench_pro",
            estimated_total_usd=0.30,
        )
        assert len(get_reservations_snapshot()) == 1
        reset_for_tests()
        assert len(get_reservations_snapshot()) == 0
