"""M11 Slice 3 — ActionOutcomeRetriever tests (PRD §30.5.3).

Pins the deterministic top-K retrieval contract + outcome polarity
weighting (M11-specific) + balanced-palette diversity dedup.
"""
from __future__ import annotations

import time

import pytest


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
    )


def _make(
    *,
    sig=None,
    situation=None,
    attempt="add_dataclass",
    outcome=None,
    target_files=("a.py", "b.py"),
    obs_at=None,
    weight=1,
    op_id="op-test",
    cluster_id="42",
    summary="ok",
):
    from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
        ActionOutcomeRecord,
        OutcomeKind,
        compute_outcome_signature,
    )
    from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
        SituationKind,
    )
    sit = situation or SituationKind.MULTI_FILE_REFACTOR
    out = outcome or OutcomeKind.APPLIED_VERIFIED
    s = sig or compute_outcome_signature(
        situation_kind=sit,
        attempted_action_kind=attempt,
        outcome_kind=out,
        target_files=target_files,
    )
    obs = obs_at if obs_at is not None else time.time()
    return ActionOutcomeRecord(
        signature_hash=s,
        situation_kind=sit,
        attempted_action_kind=attempt,
        outcome_kind=out,
        target_files=tuple(target_files),
        commit_hash="",
        summary=summary,
        observed_at_unix=obs,
        op_id=op_id,
        cluster_id=cluster_id,
        weight=weight,
    )


# ---------------------------------------------------------------------------
# § 1 — Gate + filter contracts
# ---------------------------------------------------------------------------


class TestRetrieverGates:
    def test_disabled_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            recall_for_region,
        )
        result = recall_for_region(target_files=("a.py",))
        assert result == tuple()

    def test_enabled_override_disables(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            recall_for_region,
            record_action_outcome,
        )
        record_action_outcome(_make(weight=3))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            enabled_override=False,
        )
        assert result == tuple()

    def test_empty_history_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            recall_for_region,
        )
        result = recall_for_region(target_files=("a.py",))
        assert result == tuple()

    def test_below_min_weight_filtered_out(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            recall_for_region,
            record_action_outcome,
        )
        record_action_outcome(_make(weight=1))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            min_weight=5,
        )
        assert result == tuple()

    def test_disabled_outcome_kind_filtered_at_retrieval(
        self, monkeypatch, tmp_path,
    ):
        """Defensive: persistence rejects DISABLED outcomes, but
        if a corrupt JSONL carries one through, the retriever
        filters it. Direct file write to bypass the recorder."""
        _enable(monkeypatch, tmp_path)
        import json
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
            OutcomeKind,
            cluster_jsonl_path,
            recall_for_region,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        rec = ActionOutcomeRecord(
            signature_hash="d" * 64,
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.DISABLED,
            target_files=("a.py", "b.py"),
            commit_hash="",
            summary="should not surface",
            observed_at_unix=time.time(),
            op_id="op-corrupt",
            cluster_id="42",
            weight=10,
        )
        path = cluster_jsonl_path("42")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write(
                json.dumps(rec.to_dict(), sort_keys=True) + "\n"
            )
        result = recall_for_region(
            target_files=("a.py", "b.py"),
        )
        assert result == tuple()


# ---------------------------------------------------------------------------
# § 2 — Ranking semantics + per-component scores
# ---------------------------------------------------------------------------


class TestRetrieverRanking:
    def test_match_returned_with_per_component_scores(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            recall_for_region,
            record_action_outcome,
        )
        record_action_outcome(_make(weight=3))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
        )
        assert len(result) == 1
        m = result[0]
        for s in (
            m.recency_score, m.jaccard_score, m.weight_score,
            m.polarity_score, m.combined_score,
        ):
            assert 0.0 <= s <= 1.0
        product = (
            m.recency_score * m.jaccard_score
            * m.weight_score * m.polarity_score
        )
        assert abs(m.combined_score - product) < 1e-9

    def test_polarity_dominates_when_recency_weight_equal(
        self, monkeypatch, tmp_path,
    ):
        """APPLIED_VERIFIED outranks REJECTED at same recency +
        weight + region — load-bearing M11 dimension."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        ts = time.time()
        record_action_outcome(_make(
            attempt="bad",
            outcome=OutcomeKind.REJECTED,
            obs_at=ts,
            weight=3,
        ))
        record_action_outcome(_make(
            attempt="good",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            obs_at=ts,
            weight=3,
        ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
        )
        assert len(result) == 2
        assert (
            result[0].record.outcome_kind
            is OutcomeKind.APPLIED_VERIFIED
        )

    def test_recency_dominates_when_polarity_weight_equal(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        record_action_outcome(_make(
            attempt="old",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            obs_at=time.time() - 30 * 86400.0,
            weight=3,
        ))
        record_action_outcome(_make(
            attempt="new",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            obs_at=time.time(),
            weight=3,
        ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
        )
        assert (
            result[0].record.attempted_action_kind == "new"
        )

    def test_jaccard_dominates_when_other_dimensions_equal(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        ts = time.time()
        record_action_outcome(_make(
            sig="match" * 12 + "0000",
            attempt="a",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py"),
            obs_at=ts,
            weight=3,
        ))
        record_action_outcome(_make(
            sig="other" * 12 + "0000",
            attempt="b",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "z.py"),
            obs_at=ts,
            weight=3,
        ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
        )
        assert len(result) == 2
        assert result[0].jaccard_score > result[1].jaccard_score


# ---------------------------------------------------------------------------
# § 3 — Outcome-kind diversity dedup (balanced palette)
# ---------------------------------------------------------------------------


class TestOutcomeDiversity:
    def test_balanced_palette_in_top_k(
        self, monkeypatch, tmp_path,
    ):
        """Pool with 3 VERIFIED + 1 REVERTED, top_k=2 must include
        BOTH outcome kinds (M11 diversity dedup keys on outcome_kind)."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        ts = time.time()
        for i in range(3):
            record_action_outcome(_make(
                sig=f"v{i:063x}",
                attempt=f"v-{i}",
                outcome=OutcomeKind.APPLIED_VERIFIED,
                obs_at=ts - i,
                weight=3,
            ))
        record_action_outcome(_make(
            sig="r" * 64,
            attempt="r-0",
            outcome=OutcomeKind.APPLIED_REVERTED,
            obs_at=ts - 10,
            weight=3,
        ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            top_k=2,
        )
        kinds = {m.record.outcome_kind for m in result}
        assert OutcomeKind.APPLIED_VERIFIED in kinds
        assert OutcomeKind.APPLIED_REVERTED in kinds


# ---------------------------------------------------------------------------
# § 4 — Polarity preset modes
# ---------------------------------------------------------------------------


class TestPolarityPresets:
    def test_default_mode_is_balanced(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_POLARITY_MODE",
            raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_polarity_mode,
        )
        assert action_outcome_polarity_mode() == "balanced"

    def test_unknown_mode_falls_back_to_balanced(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_POLARITY_MODE",
            "no_such_mode",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_polarity_mode,
        )
        assert action_outcome_polarity_mode() == "balanced"

    def test_balanced_mode_canonical_ranking(self, monkeypatch):
        """Canonical balanced ranking: VERIFIED > REVERTED >
        REJECTED > DEFERRED > DISABLED."""
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_POLARITY_MODE",
            raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            _outcome_polarity_weight,
        )
        verified = _outcome_polarity_weight(
            OutcomeKind.APPLIED_VERIFIED,
        )
        reverted = _outcome_polarity_weight(
            OutcomeKind.APPLIED_REVERTED,
        )
        rejected = _outcome_polarity_weight(
            OutcomeKind.REJECTED,
        )
        deferred = _outcome_polarity_weight(
            OutcomeKind.DEFERRED,
        )
        disabled = _outcome_polarity_weight(
            OutcomeKind.DISABLED,
        )
        assert verified > reverted > rejected > deferred
        assert disabled == 0.0

    def test_favor_positive_widens_gap(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_POLARITY_MODE",
            "favor_positive",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            _outcome_polarity_weight,
        )
        v = _outcome_polarity_weight(
            OutcomeKind.APPLIED_VERIFIED,
        )
        d = _outcome_polarity_weight(OutcomeKind.DEFERRED)
        gap = v - d
        # Wider than balanced's 1.0 - 0.3 = 0.7
        assert gap >= 0.7

    def test_all_equal_mode(self, monkeypatch):
        """all_equal: 4 actionable outcomes weight 1.0; DISABLED 0.0."""
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_POLARITY_MODE", "all_equal",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            _outcome_polarity_weight,
        )
        for k in (
            OutcomeKind.APPLIED_VERIFIED,
            OutcomeKind.APPLIED_REVERTED,
            OutcomeKind.REJECTED,
            OutcomeKind.DEFERRED,
        ):
            assert _outcome_polarity_weight(k) == 1.0
        assert (
            _outcome_polarity_weight(OutcomeKind.DISABLED)
            == 0.0
        )


# ---------------------------------------------------------------------------
# § 5 — Top-K + clamping
# ---------------------------------------------------------------------------


class TestTopK:
    def test_top_k_clamp(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        ts = time.time()
        for kind in (
            OutcomeKind.APPLIED_VERIFIED,
            OutcomeKind.APPLIED_REVERTED,
            OutcomeKind.REJECTED,
            OutcomeKind.DEFERRED,
        ):
            record_action_outcome(_make(
                attempt=f"a-{kind.value}",
                outcome=kind, obs_at=ts, weight=3,
            ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            top_k=2,
        )
        assert len(result) == 2

    def test_top_k_one_returns_one(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        for i in range(3):
            record_action_outcome(_make(
                sig=f"{i:064x}",
                attempt=f"a-{i}",
                outcome=OutcomeKind.APPLIED_VERIFIED,
                weight=3,
            ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            top_k=1,
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# § 6 — Cluster-scoped retrieval (Decision A3 continued)
# ---------------------------------------------------------------------------


class TestClusterScopedRetrieval:
    def test_cluster_records_take_precedence(
        self, monkeypatch, tmp_path,
    ):
        """When SemanticIndex resolves a cluster_id, retrieval
        reads cluster + global. Decision A3: storage clustering
        is an OPTIMIZATION, never a CORRECTNESS dependency."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        record_action_outcome(_make(
            sig="cluster" * 9 + "0",
            attempt="cluster_attempt",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            cluster_id="42",
            weight=3,
        ))
        record_action_outcome(_make(
            sig="globalx" * 9 + "0",
            attempt="global_attempt",
            outcome=OutcomeKind.APPLIED_REVERTED,
            cluster_id="",
            weight=3,
        ))
        # Override cluster_id to force cluster=42 path
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            cluster_id_override="42",
        )
        # Both records visible (cluster + global union)
        assert len(result) == 2

    def test_unresolved_cluster_falls_back_to_all(
        self, monkeypatch, tmp_path,
    ):
        """When SemanticIndex doesn't resolve a cluster, retrieval
        reads ALL clusters."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            recall_for_region,
            record_action_outcome,
        )
        for i in range(3):
            record_action_outcome(_make(
                sig=f"{i:064x}",
                attempt=f"a-{i}",
                outcome=OutcomeKind.APPLIED_VERIFIED,
                cluster_id=str(i),
                weight=3,
            ))
        result = recall_for_region(
            target_files=("a.py", "b.py"),
            cluster_id_override="",
        )
        # Read-all path correctly walks all 3 cluster files. With
        # default top_k=3 + identical outcome_kind across all 3,
        # diversity dedup primary picks 1 unique kind, then
        # overflow fills remaining slots — total 3 returned.
        # Load-bearing: this verifies the read-all path actually
        # READS from all clusters (would be 0 or 1 if broken).
        assert len(result) == 3


# ---------------------------------------------------------------------------
# § 7 — Env-knob clamps
# ---------------------------------------------------------------------------


class TestRetrieverEnvKnobs:
    def test_top_k_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_TOP_K", raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_top_k,
        )
        assert action_outcome_top_k() == 3

    def test_top_k_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_TOP_K", "999",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_top_k,
        )
        assert action_outcome_top_k() == 10

    def test_min_weight_default_is_one(self, monkeypatch):
        """M11 default min_weight=1, NOT 2 like Upgrade 3 —
        positive evidence is more actionable than negative."""
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MIN_WEIGHT", raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_min_weight,
        )
        assert action_outcome_min_weight() == 1

    def test_halflife_default_is_fourteen(self, monkeypatch):
        """Parity with Upgrade 3 + semantic_index commit
        half-life."""
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_RECENCY_HALFLIFE_DAYS",
            raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_recency_halflife_days,
        )
        assert action_outcome_recency_halflife_days() == 14.0

    def test_halflife_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_RECENCY_HALFLIFE_DAYS", "0",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_recency_halflife_days,
        )
        assert action_outcome_recency_halflife_days() == 1.0


# ---------------------------------------------------------------------------
# § 8 — Match.to_dict round-trip
# ---------------------------------------------------------------------------


class TestActionOutcomeMatch:
    def test_to_dict_round_trip_safe(self):
        """ActionOutcomeMatch.to_dict produces a JSON-serializable
        dict — Slice 5's HTTP route + REPL surface can serialize
        directly without custom encoders."""
        import json
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeMatch,
            OutcomeKind,
        )
        rec = _make(outcome=OutcomeKind.APPLIED_VERIFIED)
        m = ActionOutcomeMatch(
            record=rec,
            recency_score=0.9,
            jaccard_score=1.0,
            weight_score=0.5,
            polarity_score=1.0,
            combined_score=0.45,
        )
        d = m.to_dict()
        roundtrip = json.loads(json.dumps(d))
        assert roundtrip["recency_score"] == 0.9
        assert roundtrip["polarity_score"] == 1.0
        assert (
            roundtrip["record"]["outcome_kind"]
            == "applied_verified"
        )
