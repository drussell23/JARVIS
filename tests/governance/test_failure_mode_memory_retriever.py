"""Upgrade 3 Slice 3 — FailureModeRetriever tests (RAG layer).

Pins the deterministic top-K retrieval contract from PRD §31.4.2
Slice 3 + the chain of bounded scoring primitives (recency,
jaccard, log-scale weight) + diversity dedup (Coherence Auditor
pattern).
"""
from __future__ import annotations

import math
import time

import pytest


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
    )


def _make(
    *,
    sig,
    situation,
    attempt="add_dataclass",
    weight=2,
    age_days=0.0,
    op_id=None,
    now=None,
):
    from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
        FailureModeKind,
        FailureModeRecord,
    )
    if now is None:
        now = time.time()
    return FailureModeRecord(
        signature_hash=str(sig).rjust(64, "0")[:64],
        situation_kind=situation,
        attempted_action_kind=attempt,
        failure_mode_kind=FailureModeKind.MISSING_IMPORT,
        mitigation_summary="check imports",
        observed_at_unix=now - age_days * 86400.0,
        op_id=op_id or f"op-{sig}",
        weight=weight,
    )


# ---------------------------------------------------------------------------
# § 1 — Scoring primitives
# ---------------------------------------------------------------------------


class TestRecencyWeight:
    def test_zero_age_is_one(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight,
        )
        assert _recency_weight(0.0, 14.0) == 1.0

    def test_one_halflife_is_half(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight,
        )
        result = _recency_weight(14.0 * 86400.0, 14.0)
        assert abs(result - 0.5) < 1e-9

    def test_two_halflives_is_quarter(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight,
        )
        result = _recency_weight(28.0 * 86400.0, 14.0)
        assert abs(result - 0.25) < 1e-9

    def test_negative_age_clamps_to_one(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight,
        )
        assert _recency_weight(-100.0, 14.0) == 1.0

    def test_zero_halflife_clamps_to_one(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight,
        )
        assert _recency_weight(86400.0, 0.0) == 1.0


class TestJaccardSimilarity:
    def test_identical_sets(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _jaccard_similarity,
        )
        assert _jaccard_similarity(
            ("a.py", "b.py"), ("a.py", "b.py"),
        ) == 1.0

    def test_disjoint_sets(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _jaccard_similarity,
        )
        assert _jaccard_similarity(
            ("a.py",), ("b.py",),
        ) == 0.0

    def test_partial_overlap(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _jaccard_similarity,
        )
        result = _jaccard_similarity(
            ("a.py", "b.py"), ("b.py", "c.py"),
        )
        assert abs(result - 1.0 / 3.0) < 1e-9

    def test_both_empty_is_one(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _jaccard_similarity,
        )
        assert _jaccard_similarity((), ()) == 1.0

    def test_handles_garbage_iterable(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _jaccard_similarity,
        )
        assert _jaccard_similarity(42, 17) == 0.0  # type: ignore[arg-type]


class TestWeightScore:
    def test_weight_zero_is_zero(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _weight_score,
        )
        assert _weight_score(0) == 0.0

    def test_weight_two_is_above_min(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _weight_score,
        )
        s = _weight_score(2)
        assert 0.4 < s < 0.6

    def test_weight_saturates_near_reference(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _weight_score,
        )
        assert _weight_score(10) == 1.0

    def test_weight_above_reference_caps_at_one(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _weight_score,
        )
        assert _weight_score(50) == 1.0
        assert _weight_score(1000) == 1.0

    def test_log_scale_compresses_outliers(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _weight_score,
        )
        ratio = _weight_score(50) / _weight_score(2)
        assert ratio < 3.0


# ---------------------------------------------------------------------------
# § 2 — Diversity dedup
# ---------------------------------------------------------------------------


class TestDiversityDedup:
    def test_unique_attempts_all_returned(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeMatch,
            SituationKind,
            _diversity_dedup,
        )
        recs = [
            _make(
                sig=str(i),
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt=f"attempt-{i}",
            )
            for i in range(3)
        ]
        matches = [
            FailureModeMatch(
                record=r,
                recency_score=1.0,
                jaccard_score=1.0,
                weight_score=0.5,
                combined_score=0.5,
            )
            for r in recs
        ]
        result = _diversity_dedup(matches, top_k=3)
        assert len(result) == 3
        assert {m.record.attempted_action_kind for m in result} == {
            "attempt-0", "attempt-1", "attempt-2",
        }

    def test_duplicate_attempts_deduped_when_pool_diverse(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeMatch,
            SituationKind,
            _diversity_dedup,
        )
        recs = [
            _make(
                sig="0",
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt="A",
            ),
            _make(
                sig="1",
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt="A",
            ),
            _make(
                sig="2",
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt="B",
            ),
        ]
        matches = [
            FailureModeMatch(
                record=r,
                recency_score=1.0,
                jaccard_score=1.0,
                weight_score=0.5,
                combined_score=1.0 - i * 0.1,
            )
            for i, r in enumerate(recs)
        ]
        result = _diversity_dedup(matches, top_k=2)
        kinds = [m.record.attempted_action_kind for m in result]
        assert kinds == ["A", "B"]

    def test_overflow_fills_when_diversity_exhausted(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeMatch,
            SituationKind,
            _diversity_dedup,
        )
        recs = [
            _make(
                sig=str(i),
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt="A",
            )
            for i in range(5)
        ]
        matches = [
            FailureModeMatch(
                record=r,
                recency_score=1.0,
                jaccard_score=1.0,
                weight_score=0.5,
                combined_score=1.0 - i * 0.1,
            )
            for i, r in enumerate(recs)
        ]
        result = _diversity_dedup(matches, top_k=3)
        assert len(result) == 3

    def test_top_k_zero_returns_empty(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _diversity_dedup,
        )
        assert _diversity_dedup([], top_k=0) == tuple()

    def test_top_k_negative_returns_empty(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _diversity_dedup,
        )
        assert _diversity_dedup([], top_k=-5) == tuple()


# ---------------------------------------------------------------------------
# § 3 — retrieve_failure_modes — gates + filters
# ---------------------------------------------------------------------------


class TestRetrieverGates:
    def test_disabled_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            retrieve_failure_modes,
        )
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert result == tuple()

    def test_unknown_situation_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="0", situation=SituationKind.UNKNOWN,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.UNKNOWN,
            target_files=("a.py",),
        )
        assert result == tuple()

    def test_explicit_override_disables(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="0",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            weight=5,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
            enabled_override=False,
        )
        assert result == tuple()

    def test_empty_history_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            retrieve_failure_modes,
        )
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert result == tuple()

    def test_situation_mismatch_filtered_out(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="0",
            situation=SituationKind.DB_MIGRATION,
            weight=5,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert result == tuple()

    def test_below_min_weight_filtered_out(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="0",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            weight=1,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert result == tuple()


# ---------------------------------------------------------------------------
# § 4 — Ranking semantics
# ---------------------------------------------------------------------------


class TestRetrieverRanking:
    def test_match_returned_with_per_component_scores(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="0",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            weight=3, age_days=0.0,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert len(result) == 1
        m = result[0]
        assert 0.0 <= m.recency_score <= 1.0
        assert 0.0 <= m.jaccard_score <= 1.0
        assert 0.0 <= m.weight_score <= 1.0
        assert 0.0 <= m.combined_score <= 1.0
        product = (
            m.recency_score * m.jaccard_score * m.weight_score
        )
        assert abs(m.combined_score - product) < 1e-9

    def test_recency_dominates_when_weight_equal(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="old",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            attempt="old", weight=3, age_days=30.0,
        ))
        record_failure_mode(_make(
            sig="new",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            attempt="new", weight=3, age_days=0.0,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert len(result) == 2
        assert result[0].record.attempted_action_kind == "new"

    def test_weight_dominates_when_recency_equal(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        record_failure_mode(_make(
            sig="lo",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            attempt="lo", weight=2, age_days=0.0,
        ))
        record_failure_mode(_make(
            sig="hi",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            attempt="hi", weight=10, age_days=0.0,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
        )
        assert result[0].record.attempted_action_kind == "hi"


# ---------------------------------------------------------------------------
# § 5 — Top-K + diversity in concert
# ---------------------------------------------------------------------------


class TestTopKDiversity:
    def test_top_k_clamp(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        for i in range(10):
            record_failure_mode(_make(
                sig=str(i),
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt=f"a-{i}", weight=3,
            ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
            top_k=3,
        )
        assert len(result) == 3

    def test_top_k_one_returns_one(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        for i in range(5):
            record_failure_mode(_make(
                sig=str(i),
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt=f"a-{i}", weight=3,
            ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
            top_k=1,
        )
        assert len(result) == 1

    def test_diversity_preserved_in_retrieval(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            record_failure_mode,
            retrieve_failure_modes,
        )
        for i in range(5):
            record_failure_mode(_make(
                sig=f"a{i}",
                situation=SituationKind.MULTI_FILE_REFACTOR,
                attempt="A", weight=3,
            ))
        record_failure_mode(_make(
            sig="b0",
            situation=SituationKind.MULTI_FILE_REFACTOR,
            attempt="B", weight=2,
        ))
        result = retrieve_failure_modes(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            target_files=("a.py",),
            top_k=2,
        )
        kinds = {m.record.attempted_action_kind for m in result}
        assert "A" in kinds
        assert "B" in kinds


# ---------------------------------------------------------------------------
# § 6 — Env-knob clamps
# ---------------------------------------------------------------------------


class TestRetrieverEnvKnobs:
    def test_top_k_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_TOP_K", raising=False,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_top_k,
        )
        assert failure_mode_top_k() == 3

    def test_top_k_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FAILURE_MODE_TOP_K", "0")
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_top_k,
        )
        assert failure_mode_top_k() == 1

    def test_top_k_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FAILURE_MODE_TOP_K", "999")
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_top_k,
        )
        assert failure_mode_top_k() == 10

    def test_min_weight_default_is_two(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_MIN_WEIGHT", raising=False,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_min_weight,
        )
        assert failure_mode_min_weight() == 2

    def test_min_weight_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MIN_WEIGHT", "0",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_min_weight,
        )
        assert failure_mode_min_weight() == 1

    def test_halflife_default_is_fourteen(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_RECENCY_HALFLIFE_DAYS",
            raising=False,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_recency_halflife_days,
        )
        assert failure_mode_recency_halflife_days() == 14.0

    def test_halflife_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_RECENCY_HALFLIFE_DAYS", "0",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_recency_halflife_days,
        )
        assert failure_mode_recency_halflife_days() == 1.0


# ---------------------------------------------------------------------------
# § 7 — Schema parity with existing recency formula
# ---------------------------------------------------------------------------


class TestSchemaParity:
    def test_recency_formula_matches_canonical(self):
        """PRD §31.4.6: same as SemanticIndex's commit half-life.
        Slice 3 reuses the literal formula. If
        coherence_auditor / semantic_index ever diverge from
        ``0.5 ** (age_days / halflife_days)``, this pin trips so
        we update Slice 3 in lockstep."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight,
        )
        for age_d, hl_d in (
            (0.0, 14.0), (7.0, 14.0), (14.0, 14.0),
            (28.0, 14.0), (1.0, 1.0), (3.5, 7.0),
        ):
            expected = 0.5 ** (age_d / hl_d)
            actual = _recency_weight(
                age_d * 86400.0, hl_d,
            )
            assert abs(actual - expected) < 1e-9, (
                f"halflife formula drift at age_d={age_d}, "
                f"hl_d={hl_d}: got {actual}, expected {expected}"
            )

    def test_match_to_dict_round_trip_safe(self):
        """FailureModeMatch.to_dict produces a JSON-serializable
        dict — Slice 5's HTTP route + REPL surface can serialize
        directly without custom encoders."""
        import json

        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeMatch,
            SituationKind,
        )
        rec = _make(
            sig="0", situation=SituationKind.MULTI_FILE_REFACTOR,
        )
        m = FailureModeMatch(
            record=rec,
            recency_score=0.9,
            jaccard_score=1.0,
            weight_score=0.5,
            combined_score=0.45,
        )
        d = m.to_dict()
        roundtrip = json.loads(json.dumps(d))
        assert roundtrip["recency_score"] == 0.9
        assert roundtrip["jaccard_score"] == 1.0
        assert roundtrip["combined_score"] == 0.45
        assert (
            roundtrip["record"]["situation_kind"]
            == "multi_file_refactor"
        )
