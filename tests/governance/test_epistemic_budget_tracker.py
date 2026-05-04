"""Upgrade 1 Slice 2 — EpistemicBudgetTracker tests (PRD §31.2).

Pins:
  * Lifecycle (open/close/get) — Decision A1 per-op_id dict
  * Idempotent open — reopening returns existing without reset
  * Frozen-swap atomic mutation pattern (no in-place mutation)
  * Trajectory math — peak/nadir/latest/dropped_in_window
  * Bounded ring (samples truncated at _TRAJECTORY_MAX_SAMPLES=32)
  * Probe + SBT verdict normalization (enum / string / object)
  * note_round_complete with no confidence — only round count
  * note_round_complete on untracked op — no-op (None)
  * next_action on untracked op — DISABLED
  * TTL orphan cleanup
  * Threadsafe under concurrent access
  * Default tracker singleton + reset_for_tests
  * Full happy-path: open → rounds with drop → probe →
    CONVERGED → close
"""
from __future__ import annotations

import threading
import time

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
    )


# ---------------------------------------------------------------------------
# § 1 — Lifecycle (open/close/get)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_open_returns_new_budget(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        b = t.open(
            op_id="op-1", route="standard",
            risk_tier="safe_auto",
        )
        assert b is not None
        assert b.op_id == "op-1"
        assert b.route == "standard"
        assert b.risk_tier == "safe_auto"
        assert b.rounds_consumed == 0
        assert b.created_at_unix > 0

    def test_open_normalizes_route_lowercase(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        b = t.open(
            op_id="op-x", route="STANDARD",
            risk_tier="SAFE_AUTO",
        )
        assert b.route == "standard"
        assert b.risk_tier == "safe_auto"

    def test_open_idempotent_returns_existing(
        self, monkeypatch,
    ):
        """Reopening an existing op_id MUST return the existing
        budget unchanged — does NOT reset counters. Operator
        wanting reset must close first."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        b1 = t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        # Mutate first
        t.note_round_complete("op-x", confidence=0.7)
        # Reopen
        b2 = t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        # Counters preserved
        assert b2.rounds_consumed == 1

    def test_close_returns_last_budget(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.8)
        last = t.close("op-x")
        assert last is not None
        assert last.rounds_consumed == 1
        # Subsequent get returns None
        assert t.get("op-x") is None

    def test_close_untracked_returns_none(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        assert t.close("nonexistent") is None

    def test_get_untracked_returns_none(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        assert t.get("nonexistent") is None

    def test_open_garbage_op_id_returns_none(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        assert t.open(
            op_id="", route="standard", risk_tier="safe_auto",
        ) is None
        assert t.open(
            op_id=None, route="standard",  # type: ignore[arg-type]
            risk_tier="safe_auto",
        ) is None


# ---------------------------------------------------------------------------
# § 2 — Frozen-swap mutation pattern (no in-place mutation)
# ---------------------------------------------------------------------------


class TestFrozenSwap:
    def test_mutations_swap_atomically(self, monkeypatch):
        """Each mutation returns a NEW frozen instance. The dict
        is the only mutable surface."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        b1 = t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        b2 = t.note_round_complete("op-x", confidence=0.8)
        # Different frozen instances
        assert b1 is not b2
        # b1 is unchanged (frozen)
        assert b1.rounds_consumed == 0
        # b2 reflects the mutation
        assert b2.rounds_consumed == 1
        # Dict now stores b2
        assert t.get("op-x") is b2

    def test_returned_budget_is_frozen(self, monkeypatch):
        from dataclasses import FrozenInstanceError
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        b = t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        with pytest.raises(FrozenInstanceError):
            b.rounds_consumed = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# § 3 — note_round_complete + trajectory math
# ---------------------------------------------------------------------------


class TestRoundComplete:
    def test_increments_rounds(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        for i in range(5):
            b = t.note_round_complete("op-x", confidence=0.7)
            assert b.rounds_consumed == i + 1

    def test_confidence_none_keeps_trajectory_unchanged(
        self, monkeypatch,
    ):
        """Slice 3 may call note_round_complete WITHOUT a
        confidence reading (e.g., between rounds before
        ConfidenceMonitor has updated). Trajectory unchanged."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        b1 = t.note_round_complete("op-x", confidence=0.8)
        b2 = t.note_round_complete("op-x", confidence=None)
        # Round still incremented
        assert b2.rounds_consumed == 2
        # Trajectory same instance (no re-computation)
        assert (
            b2.confidence_trajectory.latest
            == b1.confidence_trajectory.latest
        )

    def test_first_sample_sets_peak_nadir_latest(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        b = t.note_round_complete("op-x", confidence=0.7)
        traj = b.confidence_trajectory
        assert traj.peak == 0.7
        assert traj.nadir == 0.7
        assert traj.latest == 0.7
        assert traj.dropped_in_window is False

    def test_drop_detection(self, monkeypatch):
        """peak=0.9, latest=0.5 → drop=0.4 ≥ threshold 0.25 →
        dropped_in_window=True."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.9)
        b = t.note_round_complete("op-x", confidence=0.5)
        assert b.confidence_trajectory.peak == 0.9
        assert b.confidence_trajectory.latest == 0.5
        assert b.confidence_trajectory.dropped_in_window is True

    def test_no_drop_when_below_threshold(self, monkeypatch):
        """drop=0.1 < threshold 0.25 → dropped_in_window=False."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.9)
        b = t.note_round_complete("op-x", confidence=0.8)
        assert (
            b.confidence_trajectory.dropped_in_window is False
        )

    def test_bounded_ring_truncates_to_32(self, monkeypatch):
        """Pump 50 samples; trajectory caps at 32."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        for i in range(50):
            t.note_round_complete(
                "op-x", confidence=0.5 + (i % 5) * 0.1,
            )
        b = t.get("op-x")
        # _TRAJECTORY_MAX_SAMPLES = 32
        assert len(b.confidence_trajectory.samples) == 32

    def test_untracked_op_returns_none(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        # Did NOT open
        assert (
            t.note_round_complete("ghost", confidence=0.7)
            is None
        )

    def test_sample_round_index_correct(self, monkeypatch):
        """ConfidenceSample.at_round_index reflects the round
        being recorded (1-indexed since round 0 is pre-first-call)."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        b1 = t.note_round_complete("op-x", confidence=0.8)
        b2 = t.note_round_complete("op-x", confidence=0.7)
        samples = b2.confidence_trajectory.samples
        assert samples[0].at_round_index == 1
        assert samples[1].at_round_index == 2


# ---------------------------------------------------------------------------
# § 4 — note_probe_completed + verdict normalization
# ---------------------------------------------------------------------------


class TestProbeCompleted:
    def test_increments_probe_calls(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        for i in range(3):
            b = t.note_probe_completed(
                "op-x", verdict="confirmed",
            )
            assert b.probe_calls_consumed == i + 1

    def test_normalizes_string_verdict(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        b = t.note_probe_completed(
            "op-x", verdict="CONFIRMED",
        )
        # Lowercase normalized
        assert b.last_probe_verdict == "confirmed"

    def test_normalizes_enum_verdict(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (  # noqa: E501
            ProbeVerdict,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        # Pass actual ProbeVerdict enum member
        b = t.note_probe_completed(
            "op-x", verdict=ProbeVerdict.INCONCLUSIVE_DIMINISHING,
        )
        assert (
            b.last_probe_verdict == "inconclusive_diminishing"
        )

    def test_garbage_verdict_yields_none(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        b = t.note_probe_completed("op-x", verdict=None)
        # None verdict normalizes to None
        assert b.last_probe_verdict is None

    def test_untracked_op_returns_none(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        assert (
            t.note_probe_completed("ghost", verdict="confirmed")
            is None
        )


# ---------------------------------------------------------------------------
# § 5 — note_sbt_completed
# ---------------------------------------------------------------------------


class TestSBTCompleted:
    def test_increments_branch_calls(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="notify_apply",
        )
        for i in range(2):
            b = t.note_sbt_completed(
                "op-x", verdict="consensus",
            )
            assert b.branch_calls_consumed == i + 1

    def test_records_last_sbt_verdict(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="notify_apply",
        )
        b = t.note_sbt_completed(
            "op-x", verdict="DISAGREEMENT",
        )
        assert b.last_sbt_verdict == "disagreement"


# ---------------------------------------------------------------------------
# § 6 — next_action
# ---------------------------------------------------------------------------


class TestNextAction:
    def test_dispatches_to_compute_budget_action(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        # No drop, no exhaustion → WITHIN_BUDGET
        action = t.next_action("op-x")
        assert action.outcome is BudgetOutcome.WITHIN_BUDGET

    def test_untracked_returns_disabled(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        action = t.next_action("ghost")
        assert action.outcome is BudgetOutcome.DISABLED
        assert "op_not_tracked" in action.reason

    def test_full_lifecycle_to_converged(self, monkeypatch):
        """Happy path: open → 2 rounds with drop → probe
        confirmed → CONVERGED."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        # PROBE_TRIGGERED at this point
        a1 = t.next_action("op-x")
        assert a1.outcome is BudgetOutcome.PROBE_TRIGGERED
        # Probe runs; confirmed → CONVERGED next
        t.note_probe_completed("op-x", verdict="confirmed")
        a2 = t.next_action("op-x")
        assert a2.outcome is BudgetOutcome.CONVERGED


# ---------------------------------------------------------------------------
# § 7 — TTL orphan cleanup
# ---------------------------------------------------------------------------


class TestReapOrphans:
    def test_reaps_stale_trackers(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        # Open 3 trackers at synthetic time t=1000
        for i in range(3):
            t.open(
                op_id=f"op-{i}", route="standard",
                risk_tier="safe_auto",
                now_ts=1000.0,
            )
        # Reap with now=10000 + ttl=3600 → all stale (delta=9000)
        reaped = t.reap_orphans(now_ts=10000.0, ttl_s=3600)
        assert reaped == 3
        assert len(t) == 0

    def test_keeps_fresh_trackers(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        # Open at t=1000
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
            now_ts=1000.0,
        )
        # Reap with now=2000 + ttl=3600 → fresh (delta=1000 < 3600)
        reaped = t.reap_orphans(now_ts=2000.0, ttl_s=3600)
        assert reaped == 0
        assert len(t) == 1

    def test_zero_ttl_disables_reaping(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
            now_ts=1000.0,
        )
        # ttl_s=0 → fail-safe, no reaping
        reaped = t.reap_orphans(now_ts=99999.0, ttl_s=0)
        assert reaped == 0
        assert len(t) == 1

    def test_default_ttl_from_env(self, monkeypatch):
        """When ttl_s arg omitted, defers to
        epistemic_tracker_ttl_s() env reader."""
        _enable(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_TRACKER_TTL_S", "60",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
            now_ts=1000.0,
        )
        # Reap at now=2000 with default ttl=60 → stale (delta=1000 > 60)
        reaped = t.reap_orphans(now_ts=2000.0)
        assert reaped == 1


# ---------------------------------------------------------------------------
# § 8 — Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_open_is_safe(self, monkeypatch):
        """Multiple threads opening the SAME op_id concurrently
        must converge on a single budget instance (no torn
        state)."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()

        def opener():
            t.open(
                op_id="shared-op", route="standard",
                risk_tier="safe_auto",
            )

        threads = [
            threading.Thread(target=opener) for _ in range(10)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # Exactly one budget for shared-op
        assert len(t) == 1

    def test_concurrent_round_completes_increment_correctly(
        self, monkeypatch,
    ):
        """N threads each calling note_round_complete N times
        on the same op → final rounds_consumed == N*N
        (atomic increments under lock)."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )

        N = 5
        ROUNDS = 20

        def rounder():
            for _ in range(ROUNDS):
                t.note_round_complete("op-x", confidence=0.7)

        threads = [
            threading.Thread(target=rounder) for _ in range(N)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        b = t.get("op-x")
        assert b.rounds_consumed == N * ROUNDS

    def test_concurrent_open_different_ops(self, monkeypatch):
        """N threads each opening a distinct op_id → exactly N
        budgets in the dict."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()

        N = 20

        def opener(idx):
            t.open(
                op_id=f"op-{idx}", route="standard",
                risk_tier="safe_auto",
            )

        threads = [
            threading.Thread(target=opener, args=(i,))
            for i in range(N)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(t) == N


# ---------------------------------------------------------------------------
# § 9 — Default tracker singleton
# ---------------------------------------------------------------------------


class TestDefaultTracker:
    def test_get_default_returns_singleton(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            get_default_tracker,
            reset_default_tracker_for_tests,
        )
        reset_default_tracker_for_tests()
        t1 = get_default_tracker()
        t2 = get_default_tracker()
        assert t1 is t2

    def test_reset_replaces_singleton(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            get_default_tracker,
            reset_default_tracker_for_tests,
        )
        t1 = get_default_tracker()
        reset_default_tracker_for_tests()
        t2 = get_default_tracker()
        assert t1 is not t2


# ---------------------------------------------------------------------------
# § 10 — Observability surface for Slice 4
# ---------------------------------------------------------------------------


class TestObservabilityHooks:
    def test_all_op_ids_returns_snapshot(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        for i in range(3):
            t.open(
                op_id=f"op-{i}", route="standard",
                risk_tier="safe_auto",
            )
        op_ids = t.all_op_ids()
        assert set(op_ids) == {"op-0", "op-1", "op-2"}

    def test_len_reflects_open_trackers(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        assert len(t) == 0
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        assert len(t) == 1
        t.close("op-x")
        assert len(t) == 0
