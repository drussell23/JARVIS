"""tests/test_ouroboros_governance/test_dream_engine.py

TDD tests for DreamEngine — idle GPU speculative analysis.

Test cases:
    TC09:  Dream gate rejects when VM not ready
    TC10:  Dream gate rejects when user is active
    TC11:  Dream gate rejects when VM was woken by dream (not user)
    TC17:  Preemption on user activity abandons job
    TC18:  Flap damping prevents rapid re-entry
    TC23:  Dream prompts capped at 2048 tokens
    TC24:  Prime unavailable -> DREAM_DORMANT reason code
    TC29:  Direct HTTP used, NOT PrimeClient
    TC30:  Preemption saves partial state for resume
    Plus:  idempotent job key skip, sorted blueprints, stale discard,
           budget cap, resource governor yield, stop persists, start loads
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.consciousness.types import (
    BudgetHealth,
    ConsciousnessConfig,
    DreamMetrics,
    HealthTrend,
    ImprovementBlueprint,
    ResourceHealth,
    SubsystemHealth,
    TrinityHealthSnapshot,
    TrustHealth,
    UserActivityMonitor,
    compute_blueprint_id,
    compute_job_key,
)
from backend.core.ouroboros.consciousness.dream_metrics import DreamMetricsTracker


# ---------------------------------------------------------------------------
# Fixtures
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


def _make_prime_health(
    status: str = "healthy",
    model_loaded: bool = True,
    uptime_s: float = 600.0,
) -> SubsystemHealth:
    return SubsystemHealth(
        name="prime",
        status=status,
        score=1.0 if status == "healthy" else 0.0,
        details={"model_loaded": model_loaded, "uptime_s": uptime_s},
        polled_at_utc="2026-03-20T00:00:00+00:00",
    )


def _make_snapshot(
    prime_status: str = "healthy",
    model_loaded: bool = True,
    prime_uptime_s: float = 600.0,
) -> TrinityHealthSnapshot:
    prime = _make_prime_health(prime_status, model_loaded, prime_uptime_s)
    return TrinityHealthSnapshot(
        timestamp_utc="2026-03-20T00:00:00+00:00",
        overall_verdict="HEALTHY",
        overall_score=1.0,
        jarvis=SubsystemHealth(
            name="jarvis", status="healthy", score=1.0,
            details={}, polled_at_utc="2026-03-20T00:00:00+00:00",
        ),
        prime=prime,
        reactor=SubsystemHealth(
            name="reactor", status="healthy", score=1.0,
            details={}, polled_at_utc="2026-03-20T00:00:00+00:00",
        ),
        resources=ResourceHealth(
            cpu_percent=30.0, ram_percent=50.0, disk_percent=40.0,
            pressure="NORMAL",
        ),
        budget=BudgetHealth(
            daily_spend_usd=1.0, iteration_spend_usd=0.1, remaining_usd=9.0,
        ),
        trust=TrustHealth(current_tier="governed", graduation_progress=0.0),
    )


def _make_blueprint(
    blueprint_id: str = "test-bp-001",
    priority: float = 0.8,
    repo_sha: str = "abc123",
    policy_hash: str = "pol123",
) -> ImprovementBlueprint:
    return ImprovementBlueprint(
        blueprint_id=blueprint_id,
        title="Test Blueprint",
        description="A test improvement",
        category="test_coverage",
        priority_score=priority,
        target_files=("src/foo.py",),
        estimated_effort="small",
        estimated_cost_usd=0.01,
        repo="jarvis",
        repo_sha=repo_sha,
        computed_at_utc="2026-03-20T00:00:00+00:00",
        ttl_hours=24.0,
        model_used="qwen2.5-7b",
        policy_hash=policy_hash,
        oracle_neighborhood={},
        suggested_approach="Add unit test",
        risk_assessment="Low risk",
    )


class MockActivityMonitor:
    """Test double implementing UserActivityMonitor protocol."""

    def __init__(self, idle_seconds: float = 600.0) -> None:
        self._idle_seconds = idle_seconds

    def last_activity_s(self) -> float:
        return self._idle_seconds


@pytest.fixture
def tmp_dream_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dreams"
    d.mkdir()
    return d


@pytest.fixture
def healthy_cortex() -> MagicMock:
    cortex = MagicMock()
    cortex.get_snapshot.return_value = _make_snapshot()
    return cortex


@pytest.fixture
def memory_engine() -> MagicMock:
    engine = MagicMock()
    engine.get_file_reputation.return_value = MagicMock(fragility_score=0.1)
    return engine


@pytest.fixture
def idle_monitor() -> MockActivityMonitor:
    return MockActivityMonitor(idle_seconds=600.0)


@pytest.fixture
def active_monitor() -> MockActivityMonitor:
    return MockActivityMonitor(idle_seconds=10.0)


@pytest.fixture
def resource_governor() -> MagicMock:
    gov = MagicMock()
    gov.should_yield = AsyncMock(return_value=False)
    return gov


@pytest.fixture
def metrics_tracker() -> DreamMetricsTracker:
    return DreamMetricsTracker()


@pytest.fixture
def comm_protocol() -> MagicMock:
    comm = MagicMock()
    comm.emit_heartbeat = AsyncMock()
    return comm


@pytest.fixture
def config() -> ConsciousnessConfig:
    return _make_config()


def _build_engine(
    health_cortex: Any,
    memory_engine: Any,
    activity_monitor: Any,
    resource_governor: Any,
    metrics_tracker: DreamMetricsTracker,
    config: ConsciousnessConfig,
    persistence_dir: Path,
    jprime_url: str = "http://localhost:8000",
    comm: Any = None,
):
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    return DreamEngine(
        health_cortex=health_cortex,
        memory_engine=memory_engine,
        activity_monitor=activity_monitor,
        resource_governor=resource_governor,
        metrics_tracker=metrics_tracker,
        config=config,
        jprime_url=jprime_url,
        persistence_dir=persistence_dir,
        comm=comm,
    )


# ============================================================================
# TC09: Dream gate rejects VM not ready
# ============================================================================


@pytest.mark.asyncio
async def test_dream_gate_rejects_vm_not_ready(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC09: prime not healthy -> cannot dream."""
    # Case 1: prime status is not healthy
    healthy_cortex.get_snapshot.return_value = _make_snapshot(prime_status="offline")
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    can, reason = await engine._can_dream()
    assert can is False
    assert "prime" in reason.lower() or "health" in reason.lower()

    # Case 2: model not loaded
    healthy_cortex.get_snapshot.return_value = _make_snapshot(
        prime_status="healthy", model_loaded=False,
    )
    can2, reason2 = await engine._can_dream()
    assert can2 is False
    assert "model" in reason2.lower()


# ============================================================================
# TC10: Dream gate rejects user active
# ============================================================================


@pytest.mark.asyncio
async def test_dream_gate_rejects_user_active(
    healthy_cortex,
    memory_engine,
    active_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC10: last_activity < threshold -> cannot dream."""
    engine = _build_engine(
        healthy_cortex, memory_engine, active_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    can, reason = await engine._can_dream()
    assert can is False
    assert "user" in reason.lower() or "active" in reason.lower() or "idle" in reason.lower()


# ============================================================================
# TC11: Dream gate rejects VM woken by dream
# ============================================================================


@pytest.mark.asyncio
async def test_dream_gate_rejects_vm_woken_by_dream(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC11: VM uptime < idle threshold means VM was woken for dream, not by user."""
    healthy_cortex.get_snapshot.return_value = _make_snapshot(
        prime_uptime_s=10.0,  # VM just started, not warmed by user
    )
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    can, reason = await engine._can_dream()
    assert can is False
    assert "uptime" in reason.lower() or "warm" in reason.lower()


# ============================================================================
# TC17: Preemption on user activity
# ============================================================================


@pytest.mark.asyncio
async def test_dream_preemption_on_user_activity(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC17: setting preempted event -> job abandoned."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    # Simulate that preemption fires
    engine._preempted.set()

    # _check_preempted should return True
    assert engine._check_preempted() is True

    # Verify the metrics tracker can record preemption
    metrics_tracker.record_preemption()
    m = metrics_tracker.get_metrics()
    assert m.preemptions_count == 1


# ============================================================================
# TC18: Flap damping
# ============================================================================


@pytest.mark.asyncio
async def test_dream_flap_damping(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    tmp_dream_dir,
):
    """TC18: After preemption, cannot re-enter dream for cooldown_s period."""
    config = _make_config(dream_reentry_cooldown_s=60.0)
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )

    # Simulate a recent user return (just happened)
    engine._last_user_return = time.monotonic()

    can, reason = await engine._can_dream()
    assert can is False
    assert "cooldown" in reason.lower() or "flap" in reason.lower()


# ============================================================================
# TC23: Dream prompts capped at 2048 tokens
# ============================================================================


def test_separate_token_budgets(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC23: dream prompt text is capped at DREAM_MAX_PROMPT_CHARS."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    # Access the class constant
    from backend.core.ouroboros.consciousness.dream_engine import DREAM_MAX_PROMPT_CHARS
    assert DREAM_MAX_PROMPT_CHARS == 2048

    # Verify _truncate_prompt actually caps text
    long_text = "x" * 5000
    truncated = engine._truncate_prompt(long_text)
    assert len(truncated) <= DREAM_MAX_PROMPT_CHARS


# ============================================================================
# TC24: Dream dormant reason code
# ============================================================================


@pytest.mark.asyncio
async def test_dream_dormant_reason_code(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
    comm_protocol,
):
    """TC24: prime unavailable -> DREAM_DORMANT reason code via CommProtocol."""
    # Prime is offline
    healthy_cortex.get_snapshot.return_value = _make_snapshot(prime_status="offline")
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
        comm=comm_protocol,
    )

    # Call _emit_dormant directly
    await engine._emit_dormant("prime_unavailable")

    comm_protocol.emit_heartbeat.assert_called_once()
    call_kwargs = comm_protocol.emit_heartbeat.call_args
    # Should contain DREAM_DORMANT in phase
    assert "DREAM_DORMANT" in call_kwargs[1]["phase"] or "DREAM_DORMANT" in str(call_kwargs)


# ============================================================================
# TC29: Direct HTTP, NOT PrimeClient
# ============================================================================


def test_dream_http_direct(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC29: DreamEngine uses aiohttp directly, not PrimeClient or PrimeRouter."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
        jprime_url="http://136.113.252.164:8000",
    )
    # Verify engine stores the URL for direct HTTP
    assert engine._jprime_url == "http://136.113.252.164:8000"
    # Verify it has no PrimeClient/PrimeRouter reference
    assert not hasattr(engine, "_prime_client")
    assert not hasattr(engine, "_prime_router")


# ============================================================================
# TC30: Preemption saves partial state
# ============================================================================


@pytest.mark.asyncio
async def test_preemption_saves_partial(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """TC30: interrupted job info is preserved in _interrupted_jobs for resume."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )

    candidate_info = {
        "repo": "jarvis",
        "repo_sha": "abc123",
        "policy_hash": "pol123",
        "prompt_family": "test_coverage",
        "model_class": "qwen2.5-7b",
    }
    job_key = compute_job_key(
        candidate_info["repo_sha"],
        candidate_info["policy_hash"],
        candidate_info["prompt_family"],
        candidate_info["model_class"],
    )

    engine._save_interrupted(job_key, candidate_info)
    assert job_key in engine._interrupted_jobs
    assert engine._interrupted_jobs[job_key] == candidate_info


# ============================================================================
# Idempotent job key skip
# ============================================================================


@pytest.mark.asyncio
async def test_idempotent_job_key_skip(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """Same job key -> skip computation."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    key = compute_job_key("sha1", "pol1", "coverage", "qwen")
    engine._completed_keys.add(key)

    # Non-stale blueprint exists for this key
    bp = _make_blueprint(blueprint_id=key, repo_sha="sha1", policy_hash="pol1")
    engine._blueprints[key] = bp

    assert engine._is_job_completed(key, current_head="sha1", current_policy_hash="pol1") is True


# ============================================================================
# Blueprints sorted by priority
# ============================================================================


def test_get_blueprints_sorted_by_priority(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """Blueprints returned sorted by priority_score descending."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    bp_low = _make_blueprint(blueprint_id="low", priority=0.3, repo_sha="cur", policy_hash="pol")
    bp_high = _make_blueprint(blueprint_id="high", priority=0.9, repo_sha="cur", policy_hash="pol")
    bp_mid = _make_blueprint(blueprint_id="mid", priority=0.6, repo_sha="cur", policy_hash="pol")

    engine._blueprints["low"] = bp_low
    engine._blueprints["high"] = bp_high
    engine._blueprints["mid"] = bp_mid

    # Provide current head/policy that match so none are stale
    engine._current_head = "cur"
    engine._current_policy_hash = "pol"

    result = engine.get_blueprints(top_n=5)
    assert len(result) == 3
    assert result[0].priority_score == 0.9
    assert result[1].priority_score == 0.6
    assert result[2].priority_score == 0.3


# ============================================================================
# Discard stale removes expired
# ============================================================================


def test_discard_stale_removes_expired(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """discard_stale removes blueprints where repo_sha or policy_hash drifted."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    bp_fresh = _make_blueprint(
        blueprint_id="fresh", repo_sha="current", policy_hash="current_pol",
    )
    bp_stale = _make_blueprint(
        blueprint_id="stale", repo_sha="old_sha", policy_hash="current_pol",
    )

    engine._blueprints["fresh"] = bp_fresh
    engine._blueprints["stale"] = bp_stale
    engine._current_head = "current"
    engine._current_policy_hash = "current_pol"

    removed = engine.discard_stale()
    assert removed == 1
    assert "fresh" in engine._blueprints
    assert "stale" not in engine._blueprints


# ============================================================================
# Dream budget cap
# ============================================================================


@pytest.mark.asyncio
async def test_dream_budget_cap(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    tmp_dream_dir,
):
    """Max minutes exceeded -> cannot dream."""
    config = _make_config(dream_max_minutes_per_day=10.0)
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    # Simulate that we've already used 15 minutes today
    metrics_tracker.record_compute_time(15.0)

    can, reason = await engine._can_dream()
    assert can is False
    assert "budget" in reason.lower() or "minutes" in reason.lower()


# ============================================================================
# Resource governor yield
# ============================================================================


@pytest.mark.asyncio
async def test_resource_governor_yield(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """Resource governor says yield -> cannot dream."""
    gov = MagicMock()
    gov.should_yield = AsyncMock(return_value=True)

    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        gov, metrics_tracker, config, tmp_dream_dir,
    )
    can, reason = await engine._can_dream()
    assert can is False
    assert "resource" in reason.lower() or "yield" in reason.lower()


# ============================================================================
# Stop persists state
# ============================================================================


@pytest.mark.asyncio
async def test_stop_persists_state(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """stop() -> blueprints + completed keys saved to disk."""
    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    bp = _make_blueprint(blueprint_id="persist-test", repo_sha="sha1", policy_hash="pol1")
    engine._blueprints["persist-test"] = bp
    engine._completed_keys.add("key1")

    await engine.stop()

    # Check files were created
    bp_file = tmp_dream_dir / "blueprint_persist-test.json"
    keys_file = tmp_dream_dir / "job_keys.json"

    assert bp_file.exists()
    assert keys_file.exists()

    data = json.loads(bp_file.read_text())
    assert data["blueprint_id"] == "persist-test"

    keys_data = json.loads(keys_file.read_text())
    assert "key1" in keys_data


# ============================================================================
# Start loads state
# ============================================================================


@pytest.mark.asyncio
async def test_start_loads_state(
    healthy_cortex,
    memory_engine,
    idle_monitor,
    resource_governor,
    metrics_tracker,
    config,
    tmp_dream_dir,
):
    """start() -> blueprints + completed keys restored from disk."""
    # Pre-write a blueprint file and job_keys file
    bp = _make_blueprint(blueprint_id="loaded-bp", repo_sha="sha1", policy_hash="pol1")
    bp_data = {
        "blueprint_id": bp.blueprint_id,
        "title": bp.title,
        "description": bp.description,
        "category": bp.category,
        "priority_score": bp.priority_score,
        "target_files": list(bp.target_files),
        "estimated_effort": bp.estimated_effort,
        "estimated_cost_usd": bp.estimated_cost_usd,
        "repo": bp.repo,
        "repo_sha": bp.repo_sha,
        "computed_at_utc": bp.computed_at_utc,
        "ttl_hours": bp.ttl_hours,
        "model_used": bp.model_used,
        "policy_hash": bp.policy_hash,
        "oracle_neighborhood": bp.oracle_neighborhood,
        "suggested_approach": bp.suggested_approach,
        "risk_assessment": bp.risk_assessment,
    }
    bp_file = tmp_dream_dir / "blueprint_loaded-bp.json"
    bp_file.write_text(json.dumps(bp_data))

    keys_file = tmp_dream_dir / "job_keys.json"
    keys_file.write_text(json.dumps(["restored-key-1", "restored-key-2"]))

    engine = _build_engine(
        healthy_cortex, memory_engine, idle_monitor,
        resource_governor, metrics_tracker, config, tmp_dream_dir,
    )
    await engine.start()

    # Verify blueprints were loaded
    assert "loaded-bp" in engine._blueprints
    assert engine._blueprints["loaded-bp"].blueprint_id == "loaded-bp"

    # Verify completed keys were loaded
    assert "restored-key-1" in engine._completed_keys
    assert "restored-key-2" in engine._completed_keys

    # Clean up the dream loop task
    await engine.stop()
