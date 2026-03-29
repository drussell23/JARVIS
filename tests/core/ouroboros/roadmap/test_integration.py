"""
End-to-End Integration Tests — Clock 1 → Clock 2 → REM Consumption
====================================================================

Validates the full cognitive-extensions pipeline:

  RoadmapSensor (Clock 1)
    └─► RoadmapSnapshot
          └─► FeatureSynthesisEngine (Clock 2)
                └─► HypothesisCache (persistence)

All filesystem I/O is rooted at ``tmp_path`` so each test is fully isolated.
The mock oracle returns an empty symbol list for every lookup, which causes the
Tier 0 engine to emit a gap hypothesis for every capability reference it finds.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.sensor import RoadmapSensor, RoadmapSensorConfig
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot
from backend.core.ouroboros.roadmap.synthesis_engine import (
    FeatureSynthesisEngine,
    SynthesisConfig,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_mock_oracle() -> MagicMock:
    """Oracle that always reports no existing symbols — every reference is a gap."""
    oracle = MagicMock()
    oracle.find_nodes_by_name = MagicMock(return_value=[])
    return oracle


def _make_spec_file(
    tmp_path: Path,
    name: str = "whatsapp_spec",
    content: str = "# WhatsApp Spec\nWe need a WhatsApp agent integration provider.",
) -> Path:
    """Create a spec file that crawl_specs() will pick up."""
    specs_dir = tmp_path / "docs" / "superpowers" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    spec_file = specs_dir / f"{name}.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


def _make_sensor(
    tmp_path: Path,
    on_snapshot_changed=None,
) -> RoadmapSensor:
    """Return a sensor with P1 (git log) disabled to avoid subprocess in tests."""
    return RoadmapSensor(
        repo_root=tmp_path,
        config=RoadmapSensorConfig(p1_enabled=False),
        on_snapshot_changed=on_snapshot_changed,
    )


def _make_engine(
    cache: HypothesisCache,
    oracle: MagicMock,
) -> FeatureSynthesisEngine:
    """Return a synthesis engine with min_interval_s=0 so force=True is optional."""
    return FeatureSynthesisEngine(
        oracle=oracle,
        doubleword=None,
        cache=cache,
        config=SynthesisConfig(min_interval_s=0),
    )


# ---------------------------------------------------------------------------
# test_clock1_produces_snapshot
# ---------------------------------------------------------------------------

def test_clock1_produces_snapshot(tmp_path: Path) -> None:
    """Clock 1: RoadmapSensor.refresh() produces a version-1 snapshot with spec fragment."""
    _make_spec_file(tmp_path)

    sensor = _make_sensor(tmp_path)
    snapshot = sensor.refresh()

    assert isinstance(snapshot, RoadmapSnapshot)
    assert snapshot.version == 1
    assert len(snapshot.fragments) >= 1

    # At least one fragment should come from the spec file
    source_ids = [f.source_id for f in snapshot.fragments]
    assert any("whatsapp_spec" in sid or "spec:" in sid for sid in source_ids), (
        f"Expected spec fragment, got source_ids={source_ids}"
    )


# ---------------------------------------------------------------------------
# test_clock2_produces_hypotheses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clock2_produces_hypotheses(tmp_path: Path) -> None:
    """Clock 2: FeatureSynthesisEngine synthesizes gap hypotheses from the snapshot.

    The spec file mentions "whatsapp agent" and "integration provider" which
    the Tier 0 engine recognises as capability references.  Because the mock
    oracle returns no symbols, each reference becomes a hypothesis.
    """
    _make_spec_file(
        tmp_path,
        content="# WhatsApp Spec\nWe need a whatsapp agent and an integration provider.",
    )

    sensor = _make_sensor(tmp_path)
    snapshot = sensor.refresh()
    assert snapshot.version == 1

    cache = HypothesisCache(cache_dir=tmp_path / "cache")
    oracle = _make_mock_oracle()
    engine = _make_engine(cache, oracle)

    hypotheses = await engine.synthesize(snapshot, force=True)

    assert isinstance(hypotheses, list), "synthesize must return a list"
    assert len(hypotheses) >= 1, "Expected at least one hypothesis from the spec"

    # At least one hypothesis should relate to whatsapp
    descriptions_lower = [h.description.lower() for h in hypotheses]
    assert any("whatsapp" in desc for desc in descriptions_lower), (
        f"Expected a 'whatsapp' hypothesis, got: {descriptions_lower}"
    )


# ---------------------------------------------------------------------------
# test_full_pipeline_clock1_to_clock2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_pipeline_clock1_to_clock2(tmp_path: Path) -> None:
    """Full pipeline: sensor callback triggers synthesis; cache is populated after refresh.

    Wire ``on_snapshot_changed`` to fire ``engine.trigger(snapshot)`` so that
    a single ``sensor.refresh()`` drives both clocks end-to-end.
    """
    _make_spec_file(
        tmp_path,
        content="# Pipeline Spec\nWe need a pipeline agent.",
    )

    cache = HypothesisCache(cache_dir=tmp_path / "cache")
    oracle = _make_mock_oracle()
    engine = _make_engine(cache, oracle)

    # Collect snapshots delivered to the callback to verify wiring
    received_snapshots: List[RoadmapSnapshot] = []

    def _on_changed(snap: RoadmapSnapshot) -> None:
        received_snapshots.append(snap)
        # Trigger Clock 2 synchronously inside the async context by scheduling
        # a coroutine.  In the test event loop, asyncio.run() is not available,
        # so we schedule it and let the event loop flush it below.
        loop = asyncio.get_event_loop()
        loop.create_task(engine.trigger(snap))

    sensor = _make_sensor(tmp_path, on_snapshot_changed=_on_changed)
    sensor.refresh()

    # Yield control so the engine.trigger() task can complete
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The callback must have fired exactly once (first refresh, content changed)
    assert len(received_snapshots) == 1
    assert isinstance(received_snapshots[0], RoadmapSnapshot)

    # The cache should now be populated
    loaded = cache.load()
    assert isinstance(loaded, list)
    assert len(loaded) >= 1


# ---------------------------------------------------------------------------
# test_hypothesis_cache_survives_restart
# ---------------------------------------------------------------------------

def test_hypothesis_cache_survives_restart(tmp_path: Path) -> None:
    """Persistence: hypotheses saved to disk are still readable by a new HypothesisCache instance.

    This simulates a process restart: save with one cache object, reload with
    a fresh one pointing to the same directory.
    """
    cache_dir = tmp_path / "cache"
    snapshot_hash = "deadbeef" * 8  # 64-char fake hash

    hypothesis = FeatureHypothesis.new(
        description="Missing agent: whatsapp",
        evidence_fragments=("spec:whatsapp_spec",),
        gap_type="missing_capability",
        confidence=0.85,
        confidence_rule_id="spec_symbol_miss",
        urgency="medium",
        suggested_scope="new-agent",
        suggested_repos=(),
        provenance="deterministic",
        synthesized_for_snapshot_hash=snapshot_hash,
        synthesis_input_fingerprint="fp_test_" + snapshot_hash[:8],
    )

    # Save with the first cache instance
    cache1 = HypothesisCache(cache_dir=cache_dir)
    fingerprint = "test_fingerprint_" + str(uuid.uuid4())[:8]
    cache1.save(
        [hypothesis],
        input_fingerprint=fingerprint,
        snapshot_hash=snapshot_hash,
    )

    # Create a brand-new cache instance pointing at the same directory
    cache2 = HypothesisCache(cache_dir=cache_dir)
    loaded = cache2.load()

    assert len(loaded) == 1, f"Expected 1 hypothesis after restart, got {len(loaded)}"
    restored = loaded[0]

    assert restored.description == hypothesis.description
    assert restored.gap_type == hypothesis.gap_type
    assert restored.confidence == pytest.approx(hypothesis.confidence)
    assert restored.provenance == hypothesis.provenance
    assert restored.synthesized_for_snapshot_hash == snapshot_hash
    # Fingerprint must survive serialisation round-trip
    assert restored.hypothesis_fingerprint == hypothesis.hypothesis_fingerprint


# ---------------------------------------------------------------------------
# test_cache_get_if_valid_matches_fingerprint
# ---------------------------------------------------------------------------

def test_cache_get_if_valid_matches_fingerprint(tmp_path: Path) -> None:
    """get_if_valid returns the list when the fingerprint matches, None otherwise."""
    cache_dir = tmp_path / "cache"
    snapshot_hash = "aabbccdd" * 8
    fingerprint = "exact_fp_" + str(uuid.uuid4())[:12]

    hypothesis = FeatureHypothesis.new(
        description="Missing integration: slack",
        evidence_fragments=("spec:slack_spec",),
        gap_type="missing_capability",
        confidence=0.80,
        confidence_rule_id="spec_symbol_miss",
        urgency="low",
        suggested_scope="new-agent",
        suggested_repos=(),
        provenance="deterministic",
        synthesized_for_snapshot_hash=snapshot_hash,
        synthesis_input_fingerprint=fingerprint,
    )

    cache = HypothesisCache(cache_dir=cache_dir)
    cache.save([hypothesis], input_fingerprint=fingerprint, snapshot_hash=snapshot_hash)

    # Exact fingerprint match → should return the list
    result = cache.get_if_valid(fingerprint)
    assert result is not None
    assert len(result) == 1
    assert result[0].description == hypothesis.description

    # Different fingerprint → cache miss
    miss = cache.get_if_valid("totally_different_fingerprint")
    assert miss is None


# ---------------------------------------------------------------------------
# test_snapshot_version_increments_on_content_change
# ---------------------------------------------------------------------------

def test_snapshot_version_increments_on_content_change(tmp_path: Path) -> None:
    """Clock 1 change-detection: version bumps only when file content actually changes."""
    spec_file = _make_spec_file(tmp_path, content="# Initial\nWe need a WhatsApp agent.")
    sensor = _make_sensor(tmp_path)

    snap1 = sensor.refresh()
    assert snap1.version == 1

    # Second refresh with no changes — version must stay the same
    snap2 = sensor.refresh()
    assert snap2.version == 1
    assert snap2.content_hash == snap1.content_hash

    # Modify the file — content changes
    spec_file.write_text(
        "# Updated\nWe need a WhatsApp agent and a Telegram sensor.", encoding="utf-8"
    )
    snap3 = sensor.refresh()
    assert snap3.version == 2
    assert snap3.content_hash != snap1.content_hash


# ---------------------------------------------------------------------------
# test_synthesis_engine_deduplicates_hypotheses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_engine_deduplicates_hypotheses(tmp_path: Path) -> None:
    """Tier 0 deduplication: same capability reference from multiple specs produces one hypothesis."""
    # Two spec files both mentioning "telegram agent"
    specs_dir = tmp_path / "docs" / "superpowers" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / "spec_a.md").write_text(
        "# Spec A\nWe need a telegram agent.", encoding="utf-8"
    )
    (specs_dir / "spec_b.md").write_text(
        "# Spec B\nWe also need a telegram agent.", encoding="utf-8"
    )

    sensor = _make_sensor(tmp_path)
    snapshot = sensor.refresh()

    cache = HypothesisCache(cache_dir=tmp_path / "cache")
    oracle = _make_mock_oracle()
    engine = _make_engine(cache, oracle)

    hypotheses = await engine.synthesize(snapshot, force=True)

    # Even though two specs mention "telegram agent", only one hypothesis should
    # appear (deduplicated by hypothesis_fingerprint which is content-based)
    telegram_hypotheses = [
        h for h in hypotheses if "telegram" in h.description.lower()
    ]
    assert len(telegram_hypotheses) == 1, (
        f"Expected exactly 1 telegram hypothesis after dedup, "
        f"got {len(telegram_hypotheses)}: {[h.description for h in telegram_hypotheses]}"
    )
