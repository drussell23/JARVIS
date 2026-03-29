"""
Tests for FeatureSynthesisEngine (Clock 2)
===========================================

Coverage:
- test_cache_hit_returns_cached
- test_cache_miss_runs_tier0
- test_single_flight_guard
- test_min_interval_respected
- test_input_fingerprint_deterministic
- test_input_fingerprint_changes
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.synthesis_engine import (
    FeatureSynthesisEngine,
    SynthesisConfig,
    compute_input_fingerprint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fragment(source_id: str = "spec:test", summary: str = "test summary") -> SnapshotFragment:
    return SnapshotFragment(
        source_id=source_id,
        uri="docs/test.md",
        tier=0,
        content_hash=hashlib.sha256(summary.encode()).hexdigest(),
        fetched_at=time.time(),
        mtime=time.time(),
        title="Test Spec",
        summary=summary,
        fragment_type="spec",
    )


def _make_snapshot(summary: str = "analytics agent spec") -> RoadmapSnapshot:
    fragment = _make_fragment(summary=summary)
    return RoadmapSnapshot.create(fragments=(fragment,))


def _make_hypothesis(snapshot_hash: str = "abc123") -> FeatureHypothesis:
    return FeatureHypothesis.new(
        description="Missing agent: analytics",
        evidence_fragments=("spec:test",),
        gap_type="missing_capability",
        confidence=0.85,
        confidence_rule_id="spec_symbol_miss",
        urgency="medium",
        suggested_scope="new-agent",
        suggested_repos=(),
        provenance="deterministic",
        synthesized_for_snapshot_hash=snapshot_hash,
        synthesis_input_fingerprint="fp_" + snapshot_hash[:8],
    )


def _make_mock_cache(
    get_if_valid_return: Optional[List[FeatureHypothesis]] = None,
    load_return: Optional[List[FeatureHypothesis]] = None,
) -> MagicMock:
    cache = MagicMock()
    cache.get_if_valid.return_value = get_if_valid_return
    cache.load.return_value = load_return if load_return is not None else []
    cache.save.return_value = None
    return cache


def _make_mock_oracle() -> MagicMock:
    oracle = MagicMock()
    oracle.find_nodes_by_name.return_value = []  # no existing symbols → gaps detected
    return oracle


def _make_engine(
    cache: Optional[MagicMock] = None,
    oracle: Optional[MagicMock] = None,
    config: Optional[SynthesisConfig] = None,
) -> FeatureSynthesisEngine:
    return FeatureSynthesisEngine(
        oracle=oracle or _make_mock_oracle(),
        doubleword=None,
        cache=cache or _make_mock_cache(),
        config=config or SynthesisConfig(),
    )


# ---------------------------------------------------------------------------
# compute_input_fingerprint
# ---------------------------------------------------------------------------

def test_input_fingerprint_deterministic():
    """Same inputs always produce the same fingerprint."""
    fp1 = compute_input_fingerprint("hash_abc", 1, "doubleword-397b")
    fp2 = compute_input_fingerprint("hash_abc", 1, "doubleword-397b")
    assert fp1 == fp2
    assert len(fp1) == 64  # full sha256 hex digest


def test_input_fingerprint_changes():
    """Different inputs produce different fingerprints."""
    fp_a = compute_input_fingerprint("hash_abc", 1, "doubleword-397b")
    fp_b = compute_input_fingerprint("hash_xyz", 1, "doubleword-397b")
    fp_c = compute_input_fingerprint("hash_abc", 2, "doubleword-397b")
    fp_d = compute_input_fingerprint("hash_abc", 1, "claude")

    assert fp_a != fp_b, "different snapshot_hash should differ"
    assert fp_a != fp_c, "different prompt_version should differ"
    assert fp_a != fp_d, "different model_id should differ"


# ---------------------------------------------------------------------------
# synthesize — cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_returns_cached():
    """When get_if_valid returns a list, synthesize returns it immediately without running Tier 0."""
    snapshot = _make_snapshot()
    cached = [_make_hypothesis(snapshot.content_hash)]
    cache = _make_mock_cache(get_if_valid_return=cached)
    engine = _make_engine(cache=cache)

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints"
    ) as mock_tier0:
        result = await engine.synthesize(snapshot)

    assert result == cached
    mock_tier0.assert_not_called()
    cache.get_if_valid.assert_called_once()


# ---------------------------------------------------------------------------
# synthesize — cache miss → Tier 0 runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_miss_runs_tier0():
    """When cache returns None, synthesize runs Tier 0 and returns a list."""
    snapshot = _make_snapshot("analytics agent spec")
    cache = _make_mock_cache(get_if_valid_return=None, load_return=[])

    tier0_result = [_make_hypothesis(snapshot.content_hash)]

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=tier0_result,
    ) as mock_tier0:
        engine = _make_engine(cache=cache)
        result = await engine.synthesize(snapshot)

    assert isinstance(result, list)
    assert len(result) == 1
    mock_tier0.assert_called_once()
    cache.save.assert_called_once()


# ---------------------------------------------------------------------------
# synthesize — single-flight guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_flight_guard():
    """Second concurrent call sees lock held and returns cache.load() without running Tier 0."""
    snapshot = _make_snapshot()
    cached_on_load = [_make_hypothesis(snapshot.content_hash)]
    cache = _make_mock_cache(get_if_valid_return=None, load_return=cached_on_load)

    engine = _make_engine(cache=cache)

    # Manually acquire the lock to simulate a synthesis already in flight.
    await engine._synthesis_lock.acquire()
    try:
        # With lock held, synthesize should short-circuit and return cache.load().
        result = await engine.synthesize(snapshot)
    finally:
        engine._synthesis_lock.release()

    assert isinstance(result, list)
    assert result == cached_on_load


# ---------------------------------------------------------------------------
# synthesize — min-interval respected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_interval_respected():
    """A second synthesize call within min_interval_s returns cache.load() without re-running Tier 0."""
    snapshot = _make_snapshot()
    cached_on_load = [_make_hypothesis(snapshot.content_hash)]
    cache = _make_mock_cache(get_if_valid_return=None, load_return=cached_on_load)

    # Very short min_interval so the second call hits the cooldown
    config = SynthesisConfig(min_interval_s=3600.0)

    tier0_result = [_make_hypothesis(snapshot.content_hash)]

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=tier0_result,
    ) as mock_tier0:
        engine = _make_engine(cache=cache, config=config)

        # First call — cache miss, runs Tier 0, updates _last_synthesis_at
        result1 = await engine.synthesize(snapshot)

        # Reset cache mock so get_if_valid returns None again on second call
        cache.get_if_valid.return_value = None

        # Second call — within min_interval, should NOT re-run Tier 0
        result2 = await engine.synthesize(snapshot)

    assert isinstance(result1, list)
    assert isinstance(result2, list)
    # Tier 0 should only have run once
    assert mock_tier0.call_count == 1


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

def test_health_returns_dict():
    """health() returns a dict with expected keys."""
    engine = _make_engine()
    h = engine.health()
    assert isinstance(h, dict)
    assert "last_synthesis_at" in h
    assert "lock_held" in h
    assert "config" in h
