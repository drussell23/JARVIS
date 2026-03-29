"""Tests for FeatureHypothesis and compute_hypothesis_fingerprint."""
from __future__ import annotations

import time
import uuid

import pytest

from backend.core.ouroboros.roadmap.hypothesis import (
    FeatureHypothesis,
    compute_hypothesis_fingerprint,
)


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


# ---------------------------------------------------------------------------
# compute_hypothesis_fingerprint
# ---------------------------------------------------------------------------

class TestComputeHypothesisFingerprint:
    def test_fingerprint_is_deterministic(self):
        fp1 = compute_hypothesis_fingerprint(
            "Add RoadmapSensor",
            ("spec:design", "memory:MEMORY.md"),
            "missing_capability",
        )
        fp2 = compute_hypothesis_fingerprint(
            "Add RoadmapSensor",
            ("spec:design", "memory:MEMORY.md"),
            "missing_capability",
        )
        assert fp1 == fp2

    def test_fingerprint_changes_with_description(self):
        fp1 = compute_hypothesis_fingerprint(
            "Add RoadmapSensor",
            ("spec:design",),
            "missing_capability",
        )
        fp2 = compute_hypothesis_fingerprint(
            "Wire existing RoadmapSensor",
            ("spec:design",),
            "missing_capability",
        )
        assert fp1 != fp2

    def test_fingerprint_changes_with_gap_type(self):
        fp1 = compute_hypothesis_fingerprint(
            "Refactor memory store",
            ("spec:design",),
            "stale_implementation",
        )
        fp2 = compute_hypothesis_fingerprint(
            "Refactor memory store",
            ("spec:design",),
            "incomplete_wiring",
        )
        assert fp1 != fp2

    def test_fingerprint_changes_with_evidence(self):
        fp1 = compute_hypothesis_fingerprint(
            "Add feature",
            ("spec:a",),
            "missing_capability",
        )
        fp2 = compute_hypothesis_fingerprint(
            "Add feature",
            ("spec:b",),
            "missing_capability",
        )
        assert fp1 != fp2

    def test_fingerprint_order_independent_evidence(self):
        """Evidence tuple ordering must not affect fingerprint."""
        fp1 = compute_hypothesis_fingerprint(
            "Add feature",
            ("spec:a", "plan:b"),
            "missing_capability",
        )
        fp2 = compute_hypothesis_fingerprint(
            "Add feature",
            ("plan:b", "spec:a"),
            "missing_capability",
        )
        assert fp1 == fp2

    def test_fingerprint_ignores_uuid(self):
        """Two hypotheses with different UUIDs but same content → same fingerprint."""
        h1 = FeatureHypothesis(
            hypothesis_id=str(uuid.uuid4()),
            description="Add RoadmapSensor",
            evidence_fragments=("spec:design",),
            gap_type="missing_capability",
            confidence=0.9,
            confidence_rule_id="r1",
            urgency="high",
            suggested_scope="new-agent",
            suggested_repos=("jarvis",),
            provenance="deterministic",
            synthesized_for_snapshot_hash="snap1",
            synthesized_at=1_700_000_000.0,
            synthesis_input_fingerprint="fp1",
        )
        h2 = FeatureHypothesis(
            hypothesis_id=str(uuid.uuid4()),  # different UUID
            description="Add RoadmapSensor",
            evidence_fragments=("spec:design",),
            gap_type="missing_capability",
            confidence=0.9,
            confidence_rule_id="r1",
            urgency="high",
            suggested_scope="new-agent",
            suggested_repos=("jarvis",),
            provenance="deterministic",
            synthesized_for_snapshot_hash="snap1",
            synthesized_at=1_700_000_000.0,
            synthesis_input_fingerprint="fp1",
        )
        assert h1.hypothesis_fingerprint == h2.hypothesis_fingerprint

    def test_fingerprint_ignores_timestamps(self):
        """Different synthesized_at must not affect fingerprint."""
        h1 = FeatureHypothesis(
            hypothesis_id=str(uuid.uuid4()),
            description="Add RoadmapSensor",
            evidence_fragments=("spec:design",),
            gap_type="missing_capability",
            confidence=0.9,
            confidence_rule_id="r1",
            urgency="high",
            suggested_scope="new-agent",
            suggested_repos=("jarvis",),
            provenance="deterministic",
            synthesized_for_snapshot_hash="snap1",
            synthesized_at=1_700_000_000.0,
            synthesis_input_fingerprint="fp1",
        )
        h2 = FeatureHypothesis(
            hypothesis_id=str(uuid.uuid4()),
            description="Add RoadmapSensor",
            evidence_fragments=("spec:design",),
            gap_type="missing_capability",
            confidence=0.9,
            confidence_rule_id="r1",
            urgency="high",
            suggested_scope="new-agent",
            suggested_repos=("jarvis",),
            provenance="deterministic",
            synthesized_for_snapshot_hash="snap1",
            synthesized_at=1_799_999_999.0,  # different timestamp
            synthesis_input_fingerprint="fp1",
        )
        assert h1.hypothesis_fingerprint == h2.hypothesis_fingerprint

    def test_fingerprint_function_matches_property(self):
        h = _hyp()
        expected = compute_hypothesis_fingerprint(
            h.description, h.evidence_fragments, h.gap_type
        )
        assert h.hypothesis_fingerprint == expected

    def test_fingerprint_length_is_32(self):
        fp = compute_hypothesis_fingerprint("desc", ("src:a",), "missing_capability")
        assert len(fp) == 32

    def test_fingerprint_is_hex(self):
        fp = compute_hypothesis_fingerprint("desc", ("src:a",), "missing_capability")
        int(fp, 16)  # raises ValueError if not valid hex


# ---------------------------------------------------------------------------
# FeatureHypothesis validation
# ---------------------------------------------------------------------------

class TestFeatureHypothesisValidation:
    def test_default_status_is_active(self):
        h = _hyp()
        assert h.status == "active"

    def test_invalid_gap_type_raises(self):
        with pytest.raises(ValueError, match="gap_type"):
            _hyp(gap_type="totally_invalid")

    def test_invalid_provenance_raises(self):
        with pytest.raises(ValueError, match="provenance"):
            _hyp(provenance="random_tool")

    def test_model_provenance_accepted(self):
        h = _hyp(provenance="model:claude")
        assert h.provenance == "model:claude"

    def test_doubleword_provenance_accepted(self):
        h = _hyp(provenance="model:doubleword-397b")
        assert h.provenance == "model:doubleword-397b"

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            _hyp(confidence=1.5)

    def test_confidence_zero_accepted(self):
        h = _hyp(confidence=0.0)
        assert h.confidence == 0.0

    def test_confidence_one_accepted(self):
        h = _hyp(confidence=1.0)
        assert h.confidence == 1.0


# ---------------------------------------------------------------------------
# FeatureHypothesis.is_stale
# ---------------------------------------------------------------------------

class TestIsStale:
    def test_is_stale_hash_mismatch(self):
        h = _hyp(
            synthesized_for_snapshot_hash="old_hash",
            synthesized_at=time.time(),  # fresh
        )
        assert h.is_stale("new_hash", ttl_s=3600) is True

    def test_is_stale_age_exceeded(self):
        h = _hyp(
            synthesized_for_snapshot_hash="current_hash",
            synthesized_at=time.time() - 7200,  # 2 hours ago
        )
        assert h.is_stale("current_hash", ttl_s=3600) is True

    def test_not_stale_when_fresh_and_matching(self):
        h = _hyp(
            synthesized_for_snapshot_hash="current_hash",
            synthesized_at=time.time(),  # just now
        )
        assert h.is_stale("current_hash", ttl_s=3600) is False

    def test_stale_when_both_hash_mismatch_and_expired(self):
        h = _hyp(
            synthesized_for_snapshot_hash="old_hash",
            synthesized_at=time.time() - 9999,
        )
        assert h.is_stale("new_hash", ttl_s=3600) is True

    def test_new_factory_populates_hypothesis_id(self):
        h = FeatureHypothesis.new(
            description="Wire sensor",
            evidence_fragments=("spec:a",),
            gap_type="incomplete_wiring",
            confidence=0.75,
            confidence_rule_id="r2",
            urgency="medium",
            suggested_scope="wire-existing",
            suggested_repos=("jarvis",),
            provenance="deterministic",
            synthesized_for_snapshot_hash="hash1",
            synthesis_input_fingerprint="fp2",
        )
        # Must be a valid UUID4
        parsed = uuid.UUID(h.hypothesis_id)
        assert parsed.version == 4
