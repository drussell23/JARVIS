"""Tests for the beefed-up GoalTracker (Week 2 Perceived Intelligence Sprint).

Covers:
* GoalStatus enum parsing
* ActiveGoal dataclass defaults + lifecycle helpers
* CRUD + capacity management (drops inactive-first when full)
* Relevance scoring (_tokenize, _staleness_multiplier, _score_goal)
* find_relevant (top-N, min-relevance filter, sort order)
* alignment_boost (v1 API preserved)
* format_for_prompt (scoped vs unscoped)
* extract_keywords (stopword filter, dedup, min-len)
* slugify edge cases
* Persistence round-trip + v1→v2 schema migration
* Env-driven configuration
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.strategic_direction import (
    ActiveGoal,
    GoalMigrationReport,
    GoalStatus,
    GoalTracker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_goal(
    gid: str = "test-goal",
    description: str = "Improve test coverage in governance",
    keywords=("test", "coverage"),
    path_patterns=("backend/core/ouroboros/governance/",),
    tags=(),
    priority_weight: float = 1.0,
    status: GoalStatus = GoalStatus.ACTIVE,
    created_at: Optional[float] = None,
) -> ActiveGoal:
    return ActiveGoal(
        goal_id=gid,
        description=description,
        keywords=tuple(keywords),
        path_patterns=tuple(path_patterns),
        tags=tuple(tags),
        priority_weight=priority_weight,
        status=status,
        created_at=created_at if created_at is not None else time.time(),
    )


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    """Isolated project root — each test gets a clean .jarvis dir."""
    return tmp_path


@pytest.fixture
def tracker(tmp_root: Path) -> GoalTracker:
    return GoalTracker(tmp_root)


# ---------------------------------------------------------------------------
# GoalStatus
# ---------------------------------------------------------------------------


class TestGoalStatus:
    def test_values(self):
        assert GoalStatus.ACTIVE.value == "active"
        assert GoalStatus.PAUSED.value == "paused"
        assert GoalStatus.COMPLETED.value == "completed"

    @pytest.mark.parametrize("raw,expected", [
        ("active", GoalStatus.ACTIVE),
        ("ACTIVE", GoalStatus.ACTIVE),
        ("  paused  ", GoalStatus.PAUSED),
        ("Completed", GoalStatus.COMPLETED),
    ])
    def test_from_str_valid(self, raw, expected):
        assert GoalStatus.from_str(raw) is expected

    @pytest.mark.parametrize("raw", ["", "garbage", "unknown", None])
    def test_from_str_invalid_defaults_active(self, raw):
        assert GoalStatus.from_str(raw) is GoalStatus.ACTIVE


# ---------------------------------------------------------------------------
# ActiveGoal dataclass
# ---------------------------------------------------------------------------


class TestActiveGoal:
    def test_defaults(self):
        g = ActiveGoal(
            goal_id="g1",
            description="desc",
            keywords=("a",),
        )
        assert g.path_patterns == ()
        assert g.tags == ()
        assert g.priority_weight == 1.0
        assert g.status is GoalStatus.ACTIVE
        assert g.due_at is None
        assert g.created_at > 0
        assert g.updated_at > 0
        assert g.is_active is True

    def test_is_active_false_when_paused(self):
        g = _mk_goal(status=GoalStatus.PAUSED)
        assert g.is_active is False

    def test_is_active_false_when_completed(self):
        g = _mk_goal(status=GoalStatus.COMPLETED)
        assert g.is_active is False

    def test_touch_bumps_updated_at(self):
        g = _mk_goal()
        old = g.updated_at
        time.sleep(0.01)
        g.touch()
        assert g.updated_at > old


# ---------------------------------------------------------------------------
# CRUD + capacity
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_empty_on_init(self, tracker: GoalTracker):
        assert tracker.all_goals == []
        assert tracker.active_goals == []

    def test_add_goal_persists(self, tracker: GoalTracker, tmp_root: Path):
        tracker.add_goal(_mk_goal("g1"))
        assert len(tracker.active_goals) == 1
        assert (tmp_root / ".jarvis" / "active_goals.json").exists()

    def test_add_goal_dedupes_by_id(self, tracker: GoalTracker):
        g1 = _mk_goal("same", description="v1")
        tracker.add_goal(g1)
        first = tracker.get("same")
        assert first is not None
        original_created = first.created_at

        time.sleep(0.01)
        g2 = _mk_goal("same", description="v2")
        tracker.add_goal(g2)

        assert len(tracker.all_goals) == 1
        found = tracker.get("same")
        assert found is not None
        assert found.description == "v2"
        # Upsert preserves created_at, bumps updated_at.
        assert found.created_at == original_created
        assert found.updated_at >= original_created

    def test_capacity_drops_inactive_first(self, tracker: GoalTracker):
        with patch(
            "backend.core.ouroboros.governance.strategic_direction._MAX_GOALS", 3
        ):
            tracker.add_goal(_mk_goal("paused1", status=GoalStatus.PAUSED))
            tracker.add_goal(_mk_goal("active1"))
            tracker.add_goal(_mk_goal("active2"))
            tracker.add_goal(_mk_goal("active3"))  # triggers drop

            ids = [g.goal_id for g in tracker.all_goals]
            assert "paused1" not in ids  # inactive dropped first
            assert "active1" in ids
            assert "active3" in ids

    def test_capacity_drops_oldest_active_when_no_inactive(
        self, tracker: GoalTracker
    ):
        with patch(
            "backend.core.ouroboros.governance.strategic_direction._MAX_GOALS", 2
        ):
            tracker.add_goal(_mk_goal("g1"))
            tracker.add_goal(_mk_goal("g2"))
            tracker.add_goal(_mk_goal("g3"))

            ids = [g.goal_id for g in tracker.all_goals]
            assert "g1" not in ids
            assert "g2" in ids and "g3" in ids

    def test_remove_goal_returns_true_when_found(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1"))
        assert tracker.remove_goal("g1") is True
        assert tracker.all_goals == []

    def test_remove_goal_returns_false_when_missing(self, tracker: GoalTracker):
        assert tracker.remove_goal("nope") is False

    def test_set_goals_replaces_all(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("old"))
        tracker.set_goals([_mk_goal("new1"), _mk_goal("new2")])
        ids = [g.goal_id for g in tracker.all_goals]
        assert ids == ["new1", "new2"]

    def test_set_goals_respects_max(self, tracker: GoalTracker):
        with patch(
            "backend.core.ouroboros.governance.strategic_direction._MAX_GOALS", 2
        ):
            tracker.set_goals([
                _mk_goal("g1"), _mk_goal("g2"), _mk_goal("g3"),
            ])
            assert len(tracker.all_goals) == 2

    def test_get_returns_none_when_missing(self, tracker: GoalTracker):
        assert tracker.get("ghost") is None


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_pause_resume(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1"))
        assert tracker.pause("g1") is True
        g = tracker.get("g1")
        assert g is not None and g.status is GoalStatus.PAUSED
        assert tracker.active_goals == []

        assert tracker.resume("g1") is True
        g = tracker.get("g1")
        assert g is not None and g.status is GoalStatus.ACTIVE
        assert len(tracker.active_goals) == 1

    def test_complete(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1"))
        assert tracker.complete("g1") is True
        g = tracker.get("g1")
        assert g is not None and g.status is GoalStatus.COMPLETED
        assert tracker.active_goals == []

    def test_set_status_returns_false_when_missing(self, tracker: GoalTracker):
        assert tracker.pause("ghost") is False
        assert tracker.resume("ghost") is False
        assert tracker.complete("ghost") is False

    def test_purge_completed(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("keep1"))
        tracker.add_goal(_mk_goal("done1"))
        tracker.add_goal(_mk_goal("done2"))
        tracker.complete("done1")
        tracker.complete("done2")

        removed = tracker.purge_completed()
        assert removed == 2
        ids = [g.goal_id for g in tracker.all_goals]
        assert ids == ["keep1"]

    def test_purge_completed_empty(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1"))
        assert tracker.purge_completed() == 0

    def test_goals_by_status(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("a1"))
        tracker.add_goal(_mk_goal("p1"))
        tracker.add_goal(_mk_goal("c1"))
        tracker.pause("p1")
        tracker.complete("c1")

        assert [g.goal_id for g in tracker.goals_by_status(GoalStatus.ACTIVE)] == ["a1"]
        assert [g.goal_id for g in tracker.goals_by_status(GoalStatus.PAUSED)] == ["p1"]
        assert [g.goal_id for g in tracker.goals_by_status(GoalStatus.COMPLETED)] == ["c1"]


# ---------------------------------------------------------------------------
# Relevance scoring internals
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic_split(self):
        assert GoalTracker._tokenize("Hello World Test") == {"hello", "world", "test"}

    def test_drops_short(self):
        toks = GoalTracker._tokenize("a bb ccc dddd")
        assert toks == {"dddd"}

    def test_non_alnum_separators(self):
        toks = GoalTracker._tokenize("foo-bar.baz/qux")
        assert "bar" not in toks  # <4 chars
        assert "qux" not in toks
        assert "baz" not in toks

    def test_empty(self):
        assert GoalTracker._tokenize("") == set()
        assert GoalTracker._tokenize(None) == set()  # type: ignore[arg-type]

    def test_case_insensitive(self):
        assert GoalTracker._tokenize("TESTING") == {"testing"}


class TestStalenessMultiplier:
    def test_fresh_goal_has_no_decay(self):
        now = time.time()
        assert GoalTracker._staleness_multiplier(now, 14.0) == pytest.approx(1.0, abs=0.001)

    def test_halflife_decay(self):
        halflife_days = 10.0
        created = time.time() - (10 * 86400.0)  # 10 days ago
        mult = GoalTracker._staleness_multiplier(created, halflife_days)
        assert mult == pytest.approx(0.5, abs=0.01)

    def test_two_halflives(self):
        halflife_days = 5.0
        created = time.time() - (10 * 86400.0)  # 2 halflives
        mult = GoalTracker._staleness_multiplier(created, halflife_days)
        assert mult == pytest.approx(0.25, abs=0.01)

    def test_zero_halflife_disables_decay(self):
        created = time.time() - (100 * 86400.0)
        assert GoalTracker._staleness_multiplier(created, 0.0) == 1.0

    def test_zero_created_is_noop(self):
        assert GoalTracker._staleness_multiplier(0.0, 14.0) == 1.0

    def test_future_created_at_clamps_to_one(self):
        future = time.time() + 1000
        assert GoalTracker._staleness_multiplier(future, 14.0) == 1.0


class TestScoreGoal:
    def test_inactive_goal_scores_zero(self):
        g = _mk_goal(status=GoalStatus.PAUSED)
        assert GoalTracker._score_goal(
            g, description="test coverage", target_files=()
        ) == 0.0

    def test_path_match(self):
        g = _mk_goal(path_patterns=("backend/core/",), keywords=())
        score = GoalTracker._score_goal(
            g,
            description="",
            target_files=["backend/core/foo.py"],
            halflife_days=0.0,
        )
        assert score >= 10.0  # _SCORE_PATH_MATCH default

    def test_tag_match(self):
        g = _mk_goal(tags=("reliability",), keywords=(), path_patterns=())
        score = GoalTracker._score_goal(
            g,
            description="improve reliability of the system",
            target_files=(),
            halflife_days=0.0,
        )
        assert score >= 6.0  # _SCORE_TAG_MATCH

    def test_keyword_match(self):
        g = _mk_goal(keywords=("pytest",), path_patterns=(), tags=())
        score = GoalTracker._score_goal(
            g,
            description="add pytest fixtures",
            target_files=(),
            halflife_days=0.0,
        )
        assert score >= 4.0  # _SCORE_KEYWORD_MATCH

    def test_no_signals_scores_zero(self):
        g = _mk_goal(keywords=("xyz",), path_patterns=("nowhere/",), tags=("none",))
        assert GoalTracker._score_goal(
            g,
            description="totally unrelated topic",
            target_files=["src/main.py"],
            halflife_days=0.0,
        ) == 0.0

    def test_priority_weight_multiplier(self):
        g_low = _mk_goal("low", keywords=("test",), priority_weight=1.0)
        g_hi = _mk_goal("hi", keywords=("test",), priority_weight=2.0)
        s_low = GoalTracker._score_goal(
            g_low, description="run the tests", target_files=(), halflife_days=0.0,
        )
        s_hi = GoalTracker._score_goal(
            g_hi, description="run the tests", target_files=(), halflife_days=0.0,
        )
        assert s_hi == pytest.approx(s_low * 2.0, abs=0.001)

    def test_combined_signals_stack(self):
        g = _mk_goal(
            path_patterns=("backend/",),
            keywords=("coverage",),
            tags=("reliability",),
        )
        score = GoalTracker._score_goal(
            g,
            description="improve coverage reliability",
            target_files=["backend/foo.py"],
            halflife_days=0.0,
        )
        # Path (10) + tag (6) + keyword (4) = 20
        assert score >= 20.0

    def test_staleness_reduces_score(self):
        fresh = _mk_goal("f", keywords=("test",), created_at=time.time())
        old = _mk_goal(
            "o", keywords=("test",), created_at=time.time() - (14 * 86400.0),
        )
        s_fresh = GoalTracker._score_goal(
            fresh, description="test it", target_files=(), halflife_days=14.0,
        )
        s_old = GoalTracker._score_goal(
            old, description="test it", target_files=(), halflife_days=14.0,
        )
        assert s_old < s_fresh
        assert s_old == pytest.approx(s_fresh * 0.5, abs=0.1)


# ---------------------------------------------------------------------------
# find_relevant
# ---------------------------------------------------------------------------


class TestFindRelevant:
    def test_empty_tracker(self, tracker: GoalTracker):
        assert tracker.find_relevant(description="anything") == []

    def test_filters_below_min_relevance(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("unrelated", keywords=("xyz",), path_patterns=()))
        # No signal matches — score 0 < min_relevance
        assert tracker.find_relevant(description="totally different") == []

    def test_sort_by_score_desc(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(
            "weak", keywords=("test",), path_patterns=(), tags=(),
        ))
        tracker.add_goal(_mk_goal(
            "strong",
            keywords=("test",),
            path_patterns=("backend/",),
            tags=("reliability",),
        ))
        results = tracker.find_relevant(
            description="test the reliability",
            target_files=["backend/foo.py"],
        )
        assert results[0][0].goal_id == "strong"
        assert results[0][1] > results[1][1]

    def test_limit_truncates(self, tracker: GoalTracker):
        for i in range(5):
            tracker.add_goal(_mk_goal(f"g{i}", keywords=("test",)))
        results = tracker.find_relevant(description="test me", limit=2)
        assert len(results) == 2

    def test_default_limit_uses_env(self, tracker: GoalTracker):
        with patch(
            "backend.core.ouroboros.governance.strategic_direction._MAX_PROMPT_GOALS", 2
        ):
            for i in range(5):
                tracker.add_goal(_mk_goal(f"g{i}", keywords=("test",)))
            results = tracker.find_relevant(description="test me")
            assert len(results) == 2

    def test_skips_paused_goals(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("active", keywords=("test",)))
        tracker.add_goal(_mk_goal("paused", keywords=("test",)))
        tracker.pause("paused")
        results = tracker.find_relevant(description="run the tests")
        assert len(results) == 1
        assert results[0][0].goal_id == "active"


# ---------------------------------------------------------------------------
# alignment_boost (v1 API preserved)
# ---------------------------------------------------------------------------


class TestAlignmentBoost:
    def test_returns_zero_on_no_match(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1", keywords=("xyz",), path_patterns=()))
        assert tracker.alignment_boost("unrelated work") == 0

    def test_returns_positive_on_match(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1", keywords=("test",)))
        boost = tracker.alignment_boost("improve test coverage")
        assert boost >= 1

    def test_priority_weight_scales_boost(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1", keywords=("test",), priority_weight=3.0))
        boost = tracker.alignment_boost("run tests")
        assert boost >= 3  # _GOAL_ALIGNMENT_BOOST (2) * 3.0 floored ≈ 6

    def test_target_files_factor_in(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(
            "g1", keywords=(), path_patterns=("backend/core/",),
        ))
        assert tracker.alignment_boost(
            "refactor", target_files=["backend/core/foo.py"],
        ) >= 1


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------


class TestFormatForPrompt:
    def test_empty_returns_empty_string(self, tracker: GoalTracker):
        assert tracker.format_for_prompt() == ""

    def test_unscoped_renders_all_active(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1", description="first goal"))
        tracker.add_goal(_mk_goal("g2", description="second goal"))
        out = tracker.format_for_prompt()
        assert "## Active Goals" in out
        assert "**g1**" in out
        assert "**g2**" in out

    def test_unscoped_skips_paused(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("active", description="keep"))
        tracker.add_goal(_mk_goal("paused", description="hide"))
        tracker.pause("paused")
        out = tracker.format_for_prompt()
        assert "active" in out
        assert "paused" not in out

    def test_scoped_filters_by_relevance(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(
            "matching", keywords=("pytest",), path_patterns=(),
        ))
        tracker.add_goal(_mk_goal(
            "unrelated", keywords=("deploy",), path_patterns=(),
        ))
        out = tracker.format_for_prompt(description="add pytest fixtures")
        assert "matching" in out
        assert "unrelated" not in out

    def test_scoped_empty_when_nothing_matches(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1", keywords=("xyz",), path_patterns=()))
        assert tracker.format_for_prompt(description="unrelated") == ""

    def test_high_priority_tag_rendered(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("hp", priority_weight=2.5))
        out = tracker.format_for_prompt()
        assert "[HIGH PRIORITY]" in out

    def test_low_priority_tag_rendered(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("lp", priority_weight=0.3))
        out = tracker.format_for_prompt()
        assert "[low priority]" in out

    def test_tags_rendered(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("t1", tags=("reliability", "sprint-2")))
        out = tracker.format_for_prompt()
        assert "#reliability" in out
        assert "#sprint-2" in out

    def test_relevance_score_rendered_when_scoped(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1", keywords=("test",)))
        out = tracker.format_for_prompt(description="run tests")
        assert "relevance=" in out

    def test_path_patterns_in_focus_section(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(
            "g1", path_patterns=("backend/core/",),
        ))
        out = tracker.format_for_prompt()
        assert "backend/core/" in out


# ---------------------------------------------------------------------------
# extract_keywords
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_basic(self):
        kws = GoalTracker.extract_keywords("improve test coverage in governance")
        assert "test" in kws
        assert "coverage" in kws
        assert "governance" in kws
        assert "improve" in kws

    def test_filters_short_words(self):
        kws = GoalTracker.extract_keywords("fix a bug in my code")
        assert "bug" not in kws   # 3 chars
        assert "fix" not in kws   # 3 chars
        assert "code" in kws

    def test_filters_stopwords(self):
        kws = GoalTracker.extract_keywords(
            "refactor the code that would have been better"
        )
        assert "that" not in kws
        assert "would" not in kws
        assert "have" not in kws
        assert "been" not in kws
        assert "refactor" in kws

    def test_deduplicates(self):
        kws = GoalTracker.extract_keywords("test test test coverage")
        assert list(kws).count("test") == 1

    def test_respects_limit(self):
        desc = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        kws = GoalTracker.extract_keywords(desc, limit=3)
        assert len(kws) == 3

    def test_respects_min_len(self):
        kws = GoalTracker.extract_keywords("foo barr bazz quux", min_len=4)
        assert "foo" not in kws
        assert "barr" in kws
        assert "bazz" in kws

    def test_empty_description(self):
        assert GoalTracker.extract_keywords("") == ()

    def test_custom_stopwords(self):
        kws = GoalTracker.extract_keywords(
            "refactor implement", stopwords=("refactor",),
        )
        assert "refactor" not in kws
        assert "implement" in kws

    def test_returns_tuple(self):
        kws = GoalTracker.extract_keywords("improve coverage")
        assert isinstance(kws, tuple)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert GoalTracker.slugify("Improve test coverage") == "improve-test-coverage"

    def test_special_chars(self):
        assert GoalTracker.slugify("Fix Bug! In Code?") == "fix-bug-in-code"

    def test_empty(self):
        assert GoalTracker.slugify("") == "goal"

    def test_only_special_chars(self):
        assert GoalTracker.slugify("!!!") == "goal"

    def test_respects_max_len(self):
        desc = "a" * 100
        s = GoalTracker.slugify(desc, max_len=10)
        assert len(s) <= 10

    def test_strips_trailing_dash(self):
        s = GoalTracker.slugify("Goal name with trailing space")
        assert not s.endswith("-")


# ---------------------------------------------------------------------------
# Persistence (v1→v2 migration + round-trip)
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_round_trip_v2(self, tmp_root: Path):
        t1 = GoalTracker(tmp_root)
        t1.add_goal(_mk_goal(
            "g1",
            description="Improve test coverage",
            keywords=("test",),
            path_patterns=("backend/",),
            tags=("reliability",),
            priority_weight=1.5,
        ))
        t1.pause("g1")

        t2 = GoalTracker(tmp_root)
        loaded = t2.get("g1")
        assert loaded is not None
        assert loaded.description == "Improve test coverage"
        assert loaded.keywords == ("test",)
        assert loaded.path_patterns == ("backend/",)
        assert loaded.tags == ("reliability",)
        assert loaded.priority_weight == 1.5
        assert loaded.status is GoalStatus.PAUSED

    def test_v1_bare_list_loads(self, tmp_root: Path):
        """v1 format: JSON file was a bare list, no schema_version wrapper."""
        goal_file = tmp_root / ".jarvis" / "active_goals.json"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        v1_data = [
            {
                "goal_id": "legacy",
                "description": "Old format goal",
                "keywords": ["test"],
                "path_patterns": ["backend/"],
                "priority_weight": 1.0,
                "created_at": time.time(),
            }
        ]
        goal_file.write_text(json.dumps(v1_data))

        tracker = GoalTracker(tmp_root)
        loaded = tracker.get("legacy")
        assert loaded is not None
        assert loaded.description == "Old format goal"
        # Missing v2 fields default sensibly.
        assert loaded.status is GoalStatus.ACTIVE
        assert loaded.tags == ()
        assert loaded.due_at is None

    def test_v2_loader_reads_schema(self, tmp_root: Path):
        goal_file = tmp_root / ".jarvis" / "active_goals.json"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        v2_data = {
            "schema_version": 2,
            "goals": [
                {
                    "goal_id": "g1",
                    "description": "v2 goal",
                    "keywords": ["test"],
                    "path_patterns": [],
                    "tags": ["reliability"],
                    "priority_weight": 2.0,
                    "status": "paused",
                    "due_at": None,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
            ],
        }
        goal_file.write_text(json.dumps(v2_data))

        tracker = GoalTracker(tmp_root)
        loaded = tracker.get("g1")
        assert loaded is not None
        assert loaded.status is GoalStatus.PAUSED
        assert loaded.tags == ("reliability",)
        assert loaded.priority_weight == 2.0

    def test_corrupt_file_recovers_empty(self, tmp_root: Path):
        goal_file = tmp_root / ".jarvis" / "active_goals.json"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        goal_file.write_text("{{{ not valid json")

        tracker = GoalTracker(tmp_root)
        assert tracker.all_goals == []

    def test_missing_file_empty_tracker(self, tmp_root: Path):
        tracker = GoalTracker(tmp_root)
        assert tracker.all_goals == []

    def test_non_dict_entries_skipped(self, tmp_root: Path):
        goal_file = tmp_root / ".jarvis" / "active_goals.json"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": 2,
            "goals": [
                {"goal_id": "good", "description": "ok", "keywords": []},
                "not_a_dict",
                None,
            ],
        }
        goal_file.write_text(json.dumps(data))

        tracker = GoalTracker(tmp_root)
        ids = [g.goal_id for g in tracker.all_goals]
        assert ids == ["good"]

    def test_blank_goal_ids_dropped(self, tmp_root: Path):
        goal_file = tmp_root / ".jarvis" / "active_goals.json"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": 2,
            "goals": [
                {"goal_id": "", "description": "blank id"},
                {"goal_id": "valid", "description": "valid goal"},
            ],
        }
        goal_file.write_text(json.dumps(data))

        tracker = GoalTracker(tmp_root)
        assert [g.goal_id for g in tracker.all_goals] == ["valid"]

    def test_persist_writes_schema_version(self, tmp_root: Path):
        tracker = GoalTracker(tmp_root)
        tracker.add_goal(_mk_goal("g1"))

        raw = json.loads(
            (tmp_root / ".jarvis" / "active_goals.json").read_text()
        )
        assert isinstance(raw, dict)
        # Schema bumped to v3 for Persistent Goal Hierarchy (parent_id).
        assert raw["schema_version"] == 3
        assert "goals" in raw


# ---------------------------------------------------------------------------
# Env-driven config
# ---------------------------------------------------------------------------


class TestEnvConfig:
    def test_max_goals_env(self, tmp_root: Path):
        """Changing _MAX_GOALS via env must take effect on import."""
        # We validate via the module-level patching used elsewhere rather
        # than re-importing. The module reads env vars exactly once at
        # import time, so test the behavior via direct patch.
        with patch(
            "backend.core.ouroboros.governance.strategic_direction._MAX_GOALS", 1
        ):
            t = GoalTracker(tmp_root)
            t.add_goal(_mk_goal("g1"))
            t.add_goal(_mk_goal("g2"))
            assert len(t.all_goals) == 1

    def test_env_helpers_fallback_on_garbage(self):
        from backend.core.ouroboros.governance.strategic_direction import (
            _env_int, _env_float,
        )
        with patch.dict(os.environ, {
            "JARVIS_TEST_INT": "not_a_number",
            "JARVIS_TEST_FLOAT": "also_not",
        }):
            assert _env_int("JARVIS_TEST_INT", 42) == 42
            assert _env_float("JARVIS_TEST_FLOAT", 1.5) == 1.5

    def test_env_helpers_honor_minimum(self):
        from backend.core.ouroboros.governance.strategic_direction import (
            _env_int, _env_float,
        )
        with patch.dict(os.environ, {
            "JARVIS_TEST_INT_NEG": "-5",
            "JARVIS_TEST_FLOAT_NEG": "-2.5",
        }):
            assert _env_int("JARVIS_TEST_INT_NEG", 1, minimum=1) == 1
            assert _env_float("JARVIS_TEST_FLOAT_NEG", 0.0, minimum=0.0) == 0.0

    def test_env_set_parses_csv(self):
        from backend.core.ouroboros.governance.strategic_direction import _env_set
        with patch.dict(os.environ, {"JARVIS_TEST_SET": "foo, bar,  baz"}):
            assert _env_set("JARVIS_TEST_SET", ("default",)) == ("foo", "bar", "baz")

    def test_env_set_empty_uses_default(self):
        from backend.core.ouroboros.governance.strategic_direction import _env_set
        with patch.dict(os.environ, {"JARVIS_TEST_SET_EMPTY": ",,,"}):
            assert _env_set(
                "JARVIS_TEST_SET_EMPTY", ("default",)
            ) == ("default",)

    def test_env_set_missing_uses_default(self):
        from backend.core.ouroboros.governance.strategic_direction import _env_set
        os.environ.pop("JARVIS_TEST_SET_MISSING", None)
        assert _env_set(
            "JARVIS_TEST_SET_MISSING", ("a", "b"),
        ) == ("a", "b")


# ---------------------------------------------------------------------------
# v3 schema — Persistent Goal Hierarchy (parent_id)
# ---------------------------------------------------------------------------


def _write_goals_file(root: Path, payload) -> Path:
    """Write a goals JSON file at the v1/v2/v3 path. Returns the path."""
    path = root / ".jarvis" / "active_goals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


class TestActiveGoalV3:
    def test_parent_id_defaults_none(self):
        g = _mk_goal("g1")
        assert g.parent_id is None
        assert g.is_root is True

    def test_is_root_false_when_parent_set(self):
        g = ActiveGoal(
            goal_id="child",
            description="child goal",
            keywords=("x",),
            parent_id="parent",
        )
        assert g.is_root is False
        assert g.parent_id == "parent"


class TestHierarchyQueries:
    def test_roots_and_active_roots(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("root-a"))
        tracker.add_goal(_mk_goal("root-b"))
        tracker.add_goal(ActiveGoal(
            goal_id="child-a",
            description="child of root-a",
            keywords=("x",),
            parent_id="root-a",
        ))
        assert {g.goal_id for g in tracker.roots} == {"root-a", "root-b"}
        assert {g.goal_id for g in tracker.active_roots} == {"root-a", "root-b"}

        # Paused root still counts as root but not active.
        tracker.pause("root-b")
        assert {g.goal_id for g in tracker.roots} == {"root-a", "root-b"}
        assert {g.goal_id for g in tracker.active_roots} == {"root-a"}

    def test_children_of(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("parent"))
        tracker.add_goal(ActiveGoal(
            goal_id="c1", description="c1", keywords=("x",), parent_id="parent",
        ))
        tracker.add_goal(ActiveGoal(
            goal_id="c2", description="c2", keywords=("x",), parent_id="parent",
        ))
        tracker.add_goal(_mk_goal("unrelated"))
        assert {g.goal_id for g in tracker.children_of("parent")} == {"c1", "c2"}
        assert tracker.children_of("unrelated") == []
        assert tracker.children_of("missing") == []

    def test_parent_of(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("p"))
        tracker.add_goal(ActiveGoal(
            goal_id="c", description="c", keywords=("x",), parent_id="p",
        ))
        parent = tracker.parent_of("c")
        assert parent is not None
        assert parent.goal_id == "p"
        assert tracker.parent_of("p") is None
        assert tracker.parent_of("missing") is None

    def test_has_cycle_self_reference(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("g1"))
        assert tracker.has_cycle("g1", "g1") is True

    def test_has_cycle_transitive(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("a"))
        tracker.add_goal(ActiveGoal(
            goal_id="b", description="b", keywords=("x",), parent_id="a",
        ))
        tracker.add_goal(ActiveGoal(
            goal_id="c", description="c", keywords=("x",), parent_id="b",
        ))
        # Making a's parent c would close the loop a → c → b → a
        assert tracker.has_cycle("a", "c") is True
        # Making c's parent a would just reinforce the existing chain — fine.
        assert tracker.has_cycle("c", "a") is False


class TestAddGoalParentValidation:
    def test_self_reference_is_dropped(self, tracker: GoalTracker):
        bad = ActiveGoal(
            goal_id="loop",
            description="loop",
            keywords=("x",),
            parent_id="loop",
        )
        tracker.add_goal(bad)
        stored = tracker.get("loop")
        assert stored is not None
        assert stored.parent_id is None

    def test_orphan_parent_installed_as_root(self, tracker: GoalTracker):
        orphan = ActiveGoal(
            goal_id="lone",
            description="lone",
            keywords=("x",),
            parent_id="nonexistent",
        )
        tracker.add_goal(orphan)
        stored = tracker.get("lone")
        assert stored is not None
        assert stored.parent_id is None

    def test_valid_parent_is_preserved(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("p"))
        tracker.add_goal(ActiveGoal(
            goal_id="c", description="c", keywords=("x",), parent_id="p",
        ))
        stored = tracker.get("c")
        assert stored is not None
        assert stored.parent_id == "p"

    def test_cycle_via_upsert_is_broken(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("a"))
        tracker.add_goal(ActiveGoal(
            goal_id="b", description="b", keywords=("x",), parent_id="a",
        ))
        # Upserting a to have parent=b closes the loop a → b → a
        tracker.add_goal(ActiveGoal(
            goal_id="a",
            description="a upserted",
            keywords=("x",),
            parent_id="b",
        ))
        stored_a = tracker.get("a")
        assert stored_a is not None
        assert stored_a.parent_id is None  # cycle broken
        assert stored_a.description == "a upserted"

    def test_capacity_eviction_heals_child_pointers(self, tmp_root: Path):
        """When a parent is dropped at capacity, children are promoted."""
        with patch(
            "backend.core.ouroboros.governance.strategic_direction._MAX_GOALS", 3
        ):
            t = GoalTracker(tmp_root)
            # Mark parent as PAUSED so eviction picks it as the drop target.
            t.add_goal(_mk_goal("p", status=GoalStatus.PAUSED))
            t.add_goal(ActiveGoal(
                goal_id="c1", description="c1", keywords=("x",), parent_id="p",
            ))
            t.add_goal(ActiveGoal(
                goal_id="c2", description="c2", keywords=("x",), parent_id="p",
            ))
            # Fourth add triggers eviction — "p" (inactive) is dropped.
            t.add_goal(_mk_goal("new-one"))
            assert t.get("p") is None
            # Children were promoted to roots (parent_id nulled).
            c1 = t.get("c1")
            c2 = t.get("c2")
            assert c1 is not None and c1.parent_id is None
            assert c2 is not None and c2.parent_id is None


class TestPersistenceV3:
    def test_round_trip_v3_with_parent_id(self, tmp_root: Path):
        t1 = GoalTracker(tmp_root)
        t1.add_goal(_mk_goal("parent"))
        t1.add_goal(ActiveGoal(
            goal_id="child",
            description="child",
            keywords=("y",),
            parent_id="parent",
        ))

        t2 = GoalTracker(tmp_root)
        parent = t2.get("parent")
        child = t2.get("child")
        assert parent is not None and parent.parent_id is None
        assert child is not None and child.parent_id == "parent"

    def test_persist_writes_schema_version_3(self, tmp_root: Path):
        t = GoalTracker(tmp_root)
        t.add_goal(_mk_goal("g1"))
        raw = json.loads((tmp_root / ".jarvis" / "active_goals.json").read_text())
        assert raw["schema_version"] == 3
        assert raw["goals"][0]["parent_id"] is None

    def test_v2_auto_upgrades_to_v3(self, tmp_root: Path):
        """Loading a v2 file should upgrade it to v3 in place."""
        _write_goals_file(tmp_root, {
            "schema_version": 2,
            "goals": [
                {
                    "goal_id": "legacy",
                    "description": "legacy v2 goal",
                    "keywords": ["x"],
                    "path_patterns": [],
                    "tags": [],
                    "priority_weight": 1.0,
                    "status": "active",
                    "due_at": None,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                },
            ],
        })
        t = GoalTracker(tmp_root)
        assert t.last_migration_report.source_version == 2
        assert t.last_migration_report.upgraded is True
        raw = json.loads((tmp_root / ".jarvis" / "active_goals.json").read_text())
        assert raw["schema_version"] == 3
        # parent_id is written even though the v2 source didn't have it.
        assert "parent_id" in raw["goals"][0]
        assert raw["goals"][0]["parent_id"] is None

    def test_v1_bare_list_upgrades_to_v3(self, tmp_root: Path):
        _write_goals_file(tmp_root, [
            {
                "goal_id": "g1",
                "description": "ancient",
                "keywords": ["old"],
            },
        ])
        t = GoalTracker(tmp_root)
        assert t.last_migration_report.source_version == 1
        assert t.last_migration_report.upgraded is True
        stored = t.get("g1")
        assert stored is not None and stored.parent_id is None
        raw = json.loads((tmp_root / ".jarvis" / "active_goals.json").read_text())
        assert raw["schema_version"] == 3

    def test_load_heals_self_reference(self, tmp_root: Path):
        _write_goals_file(tmp_root, {
            "schema_version": 3,
            "goals": [
                {
                    "goal_id": "narcissist",
                    "description": "points at self",
                    "keywords": ["x"],
                    "path_patterns": [],
                    "tags": [],
                    "priority_weight": 1.0,
                    "status": "active",
                    "parent_id": "narcissist",
                },
            ],
        })
        t = GoalTracker(tmp_root)
        stored = t.get("narcissist")
        assert stored is not None and stored.parent_id is None
        assert t.last_migration_report.healed_self_reference == 1
        assert t.last_migration_report.has_issues is True

    def test_load_heals_orphan_parent(self, tmp_root: Path):
        _write_goals_file(tmp_root, {
            "schema_version": 3,
            "goals": [
                {
                    "goal_id": "lone",
                    "description": "parent vanished",
                    "keywords": ["x"],
                    "path_patterns": [],
                    "tags": [],
                    "priority_weight": 1.0,
                    "status": "active",
                    "parent_id": "ghost",
                },
            ],
        })
        t = GoalTracker(tmp_root)
        stored = t.get("lone")
        assert stored is not None
        assert stored.parent_id is None
        assert t.last_migration_report.healed_orphan_parent == 1

    def test_load_breaks_cycle(self, tmp_root: Path):
        _write_goals_file(tmp_root, {
            "schema_version": 3,
            "goals": [
                {
                    "goal_id": "a",
                    "description": "a",
                    "keywords": ["x"],
                    "path_patterns": [],
                    "tags": [],
                    "priority_weight": 1.0,
                    "status": "active",
                    "parent_id": "b",
                },
                {
                    "goal_id": "b",
                    "description": "b",
                    "keywords": ["x"],
                    "path_patterns": [],
                    "tags": [],
                    "priority_weight": 1.0,
                    "status": "active",
                    "parent_id": "a",
                },
            ],
        })
        t = GoalTracker(tmp_root)
        # At least one goal in the cycle had its parent_id nulled.
        roots = t.roots
        assert len(roots) >= 1
        assert t.last_migration_report.healed_cycle >= 1

    def test_load_drops_duplicate_ids(self, tmp_root: Path):
        _write_goals_file(tmp_root, {
            "schema_version": 3,
            "goals": [
                {"goal_id": "dup", "description": "first", "keywords": ["x"]},
                {"goal_id": "dup", "description": "second", "keywords": ["y"]},
            ],
        })
        t = GoalTracker(tmp_root)
        assert len(t.all_goals) == 1
        stored = t.get("dup")
        assert stored is not None
        assert stored.description == "first"
        assert t.last_migration_report.dropped_duplicate_id == 1

    def test_load_drops_invalid_entries(self, tmp_root: Path):
        _write_goals_file(tmp_root, {
            "schema_version": 3,
            "goals": [
                "not-a-dict",
                {"description": "no goal_id"},
                {"goal_id": "", "description": "empty id"},
                {"goal_id": "good", "description": "ok", "keywords": ["x"]},
            ],
        })
        t = GoalTracker(tmp_root)
        assert [g.goal_id for g in t.all_goals] == ["good"]
        assert t.last_migration_report.dropped_invalid == 3

    def test_atomic_persist_uses_temp_file(self, tmp_root: Path):
        """Persist must route through a .tmp sibling before os.replace."""
        t = GoalTracker(tmp_root)
        t.add_goal(_mk_goal("g1"))
        goals_file = tmp_root / ".jarvis" / "active_goals.json"
        tmp_file = tmp_root / ".jarvis" / "active_goals.json.tmp"
        assert goals_file.exists()
        # Atomic rename should leave no stray .tmp after a successful write.
        assert not tmp_file.exists()

    def test_migration_report_clean_on_fresh_tracker(self, tmp_root: Path):
        t = GoalTracker(tmp_root)
        report = t.last_migration_report
        assert report.source_version is None  # no file existed
        assert report.loaded_count == 0
        assert report.has_issues is False
        assert report.upgraded is False

    def test_migration_report_summary_reads_cleanly(self):
        report = GoalMigrationReport(
            source_version=3,
            healed_orphan_parent=2,
            healed_cycle=1,
        )
        summary = report.summary()
        assert "2 orphan parents" in summary
        assert "1 cycles" in summary
        assert report.has_issues is True


# ---------------------------------------------------------------------------
# Increment 2: hierarchy traversal helpers
# ---------------------------------------------------------------------------


def _build_chain(t: GoalTracker) -> None:
    """Build a 4-level chain: root → a → b → c, plus sibling root2."""
    t.add_goal(_mk_goal("root"))
    t.add_goal(_mk_goal("a"))
    t.set_goals([
        _mk_goal("root"),
        ActiveGoal(goal_id="a", description="a", keywords=(), parent_id="root"),
        ActiveGoal(goal_id="b", description="b", keywords=(), parent_id="a"),
        ActiveGoal(goal_id="c", description="c", keywords=(), parent_id="b"),
        _mk_goal("root2"),
    ])


class TestAncestorsOf:
    def test_unknown_goal_returns_empty(self, tracker: GoalTracker):
        assert tracker.ancestors_of("nope") == []

    def test_root_returns_empty(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("root"))
        assert tracker.ancestors_of("root") == []

    def test_direct_parent(self, tracker: GoalTracker):
        tracker.set_goals([
            _mk_goal("root"),
            ActiveGoal(goal_id="child", description="c", keywords=(), parent_id="root"),
        ])
        chain = tracker.ancestors_of("child")
        assert [g.goal_id for g in chain] == ["root"]

    def test_multi_level_nearest_first(self, tracker: GoalTracker):
        _build_chain(tracker)
        chain = tracker.ancestors_of("c")
        # Nearest parent first, root last.
        assert [g.goal_id for g in chain] == ["b", "a", "root"]

    def test_cycle_defense_does_not_infinite_loop(self, tracker: GoalTracker):
        # _heal_hierarchy should have broken the cycle at load, but
        # ancestors_of is also defensively bounded by a visited set.
        # We simulate a graph that somehow still carries a cycle in
        # memory by bypassing add_goal's validation via set_goals +
        # a malformed internal state.
        tracker.set_goals([
            _mk_goal("root"),
            ActiveGoal(goal_id="a", description="a", keywords=(), parent_id="root"),
            ActiveGoal(goal_id="b", description="b", keywords=(), parent_id="a"),
        ])
        # Poke in-memory to create a cycle (not via public API).
        stored_a = tracker.get("a")
        assert stored_a is not None
        # Rebuild tracker state with a forced cycle by replacing
        # stored entries directly on the internal list.
        goals = list(tracker.all_goals)
        for i, g in enumerate(goals):
            if g.goal_id == "root":
                goals[i] = ActiveGoal(
                    goal_id="root", description="r", keywords=(), parent_id="b",
                )
        tracker._goals = goals  # type: ignore[attr-defined]
        # Should terminate even though root.parent=b → a → root → ...
        chain = tracker.ancestors_of("a")
        assert len(chain) < 10  # bounded


class TestDescendantsOf:
    def test_unknown_goal_returns_empty(self, tracker: GoalTracker):
        assert tracker.descendants_of("nope") == []

    def test_leaf_returns_empty(self, tracker: GoalTracker):
        _build_chain(tracker)
        assert tracker.descendants_of("c") == []

    def test_bfs_order(self, tracker: GoalTracker):
        # _MAX_GOALS default is 5, so stay within that cap.
        tracker.set_goals([
            _mk_goal("root"),
            ActiveGoal(goal_id="a", description="a", keywords=(), parent_id="root"),
            ActiveGoal(goal_id="b", description="b", keywords=(), parent_id="root"),
            ActiveGoal(goal_id="a1", description="a1", keywords=(), parent_id="a"),
            ActiveGoal(goal_id="a2", description="a2", keywords=(), parent_id="a"),
        ])
        ids = [g.goal_id for g in tracker.descendants_of("root")]
        # BFS: root's direct children first (sorted by id), then grandkids.
        assert ids == ["a", "b", "a1", "a2"]

    def test_deterministic_sort_within_layer(self, tracker: GoalTracker):
        tracker.set_goals([
            _mk_goal("root"),
            ActiveGoal(goal_id="zulu", description="z", keywords=(), parent_id="root"),
            ActiveGoal(goal_id="alpha", description="a", keywords=(), parent_id="root"),
            ActiveGoal(goal_id="mike", description="m", keywords=(), parent_id="root"),
        ])
        ids = [g.goal_id for g in tracker.descendants_of("root")]
        assert ids == ["alpha", "mike", "zulu"]


class TestDepthOf:
    def test_unknown_returns_negative_one(self, tracker: GoalTracker):
        assert tracker.depth_of("missing") == -1

    def test_root_is_zero(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("root"))
        assert tracker.depth_of("root") == 0

    def test_child_is_one(self, tracker: GoalTracker):
        tracker.set_goals([
            _mk_goal("root"),
            ActiveGoal(goal_id="c", description="c", keywords=(), parent_id="root"),
        ])
        assert tracker.depth_of("c") == 1

    def test_grandchild_is_two(self, tracker: GoalTracker):
        _build_chain(tracker)
        assert tracker.depth_of("b") == 2
        assert tracker.depth_of("c") == 3


class TestHierarchyTree:
    def test_empty_tracker_empty_tree(self, tracker: GoalTracker):
        assert tracker.hierarchy_tree() == []

    def test_single_root(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal("solo"))
        tree = tracker.hierarchy_tree()
        assert tree == [(tracker.get("solo"), 0)]

    def test_dfs_tree_order(self, tracker: GoalTracker):
        _build_chain(tracker)
        tree = tracker.hierarchy_tree()
        # DFS walks root → a → b → c before visiting sibling root2.
        # Roots are goal_id-sorted: root, root2.
        ids_and_depths = [(g.goal_id, d) for g, d in tree]
        assert ids_and_depths == [
            ("root", 0),
            ("a", 1),
            ("b", 2),
            ("c", 3),
            ("root2", 0),
        ]

    def test_deterministic_goal_id_sort(self, tracker: GoalTracker):
        tracker.set_goals([
            _mk_goal("zulu"),
            _mk_goal("alpha"),
            _mk_goal("mike"),
        ])
        ids = [g.goal_id for g, _ in tracker.hierarchy_tree()]
        assert ids == ["alpha", "mike", "zulu"]

    def test_include_inactive_false_hides_paused(self, tracker: GoalTracker):
        tracker.set_goals([
            _mk_goal("keep"),
            _mk_goal("hide", status=GoalStatus.PAUSED),
        ])
        ids_all = [g.goal_id for g, _ in tracker.hierarchy_tree(include_inactive=True)]
        ids_active = [g.goal_id for g, _ in tracker.hierarchy_tree(include_inactive=False)]
        assert "hide" in ids_all
        assert "hide" not in ids_active
        assert "keep" in ids_active

    def test_paused_parent_promotes_active_child_to_root(
        self, tracker: GoalTracker
    ):
        # When include_inactive=False, an active child of a paused parent
        # becomes a transient root at depth 0 rather than being orphaned.
        tracker.set_goals([
            ActiveGoal(
                goal_id="paused_root",
                description="p",
                keywords=(),
                status=GoalStatus.PAUSED,
            ),
            ActiveGoal(
                goal_id="active_child",
                description="c",
                keywords=(),
                parent_id="paused_root",
            ),
        ])
        tree = tracker.hierarchy_tree(include_inactive=False)
        ids_and_depths = [(g.goal_id, d) for g, d in tree]
        assert ids_and_depths == [("active_child", 0)]


# ---------------------------------------------------------------------------
# Increment 2: format_for_prompt ancestor injection
# ---------------------------------------------------------------------------


class TestFormatForPromptAncestry:
    def test_ancestors_rendered_when_child_matches(self, tracker: GoalTracker):
        # Child matches relevance but parent does not — parent should
        # still appear in the prompt as a "strategic ancestor".
        tracker.set_goals([
            ActiveGoal(
                goal_id="rsi",
                description="Ship RSI framework",
                keywords=("rsi", "framework"),
            ),
            ActiveGoal(
                goal_id="hierarchy",
                description="Persistent Goal Hierarchy memory",
                keywords=("hierarchy", "memory"),
                parent_id="rsi",
            ),
        ])
        out = tracker.format_for_prompt(description="hierarchy memory work")
        assert "**hierarchy**" in out  # matched (bold)
        assert "_rsi_" in out  # ancestor (italic)
        assert "(strategic ancestor)" in out

    def test_ancestry_injection_env_gate_off(self, tracker: GoalTracker):
        tracker.set_goals([
            ActiveGoal(
                goal_id="rsi",
                description="Ship RSI framework",
                keywords=("rsi",),
            ),
            ActiveGoal(
                goal_id="hierarchy",
                description="Hierarchy memory",
                keywords=("hierarchy",),
                parent_id="rsi",
            ),
        ])
        with patch.dict(os.environ, {"JARVIS_GOAL_INJECT_ANCESTRY": "false"}):
            out = tracker.format_for_prompt(description="hierarchy")
        assert "**hierarchy**" in out
        assert "rsi" not in out  # ancestor suppressed
        assert "(strategic ancestor)" not in out

    def test_indentation_reflects_depth(self, tracker: GoalTracker):
        tracker.set_goals([
            ActiveGoal(
                goal_id="root",
                description="Root objective",
                keywords=("root",),
            ),
            ActiveGoal(
                goal_id="mid",
                description="Mid objective",
                keywords=("mid",),
                parent_id="root",
            ),
            ActiveGoal(
                goal_id="leaf",
                description="Leaf objective",
                keywords=("leaf",),
                parent_id="mid",
            ),
        ])
        out = tracker.format_for_prompt(description="leaf work")
        # Depth 0 = "- ", depth 1 = "  - ", depth 2 = "    - "
        lines = out.splitlines()
        leaf_line = [l for l in lines if "leaf" in l][0]
        mid_line = [l for l in lines if "_mid_" in l][0]
        root_line = [l for l in lines if "_root_" in l][0]
        assert root_line.startswith("- ")
        assert mid_line.startswith("  - ")
        assert leaf_line.startswith("    - ")

    def test_matched_child_not_flagged_as_ancestor(self, tracker: GoalTracker):
        tracker.set_goals([
            ActiveGoal(goal_id="root", description="r", keywords=("r",)),
            ActiveGoal(
                goal_id="child",
                description="child",
                keywords=("child",),
                parent_id="root",
            ),
        ])
        out = tracker.format_for_prompt(description="child work")
        # The matched child must not carry the ancestor tag.
        child_line = [l for l in out.splitlines() if "**child**" in l][0]
        assert "(strategic ancestor)" not in child_line


# ---------------------------------------------------------------------------
# Increment 2: first-boot seeding
# ---------------------------------------------------------------------------


def _write_seed_file(root: Path, goals: list, *, rel: str = "config/goal_seeds.json") -> Path:
    """Install a seed template at ``root/rel``."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 3,
        "goals": goals,
    }))
    return path


class TestFirstBootSeeding:
    def test_seed_installs_on_first_boot(self, tmp_root: Path):
        _write_seed_file(tmp_root, [
            {
                "goal_id": "rsi-v4",
                "description": "Ship RSI v4",
                "keywords": ["rsi"],
                "priority_weight": 2.0,
                "parent_id": None,
            },
            {
                "goal_id": "hierarchy-v3",
                "description": "Hierarchy memory",
                "keywords": ["hierarchy"],
                "parent_id": "rsi-v4",
            },
        ])
        t = GoalTracker(tmp_root)
        ids = [g.goal_id for g in t.all_goals]
        assert "rsi-v4" in ids
        assert "hierarchy-v3" in ids
        report = t.last_migration_report
        assert report.seeded is True
        assert report.seed_source == "config/goal_seeds.json"
        assert report.loaded_count == 2

    def test_seed_persists_to_active_goals(self, tmp_root: Path):
        _write_seed_file(tmp_root, [
            {"goal_id": "g1", "description": "d"},
        ])
        GoalTracker(tmp_root)
        # Subsequent boots should read from active_goals.json, not re-seed.
        goals_file = tmp_root / ".jarvis" / "active_goals.json"
        assert goals_file.exists()
        t2 = GoalTracker(tmp_root)
        assert [g.goal_id for g in t2.all_goals] == ["g1"]
        # Second boot: source came from the persisted file, not the seed.
        assert t2.last_migration_report.seeded is False
        assert t2.last_migration_report.source_version == 3

    def test_seed_env_gate_disables(self, tmp_root: Path):
        _write_seed_file(tmp_root, [
            {"goal_id": "g1", "description": "d"},
        ])
        with patch.dict(os.environ, {"JARVIS_GOAL_SEED_ON_FIRST_BOOT": "false"}):
            t = GoalTracker(tmp_root)
        assert t.all_goals == []
        assert t.last_migration_report.seeded is False

    def test_custom_seed_file_path(self, tmp_root: Path):
        _write_seed_file(
            tmp_root,
            [{"goal_id": "custom", "description": "d"}],
            rel="config/my_seeds.json",
        )
        with patch.dict(
            os.environ,
            {"JARVIS_GOAL_SEED_FILE": "config/my_seeds.json"},
        ):
            t = GoalTracker(tmp_root)
        assert [g.goal_id for g in t.all_goals] == ["custom"]
        assert t.last_migration_report.seed_source == "config/my_seeds.json"

    def test_missing_seed_file_degrades_gracefully(self, tmp_root: Path):
        # No seed file, no active_goals.json → empty tracker, no error.
        t = GoalTracker(tmp_root)
        assert t.all_goals == []
        assert t.last_migration_report.seeded is False
        assert t.last_migration_report.has_issues is False

    def test_malformed_seed_file_degrades_gracefully(self, tmp_root: Path):
        seed = tmp_root / "config" / "goal_seeds.json"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_text("{not valid json")
        t = GoalTracker(tmp_root)
        assert t.all_goals == []
        assert t.last_migration_report.seeded is False

    def test_seed_heals_orphan_parent(self, tmp_root: Path):
        _write_seed_file(tmp_root, [
            {
                "goal_id": "child",
                "description": "d",
                "parent_id": "nonexistent",
            },
        ])
        t = GoalTracker(tmp_root)
        stored = t.get("child")
        assert stored is not None
        assert stored.parent_id is None  # healed
        assert t.last_migration_report.healed_orphan_parent == 1

    def test_seed_accepts_bare_list(self, tmp_root: Path):
        # Some operators prefer the v1-style bare-list seed format.
        seed = tmp_root / "config" / "goal_seeds.json"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_text(json.dumps([
            {"goal_id": "bare", "description": "d"},
        ]))
        t = GoalTracker(tmp_root)
        assert [g.goal_id for g in t.all_goals] == ["bare"]
        assert t.last_migration_report.seeded is True


# ---------------------------------------------------------------------------
# Increment 3: Scoring provenance, propagation, ledger, drift
# ---------------------------------------------------------------------------


from backend.core.ouroboros.governance.strategic_direction import (  # noqa: E402
    GoalActivityLedger,
    GoalAlignment,
    GoalAlignmentEntry,
    SessionDriftSummary,
    get_active_session_id,
    set_active_session_id,
)


class TestGoalAlignmentEntry:
    def test_to_dict_round_trip(self):
        entry = GoalAlignmentEntry(
            goal_id="g1",
            score=1.23456,
            reasons=("kw:test", "path:backend/"),
            kind="direct",
            source_goal_id="",
        )
        d = entry.to_dict()
        assert d["goal_id"] == "g1"
        assert d["score"] == 1.2346  # rounded to 4 decimals
        assert d["reasons"] == ["kw:test", "path:backend/"]
        assert d["kind"] == "direct"
        assert d["source_goal_id"] == ""

    def test_defaults(self):
        e = GoalAlignmentEntry(goal_id="x", score=0.5)
        assert e.kind == "direct"
        assert e.reasons == ()
        assert e.source_goal_id == ""


class TestScoringProvenance:
    def test_keyword_reason_emitted(self, tracker: GoalTracker):
        goal = _mk_goal(
            gid="kw-goal",
            keywords=("alpha", "beta"),
            path_patterns=(),
            tags=(),
        )
        tracker.add_goal(goal)
        matches = tracker.find_relevant_with_reasons(
            description="We need to improve alpha handling",
        )
        assert len(matches) == 1
        g, score, reasons = matches[0]
        assert g.goal_id == "kw-goal"
        assert score > 0
        assert "kw:alpha" in reasons
        assert "kw:beta" not in reasons  # only alpha in desc

    def test_path_reason_deduped(self, tracker: GoalTracker):
        # Two target files hitting the same pattern produce one reason
        # but still score twice (per-file path semantics preserved).
        goal = _mk_goal(
            gid="path-goal",
            keywords=(),
            path_patterns=("backend/core/ouroboros/",),
            tags=(),
        )
        tracker.add_goal(goal)
        matches = tracker.find_relevant_with_reasons(
            description="unrelated",
            target_files=(
                "backend/core/ouroboros/governance/orchestrator.py",
                "backend/core/ouroboros/governance/providers.py",
            ),
        )
        assert len(matches) == 1
        _g, _s, reasons = matches[0]
        path_reasons = [r for r in reasons if r.startswith("path:")]
        assert path_reasons == ["path:backend/core/ouroboros/"]

    def test_tag_reasons_sorted_deterministic(self, tracker: GoalTracker):
        # Tag ordering must be deterministic (sorted) so ledger rows
        # do not drift between runs for the same input.
        goal = _mk_goal(
            gid="tag-goal",
            keywords=(),
            path_patterns=(),
            tags=("zebra", "alpha", "mango"),
        )
        tracker.add_goal(goal)
        matches = tracker.find_relevant_with_reasons(
            description="zebra alpha mango",
        )
        assert len(matches) == 1
        _g, _s, reasons = matches[0]
        tag_reasons = [r for r in reasons if r.startswith("tag:")]
        assert tag_reasons == ["tag:alpha", "tag:mango", "tag:zebra"]

    def test_mixed_prefixes(self, tracker: GoalTracker):
        # Note: _tokenize drops <4-char words, so the tag needs ≥4 chars
        # AND must appear as a standalone token in the description.
        goal = _mk_goal(
            gid="mixed",
            keywords=("foo",),
            path_patterns=("backend/",),
            tags=("reliability",),
        )
        tracker.add_goal(goal)
        matches = tracker.find_relevant_with_reasons(
            description="foo involves reliability work",
            target_files=("backend/x.py",),
        )
        assert len(matches) == 1
        _g, _s, reasons = matches[0]
        prefixes = {r.split(":", 1)[0] for r in reasons}
        assert prefixes == {"kw", "path", "tag"}

    def test_inactive_goal_returns_no_reasons(self, tracker: GoalTracker):
        goal = _mk_goal(
            gid="paused-g",
            keywords=("alpha",),
            status=GoalStatus.PAUSED,
        )
        tracker.add_goal(goal)
        matches = tracker.find_relevant_with_reasons(description="alpha path")
        assert matches == []

    def test_below_threshold_drops_reasons(self, tracker: GoalTracker):
        # If nothing matches, caller sees (0.0, ()) — no reason residue.
        goal = _mk_goal(
            gid="sad",
            keywords=("zzz",),
            path_patterns=(),
            tags=(),
        )
        tracker.add_goal(goal)
        matches = tracker.find_relevant_with_reasons(description="unrelated text")
        assert matches == []


class TestGoalAlignmentMatches:
    def test_alignment_context_populates_matches(self, tracker: GoalTracker):
        tracker.add_goal(
            _mk_goal(gid="g1", keywords=("alpha",), path_patterns=(), tags=())
        )
        tracker.add_goal(
            _mk_goal(gid="g2", keywords=("alpha",), path_patterns=(), tags=())
        )
        align = tracker.alignment_context("alpha process")
        assert isinstance(align, GoalAlignment)
        assert align.matched_count == 2
        assert len(align.matches) == 2
        for entry in align.matches:
            assert entry.kind == "direct"
            assert entry.score > 0
            assert any(r.startswith("kw:") for r in entry.reasons)

    def test_as_evidence_exposes_goal_matches(self, tracker: GoalTracker):
        tracker.add_goal(
            _mk_goal(gid="g1", keywords=("alpha",), path_patterns=(), tags=())
        )
        evidence = tracker.alignment_context("alpha").as_evidence()
        assert "goal_matches" in evidence
        matches = evidence["goal_matches"]
        assert isinstance(matches, list)
        assert len(matches) == 1
        assert matches[0]["goal_id"] == "g1"
        assert matches[0]["kind"] == "direct"
        # Legacy numeric fields preserved for backward compat.
        assert "goal_alignment_boost" in evidence
        assert "goal_relevance_score" in evidence
        assert "goal_top_goal_id" in evidence

    def test_no_match_evidence_has_empty_list(self, tracker: GoalTracker):
        tracker.add_goal(
            _mk_goal(gid="g1", keywords=("alpha",), path_patterns=(), tags=())
        )
        evidence = tracker.alignment_context("nothing here").as_evidence()
        assert evidence["goal_matches"] == []
        assert evidence["goal_top_goal_id"] == ""


class TestDescendantPropagation:
    def test_depth_1_credit_is_half(self, tracker: GoalTracker):
        # Parent + child; child matches directly → parent gets score * 0.5.
        tracker.add_goal(
            _mk_goal(gid="parent", keywords=("parent-only-kw",))
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="child",
                description="child",
                keywords=("uniquekw",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="uniquekw here")
        directs = [e for e in entries if e.kind == "direct"]
        ancestors = [e for e in entries if e.kind == "ancestor"]
        assert len(directs) == 1
        assert directs[0].goal_id == "child"
        assert len(ancestors) == 1
        assert ancestors[0].goal_id == "parent"
        # 0.5**1 = 0.5 exactly.
        assert abs(ancestors[0].score - directs[0].score * 0.5) < 1e-3
        assert ancestors[0].source_goal_id == "child"
        assert "descendant:child" in ancestors[0].reasons

    def test_depth_2_credit_is_quarter(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(gid="grand", keywords=("gonly",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="mid",
                description="m",
                keywords=(),
                parent_id="grand",
            )
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="leaf",
                description="l",
                keywords=("uniqleaf",),
                parent_id="mid",
            )
        )
        entries = tracker.compute_activity_entries(description="uniqleaf")
        by_id = {e.goal_id: e for e in entries}
        assert "leaf" in by_id and by_id["leaf"].kind == "direct"
        assert "mid" in by_id and by_id["mid"].kind == "ancestor"
        assert "grand" in by_id and by_id["grand"].kind == "ancestor"
        direct_score = by_id["leaf"].score
        # mid = direct * 0.5, grand = direct * 0.25
        assert abs(by_id["mid"].score - direct_score * 0.5) < 1e-3
        assert abs(by_id["grand"].score - direct_score * 0.25) < 1e-3

    def test_additive_accumulation_from_two_children(self, tracker: GoalTracker):
        # Two sibling children both match directly; their shared parent
        # accumulates both credits additively.
        tracker.add_goal(_mk_goal(gid="parent", keywords=("pnope",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="child1",
                description="c1",
                keywords=("alpha",),
                parent_id="parent",
            )
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="child2",
                description="c2",
                keywords=("beta",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="alpha beta")
        by_id = {e.goal_id: e for e in entries}
        assert by_id["child1"].kind == "direct"
        assert by_id["child2"].kind == "direct"
        expected_parent = (by_id["child1"].score + by_id["child2"].score) * 0.5
        assert abs(by_id["parent"].score - expected_parent) < 1e-3
        # Both descendant reasons recorded.
        parent_reasons = set(by_id["parent"].reasons)
        assert "descendant:child1" in parent_reasons
        assert "descendant:child2" in parent_reasons

    def test_direct_match_beats_ancestor_credit(self, tracker: GoalTracker):
        # A goal that is ALSO an ancestor of a direct match should still
        # appear as direct (direct wins the collision).
        tracker.add_goal(
            _mk_goal(gid="parent", keywords=("bothkw",))
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="child",
                description="c",
                keywords=("bothkw",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="bothkw")
        kinds = {e.goal_id: e.kind for e in entries}
        assert kinds.get("parent") == "direct"
        assert kinds.get("child") == "direct"
        # No ancestor credit row for "parent".
        ancestor_rows = [e for e in entries if e.kind == "ancestor"]
        assert all(e.goal_id != "parent" for e in ancestor_rows)

    def test_decay_zero_disables_propagation(
        self, tracker: GoalTracker, monkeypatch
    ):
        monkeypatch.setenv("JARVIS_GOAL_DESCENDANT_DECAY", "0")
        tracker.add_goal(_mk_goal(gid="parent", keywords=("pnope",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="child",
                description="c",
                keywords=("uniqkw",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="uniqkw")
        assert all(e.kind == "direct" for e in entries)

    def test_no_match_returns_empty(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(gid="g1", keywords=("xyz",)))
        entries = tracker.compute_activity_entries(description="completely unrelated")
        assert entries == []


class TestSiblingBump:
    def test_disabled_by_default(self, tracker: GoalTracker):
        tracker.add_goal(_mk_goal(gid="parent", keywords=("p",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="c1",
                description="c1",
                keywords=("uniqkw",),
                parent_id="parent",
            )
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="c2",
                description="c2",
                keywords=("nothere",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="uniqkw")
        assert all(e.kind != "sibling" for e in entries)

    def test_enabled_emits_sibling_entry(
        self, tracker: GoalTracker, monkeypatch
    ):
        monkeypatch.setenv("JARVIS_GOAL_SIBLING_BUMP_ENABLED", "true")
        monkeypatch.setenv("JARVIS_GOAL_SIBLING_BUMP_AMOUNT", "0.25")
        tracker.add_goal(_mk_goal(gid="parent", keywords=("pnope",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="c1",
                description="c1",
                keywords=("uniqkw",),
                parent_id="parent",
            )
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="c2",
                description="c2",
                keywords=("nothere",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="uniqkw")
        siblings = [e for e in entries if e.kind == "sibling"]
        assert len(siblings) == 1
        assert siblings[0].goal_id == "c2"
        assert siblings[0].source_goal_id == "c1"
        assert siblings[0].score == 0.25
        assert siblings[0].reasons == ("sibling:c1",)

    def test_direct_excluded_from_sibling(
        self, tracker: GoalTracker, monkeypatch
    ):
        monkeypatch.setenv("JARVIS_GOAL_SIBLING_BUMP_ENABLED", "true")
        tracker.add_goal(_mk_goal(gid="parent", keywords=("pnope",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="c1",
                description="c1",
                keywords=("both",),
                parent_id="parent",
            )
        )
        tracker.add_goal(
            ActiveGoal(
                goal_id="c2",
                description="c2",
                keywords=("both",),
                parent_id="parent",
            )
        )
        entries = tracker.compute_activity_entries(description="both")
        # Both children match directly — neither should get a sibling row.
        assert all(e.kind != "sibling" for e in entries)

    def test_ancestor_excluded_from_sibling(
        self, tracker: GoalTracker, monkeypatch
    ):
        # A goal that is ancestor-credited must NOT also be sibling-credited.
        monkeypatch.setenv("JARVIS_GOAL_SIBLING_BUMP_ENABLED", "true")
        tracker.add_goal(_mk_goal(gid="root", keywords=("rnope",)))
        tracker.add_goal(
            ActiveGoal(
                goal_id="b1",
                description="b1",
                keywords=("bnope",),
                parent_id="root",
            )
        )
        # b2 is both a sibling of b1 and a potential sibling bump target
        # when b1 matched — but b1 doesn't match here; b2 matches directly
        # and b1 becomes ancestor via its own branch.
        tracker.add_goal(
            ActiveGoal(
                goal_id="b2",
                description="b2",
                keywords=("uniqkw",),
                parent_id="root",
            )
        )
        entries = tracker.compute_activity_entries(description="uniqkw")
        # b1 is sibling of b2's direct match — gets sibling row.
        siblings = [e for e in entries if e.kind == "sibling"]
        sibling_ids = {e.goal_id for e in siblings}
        # root is not a sibling (it's the parent of b2), so excluded.
        assert "root" not in sibling_ids


class TestGoalActivityLedger:
    def test_append_and_read_single(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        n = ledger.append(
            session_id="s1",
            op_id="op1",
            entries=[
                GoalAlignmentEntry(
                    goal_id="g1",
                    score=5.0,
                    reasons=("kw:alpha",),
                    kind="direct",
                )
            ],
        )
        assert n == 1
        rows = ledger.read(session_id="s1")
        assert len(rows) == 1
        assert rows[0]["op_id"] == "op1"
        assert rows[0]["goal_id"] == "g1"
        assert rows[0]["kind"] == "direct"
        assert rows[0]["reasons"] == ["kw:alpha"]

    def test_empty_entries_write_zero_match_marker(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        n = ledger.append(session_id="s1", op_id="op1", entries=[])
        assert n == 1
        rows = ledger.read(session_id="s1")
        assert len(rows) == 1
        assert rows[0].get("zero_match") is True
        assert "goal_id" not in rows[0]

    def test_append_rejects_blank_ids(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        assert ledger.append(session_id="", op_id="op1", entries=[]) == 0
        assert ledger.append(session_id="s1", op_id="", entries=[]) == 0
        assert ledger.read() == []

    def test_read_filters_by_session(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        ledger.append(
            session_id="s1", op_id="op1",
            entries=[GoalAlignmentEntry(goal_id="g1", score=1.0)],
        )
        ledger.append(
            session_id="s2", op_id="op2",
            entries=[GoalAlignmentEntry(goal_id="g2", score=2.0)],
        )
        rows_s1 = ledger.read(session_id="s1")
        rows_s2 = ledger.read(session_id="s2")
        assert len(rows_s1) == 1 and rows_s1[0]["op_id"] == "op1"
        assert len(rows_s2) == 1 and rows_s2[0]["op_id"] == "op2"

    def test_read_filters_by_op_and_goal(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        ledger.append(
            session_id="s1", op_id="opA",
            entries=[
                GoalAlignmentEntry(goal_id="g1", score=1.0),
                GoalAlignmentEntry(goal_id="g2", score=2.0),
            ],
        )
        ledger.append(
            session_id="s1", op_id="opB",
            entries=[GoalAlignmentEntry(goal_id="g1", score=3.0)],
        )
        op_a = ledger.read(session_id="s1", op_id="opA")
        assert len(op_a) == 2
        g1_rows = ledger.read(session_id="s1", goal_id="g1")
        assert len(g1_rows) == 2
        assert all(r["goal_id"] == "g1" for r in g1_rows)

    def test_read_limit_trims_end(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        for i in range(5):
            ledger.append(
                session_id="s1", op_id=f"op{i}",
                entries=[GoalAlignmentEntry(goal_id=f"g{i}", score=float(i))],
            )
        rows = ledger.read(session_id="s1", limit=2)
        assert len(rows) == 2
        # limit trims from the end → the 2 newest
        assert [r["op_id"] for r in rows] == ["op3", "op4"]

    def test_missing_file_returns_empty(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        assert ledger.read() == []
        assert ledger.read(session_id="nope") == []

    def test_malformed_lines_skipped(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        # Seed a good row, then corrupt the file with bad JSON.
        ledger.append(
            session_id="s1", op_id="op1",
            entries=[GoalAlignmentEntry(goal_id="g1", score=1.0)],
        )
        with ledger.path.open("a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
            fh.write("\n")  # blank line
            fh.write('{"session_id": "s1", "op_id": "op2", "goal_id": "g2", '
                     '"score": 2.0, "kind": "direct"}\n')
        rows = ledger.read(session_id="s1")
        assert len(rows) == 2
        assert {r["op_id"] for r in rows} == {"op1", "op2"}

    def test_append_only_never_rewrites(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        ledger.append(
            session_id="s1", op_id="op1",
            entries=[GoalAlignmentEntry(goal_id="g1", score=1.0)],
        )
        first_text = ledger.path.read_text()
        ledger.append(
            session_id="s1", op_id="op2",
            entries=[GoalAlignmentEntry(goal_id="g2", score=2.0)],
        )
        second_text = ledger.path.read_text()
        # The first text is a prefix of the second — append-only.
        assert second_text.startswith(first_text)
        assert len(second_text) > len(first_text)


class TestAntiDriftDetector:
    def _ledger_with_ops(
        self,
        tmp_root: Path,
        session: str,
        ops: list,
    ) -> GoalActivityLedger:
        """Append a list of (op_id, entries) tuples to a fresh ledger."""
        ledger = GoalActivityLedger(tmp_root)
        for op_id, entries in ops:
            ledger.append(session_id=session, op_id=op_id, entries=entries)
        return ledger

    def test_insufficient_data_below_threshold(self, tmp_root: Path):
        # Default threshold is 5 — give it 4.
        ops = [
            (f"op{i}", [GoalAlignmentEntry(goal_id="g", score=1.0)])
            for i in range(4)
        ]
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.status == "insufficient_data"
        assert summary.ratio is None
        assert summary.total_ops == 4
        assert summary.warning is False

    def test_exactly_threshold_meets(self, tmp_root: Path):
        ops = [
            (f"op{i}", [GoalAlignmentEntry(goal_id="g", score=1.0)])
            for i in range(5)
        ]
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.threshold_met is True
        assert summary.status == "ok"
        assert summary.total_ops == 5
        assert summary.drifted_ops == 0
        assert summary.ratio == 0.0

    def test_all_drift_triggers_warning(self, tmp_root: Path):
        # 6 zero-match marker rows → ratio 1.0 > 0.30 default.
        ops = [(f"op{i}", []) for i in range(6)]
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.threshold_met is True
        assert summary.warning is True
        assert summary.status == "drift_warning"
        assert summary.drifted_ops == 6
        assert summary.total_ops == 6
        assert summary.ratio == 1.0

    def test_boundary_ratio_exactly_threshold_not_warn(self, tmp_root: Path):
        # 10 ops, 3 drifted → ratio 0.30 → NOT warn (> is strict).
        ops = (
            [(f"op{i}", [GoalAlignmentEntry(goal_id="g", score=1.0)]) for i in range(7)]
            + [(f"drift{i}", []) for i in range(3)]
        )
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.threshold_met is True
        assert summary.total_ops == 10
        assert summary.drifted_ops == 3
        assert summary.ratio is not None
        assert abs(summary.ratio - 0.30) < 1e-9
        assert summary.warning is False  # 0.30 is not > 0.30
        assert summary.status == "ok"

    def test_boundary_ratio_just_over_threshold_warns(self, tmp_root: Path):
        # 10 ops, 4 drifted → ratio 0.40 → warns.
        ops = (
            [(f"op{i}", [GoalAlignmentEntry(goal_id="g", score=1.0)]) for i in range(6)]
            + [(f"drift{i}", []) for i in range(4)]
        )
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.warning is True
        assert summary.drifted_ops == 4
        assert summary.total_ops == 10

    def test_ancestor_only_still_counts_as_drift(self, tmp_root: Path):
        # Important: ancestor credit does NOT save an op from drift —
        # only direct matches count for the numerator.
        ops = [
            (
                f"op{i}",
                [
                    GoalAlignmentEntry(
                        goal_id="g",
                        score=1.0,
                        kind="ancestor",
                        source_goal_id="child",
                    )
                ],
            )
            for i in range(6)
        ]
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.drifted_ops == 6
        assert summary.warning is True

    def test_mixed_direct_and_drift(self, tmp_root: Path):
        ops = (
            [
                (f"hit{i}", [GoalAlignmentEntry(goal_id="g", score=1.0)])
                for i in range(8)
            ]
            + [(f"miss{i}", []) for i in range(2)]
        )
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.total_ops == 10
        assert summary.drifted_ops == 2
        assert summary.ratio is not None
        assert abs(summary.ratio - 0.20) < 1e-9
        assert summary.warning is False

    def test_zero_score_direct_still_counts_as_drift(self, tmp_root: Path):
        # A direct row with score==0 should NOT rescue an op from drift.
        ops = [
            (
                f"op{i}",
                [GoalAlignmentEntry(goal_id="g", score=0.0, kind="direct")],
            )
            for i in range(6)
        ]
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1")
        assert summary.drifted_ops == 6

    def test_session_isolation(self, tmp_root: Path):
        ledger = GoalActivityLedger(tmp_root)
        # 5 good ops in session s1
        for i in range(5):
            ledger.append(
                session_id="s1", op_id=f"op{i}",
                entries=[GoalAlignmentEntry(goal_id="g", score=1.0)],
            )
        # 5 zero-match ops in session s2
        for i in range(5):
            ledger.append(session_id="s2", op_id=f"op{i}", entries=[])
        s1 = ledger.compute_drift(session_id="s1")
        s2 = ledger.compute_drift(session_id="s2")
        assert s1.drifted_ops == 0 and s1.total_ops == 5
        assert s2.drifted_ops == 5 and s2.total_ops == 5

    def test_custom_threshold_overrides(self, tmp_root: Path):
        # 3 ops — below default 5, but caller overrides to min_threshold=3.
        ops = [
            (f"op{i}", [GoalAlignmentEntry(goal_id="g", score=1.0)])
            for i in range(3)
        ]
        ledger = self._ledger_with_ops(tmp_root, "s1", ops)
        summary = ledger.compute_drift(session_id="s1", min_threshold=3)
        assert summary.threshold_met is True
        assert summary.status == "ok"

    def test_drift_summary_to_dict_shape(self):
        s = SessionDriftSummary(
            total_ops=10, drifted_ops=4, ratio=0.4,
            threshold_met=True, warning=True,
        )
        d = s.to_dict()
        assert d["status"] == "drift_warning"
        assert d["total_ops"] == 10
        assert d["drifted_ops"] == 4
        assert d["ratio"] == 0.4
        assert d["warning"] is True

    def test_drift_summary_insufficient_to_dict(self):
        s = SessionDriftSummary()
        d = s.to_dict()
        assert d["status"] == "insufficient_data"
        assert d["ratio"] is None
        assert d["threshold_met"] is False


class TestSessionIdModuleGlobal:
    def test_set_and_get(self):
        set_active_session_id("s-test-1")
        assert get_active_session_id() == "s-test-1"
        set_active_session_id(None)
        assert get_active_session_id() is None

    def test_empty_string_clears(self):
        set_active_session_id("s1")
        set_active_session_id("")
        assert get_active_session_id() is None
