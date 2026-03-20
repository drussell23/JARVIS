"""Tests for Trinity Consciousness type definitions.

TDD Red→Green cycle for TC12-TC14 and supporting cases.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import pytest

from backend.core.ouroboros.consciousness.types import (
    BudgetHealth,
    ConsciousnessConfig,
    DreamMetrics,
    FileReputation,
    HealthTrend,
    ImprovementBlueprint,
    MemoryInsight,
    PatternSummary,
    PredictedFailure,
    ProphecyReport,
    ResourceHealth,
    SubsystemHealth,
    TrinityHealthSnapshot,
    TrustHealth,
    compute_blueprint_id,
    compute_job_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_subsystem(name: str = "gls", status: str = "healthy", score: float = 0.9) -> SubsystemHealth:
    return SubsystemHealth(
        name=name,
        status=status,
        score=score,
        details={"uptime_s": 3600},
        polled_at_utc=_utcnow(),
    )


def _make_snapshot(
    overall_verdict: str = "HEALTHY",
    overall_score: float = 0.95,
    jarvis_score: float = 0.9,
    prime_score: float = 0.9,
    reactor_score: float = 0.9,
) -> TrinityHealthSnapshot:
    return TrinityHealthSnapshot(
        timestamp_utc=_utcnow(),
        overall_verdict=overall_verdict,
        overall_score=overall_score,
        jarvis=_make_subsystem("jarvis", score=jarvis_score),
        prime=_make_subsystem("prime", score=prime_score),
        reactor=_make_subsystem("reactor", score=reactor_score),
        resources=ResourceHealth(
            cpu_percent=20.0,
            ram_percent=50.0,
            disk_percent=30.0,
            pressure="NORMAL",
        ),
        budget=BudgetHealth(
            daily_spend_usd=0.50,
            iteration_spend_usd=0.01,
            remaining_usd=9.50,
        ),
        trust=TrustHealth(
            current_tier="governed",
            graduation_progress=0.4,
        ),
    )


def _make_blueprint(
    repo_sha: str = "abc123",
    policy_hash: str = "pol456",
    prompt_family: str = "complexity",
    model_class: str = "7b",
    ttl_hours: float = 24.0,
) -> ImprovementBlueprint:
    bid = compute_blueprint_id(repo_sha, policy_hash, prompt_family, model_class)
    return ImprovementBlueprint(
        blueprint_id=bid,
        title="Reduce cyclomatic complexity in provider",
        description="Split large functions into smaller helpers.",
        category="complexity",
        priority_score=0.75,
        target_files=("backend/core/ouroboros/governance/providers.py",),
        estimated_effort="medium",
        estimated_cost_usd=0.015,
        repo="jarvis",
        repo_sha=repo_sha,
        computed_at_utc=_utcnow(),
        ttl_hours=ttl_hours,
        model_used="Qwen2.5-7B",
        policy_hash=policy_hash,
        oracle_neighborhood={"edges": []},
        suggested_approach="Extract helper functions and add unit tests.",
        risk_assessment="Low — purely structural refactor.",
    )


# ---------------------------------------------------------------------------
# Test 1: SubsystemHealth creation with valid fields
# ---------------------------------------------------------------------------

class TestSubsystemHealth:
    def test_creation_with_valid_fields(self):
        sh = _make_subsystem(name="oracle", status="degraded", score=0.6)
        assert sh.name == "oracle"
        assert sh.status == "degraded"
        assert sh.score == pytest.approx(0.6)
        assert sh.details == {"uptime_s": 3600}
        assert isinstance(sh.polled_at_utc, str)

    def test_frozen(self):
        sh = _make_subsystem()
        with pytest.raises((AttributeError, TypeError)):
            sh.score = 0.0  # type: ignore[misc]

    def test_score_stored_exactly(self):
        sh = SubsystemHealth(
            name="ils", status="healthy", score=1.0,
            details={}, polled_at_utc=_utcnow(),
        )
        assert sh.score == 1.0


# ---------------------------------------------------------------------------
# Test 2: TrinityHealthSnapshot overall_verdict
# ---------------------------------------------------------------------------

class TestTrinityHealthSnapshot:
    def test_healthy_verdict_stored(self):
        snap = _make_snapshot(overall_verdict="HEALTHY", overall_score=0.95)
        assert snap.overall_verdict == "HEALTHY"
        assert snap.overall_score == pytest.approx(0.95)

    def test_degraded_verdict_stored(self):
        snap = _make_snapshot(overall_verdict="DEGRADED", overall_score=0.55)
        assert snap.overall_verdict == "DEGRADED"

    def test_critical_verdict_stored(self):
        snap = _make_snapshot(overall_verdict="CRITICAL", overall_score=0.1)
        assert snap.overall_verdict == "CRITICAL"

    def test_subsystem_references_intact(self):
        snap = _make_snapshot()
        assert snap.jarvis.name == "jarvis"
        assert snap.prime.name == "prime"
        assert snap.reactor.name == "reactor"

    def test_resource_health_attached(self):
        snap = _make_snapshot()
        assert snap.resources.cpu_percent == pytest.approx(20.0)
        assert snap.resources.pressure == "NORMAL"

    def test_budget_health_attached(self):
        snap = _make_snapshot()
        assert snap.budget.remaining_usd == pytest.approx(9.50)

    def test_trust_health_attached(self):
        snap = _make_snapshot()
        assert snap.trust.current_tier == "governed"
        assert snap.trust.graduation_progress == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# TC12: compute_job_key idempotent (same inputs = same key)
# ---------------------------------------------------------------------------

class TestComputeJobKey:
    def test_idempotent_same_inputs(self):
        """TC12: same inputs always produce identical key."""
        key_a = compute_job_key("sha123", "pol456", "complexity", "7b")
        key_b = compute_job_key("sha123", "pol456", "complexity", "7b")
        assert key_a == key_b

    def test_changes_with_different_sha(self):
        key_a = compute_job_key("sha_aaa", "pol456", "complexity", "7b")
        key_b = compute_job_key("sha_bbb", "pol456", "complexity", "7b")
        assert key_a != key_b

    def test_changes_with_different_policy(self):
        key_a = compute_job_key("sha123", "pol_aaa", "complexity", "7b")
        key_b = compute_job_key("sha123", "pol_bbb", "complexity", "7b")
        assert key_a != key_b

    def test_changes_with_different_prompt_family(self):
        key_a = compute_job_key("sha123", "pol456", "complexity", "7b")
        key_b = compute_job_key("sha123", "pol456", "security", "7b")
        assert key_a != key_b

    def test_changes_with_different_model_class(self):
        key_a = compute_job_key("sha123", "pol456", "complexity", "7b")
        key_b = compute_job_key("sha123", "pol456", "complexity", "13b")
        assert key_a != key_b

    def test_key_is_hex_string(self):
        key = compute_job_key("sha123", "pol456", "complexity", "7b")
        # Must be a non-empty hex string (sha256 = 64 chars)
        assert isinstance(key, str)
        assert len(key) == 64
        int(key, 16)  # raises if not hex


# ---------------------------------------------------------------------------
# Test 3: ImprovementBlueprint.is_stale() — TC13, TC14, TC15
# ---------------------------------------------------------------------------

class TestImprovementBlueprintStaleness:
    """TC13: HEAD change → stale; TC14: policy change → stale; TC15: current → not stale."""

    def test_tc13_stale_on_head_change(self):
        """TC13: blueprint becomes stale when repo HEAD changes."""
        bp = _make_blueprint(repo_sha="old_sha", policy_hash="pol_same")
        assert bp.is_stale(current_head="new_sha", current_policy_hash="pol_same") is True

    def test_tc14_stale_on_policy_change(self):
        """TC14: blueprint becomes stale when policy hash changes."""
        bp = _make_blueprint(repo_sha="sha_same", policy_hash="old_pol")
        assert bp.is_stale(current_head="sha_same", current_policy_hash="new_pol") is True

    def test_tc15_not_stale_when_current(self):
        """TC15: blueprint is fresh when both HEAD and policy match."""
        bp = _make_blueprint(repo_sha="sha_same", policy_hash="pol_same")
        assert bp.is_stale(current_head="sha_same", current_policy_hash="pol_same") is False

    def test_stale_when_both_changed(self):
        bp = _make_blueprint(repo_sha="old_sha", policy_hash="old_pol")
        assert bp.is_stale(current_head="new_sha", current_policy_hash="new_pol") is True

    def test_blueprint_id_is_deterministic(self):
        bp_a = _make_blueprint(repo_sha="sha1", policy_hash="pol1")
        bp_b = _make_blueprint(repo_sha="sha1", policy_hash="pol1")
        assert bp_a.blueprint_id == bp_b.blueprint_id

    def test_blueprint_id_uses_sha256(self):
        bp = _make_blueprint()
        assert len(bp.blueprint_id) == 64
        int(bp.blueprint_id, 16)  # must be hex


# ---------------------------------------------------------------------------
# Test 4: MemoryInsight.decay_confidence — 10% per day
# ---------------------------------------------------------------------------

class TestMemoryInsight:
    def _make_insight(self, ttl_hours: float = 168.0) -> MemoryInsight:
        return MemoryInsight(
            insight_id="ins-001",
            category="failure_pattern",
            content="providers.py times out under GCP load",
            confidence=0.8,
            evidence_count=12,
            last_seen_utc=_utcnow(),
            ttl_hours=ttl_hours,
        )

    def test_decay_one_day_past_ttl(self):
        insight = self._make_insight()
        # 1 day past TTL → confidence reduced by 10% of original
        decayed = insight.decay_confidence(days_past_ttl=1.0)
        assert decayed == pytest.approx(0.8 * (1 - 0.10 * 1.0))

    def test_decay_two_days_past_ttl(self):
        insight = self._make_insight()
        decayed = insight.decay_confidence(days_past_ttl=2.0)
        assert decayed == pytest.approx(0.8 * (1 - 0.10 * 2.0))

    def test_decay_never_negative(self):
        insight = self._make_insight()
        # 20 days past TTL would normally give -0.8, should clamp to 0.0
        decayed = insight.decay_confidence(days_past_ttl=20.0)
        assert decayed >= 0.0

    def test_is_expired_before_ttl(self):
        # TTL hasn't passed yet: not expired
        future_utc = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        insight = MemoryInsight(
            insight_id="ins-002",
            category="success_pattern",
            content="Oracle always ready within 5s",
            confidence=0.9,
            evidence_count=5,
            last_seen_utc=future_utc,
            ttl_hours=0.0,  # 0h TTL but last_seen in future → not expired
        )
        now = _utcnow()
        # with ttl_hours=0 and last_seen in future, it is not yet expired
        assert insight.is_expired(now) is False

    def test_is_expired_after_ttl(self):
        # last_seen_utc far in the past, TTL = 1h
        old_utc = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        insight = MemoryInsight(
            insight_id="ins-003",
            category="timing_pattern",
            content="GCP startup takes 4 min",
            confidence=0.7,
            evidence_count=3,
            last_seen_utc=old_utc,
            ttl_hours=1.0,
        )
        assert insight.is_expired(_utcnow()) is True


# ---------------------------------------------------------------------------
# Test 5: HealthTrend rolling window
# ---------------------------------------------------------------------------

class TestHealthTrend:
    def test_add_and_len(self):
        trend = HealthTrend(max_entries=5)
        for _ in range(3):
            trend.add(_make_snapshot())
        assert len(trend) == 3

    def test_evicts_oldest_at_capacity(self):
        trend = HealthTrend(max_entries=3)
        snaps = [_make_snapshot(overall_score=float(i) / 10) for i in range(5)]
        for s in snaps:
            trend.add(s)
        # Should only hold 3 entries; oldest two are gone
        assert len(trend) == 3

    def test_get_window_returns_recent_entries(self):
        trend = HealthTrend(max_entries=100)
        # Add one snapshot
        trend.add(_make_snapshot())
        # A very large window should return all entries
        window = trend.get_window(minutes=9999)
        assert len(window) >= 1

    def test_get_window_empty_when_all_old(self):
        # No snapshots added → empty window
        trend = HealthTrend(max_entries=10)
        window = trend.get_window(minutes=1)
        assert window == []


# ---------------------------------------------------------------------------
# Test 6: FileReputation fragility score bounds
# ---------------------------------------------------------------------------

class TestFileReputation:
    def test_fragility_score_in_bounds(self):
        fr = FileReputation(
            file_path="backend/core/ouroboros/governance/providers.py",
            change_count=42,
            success_rate=0.6,
            avg_blast_radius=8,
            common_co_failures=("tests/test_providers.py", "tests/test_orchestrator.py"),
            fragility_score=0.75,
        )
        assert 0.0 <= fr.fragility_score <= 1.0

    def test_fragility_score_zero(self):
        fr = FileReputation(
            file_path="backend/core/solid_module.py",
            change_count=2,
            success_rate=1.0,
            avg_blast_radius=0,
            common_co_failures=(),
            fragility_score=0.0,
        )
        assert fr.fragility_score == 0.0

    def test_common_co_failures_is_tuple(self):
        fr = FileReputation(
            file_path="path/to/file.py",
            change_count=5,
            success_rate=0.8,
            avg_blast_radius=2,
            common_co_failures=("a.py", "b.py"),
            fragility_score=0.3,
        )
        assert isinstance(fr.common_co_failures, tuple)


# ---------------------------------------------------------------------------
# Test 7: ConsciousnessConfig.from_env() reads env vars with defaults
# ---------------------------------------------------------------------------

class TestConsciousnessConfig:
    def test_defaults_when_no_env_vars(self, monkeypatch):
        # Clear any relevant env vars
        env_keys = [
            "JARVIS_CONSCIOUSNESS_ENABLED",
            "JARVIS_CONSCIOUSNESS_HEALTH_POLL_S",
            "JARVIS_CONSCIOUSNESS_DREAM_ENABLED",
            "JARVIS_CONSCIOUSNESS_DREAM_IDLE_THRESHOLD_S",
            "JARVIS_CONSCIOUSNESS_DREAM_REENTRY_COOLDOWN_S",
            "JARVIS_CONSCIOUSNESS_DREAM_MAX_MINUTES_PER_DAY",
            "JARVIS_CONSCIOUSNESS_DREAM_BLUEPRINT_TTL_HOURS",
            "JARVIS_CONSCIOUSNESS_PROPHECY_ENABLED",
            "JARVIS_CONSCIOUSNESS_MEMORY_TTL_HOURS",
            "JARVIS_CONSCIOUSNESS_BRIEFING_ON_STARTUP",
        ]
        for k in env_keys:
            monkeypatch.delenv(k, raising=False)

        cfg = ConsciousnessConfig.from_env()
        assert isinstance(cfg.enabled, bool)
        assert isinstance(cfg.health_poll_interval_s, float)
        assert isinstance(cfg.dream_enabled, bool)
        assert isinstance(cfg.dream_idle_threshold_s, float)
        assert isinstance(cfg.dream_reentry_cooldown_s, float)
        assert isinstance(cfg.dream_max_minutes_per_day, float)
        assert isinstance(cfg.dream_blueprint_ttl_hours, float)
        assert isinstance(cfg.prophecy_enabled, bool)
        assert isinstance(cfg.memory_ttl_hours, float)
        assert isinstance(cfg.briefing_on_startup, bool)

    def test_env_var_overrides_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONSCIOUSNESS_ENABLED", "true")
        cfg = ConsciousnessConfig.from_env()
        assert cfg.enabled is True

    def test_env_var_overrides_health_poll(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONSCIOUSNESS_HEALTH_POLL_S", "15.0")
        cfg = ConsciousnessConfig.from_env()
        assert cfg.health_poll_interval_s == pytest.approx(15.0)

    def test_env_var_overrides_dream_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONSCIOUSNESS_DREAM_ENABLED", "false")
        cfg = ConsciousnessConfig.from_env()
        assert cfg.dream_enabled is False

    def test_env_var_overrides_dream_idle_threshold(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONSCIOUSNESS_DREAM_IDLE_THRESHOLD_S", "120.0")
        cfg = ConsciousnessConfig.from_env()
        assert cfg.dream_idle_threshold_s == pytest.approx(120.0)

    def test_config_is_frozen(self):
        cfg = ConsciousnessConfig.from_env()
        with pytest.raises((AttributeError, TypeError)):
            cfg.enabled = not cfg.enabled  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 8: ProphecyReport creation with predicted failures
# ---------------------------------------------------------------------------

class TestProphecyReport:
    def test_creation_with_predicted_failures(self):
        failures = (
            PredictedFailure(
                test_file="tests/test_ouroboros_governance/test_providers.py",
                probability=0.82,
                reason="Function signature changed",
                evidence="3 callers depend on old signature",
            ),
            PredictedFailure(
                test_file="tests/test_ouroboros_governance/test_orchestrator.py",
                probability=0.45,
                reason="Shared fixture may break",
                evidence="conftest uses affected import",
            ),
        )
        report = ProphecyReport(
            change_id="chg-abc-123",
            risk_level="high",
            predicted_failures=failures,
            confidence=0.78,
            reasoning="Two test suites import the changed module directly.",
            recommended_tests=(
                "tests/test_ouroboros_governance/test_providers.py",
                "tests/test_ouroboros_governance/test_orchestrator.py",
            ),
        )
        assert report.change_id == "chg-abc-123"
        assert report.risk_level == "high"
        assert len(report.predicted_failures) == 2
        assert report.predicted_failures[0].probability == pytest.approx(0.82)
        assert report.confidence == pytest.approx(0.78)
        assert "test_providers.py" in report.recommended_tests[0]

    def test_empty_failures(self):
        report = ProphecyReport(
            change_id="chg-safe",
            risk_level="low",
            predicted_failures=(),
            confidence=0.95,
            reasoning="No overlapping test coverage.",
            recommended_tests=(),
        )
        assert report.risk_level == "low"
        assert len(report.predicted_failures) == 0


# ---------------------------------------------------------------------------
# Test 9: PatternSummary
# ---------------------------------------------------------------------------

class TestPatternSummary:
    def test_creation(self):
        insights = tuple(
            MemoryInsight(
                insight_id=f"ins-{i}",
                category="failure_pattern",
                content=f"pattern {i}",
                confidence=0.9,
                evidence_count=i + 1,
                last_seen_utc=_utcnow(),
                ttl_hours=168.0,
            )
            for i in range(3)
        )
        summary = PatternSummary(
            top_patterns=insights,
            total_insights=10,
            active_insights=7,
            archived_insights=3,
        )
        assert summary.total_insights == 10
        assert len(summary.top_patterns) == 3
        assert summary.archived_insights == 3


# ---------------------------------------------------------------------------
# Test 10: DreamMetrics mutable dataclass
# ---------------------------------------------------------------------------

class TestDreamMetrics:
    def test_defaults(self):
        dm = DreamMetrics()
        assert dm.opportunistic_compute_minutes == pytest.approx(0.0)
        assert dm.preemptions_count == 0
        assert dm.blueprints_computed == 0
        assert dm.blueprints_discarded_stale == 0
        assert dm.blueprint_hit_rate == pytest.approx(0.0)
        assert dm.jobs_deduplicated == 0
        assert dm.estimated_cost_saved_usd == pytest.approx(0.0)

    def test_mutable(self):
        dm = DreamMetrics()
        dm.blueprints_computed = 5
        assert dm.blueprints_computed == 5
