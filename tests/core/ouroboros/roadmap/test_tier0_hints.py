"""Tests for Tier 0 deterministic gap hints."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.tier0_hints import generate_tier0_hints


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _frag(
    source_id: str = "spec:test",
    content_hash: str = "abc123",
    tier: int = 0,
    fragment_type: str = "spec",
    summary: str = "",
) -> SnapshotFragment:
    return SnapshotFragment(
        source_id=source_id,
        uri=f"docs/{source_id}.md",
        tier=tier,
        content_hash=content_hash,
        fetched_at=1_700_000_000.0,
        mtime=1_699_999_000.0,
        title="Test Fragment",
        summary=summary,
        fragment_type=fragment_type,
    )


def _snapshot(*frags: SnapshotFragment) -> RoadmapSnapshot:
    return RoadmapSnapshot.create(frags)


def _oracle_with_miss(*missing_names: str):
    """Return an oracle mock that returns empty list for names in missing_names."""
    oracle = MagicMock()
    oracle.find_nodes_by_name = MagicMock(
        side_effect=lambda name, fuzzy=False: (
            [] if name.lower() in {n.lower() for n in missing_names} else [MagicMock()]
        )
    )
    return oracle


def _oracle_always_empty():
    oracle = MagicMock()
    oracle.find_nodes_by_name = MagicMock(return_value=[])
    return oracle


def _oracle_always_found():
    oracle = MagicMock()
    oracle.find_nodes_by_name = MagicMock(return_value=[MagicMock()])
    return oracle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDetectsMissingAgent:
    def test_detects_missing_agent(self):
        """Spec mentions 'WhatsApp agent', oracle has no match → hint emitted."""
        frag = _frag(
            source_id="spec:integrations",
            summary="We need a WhatsApp agent to send messages.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_with_miss("whatsapp")

        hints = generate_tier0_hints(snapshot, oracle)

        assert len(hints) >= 1
        descriptions = [h.description for h in hints]
        assert any("whatsapp" in d.lower() for d in descriptions)

    def test_detects_missing_sensor(self):
        """Spec mentions a sensor that oracle cannot find → hint emitted."""
        frag = _frag(
            source_id="plan:monitoring",
            summary="Add a heartbeat sensor to watch service health.",
            fragment_type="plan",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_with_miss("heartbeat")

        hints = generate_tier0_hints(snapshot, oracle)

        assert any("heartbeat" in h.description.lower() for h in hints)

    def test_detects_missing_integration(self):
        """Plan mentions an integration that oracle cannot find → hint emitted."""
        frag = _frag(
            source_id="backlog:integrations",
            summary="Build a Slack integration for notifications.",
            fragment_type="backlog",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_with_miss("slack")

        hints = generate_tier0_hints(snapshot, oracle)

        assert any("slack" in h.description.lower() for h in hints)

    def test_detects_missing_provider(self):
        """Spec mentions a provider that oracle cannot find → hint emitted."""
        frag = _frag(
            source_id="spec:llm",
            summary="Wire a Mistral provider into the routing chain.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_with_miss("mistral")

        hints = generate_tier0_hints(snapshot, oracle)

        assert any("mistral" in h.description.lower() for h in hints)


class TestNoHintWhenSymbolExists:
    def test_no_hint_when_symbol_exists(self):
        """Oracle returns a match for the capability → no hint emitted."""
        frag = _frag(
            source_id="spec:orchestration",
            summary="The orchestration agent handles routing decisions.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_found()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints == []

    def test_no_hint_for_found_sensor(self):
        """Sensor referenced in spec is already in oracle → no hint."""
        frag = _frag(
            source_id="spec:health",
            summary="RuntimeHealthSensor integration is complete.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_found()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints == []


class TestDeterministicProvenance:
    def test_hints_have_deterministic_provenance(self):
        """Every emitted hint must have provenance == 'deterministic'."""
        frag = _frag(
            source_id="spec:gaps",
            summary="Need a GitHub agent, a Jira integration, and a billing provider.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints, "Expected at least one hint"
        for h in hints:
            assert h.provenance == "deterministic"

    def test_hints_have_correct_confidence_rule_id(self):
        """All hints from tier0 hints must carry the 'spec_symbol_miss' rule."""
        frag = _frag(
            source_id="spec:features",
            summary="Add a Twilio provider for SMS delivery.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints
        for h in hints:
            assert h.confidence_rule_id == "spec_symbol_miss"

    def test_hints_have_correct_confidence(self):
        """All hints must have confidence == 0.85."""
        frag = _frag(
            source_id="plan:gaps",
            summary="We need a calendar integration here.",
            fragment_type="plan",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints
        for h in hints:
            assert h.confidence == pytest.approx(0.85)

    def test_hints_have_missing_capability_gap_type(self):
        """All tier0 deterministic hints must use gap_type 'missing_capability'."""
        frag = _frag(
            source_id="spec:gaps",
            summary="Add a Discord sensor to the pipeline.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints
        for h in hints:
            assert h.gap_type == "missing_capability"


class TestReturnsEmptyForNoGaps:
    def test_returns_empty_for_no_gaps(self):
        """General text with no capability keywords → no hints."""
        frag = _frag(
            source_id="spec:general",
            summary="The system is working well. No issues detected at this time.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_found()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints == []

    def test_ignores_non_p0_fragments(self):
        """Fragments at tier > 0 must NOT contribute capability references."""
        frag_tier1 = _frag(
            source_id="plan:future",
            summary="Add a Salesforce agent to the roadmap.",
            fragment_type="plan",
            tier=1,  # NOT tier 0
        )
        snapshot = _snapshot(frag_tier1)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints == []

    def test_ignores_memory_fragment_type(self):
        """Fragment type 'memory' at tier 0 must NOT contribute hints."""
        frag = _frag(
            source_id="memory:notes",
            summary="Remember to add a Pagerduty integration later.",
            fragment_type="memory",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints == []

    def test_ignores_commit_log_fragment_type(self):
        """Fragment type 'commit_log' at tier 0 must NOT contribute hints."""
        frag = _frag(
            source_id="git:recent",
            summary="Added Prometheus sensor integration to collector.",
            fragment_type="commit_log",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints == []


class TestHintsCarryEvidenceFragments:
    def test_hints_carry_evidence_fragments(self):
        """Each hint must cite the source_id of the originating fragment."""
        source_id = "spec:communications"
        frag = _frag(
            source_id=source_id,
            summary="Need a Telegram agent for outbound notifications.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints
        for h in hints:
            assert source_id in h.evidence_fragments

    def test_evidence_includes_correct_source_id_multi_fragment(self):
        """With multiple fragments, each hint cites its own originating source_id."""
        frag_a = _frag(
            source_id="spec:alpha",
            summary="Build a GitHub agent for PR automation.",
            fragment_type="spec",
            tier=0,
        )
        frag_b = _frag(
            source_id="spec:beta",
            summary="Build a Jira sensor for ticket tracking.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag_a, frag_b)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        github_hints = [h for h in hints if "github" in h.description.lower()]
        jira_hints = [h for h in hints if "jira" in h.description.lower()]

        assert github_hints, "Expected hint for github agent"
        assert jira_hints, "Expected hint for jira sensor"

        for h in github_hints:
            assert "spec:alpha" in h.evidence_fragments
        for h in jira_hints:
            assert "spec:beta" in h.evidence_fragments


class TestReturnsEmptyWhenOracleIsNone:
    def test_returns_empty_when_oracle_is_none(self):
        """When oracle is None, return empty list immediately."""
        frag = _frag(
            source_id="spec:features",
            summary="Need a WhatsApp agent and Stripe provider.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)

        hints = generate_tier0_hints(snapshot, None)

        assert hints == []


class TestSkipsCommonWords:
    def test_skips_common_words(self):
        """'the agent' should not produce a hint for 'the'."""
        frag = _frag(
            source_id="spec:description",
            summary="The agent processes the integration.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        # 'the' is a common word and should be filtered
        for h in hints:
            assert "the" not in h.description.lower().split()[0:2] or "the" not in [
                word.strip("'\".,") for word in h.description.lower().split()
                if word.strip("'\".,") == "the"
            ]
        # Specifically: no hint with cap_name == "the"
        cap_names_in_descriptions = []
        for h in hints:
            # description format is "Missing {cap_type}: {cap_name}"
            parts = h.description.split(":")
            if len(parts) >= 2:
                cap_names_in_descriptions.append(parts[-1].strip().lower())
        assert "the" not in cap_names_in_descriptions

    def test_skips_this_that_some_any_new(self):
        """Common English determiners/pronouns are filtered from capability names."""
        frag = _frag(
            source_id="spec:noise",
            summary="This agent needs some integration and any provider.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        common_words = {"this", "that", "some", "any", "new"}
        for h in hints:
            parts = h.description.split(":")
            if len(parts) >= 2:
                cap_name = parts[-1].strip().lower()
                assert cap_name not in common_words, (
                    f"Common word '{cap_name}' should have been filtered"
                )

    def test_skips_short_words(self):
        """Words shorter than 3 characters are skipped."""
        frag = _frag(
            source_id="spec:short",
            summary="A new AI agent and an ML provider.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        for h in hints:
            parts = h.description.split(":")
            if len(parts) >= 2:
                cap_name = parts[-1].strip().lower()
                assert len(cap_name) >= 3, (
                    f"Short cap name '{cap_name}' should have been filtered"
                )


class TestDeduplication:
    def test_deduplicates_same_cap_name_and_type(self):
        """The same (cap_name, cap_type) from the same fragment emits only one hint."""
        frag = _frag(
            source_id="spec:dup",
            summary="Need a GitHub agent. The GitHub agent will do X. Add GitHub agent.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        github_agent_hints = [
            h for h in hints if "github" in h.description.lower() and "agent" in h.description.lower()
        ]
        assert len(github_agent_hints) == 1

    def test_different_cap_types_are_separate(self):
        """'GitHub agent' and 'GitHub integration' are different cap types → separate hints."""
        frag = _frag(
            source_id="spec:multi",
            summary="Add GitHub agent and a GitHub integration.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        github_hints = [h for h in hints if "github" in h.description.lower()]
        assert len(github_hints) == 2


class TestSnapshotHashPassthrough:
    def test_hints_synthesized_for_snapshot_hash(self):
        """Each hint's synthesized_for_snapshot_hash must match the snapshot's content_hash."""
        frag = _frag(
            source_id="spec:hash-test",
            summary="Add a Notion agent for documentation.",
            fragment_type="spec",
            tier=0,
        )
        snapshot = _snapshot(frag)
        oracle = _oracle_always_empty()

        hints = generate_tier0_hints(snapshot, oracle)

        assert hints
        for h in hints:
            assert h.synthesized_for_snapshot_hash == snapshot.content_hash
