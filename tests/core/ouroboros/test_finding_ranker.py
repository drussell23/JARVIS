"""Tests for FindingRanker — deterministic impact scoring (TDD first).

All tests exercise only pure-Python logic: no I/O, no model calls, no
randomness.  Every assertion is deterministic given controlled inputs.
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from backend.core.ouroboros.finding_ranker import (
    RANKING_VERSION,
    RankedFinding,
    impact_score,
    merge_and_rank,
    _URGENCY_WEIGHTS,
    _RECENCY_WINDOW_S,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = time.time()
_RECENT = _NOW - 1  # 1 second ago — practically fresh


def _make(
    *,
    description: str = "sample finding",
    category: str = "dead_code",
    file_path: str = "backend/foo.py",
    blast_radius: float = 0.5,
    confidence: float = 0.5,
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
# Metadata
# ---------------------------------------------------------------------------


class TestRankingVersion:
    def test_ranking_version(self):
        assert RANKING_VERSION == "1.0"


# ---------------------------------------------------------------------------
# impact_score
# ---------------------------------------------------------------------------


class TestImpactScore:
    def test_impact_score_maximum(self):
        """All maximally-good inputs should yield a score very close to 1.0."""
        score = impact_score(
            blast_radius=1.0,
            confidence=1.0,
            urgency="critical",
            last_modified=_NOW,  # brand new — recency ≈ 1.0
        )
        # 1.0*0.4 + 1.0*0.3 + 1.0*0.2 + ~1.0*0.1 ≈ 1.0
        assert score == pytest.approx(1.0, abs=0.01)

    def test_impact_score_minimum(self):
        """All minimally-good inputs should yield a score around 0.05."""
        ancient = _NOW - _RECENCY_WINDOW_S * 2  # far outside the window
        score = impact_score(
            blast_radius=0.0,
            confidence=0.0,
            urgency="low",
            last_modified=ancient,
        )
        # 0.0*0.4 + 0.0*0.3 + 0.25*0.2 + 0.0*0.1 = 0.05
        assert score == pytest.approx(0.05, abs=1e-9)

    def test_impact_score_weights(self):
        """blast_radius has the highest single weight (0.4).

        Moving blast_radius from 0→1 changes score by 0.4; moving
        confidence from 0→1 changes score by 0.3; verifying blast_radius
        dominates.
        """
        base = impact_score(0.0, 0.0, "normal", _RECENT)
        high_blast = impact_score(1.0, 0.0, "normal", _RECENT)
        high_conf = impact_score(0.0, 1.0, "normal", _RECENT)

        blast_delta = high_blast - base
        conf_delta = high_conf - base

        assert blast_delta == pytest.approx(0.4, abs=1e-9)
        assert conf_delta == pytest.approx(0.3, abs=1e-9)
        assert blast_delta > conf_delta

    def test_urgency_mapping(self):
        """critical > high > normal > low, all else equal."""
        def _s(urgency: str) -> float:
            return impact_score(0.5, 0.5, urgency, _RECENT)

        assert _s("critical") > _s("high") > _s("normal") > _s("low")

    def test_urgency_weights_values(self):
        """Spot-check the weight constants themselves."""
        assert _URGENCY_WEIGHTS["critical"] == 1.0
        assert _URGENCY_WEIGHTS["high"] == 0.75
        assert _URGENCY_WEIGHTS["normal"] == 0.5
        assert _URGENCY_WEIGHTS["low"] == 0.25

    def test_recency_clamps_at_zero_for_ancient(self):
        """recency must not go negative for files older than the window."""
        ancient = _NOW - _RECENCY_WINDOW_S * 10
        score = impact_score(0.5, 0.5, "normal", ancient)
        # recency == 0.0, so contribution is exactly 0.0
        score_no_recency = impact_score(0.5, 0.5, "normal", _NOW - _RECENCY_WINDOW_S)
        # both should be >= 0
        assert score >= 0.0
        assert score_no_recency >= 0.0

    def test_unknown_urgency_defaults_to_0_5(self):
        """An unrecognised urgency string should default to weight 0.5."""
        score_unknown = impact_score(0.5, 0.5, "mystery", _RECENT)
        score_normal = impact_score(0.5, 0.5, "normal", _RECENT)
        assert score_unknown == pytest.approx(score_normal, abs=1e-9)


# ---------------------------------------------------------------------------
# RankedFinding dataclass
# ---------------------------------------------------------------------------


class TestRankedFinding:
    def test_score_computed_on_init(self):
        """score field is populated by __post_init__, not passed in."""
        f = _make(blast_radius=1.0, confidence=1.0, urgency="critical")
        assert f.score > 0.0

    def test_score_not_constructor_param(self):
        """Passing score= explicitly should raise TypeError."""
        with pytest.raises(TypeError):
            RankedFinding(  # type: ignore[call-arg]
                description="x",
                category="dead_code",
                file_path="foo.py",
                blast_radius=0.5,
                confidence=0.5,
                urgency="normal",
                last_modified=_RECENT,
                repo="jarvis",
                score=0.99,
            )

    def test_source_check_default_empty(self):
        f = _make()
        assert f.source_check == "check_dead_code"

    def test_all_category_values_accepted(self):
        categories = [
            "dead_code", "circular_dep", "complexity", "unwired",
            "test_gap", "todo", "doc_stale", "perf", "github_issue",
        ]
        for cat in categories:
            f = _make(category=cat)
            assert f.category == cat

    def test_all_repo_values_accepted(self):
        for repo in ("jarvis", "jarvis-prime", "reactor"):
            f = _make(repo=repo)
            assert f.repo == repo


# ---------------------------------------------------------------------------
# merge_and_rank
# ---------------------------------------------------------------------------


class TestMergeAndRank:
    def test_merge_and_rank_sorts_descending(self):
        """Higher-scored findings must come first."""
        low = _make(blast_radius=0.1, confidence=0.1, urgency="low", file_path="a.py")
        high = _make(blast_radius=0.9, confidence=0.9, urgency="critical", file_path="b.py")
        mid = _make(blast_radius=0.5, confidence=0.5, urgency="normal", file_path="c.py")

        result = merge_and_rank([low, high, mid])

        assert result[0].file_path == "b.py"
        assert result[1].file_path == "c.py"
        assert result[2].file_path == "a.py"

    def test_merge_and_rank_tiebreaker_alphabetical(self):
        """Same score → alphabetical file_path ascending (a < b < c).

        Use a future timestamp so age_s clamps to 0.0 and recency is
        exactly 1.0 regardless of sub-second drift between constructions.
        """
        # A timestamp slightly in the future → age_s=0 → recency=1.0 exactly
        future = _NOW + 60
        params = dict(blast_radius=0.5, confidence=0.5, urgency="normal", last_modified=future)
        fa = _make(file_path="alpha/z.py", category="dead_code", **params)
        fb = _make(file_path="beta/a.py", category="dead_code", **params)
        fc = _make(file_path="alpha/a.py", category="dead_code", **params)

        result = merge_and_rank([fb, fa, fc])

        paths = [r.file_path for r in result]
        assert paths == sorted(paths)

    def test_merge_and_rank_deduplicates_same_file_category(self):
        """Same (file_path, category) keeps only the higher-scored entry."""
        low = _make(
            file_path="backend/foo.py",
            category="complexity",
            blast_radius=0.2,
            confidence=0.2,
            urgency="low",
        )
        high = _make(
            file_path="backend/foo.py",
            category="complexity",
            blast_radius=0.9,
            confidence=0.9,
            urgency="critical",
        )
        result = merge_and_rank([low, high])

        assert len(result) == 1
        assert result[0].score == high.score

    def test_merge_and_rank_different_category_same_file_not_deduped(self):
        """Same file but different category must both survive deduplication."""
        f1 = _make(file_path="backend/foo.py", category="dead_code")
        f2 = _make(file_path="backend/foo.py", category="complexity")

        result = merge_and_rank([f1, f2])

        assert len(result) == 2

    def test_merge_and_rank_empty_input(self):
        assert merge_and_rank([]) == []

    def test_merge_and_rank_single_item(self):
        f = _make()
        result = merge_and_rank([f])
        assert len(result) == 1
        assert result[0] is f

    def test_merge_and_rank_returns_new_list(self):
        """Must not mutate the input list."""
        findings = [_make(file_path="a.py"), _make(file_path="b.py")]
        original_order = [f.file_path for f in findings]
        merge_and_rank(findings)
        assert [f.file_path for f in findings] == original_order

    def test_merge_and_rank_dedup_preserves_higher_score_regardless_of_order(self):
        """Deduplication winner is score-based, not insertion-order-based."""
        high = _make(
            file_path="x.py",
            category="perf",
            blast_radius=0.9,
            confidence=0.9,
            urgency="critical",
        )
        low = _make(
            file_path="x.py",
            category="perf",
            blast_radius=0.1,
            confidence=0.1,
            urgency="low",
        )
        # High first
        r1 = merge_and_rank([high, low])
        # Low first
        r2 = merge_and_rank([low, high])

        assert r1[0].score == high.score
        assert r2[0].score == high.score
