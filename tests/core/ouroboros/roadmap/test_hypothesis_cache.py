"""Tests for HypothesisCache — exact-fingerprint cache with staleness checking."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hyp(
    description: str = "Add RoadmapSensor clock",
    evidence_fragments: tuple = ("spec:ouroboros-daemon-design", "memory:MEMORY.md"),
    gap_type: str = "missing_capability",
    confidence: float = 0.85,
    confidence_rule_id: str = "tier0-spec-vs-impl-diff",
    urgency: str = "high",
    suggested_scope: str = "new-agent",
    suggested_repos: tuple = ("jarvis",),
    provenance: str = "deterministic",
    synthesized_for_snapshot_hash: str = "abcdef1234567890",
    synthesized_at: float = 1_700_000_000.0,
    synthesis_input_fingerprint: str = "inputfp123",
    status: str = "active",
) -> FeatureHypothesis:
    return FeatureHypothesis(
        hypothesis_id=str(uuid.uuid4()),
        description=description,
        evidence_fragments=evidence_fragments,
        gap_type=gap_type,
        confidence=confidence,
        confidence_rule_id=confidence_rule_id,
        urgency=urgency,
        suggested_scope=suggested_scope,
        suggested_repos=suggested_repos,
        provenance=provenance,
        synthesized_for_snapshot_hash=synthesized_for_snapshot_hash,
        synthesized_at=synthesized_at,
        synthesis_input_fingerprint=synthesis_input_fingerprint,
        status=status,
    )


def _two_hypotheses() -> list[FeatureHypothesis]:
    return [
        _hyp(description="Add RoadmapSensor clock", gap_type="missing_capability"),
        _hyp(
            description="Wire existing sensor to supervisor",
            gap_type="incomplete_wiring",
            evidence_fragments=("spec:design", "code:supervisor.py"),
            confidence=0.72,
            urgency="medium",
            suggested_scope="wire-existing",
            suggested_repos=("jarvis", "jarvis-prime"),
            provenance="model:doubleword-397b",
            status="active",
        ),
    ]


# ---------------------------------------------------------------------------
# test_cache_save_and_load
# ---------------------------------------------------------------------------

class TestCacheSaveAndLoad:
    def test_round_trip_single(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        original = [_hyp()]
        cache.save(original)
        loaded = cache.load()
        assert len(loaded) == 1
        h = loaded[0]
        assert h.description == original[0].description
        assert h.gap_type == original[0].gap_type
        assert h.confidence == original[0].confidence
        assert h.evidence_fragments == original[0].evidence_fragments
        assert h.suggested_repos == original[0].suggested_repos
        assert h.hypothesis_fingerprint == original[0].hypothesis_fingerprint
        assert h.status == original[0].status
        assert h.hypothesis_id == original[0].hypothesis_id

    def test_round_trip_multiple(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        originals = _two_hypotheses()
        cache.save(originals)
        loaded = cache.load()
        assert len(loaded) == 2
        for orig, lod in zip(originals, loaded):
            assert lod.hypothesis_id == orig.hypothesis_id
            assert lod.description == orig.description
            assert lod.gap_type == orig.gap_type
            assert lod.provenance == orig.provenance
            assert lod.hypothesis_fingerprint == orig.hypothesis_fingerprint

    def test_round_trip_empty_list(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save([])
        loaded = cache.load()
        assert loaded == []

    def test_all_fields_preserved(self, tmp_path: Path):
        """Every field on FeatureHypothesis must survive the round-trip."""
        h = _hyp(
            description="Manifesto drift in supervisor",
            evidence_fragments=("spec:manifesto", "code:supervisor.py"),
            gap_type="manifesto_violation",
            confidence=0.99,
            confidence_rule_id="manifesto-checker-v2",
            urgency="critical",
            suggested_scope="refactor",
            suggested_repos=("jarvis", "reactor"),
            provenance="model:claude",
            synthesized_for_snapshot_hash="deadbeef",
            synthesized_at=1_710_000_000.0,
            synthesis_input_fingerprint="fp_manifesto",
            status="dismissed",
        )
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save([h])
        loaded = cache.load()[0]

        assert loaded.hypothesis_id == h.hypothesis_id
        assert loaded.description == h.description
        assert loaded.evidence_fragments == h.evidence_fragments
        assert loaded.gap_type == h.gap_type
        assert loaded.confidence == h.confidence
        assert loaded.confidence_rule_id == h.confidence_rule_id
        assert loaded.urgency == h.urgency
        assert loaded.suggested_scope == h.suggested_scope
        assert loaded.suggested_repos == h.suggested_repos
        assert loaded.provenance == h.provenance
        assert loaded.synthesized_for_snapshot_hash == h.synthesized_for_snapshot_hash
        assert loaded.synthesized_at == h.synthesized_at
        assert loaded.synthesis_input_fingerprint == h.synthesis_input_fingerprint
        assert loaded.status == h.status
        assert loaded.hypothesis_fingerprint == h.hypothesis_fingerprint


# ---------------------------------------------------------------------------
# test_cache_hit_on_matching_fingerprint
# ---------------------------------------------------------------------------

class TestCacheHitOnMatchingFingerprint:
    def test_returns_list_on_match(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        hypotheses = _two_hypotheses()
        fingerprint = "exact_fp_abc123"
        cache.save(hypotheses, input_fingerprint=fingerprint)
        result = cache.get_if_valid(fingerprint)
        assert result is not None
        assert len(result) == 2

    def test_returned_hypotheses_match_originals(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        originals = _two_hypotheses()
        fingerprint = "fp_test_match"
        cache.save(originals, input_fingerprint=fingerprint)
        result = cache.get_if_valid(fingerprint)
        assert result is not None
        for orig, lod in zip(originals, result):
            assert lod.hypothesis_fingerprint == orig.hypothesis_fingerprint
            assert lod.description == orig.description


# ---------------------------------------------------------------------------
# test_cache_miss_on_different_fingerprint
# ---------------------------------------------------------------------------

class TestCacheMissOnDifferentFingerprint:
    def test_returns_none_on_mismatch(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        hypotheses = _two_hypotheses()
        cache.save(hypotheses, input_fingerprint="stored_fp")
        result = cache.get_if_valid("different_fp")
        assert result is None

    def test_returns_none_on_empty_stored_fingerprint(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        hypotheses = _two_hypotheses()
        # Save with no fingerprint (defaults to empty string or None)
        cache.save(hypotheses, input_fingerprint="")
        result = cache.get_if_valid("some_fp")
        assert result is None

    def test_returns_none_when_no_cache_files(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        result = cache.get_if_valid("any_fp")
        assert result is None


# ---------------------------------------------------------------------------
# test_cache_persists_to_disk
# ---------------------------------------------------------------------------

class TestCachePersistsToDisk:
    def test_new_instance_reads_same_data(self, tmp_path: Path):
        fingerprint = "persist_fp_xyz"
        originals = _two_hypotheses()

        cache1 = HypothesisCache(cache_dir=tmp_path)
        cache1.save(originals, input_fingerprint=fingerprint)

        # Create a brand-new instance pointed at the same directory
        cache2 = HypothesisCache(cache_dir=tmp_path)
        result = cache2.get_if_valid(fingerprint)

        assert result is not None
        assert len(result) == len(originals)
        for orig, lod in zip(originals, result):
            assert lod.hypothesis_id == orig.hypothesis_id
            assert lod.description == orig.description

    def test_hypotheses_json_file_exists(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(_two_hypotheses())
        assert (tmp_path / "hypotheses.json").exists()

    def test_hypotheses_meta_json_file_exists(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(_two_hypotheses(), input_fingerprint="fp123")
        assert (tmp_path / "hypotheses_meta.json").exists()

    def test_meta_file_has_expected_keys(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(_two_hypotheses(), input_fingerprint="fp_keys")
        meta_path = tmp_path / "hypotheses_meta.json"
        meta = json.loads(meta_path.read_text())
        assert "input_fingerprint" in meta
        assert "snapshot_hash" in meta
        assert "saved_at" in meta
        assert "count" in meta

    def test_meta_count_matches_saved_hypotheses(self, tmp_path: Path):
        hypotheses = _two_hypotheses()
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(hypotheses)
        meta_path = tmp_path / "hypotheses_meta.json"
        meta = json.loads(meta_path.read_text())
        assert meta["count"] == len(hypotheses)


# ---------------------------------------------------------------------------
# test_cache_empty_when_no_file
# ---------------------------------------------------------------------------

class TestCacheEmptyWhenNoFile:
    def test_load_returns_empty_list(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        assert cache.load() == []

    def test_load_from_missing_dir_returns_empty(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path / "nonexistent")
        assert cache.load() == []


# ---------------------------------------------------------------------------
# test_stale_hash_mismatch
# ---------------------------------------------------------------------------

class TestStaleHashMismatch:
    def test_different_snapshot_hash_is_stale(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(
            _two_hypotheses(),
            input_fingerprint="fp",
            snapshot_hash="original_hash",
        )
        assert cache.is_stale(current_snapshot_hash="different_hash", ttl_s=9999) is True

    def test_same_hash_but_different_not_stale(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(
            _two_hypotheses(),
            input_fingerprint="fp",
            snapshot_hash="same_hash",
        )
        # Same hash, very long TTL → not stale
        assert cache.is_stale(current_snapshot_hash="same_hash", ttl_s=99999) is False


# ---------------------------------------------------------------------------
# test_not_stale_when_matching
# ---------------------------------------------------------------------------

class TestNotStaleWhenMatching:
    def test_fresh_cache_with_matching_hash(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(
            _two_hypotheses(),
            input_fingerprint="fp",
            snapshot_hash="current_hash",
        )
        assert cache.is_stale(current_snapshot_hash="current_hash", ttl_s=3600) is False

    def test_no_cache_file_is_always_stale(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        # Without saving, is_stale should treat missing cache as stale
        assert cache.is_stale(current_snapshot_hash="any_hash", ttl_s=3600) is True


# ---------------------------------------------------------------------------
# test_stale_age_exceeded
# ---------------------------------------------------------------------------

class TestStaleAgeExceeded:
    def test_old_saved_at_is_stale(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(
            _two_hypotheses(),
            input_fingerprint="fp",
            snapshot_hash="current_hash",
        )
        # Manually backdate the saved_at in meta to simulate old cache
        meta_path = tmp_path / "hypotheses_meta.json"
        meta = json.loads(meta_path.read_text())
        meta["saved_at"] = time.time() - 7200  # 2 hours ago
        meta_path.write_text(json.dumps(meta))

        assert cache.is_stale(current_snapshot_hash="current_hash", ttl_s=3600) is True

    def test_fresh_cache_not_stale(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(
            _two_hypotheses(),
            input_fingerprint="fp",
            snapshot_hash="current_hash",
        )
        # saved_at is right now, TTL is 1 hour
        assert cache.is_stale(current_snapshot_hash="current_hash", ttl_s=3600) is False

    def test_ttl_zero_is_always_stale(self, tmp_path: Path):
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(
            _two_hypotheses(),
            input_fingerprint="fp",
            snapshot_hash="current_hash",
        )
        # TTL of 0 means any age exceeds it
        assert cache.is_stale(current_snapshot_hash="current_hash", ttl_s=0) is True


# ---------------------------------------------------------------------------
# test_corrupt_cache_returns_empty
# ---------------------------------------------------------------------------

class TestCorruptCacheReturnsEmpty:
    def test_garbage_hypotheses_json(self, tmp_path: Path):
        (tmp_path / "hypotheses.json").write_text("not valid json {{{{")
        (tmp_path / "hypotheses_meta.json").write_text(
            json.dumps({
                "input_fingerprint": "fp",
                "snapshot_hash": "hash",
                "saved_at": time.time(),
                "count": 1,
            })
        )
        cache = HypothesisCache(cache_dir=tmp_path)
        assert cache.load() == []

    def test_garbage_meta_json_still_loads_hypotheses(self, tmp_path: Path):
        """Corrupt meta should not prevent load() from succeeding."""
        cache = HypothesisCache(cache_dir=tmp_path)
        originals = _two_hypotheses()
        cache.save(originals, input_fingerprint="fp")
        # Corrupt the meta
        (tmp_path / "hypotheses_meta.json").write_text("{{corrupt}}")
        # load() only reads hypotheses.json, should still work
        loaded = cache.load()
        assert len(loaded) == 2

    def test_garbage_meta_makes_get_if_valid_return_none(self, tmp_path: Path):
        """Corrupt meta means we can't validate fingerprint → return None."""
        cache = HypothesisCache(cache_dir=tmp_path)
        cache.save(_two_hypotheses(), input_fingerprint="fp")
        (tmp_path / "hypotheses_meta.json").write_text("{{corrupt}}")
        assert cache.get_if_valid("fp") is None

    def test_invalid_hypothesis_dict_is_skipped(self, tmp_path: Path):
        """A hypothesis dict with an invalid gap_type should be gracefully skipped."""
        bad_data = [
            {
                "hypothesis_id": str(uuid.uuid4()),
                "description": "good",
                "evidence_fragments": ["spec:a"],
                "gap_type": "totally_invalid_type",  # will fail __post_init__
                "confidence": 0.8,
                "confidence_rule_id": "r1",
                "urgency": "high",
                "suggested_scope": "new-agent",
                "suggested_repos": ["jarvis"],
                "provenance": "deterministic",
                "synthesized_for_snapshot_hash": "hash",
                "synthesized_at": 1_700_000_000.0,
                "synthesis_input_fingerprint": "fp",
                "status": "active",
            }
        ]
        (tmp_path / "hypotheses.json").write_text(json.dumps(bad_data))
        cache = HypothesisCache(cache_dir=tmp_path)
        # Should not raise, just return empty or partial
        loaded = cache.load()
        assert isinstance(loaded, list)
        # The invalid entry should be skipped
        assert len(loaded) == 0

    def test_empty_hypotheses_json_returns_empty(self, tmp_path: Path):
        (tmp_path / "hypotheses.json").write_text("[]")
        cache = HypothesisCache(cache_dir=tmp_path)
        assert cache.load() == []
