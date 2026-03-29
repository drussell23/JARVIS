"""Tests for hypothesis_envelope_factory.hypotheses_to_envelopes."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_envelope_factory import (
    hypotheses_to_envelopes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_hypothesis(
    *,
    description: str = "Missing retry logic in agent",
    gap_type: str = "missing_capability",
    confidence: float = 0.85,
    confidence_rule_id: str = "tier0-spec-vs-impl-diff",
    urgency: str = "high",
    suggested_scope: str = "backend/agents/retry.py",
    suggested_repos: tuple = ("jarvis",),
    provenance: str = "deterministic",
) -> FeatureHypothesis:
    return FeatureHypothesis.new(
        description=description,
        evidence_fragments=("src-001", "src-002"),
        gap_type=gap_type,
        confidence=confidence,
        confidence_rule_id=confidence_rule_id,
        urgency=urgency,
        suggested_scope=suggested_scope,
        suggested_repos=suggested_repos,
        provenance=provenance,
        synthesized_for_snapshot_hash="abc123",
        synthesis_input_fingerprint="fp-xyz",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHypothesesToEnvelopes:
    def test_creates_envelope_per_hypothesis(self):
        hypotheses = [_make_hypothesis(), _make_hypothesis(description="Other gap")]
        envelopes = hypotheses_to_envelopes(hypotheses, snapshot_version=7)
        assert len(envelopes) == 2

    def test_empty_list_returns_empty(self):
        assert hypotheses_to_envelopes([], snapshot_version=1) == []

    def test_envelope_source_is_roadmap(self):
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.source == "roadmap"

    def test_envelope_carries_analysis_complete(self):
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.evidence["analysis_complete"] is True

    def test_envelope_carries_hypothesis_id(self):
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.evidence["hypothesis_id"] == h.hypothesis_id

    def test_envelope_carries_provenance(self):
        h = _make_hypothesis(provenance="model:claude")
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.evidence["provenance"] == "model:claude"

    def test_envelope_requires_no_human_ack(self):
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.requires_human_ack is False

    def test_envelope_target_files_from_scope(self):
        h = _make_hypothesis(suggested_scope="backend/core/retry.py")
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.target_files == ("backend/core/retry.py",)

    def test_envelope_repo_from_hypothesis(self):
        h = _make_hypothesis(suggested_repos=("jarvis-prime",))
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.repo == "jarvis-prime"

    def test_envelope_repo_defaults_to_jarvis_when_no_repos(self):
        h = _make_hypothesis(suggested_repos=())
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.repo == "jarvis"

    def test_envelope_repo_uses_first_when_multiple(self):
        h = _make_hypothesis(suggested_repos=("reactor", "jarvis-prime"))
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.repo == "reactor"

    def test_envelope_description_includes_gap_type_and_description(self):
        h = _make_hypothesis(gap_type="incomplete_wiring", description="Wire the sensor")
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.description == "[incomplete_wiring] Wire the sensor"

    def test_envelope_carries_snapshot_version_in_evidence(self):
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=42)
        assert env.evidence["snapshot_version"] == 42

    def test_envelope_carries_gap_type_in_evidence(self):
        h = _make_hypothesis(gap_type="stale_implementation")
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.evidence["gap_type"] == "stale_implementation"

    def test_envelope_carries_confidence_rule_id_in_evidence(self):
        h = _make_hypothesis(confidence_rule_id="model:doubleword-397b:chain-of-thought")
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.evidence["confidence_rule_id"] == "model:doubleword-397b:chain-of-thought"

    def test_envelope_confidence_propagated(self):
        h = _make_hypothesis(confidence=0.72)
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.confidence == pytest.approx(0.72)

    def test_envelope_urgency_propagated(self):
        h = _make_hypothesis(urgency="critical")
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.urgency == "critical"

    def test_each_envelope_has_unique_signal_id(self):
        hypotheses = [_make_hypothesis(), _make_hypothesis(description="Other")]
        envelopes = hypotheses_to_envelopes(hypotheses, snapshot_version=1)
        signal_ids = [e.signal_id for e in envelopes]
        assert len(set(signal_ids)) == len(signal_ids)

    def test_envelope_lease_id_is_empty_string(self):
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.lease_id == ""

    def test_envelope_schema_version_is_current(self):
        from backend.core.ouroboros.governance.intake.intent_envelope import SCHEMA_VERSION
        h = _make_hypothesis()
        (env,) = hypotheses_to_envelopes([h], snapshot_version=1)
        assert env.schema_version == SCHEMA_VERSION
