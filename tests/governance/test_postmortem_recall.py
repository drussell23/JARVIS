"""Priority #2 Slice 1 — PostmortemRecall primitive regression tests.

Coverage:

  * **Master flag** — asymmetric env semantics (truthy/falsy/
    whitespace).
  * **Closed-taxonomy pins** — RecallOutcome 5-value, RelevanceLevel
    4-value; any silent extension caught.
  * **Env knob clamps** — top_k / top_k_ceiling / max_age_days /
    halflife_days / threshold-string-parsing all enforce floor +
    ceiling with garbage-input fallback to default.
  * **Field-parity with FailureEpisode** — every
    ``episodic_memory.FailureEpisode`` field is present in
    ``PostmortemRecord`` with matching type. Load-bearing
    zero-duplication contract pinned by AST walk.
  * **SemanticIndex parity** — `_recency_weight` formula byte-
    equivalent to `semantic_index._recency_weight` across age ×
    halflife sweep. Mirrors Priority #1 Slice 1's discipline so the
    module stays pure-stdlib.
  * **Schema integrity** — frozen dataclasses + to_dict/from_dict
    round-trip + schema-mismatch tolerance + schema_version stable.
  * **compute_relevance** — full closed-taxonomy decision tree
    (parametrized over all 6 input shapes × 4 outcome levels).
  * **recall_postmortems** — full outcome matrix
    (DISABLED/EMPTY_INDEX/MISS/HIT/FAILED), recency-weighted
    ranking, max_age_days filter, top_k + top_k_ceiling clamps,
    threshold filter.
  * **Defensive contract** — every public function NEVER raises;
    garbage inputs map to closed-taxonomy values.
  * **Authority invariants** — AST-pinned: stdlib only, no
    governance imports, no `episodic_memory` import (avoid
    coupling), no exec/eval/compile, no async, no mutation tools.
"""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.postmortem_recall import (
    POSTMORTEM_RECALL_SCHEMA_VERSION,
    PostmortemRecord,
    RecallOutcome,
    RecallTarget,
    RecallVerdict,
    RelevanceLevel,
    compute_relevance,
    postmortem_recall_enabled,
    recall_halflife_days,
    recall_max_age_days,
    recall_postmortems,
    recall_relevance_threshold,
    recall_top_k,
    recall_top_k_ceiling,
)
from backend.core.ouroboros.governance.verification.postmortem_recall import (  # noqa: E501
    _RELEVANCE_RANK,
    _recency_weight,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(
    *,
    file_path: str = "auth.py",
    symbol_name: str = "login",
    failure_class: str = "test",
    ast_signature: str = "",
    timestamp: float = 0.0,
    error_summary: str = "AssertionError on line 42",
    session_id: str = "s1",
    op_id: str = "op-1",
) -> PostmortemRecord:
    if timestamp == 0.0:
        timestamp = time.time()
    return PostmortemRecord(
        file_path=file_path,
        symbol_name=symbol_name,
        failure_class=failure_class,
        ast_signature=ast_signature,
        timestamp=timestamp,
        error_summary=error_summary,
        session_id=session_id,
        op_id=op_id,
        attempt=1,
        specific_errors=("err1", "err2"),
        line_numbers=(42, 43),
    )


# ---------------------------------------------------------------------------
# 1. Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_true_post_graduation(self):
        # Slice 5 graduated 2026-05-01 — master default-true
        # because PostmortemRecall is read-only (zero LLM cost).
        os.environ.pop("JARVIS_POSTMORTEM_RECALL_ENABLED", None)
        assert postmortem_recall_enabled() is True

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE", "Yes"],
    )
    def test_truthy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_ENABLED": v},
        ):
            assert postmortem_recall_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off"],
    )
    def test_falsy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_ENABLED": v},
        ):
            assert postmortem_recall_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, v):
        # Whitespace = unset = current default = True post-Slice-5
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_ENABLED": v},
        ):
            assert postmortem_recall_enabled() is True


# ---------------------------------------------------------------------------
# 2. Env knob clamps
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_top_k_default(self):
        os.environ.pop("JARVIS_POSTMORTEM_RECALL_TOP_K", None)
        assert recall_top_k() == 3

    def test_top_k_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_TOP_K": "0"},
        ):
            assert recall_top_k() == 1

    def test_top_k_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_TOP_K": "999"},
        ):
            assert recall_top_k() == 10

    def test_top_k_ceiling_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING", None,
        )
        assert recall_top_k_ceiling() == 10

    def test_top_k_ceiling_floor_clamp(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING": "1"},
        ):
            assert recall_top_k_ceiling() == 3

    def test_top_k_ceiling_ceiling_clamp(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING": "9999"},
        ):
            assert recall_top_k_ceiling() == 30

    def test_max_age_days_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS", None,
        )
        assert recall_max_age_days() == 30.0

    def test_max_age_days_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS": "0.001"},
        ):
            assert recall_max_age_days() == 1.0

    def test_max_age_days_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS": "9999"},
        ):
            assert recall_max_age_days() == 365.0

    def test_halflife_days_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_HALFLIFE_DAYS", None,
        )
        assert recall_halflife_days() == 14.0

    def test_halflife_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_HALFLIFE_DAYS": "0.001"},
        ):
            assert recall_halflife_days() == 0.5

    def test_halflife_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_HALFLIFE_DAYS": "9999"},
        ):
            assert recall_halflife_days() == 90.0

    def test_threshold_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD", None,
        )
        assert recall_relevance_threshold() is RelevanceLevel.MEDIUM

    @pytest.mark.parametrize(
        "v,expected",
        [
            ("low", RelevanceLevel.LOW),
            ("medium", RelevanceLevel.MEDIUM),
            ("high", RelevanceLevel.HIGH),
            ("LOW", RelevanceLevel.LOW),
            ("HIGH", RelevanceLevel.HIGH),
        ],
    )
    def test_threshold_parsing(self, v, expected):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD":
                    v,
            },
        ):
            assert recall_relevance_threshold() is expected

    def test_threshold_garbage_falls_back(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD":
                    "garbage",
            },
        ):
            assert (
                recall_relevance_threshold()
                is RelevanceLevel.MEDIUM
            )

    def test_threshold_none_not_valid(self):
        # NONE would match anything — defensive: rejected
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD":
                    "none",
            },
        ):
            assert (
                recall_relevance_threshold()
                is RelevanceLevel.MEDIUM
            )

    def test_garbage_int_falls_back(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_TOP_K": "not-int"},
        ):
            assert recall_top_k() == 3


# ---------------------------------------------------------------------------
# 3. Closed taxonomies
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_recall_outcome_5_values(self):
        assert len(list(RecallOutcome)) == 5

    def test_recall_outcome_values(self):
        expected = {
            "hit", "miss", "empty_index", "disabled", "failed",
        }
        assert {o.value for o in RecallOutcome} == expected

    def test_relevance_level_4_values(self):
        assert len(list(RelevanceLevel)) == 4

    def test_relevance_level_values(self):
        expected = {"none", "low", "medium", "high"}
        assert {r.value for r in RelevanceLevel} == expected

    def test_relevance_rank_monotonic(self):
        # NONE < LOW < MEDIUM < HIGH
        assert (
            _RELEVANCE_RANK[RelevanceLevel.NONE]
            < _RELEVANCE_RANK[RelevanceLevel.LOW]
            < _RELEVANCE_RANK[RelevanceLevel.MEDIUM]
            < _RELEVANCE_RANK[RelevanceLevel.HIGH]
        )


# ---------------------------------------------------------------------------
# 4. Field-parity with FailureEpisode (load-bearing zero-duplication contract)
# ---------------------------------------------------------------------------


class TestFailureEpisodeFieldParity:
    """Every FailureEpisode field MUST be present in
    PostmortemRecord with matching type. Verified by AST walk
    rather than runtime import to avoid coupling — Slice 1 stays
    pure-stdlib (zero governance imports). This is the load-
    bearing zero-duplication contract."""

    def _episodic_memory_source(self) -> str:
        path = (
            Path(__file__).resolve().parents[2]
            / "backend" / "core" / "ouroboros" / "governance"
            / "episodic_memory.py"
        )
        return path.read_text(encoding="utf-8")

    def _postmortem_recall_source(self) -> str:
        path = (
            Path(__file__).resolve().parents[2]
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "postmortem_recall.py"
        )
        return path.read_text(encoding="utf-8")

    def _extract_dataclass_fields(
        self, source: str, classname: str,
    ) -> dict:
        """Walk AST; find the dataclass definition; return
        {field_name: type_annotation_str}."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == classname:
                fields = {}
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(
                        stmt.target, ast.Name,
                    ):
                        # Render the annotation as source for
                        # type-name comparison (e.g., "str", "int",
                        # "Tuple[str, ...]")
                        try:
                            ann_src = ast.unparse(stmt.annotation)
                        except Exception:
                            ann_src = ""
                        fields[stmt.target.id] = ann_src
                return fields
        return {}

    def test_failure_episode_fields_present_in_postmortem_record(self):
        episodic_source = self._episodic_memory_source()
        recall_source = self._postmortem_recall_source()

        episode_fields = self._extract_dataclass_fields(
            episodic_source, "FailureEpisode",
        )
        record_fields = self._extract_dataclass_fields(
            recall_source, "PostmortemRecord",
        )

        # Sanity: parser found something
        assert "file_path" in episode_fields, (
            "AST extractor failed to find FailureEpisode fields"
        )
        assert "file_path" in record_fields, (
            "AST extractor failed to find PostmortemRecord fields"
        )

        # Every FailureEpisode field MUST be in PostmortemRecord
        # with matching annotation
        for field_name, annotation in episode_fields.items():
            assert field_name in record_fields, (
                f"FailureEpisode field {field_name!r} missing "
                f"from PostmortemRecord — zero-duplication "
                f"contract broken"
            )
            assert record_fields[field_name] == annotation, (
                f"Field {field_name!r} type drift: "
                f"FailureEpisode={annotation!r} vs "
                f"PostmortemRecord={record_fields[field_name]!r}"
            )

    def test_postmortem_record_has_cross_session_extensions(self):
        recall_source = self._postmortem_recall_source()
        record_fields = self._extract_dataclass_fields(
            recall_source, "PostmortemRecord",
        )
        # These are the cross-session additions
        for ext in (
            "session_id",
            "op_id",
            "symbol_name",
            "ast_signature",
            "failure_phase",
            "failure_reason",
            "schema_version",
        ):
            assert ext in record_fields, (
                f"Cross-session extension {ext!r} missing from "
                f"PostmortemRecord"
            )


# ---------------------------------------------------------------------------
# 5. SemanticIndex byte-parity
# ---------------------------------------------------------------------------


class TestSemanticIndexParity:
    """Mirrors Priority #1 Slice 1's discipline: the
    `_recency_weight` formula must byte-match
    `semantic_index._recency_weight`. Re-implemented here so this
    module stays pure-stdlib (zero governance imports — strongest
    authority invariant)."""

    @pytest.mark.parametrize(
        "age_seconds",
        [
            0.0, 60.0, 3600.0, 86400.0,
            86400.0 * 3, 86400.0 * 7, 86400.0 * 14,
            86400.0 * 30, 86400.0 * 60,
        ],
    )
    @pytest.mark.parametrize("halflife", [3.0, 7.0, 14.0, 30.0])
    def test_recency_weight_byte_parity(
        self, age_seconds, halflife,
    ):
        from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501
            _recency_weight as si_weight,
        )
        ours = _recency_weight(age_seconds, halflife)
        theirs = si_weight(age_seconds, halflife)
        assert ours == theirs, (
            f"diverged at age={age_seconds:.0f}s "
            f"hl={halflife}d: ours={ours} theirs={theirs}"
        )

    def test_negative_age_returns_one(self):
        assert _recency_weight(-1.0, 14.0) == 1.0

    def test_zero_halflife_returns_one(self):
        assert _recency_weight(86400.0, 0.0) == 1.0


# ---------------------------------------------------------------------------
# 6. Schema integrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    def test_postmortem_record_frozen(self):
        r = _make_record()
        with pytest.raises((AttributeError, Exception)):
            r.file_path = "hax"  # type: ignore[misc]

    def test_recall_target_frozen(self):
        t = RecallTarget(target_files=frozenset({"x"}))
        with pytest.raises((AttributeError, Exception)):
            t.target_files = frozenset()  # type: ignore[misc]

    def test_recall_verdict_frozen(self):
        v = RecallVerdict(outcome=RecallOutcome.HIT)
        with pytest.raises((AttributeError, Exception)):
            v.outcome = RecallOutcome.MISS  # type: ignore[misc]

    def test_record_to_dict_round_trip(self):
        r = _make_record()
        d = r.to_dict()
        recovered = PostmortemRecord.from_dict(d)
        assert recovered is not None
        assert recovered.file_path == r.file_path
        assert recovered.failure_class == r.failure_class
        assert recovered.specific_errors == r.specific_errors
        assert recovered.line_numbers == r.line_numbers
        assert recovered.session_id == r.session_id

    def test_record_from_dict_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong.99"}
        assert PostmortemRecord.from_dict(d) is None

    def test_record_from_dict_malformed_returns_none(self):
        d = {
            "schema_version": POSTMORTEM_RECALL_SCHEMA_VERSION,
            "attempt": "not-an-int",
        }
        # Coerced via int() — "not-an-int" raises ValueError
        # which is caught defensively
        assert PostmortemRecord.from_dict(d) is None

    def test_schema_version_stable(self):
        assert (
            POSTMORTEM_RECALL_SCHEMA_VERSION
            == "postmortem_recall.1"
        )

    def test_recall_target_to_dict_shape(self):
        t = RecallTarget(
            target_files=frozenset({"a.py", "b.py"}),
            target_symbols=frozenset({"foo"}),
            target_failure_class="test",
            max_age_days=15.0,
        )
        d = t.to_dict()
        assert d["target_files"] == ["a.py", "b.py"]
        assert d["target_symbols"] == ["foo"]
        assert d["target_failure_class"] == "test"
        assert d["max_age_days"] == 15.0

    def test_recall_verdict_has_recall_helper(self):
        hit = RecallVerdict(outcome=RecallOutcome.HIT)
        assert hit.has_recall() is True
        miss = RecallVerdict(outcome=RecallOutcome.MISS)
        assert miss.has_recall() is False


# ---------------------------------------------------------------------------
# 7. compute_relevance — full closed-taxonomy decision tree
# ---------------------------------------------------------------------------


class TestComputeRelevance:
    def test_failure_class_mismatch_returns_none(self):
        r = _make_record(failure_class="test")
        t = RecallTarget(target_failure_class="build")
        assert compute_relevance(r, t) is RelevanceLevel.NONE

    def test_ast_signature_match_high(self):
        r = _make_record(
            ast_signature="abc",
            file_path="other.py",
            symbol_name="bar",
        )
        t = RecallTarget(target_ast_signature="abc")
        assert compute_relevance(r, t) is RelevanceLevel.HIGH

    def test_ast_signature_mismatch_falls_through(self):
        r = _make_record(
            ast_signature="abc",
            file_path="auth.py",
            symbol_name="login",
        )
        t = RecallTarget(
            target_ast_signature="different",
            target_files=frozenset({"auth.py"}),
            target_symbols=frozenset({"login"}),
        )
        # AST mismatch but file+symbol match → still HIGH
        assert compute_relevance(r, t) is RelevanceLevel.HIGH

    def test_file_and_symbol_match_high(self):
        r = _make_record(
            file_path="auth.py", symbol_name="login",
        )
        t = RecallTarget(
            target_files=frozenset({"auth.py"}),
            target_symbols=frozenset({"login"}),
        )
        assert compute_relevance(r, t) is RelevanceLevel.HIGH

    def test_file_only_match_medium(self):
        r = _make_record(
            file_path="auth.py", symbol_name="login",
        )
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        assert compute_relevance(r, t) is RelevanceLevel.MEDIUM

    def test_symbol_only_match_medium(self):
        r = _make_record(
            file_path="auth.py", symbol_name="login",
        )
        t = RecallTarget(target_symbols=frozenset({"login"}))
        assert compute_relevance(r, t) is RelevanceLevel.MEDIUM

    def test_failure_class_only_low(self):
        r = _make_record(failure_class="test")
        t = RecallTarget(target_failure_class="test")
        assert compute_relevance(r, t) is RelevanceLevel.LOW

    def test_failure_class_match_plus_file_medium(self):
        r = _make_record(
            failure_class="test", file_path="auth.py",
        )
        t = RecallTarget(
            target_failure_class="test",
            target_files=frozenset({"auth.py"}),
        )
        assert compute_relevance(r, t) is RelevanceLevel.MEDIUM

    def test_no_overlap_returns_none(self):
        r = _make_record()
        t = RecallTarget()
        assert compute_relevance(r, t) is RelevanceLevel.NONE

    def test_no_overlap_with_unrelated_failure_class(self):
        r = _make_record(failure_class="build")
        t = RecallTarget(target_failure_class="test")
        assert compute_relevance(r, t) is RelevanceLevel.NONE

    def test_record_with_empty_symbol_no_symbol_match(self):
        r = _make_record(symbol_name="")
        t = RecallTarget(
            target_symbols=frozenset({"login"}),
            target_files=frozenset({"auth.py"}),
        )
        # Symbol empty → no symbol match; only file match → MEDIUM
        assert compute_relevance(r, t) is RelevanceLevel.MEDIUM

    def test_garbage_record_returns_none(self):
        t = RecallTarget(target_files=frozenset({"a.py"}))
        assert (
            compute_relevance("not a record", t)  # type: ignore[arg-type]
            is RelevanceLevel.NONE
        )

    def test_garbage_target_returns_none(self):
        r = _make_record()
        assert (
            compute_relevance(r, "not a target")  # type: ignore[arg-type]
            is RelevanceLevel.NONE
        )


# ---------------------------------------------------------------------------
# 8. recall_postmortems — outcome matrix
# ---------------------------------------------------------------------------


class TestRecallPostmortems:
    def test_disabled_returns_disabled(self):
        r = _make_record()
        v = recall_postmortems([r], RecallTarget(), enabled_override=False)
        assert v.outcome is RecallOutcome.DISABLED

    def test_master_off_returns_disabled(self):
        # Default-true post graduation; explicit false to
        # exercise master-off path
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_ENABLED": "false"},
        ):
            r = _make_record()
            v = recall_postmortems([r], RecallTarget())
            assert v.outcome is RecallOutcome.DISABLED

    def test_empty_records_returns_empty_index(self):
        v = recall_postmortems(
            [], RecallTarget(), enabled_override=True,
        )
        assert v.outcome is RecallOutcome.EMPTY_INDEX

    def test_none_records_returns_empty_index(self):
        v = recall_postmortems(
            None, RecallTarget(), enabled_override=True,  # type: ignore[arg-type]
        )
        assert v.outcome is RecallOutcome.EMPTY_INDEX

    def test_garbage_records_returns_empty_index(self):
        v = recall_postmortems(
            "not iterable",  # type: ignore[arg-type]
            RecallTarget(),
            enabled_override=True,
        )
        assert v.outcome is RecallOutcome.EMPTY_INDEX

    def test_garbage_target_returns_failed(self):
        v = recall_postmortems(
            [_make_record()],
            "not a target",  # type: ignore[arg-type]
            enabled_override=True,
        )
        assert v.outcome is RecallOutcome.FAILED

    def test_below_threshold_returns_miss(self):
        # Default threshold MEDIUM; record has only failure_class
        # match (LOW)
        r = _make_record(failure_class="test")
        t = RecallTarget(target_failure_class="test")
        v = recall_postmortems([r], t, enabled_override=True)
        assert v.outcome is RecallOutcome.MISS
        assert v.total_index_size == 1

    def test_above_threshold_returns_hit(self):
        r = _make_record(file_path="auth.py")
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        v = recall_postmortems([r], t, enabled_override=True)
        assert v.outcome is RecallOutcome.HIT
        assert v.max_relevance is RelevanceLevel.MEDIUM
        assert len(v.records) == 1

    def test_only_postmortem_record_instances_counted(self):
        records = [
            _make_record(file_path="auth.py"),
            "not a record",
            42,
            _make_record(file_path="auth.py"),
        ]
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        v = recall_postmortems(records, t, enabled_override=True)  # type: ignore[arg-type]
        assert v.outcome is RecallOutcome.HIT
        # total_index_size counts only valid records
        assert v.total_index_size == 2

    def test_recency_ranking(self):
        ts = time.time()
        old = _make_record(
            file_path="auth.py", symbol_name="login",
            timestamp=ts - 86400 * 7,
        )
        recent = _make_record(
            file_path="auth.py", symbol_name="login",
            timestamp=ts,
        )
        t = RecallTarget(
            target_files=frozenset({"auth.py"}),
            target_symbols=frozenset({"login"}),
        )
        v = recall_postmortems(
            [old, recent], t,
            enabled_override=True, max_results=2,
            now_ts=ts,
        )
        assert len(v.records) == 2
        # First record should be the recent one (higher score)
        assert v.records[0].timestamp > v.records[1].timestamp

    def test_relevance_ranking_high_outranks_medium(self):
        ts = time.time()
        # HIGH: file+symbol match
        high_rec = _make_record(
            file_path="auth.py", symbol_name="login",
            timestamp=ts,
        )
        # MEDIUM: only file match
        medium_rec = _make_record(
            file_path="auth.py", symbol_name="other",
            timestamp=ts,
        )
        t = RecallTarget(
            target_files=frozenset({"auth.py"}),
            target_symbols=frozenset({"login"}),
        )
        v = recall_postmortems(
            [medium_rec, high_rec], t,
            enabled_override=True, max_results=2, now_ts=ts,
        )
        assert v.records[0].symbol_name == "login"  # HIGH first

    def test_max_age_days_filter(self):
        ts = time.time()
        ancient = _make_record(
            file_path="auth.py",
            timestamp=ts - 86400 * 100,
        )
        recent = _make_record(
            file_path="auth.py", timestamp=ts,
        )
        t = RecallTarget(
            target_files=frozenset({"auth.py"}),
            max_age_days=30.0,
        )
        v = recall_postmortems(
            [ancient, recent], t,
            enabled_override=True, now_ts=ts,
        )
        # Ancient excluded by age filter
        assert len(v.records) == 1
        assert v.records[0].timestamp == ts

    def test_top_k_clamp(self):
        records = [
            _make_record(
                file_path="auth.py", op_id=f"op-{i}",
            )
            for i in range(20)
        ]
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        v = recall_postmortems(
            records, t, enabled_override=True, max_results=5,
        )
        assert len(v.records) == 5

    def test_top_k_ceiling_clamp(self):
        records = [
            _make_record(
                file_path="auth.py", op_id=f"op-{i}",
            )
            for i in range(50)
        ]
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        v = recall_postmortems(
            records, t, enabled_override=True, max_results=999,
        )
        # Caller asked for 999 but ceiling caps at 10 (default)
        assert len(v.records) == 10

    def test_threshold_override_strict(self):
        # MEDIUM record: file match only (different symbol)
        r_med = _make_record(
            file_path="auth.py", symbol_name="other",
        )
        r_high = _make_record(
            file_path="auth.py", symbol_name="login",
        )
        t = RecallTarget(
            target_files=frozenset({"auth.py"}),
            target_symbols=frozenset({"login"}),
        )
        v = recall_postmortems(
            [r_med, r_high], t,
            enabled_override=True,
            threshold=RelevanceLevel.HIGH,
        )
        # Only HIGH-level record passes
        assert len(v.records) == 1
        assert v.records[0].symbol_name == "login"

    def test_max_relevance_reflects_top_record(self):
        r = _make_record(
            file_path="auth.py", symbol_name="login",
        )
        t = RecallTarget(
            target_files=frozenset({"auth.py"}),
            target_symbols=frozenset({"login"}),
        )
        v = recall_postmortems([r], t, enabled_override=True)
        assert v.max_relevance is RelevanceLevel.HIGH

    def test_zero_max_results_clamped_to_one(self):
        r = _make_record(file_path="auth.py")
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        v = recall_postmortems(
            [r], t, enabled_override=True, max_results=0,
        )
        # Floor at 1
        assert len(v.records) == 1


# ---------------------------------------------------------------------------
# 9. Defensive contract — never raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_recall_with_negative_halflife(self):
        r = _make_record(file_path="auth.py")
        t = RecallTarget(target_files=frozenset({"auth.py"}))
        v = recall_postmortems(
            [r], t, enabled_override=True,
            halflife_days_override=-1.0,
        )
        # Negative halflife → recency_weight returns 1.0; still HIT
        assert v.outcome is RecallOutcome.HIT

    def test_age_days_helper_handles_garbage(self):
        # Frozen dataclass; pass garbage timestamp via factory
        r = PostmortemRecord(timestamp=float("nan"))
        # Should not raise
        result = r.age_days()
        assert isinstance(result, float)

    def test_age_days_negative_clamped(self):
        ts = time.time()
        r = _make_record(timestamp=ts + 86400)  # future timestamp
        # max(0.0, ...) clamps negative age
        assert r.age_days(now_ts=ts) == 0.0


# ---------------------------------------------------------------------------
# 10. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "postmortem_recall.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_governance_imports(self, source):
        """Slice 1 is PURE-STDLIB (mirrors Priority #1 Slice 1's
        discipline). Strongest authority invariant."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "backend." not in module, (
                    f"forbidden backend import: {module}"
                )
                assert "governance" not in module, (
                    f"forbidden governance import: {module}"
                )

    def test_no_episodic_memory_import(self, source):
        """Slice 1 is field-parity-pinned with FailureEpisode but
        MUST NOT import episodic_memory (avoid coupling). Parity
        is verified by AST test, not runtime import."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                m = m or ""
                assert "episodic_memory" not in m, (
                    f"forbidden episodic_memory import: {m}"
                )

    def test_no_orchestrator_imports(self, source):
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine", "ast_canonical", "semantic_index",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                m = m or ""
                for f in forbidden:
                    assert f not in m, f"forbidden import: {m}"

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "os.remove", "os.unlink",
            "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source

    def test_no_exec_eval_compile(self, source):
        """Critical safety pin — recall NEVER executes code."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    )

    def test_no_async_functions(self, source):
        """Slice 1 is sync; Slice 3+ may introduce async."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef)

    def test_stdlib_only_imports(self, source):
        """Final pin: every import must be stdlib. Whitelist is
        exhaustive."""
        stdlib_only = {
            "__future__", "enum", "logging", "os", "time",
            "dataclasses", "typing",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                root = m.split(".", 1)[0]
                assert root in stdlib_only, (
                    f"non-stdlib import: {m}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    assert root in stdlib_only, (
                        f"non-stdlib import: {alias.name}"
                    )

    def test_public_api_exported(self, source):
        for name in (
            "PostmortemRecord", "RecallTarget", "RecallVerdict",
            "RecallOutcome", "RelevanceLevel",
            "compute_relevance", "recall_postmortems",
            "postmortem_recall_enabled",
            "recall_top_k", "recall_top_k_ceiling",
            "recall_max_age_days", "recall_halflife_days",
            "recall_relevance_threshold",
            "POSTMORTEM_RECALL_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source, (
                f"public API {name!r} not in __all__"
            )
