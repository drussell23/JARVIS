"""tests/test_ouroboros_governance/test_consciousness_service.py

TDD tests for TrinityConsciousness — Zone 6.11 orchestrator composing 4 engines.

Test cases:
    TC19:  Memory feeds planner — get_memory_for_planner returns insights for fragile files
    TC21:  Startup recovery — start() calls cortex.start() and memory.start()
    TC25:  Morning briefing — say_fn called with briefing text containing health score
    TC26:  Full lifecycle — start -> engines started, stop -> engines stopped
    TC27:  Blueprint from dream — dream.get_blueprints accessible via consciousness
    TC28:  Regression detected — memory + prophecy cross-engine -> elevated risk
    TC33:  Stop flushes all — stop calls stop() on all 4 engines
    Plus:  startup ordering, feature flag, health composite, start idempotent
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.core.ouroboros.consciousness.types import (
    BudgetHealth,
    ConsciousnessConfig,
    FileReputation,
    ImprovementBlueprint,
    MemoryInsight,
    PatternSummary,
    PredictedFailure,
    ProphecyReport,
    ResourceHealth,
    SubsystemHealth,
    TrinityHealthSnapshot,
    TrustHealth,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> ConsciousnessConfig:
    """Build a ConsciousnessConfig with sensible test defaults."""
    defaults = dict(
        enabled=True,
        health_poll_interval_s=30.0,
        dream_enabled=True,
        dream_idle_threshold_s=300.0,
        dream_reentry_cooldown_s=60.0,
        dream_max_minutes_per_day=120.0,
        dream_blueprint_ttl_hours=24.0,
        prophecy_enabled=True,
        memory_ttl_hours=168.0,
        briefing_on_startup=True,
    )
    defaults.update(overrides)
    return ConsciousnessConfig(**defaults)


def _make_snapshot(overall_score: float = 0.85) -> TrinityHealthSnapshot:
    """Build a minimal TrinityHealthSnapshot for test assertions."""
    sh = SubsystemHealth(
        name="jarvis", status="healthy", score=1.0,
        details={}, polled_at_utc="2026-03-20T00:00:00+00:00",
    )
    return TrinityHealthSnapshot(
        timestamp_utc="2026-03-20T00:00:00+00:00",
        overall_verdict="HEALTHY",
        overall_score=overall_score,
        jarvis=sh,
        prime=SubsystemHealth(
            name="prime", status="healthy", score=1.0,
            details={}, polled_at_utc="2026-03-20T00:00:00+00:00",
        ),
        reactor=SubsystemHealth(
            name="reactor", status="healthy", score=0.9,
            details={}, polled_at_utc="2026-03-20T00:00:00+00:00",
        ),
        resources=ResourceHealth(cpu_percent=20, ram_percent=50, disk_percent=30, pressure="NORMAL"),
        budget=BudgetHealth(daily_spend_usd=0.5, iteration_spend_usd=0.1, remaining_usd=9.5),
        trust=TrustHealth(current_tier="governed", graduation_progress=0.2),
    )


def _make_blueprint(
    title: str = "Refactor module X",
    priority_score: float = 0.8,
    blueprint_id: str = "bp-001",
) -> ImprovementBlueprint:
    """Build a minimal ImprovementBlueprint for test assertions."""
    return ImprovementBlueprint(
        blueprint_id=blueprint_id,
        title=title,
        description="Simplify internal coupling",
        category="complexity",
        priority_score=priority_score,
        target_files=("backend/core/foo.py",),
        estimated_effort="small",
        estimated_cost_usd=0.01,
        repo="jarvis",
        repo_sha="abc123",
        computed_at_utc="2026-03-20T00:00:00+00:00",
        ttl_hours=24.0,
        model_used="qwen2.5-7b",
        policy_hash="pol123",
        oracle_neighborhood={},
        suggested_approach="Extract helper function",
        risk_assessment="Low risk",
    )


def _make_insight(
    insight_id: str = "ins-001",
    content: str = "backend/core/foo.py fails often with test_foo.py",
    confidence: float = 0.75,
) -> MemoryInsight:
    """Build a MemoryInsight for test assertions."""
    return MemoryInsight(
        insight_id=insight_id,
        category="failure_pattern",
        content=content,
        confidence=confidence,
        evidence_count=3,
        last_seen_utc="2026-03-20T00:00:00+00:00",
        ttl_hours=168.0,
    )


def _make_prophecy_report(
    risk_level: str = "medium",
    confidence: float = 0.4,
    reasoning: str = "Heuristic analysis",
) -> ProphecyReport:
    """Build a ProphecyReport for test assertions."""
    return ProphecyReport(
        change_id="chg-abc123",
        risk_level=risk_level,
        predicted_failures=(
            PredictedFailure(
                test_file="tests/test_foo.py",
                probability=0.6,
                reason="Historical instability",
                evidence="heuristic_score=0.6 file=backend/core/foo.py",
            ),
        ),
        confidence=confidence,
        reasoning=reasoning,
        recommended_tests=("tests/test_foo.py",),
    )


def _make_file_reputation(
    file_path: str = "backend/core/foo.py",
    fragility_score: float = 0.7,
    success_rate: float = 0.3,
) -> FileReputation:
    """Build a FileReputation for test assertions."""
    return FileReputation(
        file_path=file_path,
        change_count=10,
        success_rate=success_rate,
        avg_blast_radius=3,
        common_co_failures=("backend/core/bar.py",),
        fragility_score=fragility_score,
    )


def _make_engines():
    """Build mocked engine quartet + config + comm + say_fn.

    Engines use MagicMock (sync by default) with start/stop/analyze_change
    explicitly set as AsyncMock because those are the real async methods.
    """
    cortex = MagicMock()
    cortex.start = AsyncMock()
    cortex.stop = AsyncMock()
    cortex.get_snapshot.return_value = _make_snapshot()
    cortex.get_trend.return_value = MagicMock()

    memory = MagicMock()
    memory.start = AsyncMock()
    memory.stop = AsyncMock()
    memory.get_pattern_summary.return_value = PatternSummary(
        top_patterns=(), total_insights=5, active_insights=3, archived_insights=2,
    )
    memory.get_file_reputation.return_value = _make_file_reputation()
    memory.query.return_value = [_make_insight()]

    dream = MagicMock()
    dream.start = AsyncMock()
    dream.stop = AsyncMock()
    dream.get_blueprints.return_value = [_make_blueprint()]
    dream.get_blueprint.return_value = _make_blueprint()

    prophecy = MagicMock()
    prophecy.start = AsyncMock()
    prophecy.stop = AsyncMock()
    prophecy.analyze_change = AsyncMock(return_value=_make_prophecy_report())

    comm = AsyncMock()
    say_fn = AsyncMock()

    config = _make_config()

    return cortex, memory, dream, prophecy, config, comm, say_fn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_feeds_planner_tc19():
    """TC19: get_memory_for_planner returns insights for files with fragility > 0.5."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
        comm=comm,
        say_fn=say_fn,
    )

    # fragile file => should trigger query
    insights = tc.get_memory_for_planner(("backend/core/foo.py",))
    assert len(insights) >= 1
    memory.get_file_reputation.assert_called_with("backend/core/foo.py")
    memory.query.assert_called_once()

    # non-fragile file => should NOT trigger query
    memory.get_file_reputation.return_value = _make_file_reputation(fragility_score=0.2)
    memory.query.reset_mock()
    insights = tc.get_memory_for_planner(("backend/core/safe.py",))
    assert len(insights) == 0
    memory.query.assert_not_called()


@pytest.mark.asyncio
async def test_startup_recovery_loads_tc21():
    """TC21: start() calls cortex.start() and memory.start() in Phase 1."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    await tc.start()

    cortex.start.assert_awaited_once()
    memory.start.assert_awaited_once()
    dream.start.assert_awaited_once()
    prophecy.start.assert_awaited_once()

    await tc.stop()


@pytest.mark.asyncio
async def test_morning_briefing_announced_tc25():
    """TC25: say_fn called with briefing text containing health score."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
        comm=comm,
        say_fn=say_fn,
    )

    await tc.start()

    say_fn.assert_awaited_once()
    briefing_text = say_fn.call_args[0][0]

    # Should mention health score (85% from our fixture)
    assert "85%" in briefing_text

    # Should mention blueprints
    assert "improvement" in briefing_text.lower() or "pre-analyzed" in briefing_text.lower()

    # Should mention active insights
    assert "3" in briefing_text  # 3 active insights from our fixture

    await tc.stop()


@pytest.mark.asyncio
async def test_full_lifecycle_tc26():
    """TC26: start -> engines started, stop -> engines stopped."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    assert tc.health()["running"] is False

    await tc.start()
    assert tc.health()["running"] is True

    await tc.stop()
    assert tc.health()["running"] is False

    # All 4 engines should have been stopped
    cortex.stop.assert_awaited_once()
    memory.stop.assert_awaited_once()
    dream.stop.assert_awaited_once()
    prophecy.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_blueprint_from_dream_retrievable_tc27():
    """TC27: dream.get_blueprints accessible via consciousness briefing."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
        say_fn=say_fn,
    )

    await tc.start()

    # The briefing should have called dream.get_blueprints
    dream.get_blueprints.assert_called_once_with(top_n=3)

    # Verify the blueprint title made it into the briefing
    briefing_text = say_fn.call_args[0][0]
    assert "Refactor module X" in briefing_text

    await tc.stop()


@pytest.mark.asyncio
async def test_regression_detected_tc28():
    """TC28: Cross-engine regression detection — memory enriches prophecy risk."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    # File with low success rate from memory
    memory.get_file_reputation.return_value = _make_file_reputation(
        file_path="backend/core/fragile.py",
        fragility_score=0.9,
        success_rate=0.3,
    )

    # Prophecy returns medium risk originally
    prophecy.analyze_change.return_value = _make_prophecy_report(
        risk_level="medium",
        confidence=0.4,
        reasoning="Heuristic analysis medium",
    )

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    report = await tc.detect_regression(["backend/core/fragile.py"])

    # Should have been elevated to "high" due to memory showing < 0.5 success_rate
    assert report is not None
    assert report.risk_level == "high"
    assert "memory" in report.reasoning.lower()
    assert "30%" in report.reasoning  # success_rate formatted


@pytest.mark.asyncio
async def test_stop_flushes_all_tc33():
    """TC33: stop calls stop() on all 4 engines."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    await tc.start()
    await tc.stop()

    cortex.stop.assert_awaited_once()
    memory.stop.assert_awaited_once()
    dream.stop.assert_awaited_once()
    prophecy.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_ordering():
    """Phase 1 (cortex + memory) completes before Phase 2 (dream + prophecy)."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    # Record call order
    call_order: list = []

    async def cortex_start():
        call_order.append("cortex")

    async def memory_start():
        call_order.append("memory")

    async def dream_start():
        call_order.append("dream")

    async def prophecy_start():
        call_order.append("prophecy")

    cortex.start = AsyncMock(side_effect=cortex_start)
    memory.start = AsyncMock(side_effect=memory_start)
    dream.start = AsyncMock(side_effect=dream_start)
    prophecy.start = AsyncMock(side_effect=prophecy_start)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    await tc.start()

    # Phase 1 engines must appear before Phase 2 engines
    cortex_idx = call_order.index("cortex")
    memory_idx = call_order.index("memory")
    dream_idx = call_order.index("dream")
    prophecy_idx = call_order.index("prophecy")

    assert cortex_idx < dream_idx
    assert cortex_idx < prophecy_idx
    assert memory_idx < dream_idx
    assert memory_idx < prophecy_idx

    await tc.stop()


@pytest.mark.asyncio
async def test_feature_flag_disabled():
    """config.enabled=False -> TrinityConsciousness still starts (it wraps engines).

    Individual engines gate themselves on their own flags. The service itself
    is always created and started by the supervisor.
    """
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(enabled=False, briefing_on_startup=False)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    await tc.start()
    assert tc.health()["running"] is True

    await tc.stop()


@pytest.mark.asyncio
async def test_health_returns_composite():
    """health() returns a dict with all engine status keys."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    h = tc.health()
    assert "running" in h
    assert "cortex" in h
    assert "memory" in h
    assert "dream" in h
    assert "prophecy" in h

    # Before start, all should be stopped
    assert h["running"] is False

    await tc.start()

    h = tc.health()
    assert h["running"] is True
    assert h["cortex"] == "active"
    assert h["memory"] == "active"
    assert h["dream"] == "active"
    assert h["prophecy"] == "active"

    await tc.stop()


@pytest.mark.asyncio
async def test_start_idempotent():
    """Calling start() twice is a no-op on the second call."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    await tc.start()
    await tc.start()  # second call should be no-op

    # Each engine's start should have been called exactly once
    cortex.start.assert_awaited_once()
    memory.start.assert_awaited_once()
    dream.start.assert_awaited_once()
    prophecy.start.assert_awaited_once()

    await tc.stop()


@pytest.mark.asyncio
async def test_briefing_failure_does_not_crash_start():
    """If morning briefing fails, start() still completes successfully."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    # Make say_fn raise an exception
    say_fn.side_effect = RuntimeError("TTS offline")

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
        say_fn=say_fn,
    )

    # Should not raise
    await tc.start()
    assert tc.health()["running"] is True

    await tc.stop()


@pytest.mark.asyncio
async def test_stop_tolerates_engine_errors():
    """stop() continues even if individual engine stop() raises."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()
    config = _make_config(briefing_on_startup=False)

    # Make dream.stop() raise
    dream.stop.side_effect = RuntimeError("dream stop failed")

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    await tc.start()
    # Should not raise despite dream.stop failing
    await tc.stop()

    # Other engines should still have been stopped
    cortex.stop.assert_awaited_once()
    memory.stop.assert_awaited_once()
    prophecy.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_regression_no_elevation_when_file_healthy():
    """detect_regression does NOT elevate risk when file has good success rate."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    # Healthy file — high success rate
    memory.get_file_reputation.return_value = _make_file_reputation(
        file_path="backend/core/solid.py",
        fragility_score=0.1,
        success_rate=0.95,
    )

    prophecy.analyze_change.return_value = _make_prophecy_report(
        risk_level="low",
        confidence=0.3,
    )

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
    )

    report = await tc.detect_regression(["backend/core/solid.py"])
    assert report.risk_level == "low"  # not elevated


@pytest.mark.asyncio
async def test_briefing_with_no_blueprints():
    """Morning briefing works even when dream has no blueprints."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    dream.get_blueprints.return_value = []

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
        say_fn=say_fn,
    )

    await tc.start()

    say_fn.assert_awaited_once()
    briefing_text = say_fn.call_args[0][0]
    # Should still mention health score
    assert "85%" in briefing_text
    # Should NOT mention blueprint title since there are none
    assert "Refactor module X" not in briefing_text

    await tc.stop()


@pytest.mark.asyncio
async def test_briefing_with_no_active_insights():
    """Morning briefing omits insight count when active_insights == 0."""
    cortex, memory, dream, prophecy, config, comm, say_fn = _make_engines()

    memory.get_pattern_summary.return_value = PatternSummary(
        top_patterns=(), total_insights=0, active_insights=0, archived_insights=0,
    )

    from backend.core.ouroboros.consciousness.consciousness_service import TrinityConsciousness

    tc = TrinityConsciousness(
        health_cortex=cortex,
        memory_engine=memory,
        dream_engine=dream,
        prophecy_engine=prophecy,
        config=config,
        say_fn=say_fn,
    )

    await tc.start()

    say_fn.assert_awaited_once()
    briefing_text = say_fn.call_args[0][0]
    # Should NOT mention "active insights" when count is 0
    assert "active insight" not in briefing_text.lower()

    await tc.stop()
