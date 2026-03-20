"""Tests for iteration data types — T04, T05, T07, T08, T09."""
import pytest
from backend.core.ouroboros.governance.autonomy.iteration_types import (
    IterationTask, IterationState, IterationStopPolicy, BlastRadiusPolicy,
    PlannerOutcome, PlannerRejectReason, PlanningContext, TaskRejectionTracker,
    compute_plan_id, compute_task_fingerprint, compute_policy_hash,
    RecoveryDecision, IterationBudgetWindow,
)
from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier


class TestIdempotencyKeys:
    def test_plan_id_stable_across_calls(self):
        """T04"""
        fp = compute_task_fingerprint("fix auth", ("auth.py",))
        ph = compute_policy_hash(IterationStopPolicy(), "governed")
        id1 = compute_plan_id(fp, ph, ("jarvis",))
        id2 = compute_plan_id(fp, ph, ("jarvis",))
        assert id1 == id2
        assert id1.startswith("plan-")

    def test_plan_id_changes_on_policy_change(self):
        """T05"""
        fp = compute_task_fingerprint("fix auth", ("auth.py",))
        p1 = IterationStopPolicy(max_iterations_per_session=10)
        p2 = IterationStopPolicy(max_iterations_per_session=20)
        id1 = compute_plan_id(fp, compute_policy_hash(p1, "governed"), ("jarvis",))
        id2 = compute_plan_id(fp, compute_policy_hash(p2, "governed"), ("jarvis",))
        assert id1 != id2

    def test_fingerprint_deterministic(self):
        fp1 = compute_task_fingerprint("fix X", ("a.py", "b.py"))
        fp2 = compute_task_fingerprint("fix X", ("b.py", "a.py"))
        assert fp1 == fp2  # sorted target_files

    def test_fingerprint_changes_on_description(self):
        fp1 = compute_task_fingerprint("fix X", ("a.py",))
        fp2 = compute_task_fingerprint("fix Y", ("a.py",))
        assert fp1 != fp2


class TestTaskRejectionTracker:
    def test_poisoned_after_threshold(self):
        """T07"""
        tracker = TaskRejectionTracker(poison_threshold=3)
        for _ in range(3):
            tracker.record_rejection("task-1", PlannerRejectReason.ORACLE_NO_DATA)
        assert tracker.is_poisoned("task-1")

    def test_not_poisoned_below_threshold(self):
        tracker = TaskRejectionTracker(poison_threshold=3)
        tracker.record_rejection("task-1", PlannerRejectReason.ORACLE_NO_DATA)
        assert not tracker.is_poisoned("task-1")

    def test_history_tracks_reasons(self):
        tracker = TaskRejectionTracker()
        tracker.record_rejection("t", PlannerRejectReason.BLAST_RADIUS_EXCEEDED)
        assert tracker.get_reject_history("t") == [PlannerRejectReason.BLAST_RADIUS_EXCEEDED]

    def test_unknown_task_not_poisoned(self):
        tracker = TaskRejectionTracker()
        assert not tracker.is_poisoned("unknown")


class TestBlastRadiusPolicy:
    def test_rejects_oversized_file_count(self):
        """T08"""
        policy = BlastRadiusPolicy(max_files_changed=10)
        assert policy.check_file_count(15) is not None

    def test_accepts_within_file_count(self):
        policy = BlastRadiusPolicy(max_files_changed=10)
        assert policy.check_file_count(5) is None

    def test_rejects_public_api_surface(self):
        """T09"""
        policy = BlastRadiusPolicy(max_public_api_files_touched=3)
        assert policy.check_public_api_count(4) is not None

    def test_rejects_excess_repos(self):
        policy = BlastRadiusPolicy(max_repos_touched=2)
        assert policy.check_repos(3) is not None

    def test_rejects_excess_lines(self):
        policy = BlastRadiusPolicy(max_lines_changed=500)
        assert policy.check_lines(600) is not None


class TestIterationState:
    def test_all_states_defined(self):
        expected = {"IDLE", "SELECTING", "PLANNING", "EXECUTING", "RECOVERING",
                    "EVALUATING", "REVIEW_GATE", "COOLDOWN", "PAUSED", "STOPPED"}
        actual = {s.name for s in IterationState}
        assert expected == actual


class TestRecoveryDecision:
    def test_all_decisions_defined(self):
        expected = {"EVALUATE", "RESUME", "SKIP", "PAUSE_IRRECOVERABLE"}
        actual = {d.name for d in RecoveryDecision}
        assert expected == actual


class TestPlannerOutcome:
    def test_rejected_has_reason(self):
        outcome = PlannerOutcome(status="rejected", reject_reason=PlannerRejectReason.BLAST_RADIUS_EXCEEDED)
        assert outcome.graph is None
        assert outcome.reject_reason == PlannerRejectReason.BLAST_RADIUS_EXCEEDED


class TestIterationStopPolicy:
    def test_from_env_defaults(self):
        policy = IterationStopPolicy.from_env()
        assert policy.max_iterations_per_session > 0
        assert policy.max_spend_usd > 0


class TestBudgetWindow:
    def test_not_expired_same_day(self):
        from datetime import datetime, timezone
        window = IterationBudgetWindow(
            window_start_utc=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        )
        assert not window.is_expired()
