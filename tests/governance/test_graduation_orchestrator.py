"""Tests for GraduationOrchestrator — self-programming loop."""
import asyncio
import json
import time

import pytest

from backend.core.ouroboros.governance.graduation_orchestrator import (
    EphemeralUsageTracker,
    EphemeralUsageRecord,
    GraduationOrchestrator,
    GraduationDecision,
    GraduationRecord,
    GraduationPhase,
)


# ---------------------------------------------------------------------------
# EphemeralUsageTracker tests
# ---------------------------------------------------------------------------


class TestEphemeralUsageTracker:
    @pytest.fixture
    def tracker(self, tmp_path):
        return EphemeralUsageTracker(
            persistence_path=tmp_path / "usage.json",
            graduation_threshold=3,
        )

    @pytest.mark.asyncio
    async def test_record_below_threshold(self, tracker):
        result = await tracker.record_usage("search youtube", "abc", "success", 1.0)
        assert result is None
        assert tracker.get_usage_count(tracker._normalize_goal("search youtube")) == 1

    @pytest.mark.asyncio
    async def test_threshold_fires_at_exact_count(self, tracker):
        goal = "search youtube for videos"
        r1 = await tracker.record_usage(goal, "h1", "success", 1.0)
        r2 = await tracker.record_usage(goal, "h2", "success", 1.0)
        r3 = await tracker.record_usage(goal, "h3", "success", 1.0)
        assert r1 is None
        assert r2 is None
        assert r3 is not None  # threshold hit

    @pytest.mark.asyncio
    async def test_threshold_fires_only_once(self, tracker):
        goal = "search youtube for videos"
        for i in range(5):
            await tracker.record_usage(goal, f"h{i}", "success", 1.0)
        # 4th and 5th should return None (already fired)
        r = await tracker.record_usage(goal, "h5", "success", 1.0)
        assert r is None

    @pytest.mark.asyncio
    async def test_failures_dont_count_toward_threshold(self, tracker):
        goal = "search youtube"
        await tracker.record_usage(goal, "h1", "failure", 1.0)
        await tracker.record_usage(goal, "h2", "failure", 1.0)
        await tracker.record_usage(goal, "h3", "failure", 1.0)
        # All failures — threshold not met
        gcid = tracker._normalize_goal(goal)
        assert gcid not in tracker._threshold_fired

    @pytest.mark.asyncio
    async def test_mixed_success_failure(self, tracker):
        goal = "open apple music"
        await tracker.record_usage(goal, "h1", "success", 1.0)
        await tracker.record_usage(goal, "h2", "failure", 1.0)
        await tracker.record_usage(goal, "h3", "success", 1.0)
        r = await tracker.record_usage(goal, "h4", "success", 1.0)
        assert r is not None  # 3 successes reached

    @pytest.mark.asyncio
    async def test_graduated_class_ignored(self, tracker):
        goal = "search youtube"
        gcid = tracker._normalize_goal(goal)
        tracker.mark_graduated(gcid)
        r = await tracker.record_usage(goal, "h1", "success", 1.0)
        assert r is None

    def test_normalize_goal_similar_goals_cluster(self, tracker):
        # Same verb+noun pattern should cluster even with different articles/stop words
        a = tracker._normalize_goal("search YouTube for highlights")
        b = tracker._normalize_goal("Search the YouTube for some highlights please")
        assert a == b

    def test_normalize_goal_different_nouns_split(self, tracker):
        # Different key nouns should produce different classes
        a = tracker._normalize_goal("search YouTube for NBA")
        b = tracker._normalize_goal("open Apple Music app")
        assert a != b

    def test_normalize_goal_different_actions(self, tracker):
        a = tracker._normalize_goal("open Apple Music")
        b = tracker._normalize_goal("search YouTube for music")
        assert a != b

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path):
        path = tmp_path / "usage.json"
        tracker1 = EphemeralUsageTracker(persistence_path=path, graduation_threshold=3)
        await tracker1.record_usage("test goal", "h1", "success", 1.0)
        await tracker1.record_usage("test goal", "h2", "success", 1.0)

        # New tracker loads from disk
        tracker2 = EphemeralUsageTracker(persistence_path=path, graduation_threshold=3)
        gcid = tracker2._normalize_goal("test goal")
        assert tracker2.get_usage_count(gcid) == 2

    def test_health(self, tracker):
        h = tracker.health()
        assert "tracked_classes" in h
        assert "graduated" in h
        assert "threshold" in h


# ---------------------------------------------------------------------------
# GraduationDecision tests
# ---------------------------------------------------------------------------


class TestGraduationDecision:
    def test_frozen(self):
        d = GraduationDecision(
            should_graduate=True,
            capability_name="test_agent",
            capability_domain="exploration",
            repo_owner="jarvis",
            agent_class_name="TestAgent",
            module_path="backend/test.py",
            test_module_path="tests/test_test.py",
            rationale="test",
            estimated_complexity="light",
        )
        with pytest.raises(AttributeError):
            d.should_graduate = False

    def test_default_rejection_reason(self):
        d = GraduationDecision(
            should_graduate=False,
            capability_name="x", capability_domain="x", repo_owner="jarvis",
            agent_class_name="X", module_path="x", test_module_path="x",
            rationale="not needed", estimated_complexity="light",
            rejection_reason="not useful enough",
        )
        assert d.rejection_reason == "not useful enough"


# ---------------------------------------------------------------------------
# GraduationPhase tests
# ---------------------------------------------------------------------------


class TestGraduationPhase:
    def test_all_phases_exist(self):
        assert GraduationPhase.TRACKING.value == "tracking"
        assert GraduationPhase.PUSH_FAILED.value == "push_failed"  # H3
        assert GraduationPhase.EXPIRED.value == "expired"          # H4
        assert GraduationPhase.GRADUATED.value == "graduated"

    def test_terminal_phases(self):
        terminals = {GraduationPhase.GRADUATED, GraduationPhase.FAILED,
                     GraduationPhase.REJECTED, GraduationPhase.EXPIRED}
        assert len(terminals) == 4


# ---------------------------------------------------------------------------
# GraduationOrchestrator tests
# ---------------------------------------------------------------------------


class TestGraduationOrchestrator:
    def test_health_snapshot(self):
        orch = GraduationOrchestrator()
        h = orch.health()
        assert h["active_graduations"] == 0
        assert h["total_graduated"] == 0
        assert h["semaphore_available"] == 1

    @pytest.mark.asyncio
    async def test_evaluate_without_prime_fails(self, tmp_path):
        orch = GraduationOrchestrator(prime_client=None, persistence_dir=tmp_path)
        records = [EphemeralUsageRecord(
            goal="test", goal_hash="abc", code_hash="def",
            execution_outcome="success", elapsed_s=1.0,
        )]
        result = await orch.evaluate_graduation("test_class", records)
        assert result.phase == GraduationPhase.FAILED
        assert "PrimeClient" in (result.error or "")

    @pytest.mark.asyncio
    async def test_decided_skip(self, tmp_path):
        """J-Prime says don't graduate -> DECIDED_SKIP."""

        class FakePrime:
            async def generate(self, **kwargs):
                return type("R", (), {
                    "content": json.dumps({
                        "should_graduate": False,
                        "capability_name": "test",
                        "capability_domain": "exploration",
                        "repo_owner": "jarvis",
                        "agent_class_name": "Test",
                        "module_path": "test.py",
                        "test_module_path": "test_test.py",
                        "rationale": "not useful",
                        "estimated_complexity": "light",
                        "rejection_reason": "one-time use",
                    }),
                    "cost_usd": 0.001,
                })()

        orch = GraduationOrchestrator(prime_client=FakePrime(), persistence_dir=tmp_path)
        records = [EphemeralUsageRecord(
            goal="test", goal_hash="abc", code_hash="def",
            execution_outcome="success", elapsed_s=1.0,
        )]
        result = await orch.evaluate_graduation("test_class", records)
        assert result.phase == GraduationPhase.DECIDED_SKIP
        assert result.decision is not None
        assert result.decision.should_graduate is False

    @pytest.mark.asyncio
    async def test_approval_timeout_expires(self, tmp_path):
        """H4: Approval timeout -> EXPIRED."""

        class FakePrime:
            async def generate(self, **kwargs):
                return type("R", (), {
                    "content": json.dumps({
                        "should_graduate": True,
                        "capability_name": "test_agent",
                        "capability_domain": "exploration",
                        "repo_owner": "jarvis",
                        "agent_class_name": "TestAgent",
                        "module_path": "backend/test.py",
                        "test_module_path": "tests/test_test.py",
                        "rationale": "useful",
                        "estimated_complexity": "light",
                    }),
                    "cost_usd": 0.001,
                })()

        orch = GraduationOrchestrator(
            prime_client=FakePrime(),
            approval_timeout_s=0.1,  # Very short for testing
            persistence_dir=tmp_path,
        )

        # Mock the phases that need git
        async def fake_create_wt(d):
            wt = tmp_path / "wt"
            wt.mkdir(exist_ok=True)
            return wt, "test-branch"

        async def fake_generate(d, wt, r, rec):
            (wt / d.module_path).parent.mkdir(parents=True, exist_ok=True)
            (wt / d.module_path).write_text("class TestAgent:\n    CAPABILITIES = {'test'}\n    async def execute_task(self, p): return {}")
            return [d.module_path]

        async def fake_validate(d, wt, g):
            return True

        async def fake_commit(d, wt, g):
            return "abc123"

        orch._create_worktree = fake_create_wt
        orch._generate_agent_code = fake_generate
        orch._validate_in_shadow = fake_validate
        orch._commit_to_branch = fake_commit

        records = [EphemeralUsageRecord(
            goal="test", goal_hash="abc", code_hash="def",
            execution_outcome="success", elapsed_s=1.0,
        )]
        result = await orch.evaluate_graduation("test_class", records)
        assert result.phase == GraduationPhase.EXPIRED  # H4

    def test_resolve_repo_path_env_fallback(self, tmp_path):
        from pathlib import Path
        orch = GraduationOrchestrator(persistence_dir=tmp_path)
        path = orch._resolve_repo_path("jarvis")
        assert isinstance(path, Path)

    @pytest.mark.asyncio
    async def test_save_and_load_record(self, tmp_path):
        orch = GraduationOrchestrator(persistence_dir=tmp_path)
        record = GraduationRecord(
            graduation_id="grad-test123",
            goal_class_id="abc",
            phase=GraduationPhase.GRADUATED,
            usage_count=5,
        )
        orch._save_record(record)
        saved = tmp_path / "records" / "grad-test123.json"
        assert saved.exists()
        data = json.loads(saved.read_text())
        assert data["phase"] == "graduated"
        assert data["usage_count"] == 5
