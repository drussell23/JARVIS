"""Tests that "exploration" is a valid IntentEnvelope source with priority 4.

Covers:
- IntentEnvelope accepts source="exploration" without raising
- IntentEnvelope still rejects unknown sources
- _PRIORITY_MAP assigns priority 4 to "exploration"
- Existing sources retain their expected priority values
- Priority ordering is total and consistent (exploration < capability_gap < runtime_health)
"""

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
    EnvelopeValidationError,
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    _PRIORITY_MAP,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_exploration_envelope(**overrides) -> IntentEnvelope:
    """Return a minimal valid exploration envelope."""
    kwargs = dict(
        source="exploration",
        description="Discovered unused import in utils module",
        target_files=("backend/core/utils.py",),
        repo="jarvis",
        confidence=0.85,
        urgency="low",
        evidence={"finding_type": "unused_import", "line": 42},
        requires_human_ack=False,
    )
    kwargs.update(overrides)
    return make_envelope(**kwargs)


# ---------------------------------------------------------------------------
# _VALID_SOURCES membership
# ---------------------------------------------------------------------------


class TestValidSources:
    def test_exploration_in_valid_sources(self) -> None:
        assert "exploration" in _VALID_SOURCES

    def test_all_existing_sources_still_present(self) -> None:
        expected = {"backlog", "test_failure", "voice_human", "ai_miner", "capability_gap", "runtime_health"}
        assert expected.issubset(_VALID_SOURCES)

    def test_unknown_source_raises(self) -> None:
        with pytest.raises(EnvelopeValidationError, match="source must be one of"):
            make_envelope(
                source="phantom_source",
                description="Should fail",
                target_files=("some/file.py",),
                repo="jarvis",
                confidence=0.5,
                urgency="normal",
                evidence={},
                requires_human_ack=False,
            )


# ---------------------------------------------------------------------------
# IntentEnvelope construction with source="exploration"
# ---------------------------------------------------------------------------


class TestExplorationEnvelopeConstruction:
    def test_exploration_envelope_creates_without_error(self) -> None:
        env = _make_exploration_envelope()
        assert env.source == "exploration"

    def test_exploration_envelope_schema_version_correct(self) -> None:
        env = _make_exploration_envelope()
        assert env.schema_version == "2c.1"

    def test_exploration_envelope_confidence_preserved(self) -> None:
        env = _make_exploration_envelope(confidence=0.72)
        assert env.confidence == pytest.approx(0.72)

    def test_exploration_envelope_target_files_preserved(self) -> None:
        env = _make_exploration_envelope(
            target_files=("backend/core/utils.py", "backend/core/helpers.py")
        )
        assert "backend/core/utils.py" in env.target_files
        assert "backend/core/helpers.py" in env.target_files

    def test_exploration_envelope_urgency_low_allowed(self) -> None:
        env = _make_exploration_envelope(urgency="low")
        assert env.urgency == "low"

    def test_exploration_envelope_urgency_normal_allowed(self) -> None:
        env = _make_exploration_envelope(urgency="normal")
        assert env.urgency == "normal"

    def test_exploration_envelope_empty_evidence_allowed(self) -> None:
        env = _make_exploration_envelope(evidence={})
        assert env.evidence == {}

    def test_exploration_envelope_round_trips_to_dict(self) -> None:
        env = _make_exploration_envelope()
        d = env.to_dict()
        env2 = IntentEnvelope.from_dict(d)
        assert env2.source == "exploration"
        assert env2.target_files == env.target_files

    def test_exploration_envelope_with_lease(self) -> None:
        env = _make_exploration_envelope()
        leased = env.with_lease("lse-test-001")
        assert leased.lease_id == "lse-test-001"
        assert leased.source == "exploration"


# ---------------------------------------------------------------------------
# Priority map
# ---------------------------------------------------------------------------


class TestExplorationPriority:
    def test_exploration_has_priority_4(self) -> None:
        assert _PRIORITY_MAP["exploration"] == 4

    def test_voice_human_has_priority_0(self) -> None:
        assert _PRIORITY_MAP["voice_human"] == 0

    def test_test_failure_has_priority_1(self) -> None:
        assert _PRIORITY_MAP["test_failure"] == 1

    def test_backlog_has_priority_2(self) -> None:
        assert _PRIORITY_MAP["backlog"] == 2

    def test_ai_miner_has_priority_3(self) -> None:
        assert _PRIORITY_MAP["ai_miner"] == 3

    def test_capability_gap_has_priority_5(self) -> None:
        assert _PRIORITY_MAP["capability_gap"] == 5

    def test_runtime_health_has_priority_6(self) -> None:
        assert _PRIORITY_MAP["runtime_health"] == 6

    def test_exploration_lower_priority_than_ai_miner(self) -> None:
        """exploration (4) must be lower priority (higher int) than ai_miner (3)."""
        assert _PRIORITY_MAP["exploration"] > _PRIORITY_MAP["ai_miner"]

    def test_exploration_higher_priority_than_capability_gap(self) -> None:
        """exploration (4) must be higher priority (lower int) than capability_gap (5)."""
        assert _PRIORITY_MAP["exploration"] < _PRIORITY_MAP["capability_gap"]

    def test_exploration_higher_priority_than_runtime_health(self) -> None:
        """exploration (4) must be higher priority (lower int) than runtime_health (6)."""
        assert _PRIORITY_MAP["exploration"] < _PRIORITY_MAP["runtime_health"]

    def test_all_priority_values_are_unique(self) -> None:
        values = list(_PRIORITY_MAP.values())
        assert len(values) == len(set(values)), "Each source must have a unique priority"

    def test_priority_map_contains_all_sources(self) -> None:
        """Every valid source must have an entry in _PRIORITY_MAP."""
        # voice_human is in both; verify exploration is covered
        assert "exploration" in _PRIORITY_MAP
        for source in ("voice_human", "test_failure", "backlog", "ai_miner",
                       "exploration", "capability_gap", "runtime_health"):
            assert source in _PRIORITY_MAP, f"{source!r} missing from _PRIORITY_MAP"
