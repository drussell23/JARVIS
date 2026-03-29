"""Tests for ExplorationEnvelopeFactory (TDD first).

All tests exercise only pure-Python logic: no I/O, no model calls, no
randomness.  Every assertion is deterministic given controlled inputs.
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.finding_ranker import RankedFinding
from backend.core.ouroboros.exploration_envelope_factory import findings_to_envelopes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = time.time()
_RECENT = _NOW - 1  # 1 second ago — practically fresh


def _make_finding(
    *,
    description: str = "sample finding",
    category: str = "dead_code",
    file_path: str = "backend/foo.py",
    blast_radius: float = 0.5,
    confidence: float = 0.75,
    urgency: str = "normal",
    last_modified: float | None = None,
    repo: str = "jarvis",
    source_check: str = "check_dead_code",
) -> RankedFinding:
    """Factory that fills all required fields with sensible defaults."""
    return RankedFinding(
        description=description,
        category=category,
        file_path=file_path,
        blast_radius=blast_radius,
        confidence=confidence,
        urgency=urgency,
        last_modified=last_modified if last_modified is not None else _RECENT,
        repo=repo,
        source_check=source_check,
    )


# ---------------------------------------------------------------------------
# test_creates_envelope_per_finding
# ---------------------------------------------------------------------------


class TestCreatesEnvelopePerFinding:
    def test_creates_envelope_per_finding(self):
        """2 findings → 2 envelopes, one-to-one correspondence."""
        findings = [
            _make_finding(file_path="backend/a.py"),
            _make_finding(file_path="backend/b.py"),
        ]
        envelopes = findings_to_envelopes(findings, epoch_id=1)
        assert len(envelopes) == 2

    def test_empty_findings_returns_empty_list(self):
        """0 findings → 0 envelopes."""
        envelopes = findings_to_envelopes([], epoch_id=0)
        assert envelopes == []

    def test_single_finding_returns_single_envelope(self):
        """1 finding → 1 envelope."""
        envelopes = findings_to_envelopes([_make_finding()], epoch_id=42)
        assert len(envelopes) == 1


# ---------------------------------------------------------------------------
# test_envelope_source_is_exploration
# ---------------------------------------------------------------------------


class TestEnvelopeSourceIsExploration:
    def test_envelope_source_is_exploration(self):
        """Every envelope must carry source='exploration'."""
        findings = [_make_finding(), _make_finding(file_path="backend/bar.py", category="perf")]
        envelopes = findings_to_envelopes(findings, epoch_id=5)
        for env in envelopes:
            assert env.source == "exploration"


# ---------------------------------------------------------------------------
# test_envelope_carries_epoch_id
# ---------------------------------------------------------------------------


class TestEnvelopeCarriesEpochId:
    def test_envelope_carries_epoch_id(self):
        """evidence['epoch_id'] must equal the epoch_id passed to the factory."""
        epoch = 99
        envelopes = findings_to_envelopes([_make_finding()], epoch_id=epoch)
        assert envelopes[0].evidence["epoch_id"] == epoch

    def test_epoch_id_zero_is_valid(self):
        """epoch_id=0 is a valid value and must be preserved."""
        envelopes = findings_to_envelopes([_make_finding()], epoch_id=0)
        assert envelopes[0].evidence["epoch_id"] == 0

    def test_multiple_findings_share_same_epoch_id(self):
        """All envelopes from one batch carry the same epoch_id."""
        epoch = 7
        findings = [_make_finding(file_path=f"backend/{i}.py") for i in range(3)]
        envelopes = findings_to_envelopes(findings, epoch_id=epoch)
        assert all(e.evidence["epoch_id"] == epoch for e in envelopes)


# ---------------------------------------------------------------------------
# test_envelope_target_files_from_finding
# ---------------------------------------------------------------------------


class TestEnvelopeTargetFilesFromFinding:
    def test_envelope_target_files_from_finding(self):
        """target_files tuple must contain exactly finding.file_path."""
        path = "backend/neural_mesh/core.py"
        finding = _make_finding(file_path=path)
        envelopes = findings_to_envelopes([finding], epoch_id=1)
        assert envelopes[0].target_files == (path,)

    def test_target_files_is_tuple(self):
        """target_files must be a tuple, not a list or other iterable."""
        finding = _make_finding()
        envelopes = findings_to_envelopes([finding], epoch_id=1)
        assert isinstance(envelopes[0].target_files, tuple)

    def test_target_files_length_is_one(self):
        """Each finding maps to exactly one target file."""
        finding = _make_finding()
        envelopes = findings_to_envelopes([finding], epoch_id=1)
        assert len(envelopes[0].target_files) == 1


# ---------------------------------------------------------------------------
# test_envelope_urgency_from_finding
# ---------------------------------------------------------------------------


class TestEnvelopeUrgencyFromFinding:
    def test_envelope_urgency_from_finding(self):
        """urgency on the envelope must match finding.urgency."""
        for urgency in ("critical", "high", "normal", "low"):
            finding = _make_finding(urgency=urgency)
            envelopes = findings_to_envelopes([finding], epoch_id=1)
            assert envelopes[0].urgency == urgency

    def test_urgency_preserved_across_multiple_findings(self):
        """Each envelope independently reflects its finding's urgency."""
        findings = [
            _make_finding(file_path="a.py", urgency="critical"),
            _make_finding(file_path="b.py", urgency="low"),
        ]
        envelopes = findings_to_envelopes(findings, epoch_id=1)
        assert envelopes[0].urgency == "critical"
        assert envelopes[1].urgency == "low"


# ---------------------------------------------------------------------------
# test_envelope_requires_no_human_ack
# ---------------------------------------------------------------------------


class TestEnvelopeRequiresNoHumanAck:
    def test_envelope_requires_no_human_ack(self):
        """requires_human_ack must always be False (GOVERNED tier, fully autonomous)."""
        envelopes = findings_to_envelopes([_make_finding()], epoch_id=1)
        assert envelopes[0].requires_human_ack is False

    def test_requires_human_ack_false_for_all_urgencies(self):
        """GOVERNED tier: no urgency level triggers human ack."""
        findings = [_make_finding(urgency=u) for u in ("critical", "high", "normal", "low")]
        envelopes = findings_to_envelopes(findings, epoch_id=1)
        assert all(not e.requires_human_ack for e in envelopes)


# ---------------------------------------------------------------------------
# test_envelope_repo_from_finding
# ---------------------------------------------------------------------------


class TestEnvelopeRepoFromFinding:
    def test_envelope_repo_from_finding(self):
        """repo on the envelope must match finding.repo."""
        for repo in ("jarvis", "jarvis-prime", "reactor"):
            finding = _make_finding(repo=repo)
            envelopes = findings_to_envelopes([finding], epoch_id=1)
            assert envelopes[0].repo == repo

    def test_repo_preserved_across_multiple_findings(self):
        """Each envelope independently reflects its finding's repo."""
        findings = [
            _make_finding(file_path="a.py", repo="jarvis"),
            _make_finding(file_path="b.py", repo="reactor"),
        ]
        envelopes = findings_to_envelopes(findings, epoch_id=1)
        assert envelopes[0].repo == "jarvis"
        assert envelopes[1].repo == "reactor"


# ---------------------------------------------------------------------------
# test_envelope_description_includes_category
# ---------------------------------------------------------------------------


class TestEnvelopeDescriptionIncludesCategory:
    def test_envelope_description_starts_with_category_bracket(self):
        """description must start with '[category]'."""
        finding = _make_finding(category="dead_code", description="Unused helper")
        envelopes = findings_to_envelopes([finding], epoch_id=1)
        assert envelopes[0].description.startswith("[dead_code]")

    def test_envelope_description_includes_finding_description(self):
        """description must include the original finding description text."""
        finding = _make_finding(category="todo", description="Fix the retry logic")
        envelopes = findings_to_envelopes([finding], epoch_id=1)
        assert "Fix the retry logic" in envelopes[0].description

    def test_envelope_description_format(self):
        """description format must be '[category] description'."""
        finding = _make_finding(category="perf", description="Slow DB query")
        envelopes = findings_to_envelopes([finding], epoch_id=1)
        assert envelopes[0].description == "[perf] Slow DB query"

    def test_description_for_all_categories(self):
        """Format holds for every known category value."""
        categories = [
            "dead_code", "circular_dep", "complexity", "unwired",
            "test_gap", "todo", "doc_stale", "perf", "github_issue",
        ]
        for cat in categories:
            finding = _make_finding(category=cat, description="some issue")
            envelopes = findings_to_envelopes([finding], epoch_id=1)
            assert envelopes[0].description == f"[{cat}] some issue"


# ---------------------------------------------------------------------------
# test_evidence_carries_finding_metadata
# ---------------------------------------------------------------------------


class TestEvidenceCarriesFindingMetadata:
    """Verify that all finding metadata fields land in evidence correctly."""

    def test_evidence_category(self):
        finding = _make_finding(category="complexity")
        env = findings_to_envelopes([finding], epoch_id=1)[0]
        assert env.evidence["category"] == "complexity"

    def test_evidence_blast_radius(self):
        finding = _make_finding(blast_radius=0.8)
        env = findings_to_envelopes([finding], epoch_id=1)[0]
        assert env.evidence["blast_radius"] == pytest.approx(0.8)

    def test_evidence_score(self):
        finding = _make_finding()
        env = findings_to_envelopes([finding], epoch_id=1)[0]
        assert env.evidence["score"] == pytest.approx(finding.score)

    def test_evidence_source_check(self):
        finding = _make_finding(source_check="check_complexity")
        env = findings_to_envelopes([finding], epoch_id=1)[0]
        assert env.evidence["source_check"] == "check_complexity"


# ---------------------------------------------------------------------------
# test_confidence_from_finding
# ---------------------------------------------------------------------------


class TestConfidenceFromFinding:
    def test_confidence_propagated(self):
        """envelope.confidence must equal finding.confidence."""
        finding = _make_finding(confidence=0.93)
        env = findings_to_envelopes([finding], epoch_id=1)[0]
        assert env.confidence == pytest.approx(0.93)


# ---------------------------------------------------------------------------
# test_ordering_preserved
# ---------------------------------------------------------------------------


class TestOrderingPreserved:
    def test_ordering_matches_input_order(self):
        """Envelopes are yielded in the same order as the input findings list."""
        paths = ["backend/alpha.py", "backend/beta.py", "backend/gamma.py"]
        findings = [_make_finding(file_path=p) for p in paths]
        envelopes = findings_to_envelopes(findings, epoch_id=1)
        result_paths = [e.target_files[0] for e in envelopes]
        assert result_paths == paths
