"""M11 Slice 2 — Persistence layer tests (PRD §30.5.3 Slice 2).

Pins:
  * record_action_outcome happy path (OK_NEW + OK_DEDUPED)
  * Master flag gate (DISABLED + REJECTED for sentinel outcome)
  * Garbage input rejection
  * Dedup-window merge math (weight++, observed_at_unix=max)
  * Outcome-kind in signature -> different outcomes don't merge
  * Per-cluster file routing
  * Decision A3 graceful fallback when SemanticIndex unavailable
  * cluster_jsonl_path filename safety (path traversal defense)
  * Ring-buffer truncate per cluster
  * read_action_outcomes_for_cluster filter + sort + clamp
  * read_all_action_outcomes cross-cluster aggregation
  * clear_action_outcomes operator-triggered maintenance
  * Env-knob clamps
"""
from __future__ import annotations

import json
import time
from pathlib import Path

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
    cluster_id="",
    commit_hash="abc1234",
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
        commit_hash=commit_hash,
        summary=summary,
        observed_at_unix=obs,
        op_id=op_id,
        cluster_id=cluster_id,
        weight=weight,
    )


# ---------------------------------------------------------------------------
# § 1 — record_action_outcome master-flag + type gate
# ---------------------------------------------------------------------------


class TestRecordActionOutcomeGate:
    def test_disabled_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        # Slice 2 default-FALSE; force-explicit just in case.
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            RecordOutcome,
            record_action_outcome,
        )
        outcome = record_action_outcome(_make())
        assert outcome is RecordOutcome.DISABLED

    def test_rejected_on_garbage_input(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            RecordOutcome,
            record_action_outcome,
        )
        outcome = record_action_outcome(
            "not a record",  # type: ignore[arg-type]
        )
        assert outcome is RecordOutcome.REJECTED

    def test_disabled_outcome_kind_rejected(
        self, monkeypatch, tmp_path,
    ):
        """OutcomeKind.DISABLED is the master-off sentinel — never
        persisted (records on disk would be meaningless)."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            RecordOutcome,
            record_action_outcome,
        )
        rec = _make(outcome=OutcomeKind.DISABLED)
        outcome = record_action_outcome(rec)
        assert outcome is RecordOutcome.REJECTED


# ---------------------------------------------------------------------------
# § 2 — Happy path persistence + dedup-window merge
# ---------------------------------------------------------------------------


class TestRecordActionOutcomeHappyPath:
    def test_ok_new_appends_record(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            RecordOutcome,
            read_all_action_outcomes,
            record_action_outcome,
        )
        rec = _make(cluster_id="42")
        outcome = record_action_outcome(rec)
        assert outcome is RecordOutcome.OK_NEW
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 1

    def test_dedup_increments_weight_within_window(
        self, monkeypatch, tmp_path,
    ):
        """Two records sharing signature within dedup window ->
        merged into one record with weight=2 + max ts."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            RecordOutcome,
            read_all_action_outcomes,
            record_action_outcome,
        )
        ts = 1700000000.0
        r1 = _make(cluster_id="42", obs_at=ts)
        r2 = _make(cluster_id="42", obs_at=ts + 1000.0)
        assert (
            record_action_outcome(r1) is RecordOutcome.OK_NEW
        )
        assert (
            record_action_outcome(r2) is RecordOutcome.OK_DEDUPED
        )
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 1
        assert all_recs[0].weight == 2
        # observed_at_unix updated to most recent
        assert all_recs[0].observed_at_unix == ts + 1000.0

    def test_no_dedup_outside_window(
        self, monkeypatch, tmp_path,
    ):
        """Same signature outside the 30d window -> two distinct
        records."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            RecordOutcome,
            read_all_action_outcomes,
            record_action_outcome,
        )
        r1 = _make(cluster_id="42", obs_at=1700000000.0)
        r2 = _make(
            cluster_id="42",
            obs_at=1700000000.0 + 31 * 86400.0,
        )
        assert (
            record_action_outcome(r1) is RecordOutcome.OK_NEW
        )
        assert (
            record_action_outcome(r2) is RecordOutcome.OK_NEW
        )
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 2

    def test_different_outcome_kinds_dont_merge(
        self, monkeypatch, tmp_path,
    ):
        """**Load-bearing M11 distinction from Upgrade 3**: same
        situation+region+attempt with DIFFERENT outcomes produce
        DIFFERENT signatures and therefore coexist as distinct
        records, even within the dedup window. This is the
        "we tried twice and got different results" semantics."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            RecordOutcome,
            read_all_action_outcomes,
            record_action_outcome,
        )
        r_verified = _make(
            cluster_id="42",
            outcome=OutcomeKind.APPLIED_VERIFIED,
            obs_at=1700000000.0,
        )
        r_reverted = _make(
            cluster_id="42",
            outcome=OutcomeKind.APPLIED_REVERTED,
            obs_at=1700000000.0 + 100.0,
        )
        assert (
            record_action_outcome(r_verified)
            is RecordOutcome.OK_NEW
        )
        assert (
            record_action_outcome(r_reverted)
            is RecordOutcome.OK_NEW
        )
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 2


# ---------------------------------------------------------------------------
# § 3 — Per-cluster file routing
# ---------------------------------------------------------------------------


class TestClusterRouting:
    def test_records_with_different_clusters_go_to_different_files(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            cluster_jsonl_path,
            record_action_outcome,
        )
        record_action_outcome(_make(cluster_id="1"))
        record_action_outcome(_make(cluster_id="2"))
        path_a = cluster_jsonl_path("1")
        path_b = cluster_jsonl_path("2")
        assert path_a != path_b
        assert path_a.exists()
        assert path_b.exists()

    def test_empty_cluster_id_routes_to_global_fallback(
        self, monkeypatch, tmp_path,
    ):
        """Decision A3: empty cluster_id (SemanticIndex unavailable)
        routes to ``_global.jsonl``."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            cluster_jsonl_path,
            record_action_outcome,
        )
        record_action_outcome(_make(cluster_id=""))
        global_path = cluster_jsonl_path("")
        assert global_path.name == "_global.jsonl"
        assert global_path.exists()

    def test_filename_safety_path_traversal(
        self, monkeypatch, tmp_path,
    ):
        """Hostile cluster_ids must NOT escape history_dir.
        Path-traversal defense pinned: any non-alphanum-hyphen-
        underscore stem falls through to the global fallback."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            cluster_jsonl_path,
        )
        for hostile in (
            "../etc/passwd",
            "/absolute/path",
            "foo/bar",
            "weird name with spaces",
            "tab\there",
            "newline\nhere",
            "",
        ):
            path = cluster_jsonl_path(hostile)
            # All hostile inputs resolve to _global.jsonl
            assert path.name == "_global.jsonl"

    def test_filename_safety_max_length(
        self, monkeypatch, tmp_path,
    ):
        """cluster_id >64 chars -> global fallback."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            cluster_jsonl_path,
        )
        path = cluster_jsonl_path("a" * 65)
        assert path.name == "_global.jsonl"

    def test_valid_cluster_ids_accepted(
        self, monkeypatch, tmp_path,
    ):
        """Numeric, alphanumeric, hyphen, underscore — all valid."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            cluster_jsonl_path,
        )
        for valid in (
            "0", "42", "cluster-3", "CLUSTER_42", "abc123",
        ):
            path = cluster_jsonl_path(valid)
            assert path.name == f"{valid}.jsonl"


# ---------------------------------------------------------------------------
# § 4 — Decision A3 graceful fallback
# ---------------------------------------------------------------------------


class TestSemanticIndexFallback:
    def test_resolve_cluster_id_with_override(
        self, monkeypatch, tmp_path,
    ):
        """Test fixture override path bypasses SemanticIndex."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            _resolve_cluster_id,
        )
        result = _resolve_cluster_id(
            ("a.py",), cluster_id_override="42",
        )
        assert result == "42"

    def test_resolve_cluster_id_empty_files_returns_empty(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            _resolve_cluster_id,
        )
        result = _resolve_cluster_id(())
        assert result == ""

    def test_resolve_cluster_id_falls_back_on_failure(self):
        """If SemanticIndex.score_with_cluster raises / returns None
        / SemanticIndex disabled, _resolve_cluster_id falls back to
        empty string (Decision A3 graceful degradation)."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            _resolve_cluster_id,
        )
        # We do NOT set up SemanticIndex; it will likely return
        # None (no centroid built) or raise.
        result = _resolve_cluster_id(("nonexistent.py",))
        # Result is empty string, not raising
        assert result == ""

    def test_record_persists_with_no_cluster_resolution(
        self, monkeypatch, tmp_path,
    ):
        """When cluster resolution fails (Decision A3), records
        still persist to the global fallback file. Storage is
        an OPTIMIZATION via clustering, never a CORRECTNESS
        dependency."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            RecordOutcome,
            cluster_jsonl_path,
            read_all_action_outcomes,
            record_action_outcome,
        )
        # No cluster_id on record + SemanticIndex likely returns
        # None for unknown files.
        rec = _make(target_files=("totally-novel-file.py",))
        outcome = record_action_outcome(rec)
        assert outcome is RecordOutcome.OK_NEW
        # Persisted somewhere
        assert len(read_all_action_outcomes()) == 1
        # Global fallback file likely exists
        global_path = cluster_jsonl_path("")
        assert global_path.exists()


# ---------------------------------------------------------------------------
# § 5 — Ring-buffer truncate
# ---------------------------------------------------------------------------


class TestRingBufferTruncate:
    def test_truncates_to_max_records_per_cluster(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        # Tight cap to validate truncation
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER",
            "50",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_action_outcomes_for_cluster,
            record_action_outcome,
        )
        # Insert 60 distinct signatures (well past the cap)
        for i in range(60):
            sig = f"{i:064x}"
            record_action_outcome(
                _make(
                    sig=sig,
                    cluster_id="42",
                    obs_at=1700000000.0 + i,
                    op_id=f"op-{i:03d}",
                ),
            )
        records = read_action_outcomes_for_cluster("42")
        assert len(records) == 50
        # Most-recent retained — first kept op-id is op-010
        assert records[0].op_id == "op-010"
        assert records[-1].op_id == "op-059"

    def test_per_cluster_truncate_independent(
        self, monkeypatch, tmp_path,
    ):
        """Truncation is PER cluster, not global. Cluster A
        having 50 records doesn't affect cluster B."""
        _enable(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER",
            "50",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_action_outcomes_for_cluster,
            record_action_outcome,
        )
        # 60 to cluster A (truncates to 50)
        for i in range(60):
            record_action_outcome(_make(
                sig=f"a{i:063x}",
                cluster_id="A",
                obs_at=1700000000.0 + i,
                op_id=f"a-{i:03d}",
            ))
        # 5 to cluster B (well below cap)
        for i in range(5):
            record_action_outcome(_make(
                sig=f"b{i:063x}",
                cluster_id="B",
                obs_at=1700000000.0 + i,
                op_id=f"b-{i}",
            ))
        a_recs = read_action_outcomes_for_cluster("A")
        b_recs = read_action_outcomes_for_cluster("B")
        assert len(a_recs) == 50
        assert len(b_recs) == 5


# ---------------------------------------------------------------------------
# § 6 — read_action_outcomes_for_cluster
# ---------------------------------------------------------------------------


class TestReadActionOutcomesForCluster:
    def test_empty_when_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_action_outcomes_for_cluster,
        )
        assert read_action_outcomes_for_cluster("42") == ()

    def test_since_unix_filter(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_action_outcomes_for_cluster,
            record_action_outcome,
        )
        for i in range(5):
            record_action_outcome(_make(
                sig=f"{i:064x}",
                cluster_id="42",
                obs_at=1700000000.0 + i * 1000.0,
                op_id=f"op-{i}",
            ))
        cut = 1700002000.0
        history = read_action_outcomes_for_cluster(
            "42", since_unix=cut,
        )
        assert all(r.observed_at_unix >= cut for r in history)
        assert len(history) == 3

    def test_limit_clamp(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_action_outcomes_for_cluster,
            record_action_outcome,
        )
        for i in range(10):
            record_action_outcome(_make(
                sig=f"{i:064x}",
                cluster_id="42",
                obs_at=1700000000.0 + i,
                op_id=f"op-{i}",
            ))
        history = read_action_outcomes_for_cluster(
            "42", limit=3,
        )
        assert len(history) == 3
        assert history[-1].op_id == "op-9"

    def test_empty_cluster_reads_global_fallback(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_action_outcomes_for_cluster,
            record_action_outcome,
        )
        record_action_outcome(_make(cluster_id=""))  # → _global
        records = read_action_outcomes_for_cluster("")
        assert len(records) == 1


# ---------------------------------------------------------------------------
# § 7 — read_all_action_outcomes (cross-cluster aggregation)
# ---------------------------------------------------------------------------


class TestReadAllActionOutcomes:
    def test_aggregates_across_clusters(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_all_action_outcomes,
            record_action_outcome,
        )
        record_action_outcome(_make(
            sig="a" * 64, cluster_id="A",
            obs_at=1700000000.0, op_id="op-a",
        ))
        record_action_outcome(_make(
            sig="b" * 64, cluster_id="B",
            obs_at=1700000100.0, op_id="op-b",
        ))
        record_action_outcome(_make(
            sig="c" * 64, cluster_id="",
            obs_at=1700000200.0, op_id="op-c",
        ))
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 3
        op_ids = {r.op_id for r in all_recs}
        assert op_ids == {"op-a", "op-b", "op-c"}

    def test_chronological_sort_across_clusters(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_all_action_outcomes,
            record_action_outcome,
        )
        # Insert in reverse chronological
        for i, ts in enumerate([
            1700000300.0, 1700000100.0, 1700000200.0,
        ]):
            record_action_outcome(_make(
                sig=f"{i:064x}", cluster_id=str(i),
                obs_at=ts, op_id=f"op-{i}",
            ))
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 3
        # Must be ascending by observed_at_unix
        timestamps = [r.observed_at_unix for r in all_recs]
        assert timestamps == sorted(timestamps)

    def test_empty_dir_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_all_action_outcomes,
        )
        assert read_all_action_outcomes() == ()

    def test_skips_non_jsonl_files(self, monkeypatch, tmp_path):
        """Stray files in history_dir must not crash the reader."""
        _enable(monkeypatch, tmp_path)
        # Drop a stray non-JSONL file
        stray = tmp_path / "not_jsonl.txt"
        stray.write_text("garbage")
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            read_all_action_outcomes,
            record_action_outcome,
        )
        record_action_outcome(_make(
            sig="a" * 64, cluster_id="42", op_id="op-1",
        ))
        all_recs = read_all_action_outcomes()
        assert len(all_recs) == 1


# ---------------------------------------------------------------------------
# § 8 — clear_action_outcomes (operator-triggered maintenance)
# ---------------------------------------------------------------------------


class TestClearActionOutcomes:
    def test_clear_removes_all_jsonl_files(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            clear_action_outcomes,
            history_dir,
            read_all_action_outcomes,
            record_action_outcome,
        )
        for i in range(3):
            record_action_outcome(_make(
                sig=f"{i:064x}", cluster_id=str(i),
            ))
        assert len(read_all_action_outcomes()) == 3
        ok = clear_action_outcomes()
        assert ok is True
        assert read_all_action_outcomes() == ()
        # Files actually gone
        remaining = list(history_dir().iterdir())
        # Only locks may remain; no .jsonl files
        jsonl = [f for f in remaining if f.suffix == ".jsonl"]
        assert jsonl == []

    def test_clear_disabled_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        # Don't enable master flag
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            clear_action_outcomes,
        )
        assert clear_action_outcomes() is False

    def test_clear_returns_true_on_no_dir(
        self, monkeypatch, tmp_path,
    ):
        """Clearing a non-existent dir is a no-op success."""
        _enable(monkeypatch, tmp_path / "nonexistent")
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            clear_action_outcomes,
        )
        assert clear_action_outcomes() is True

    def test_clear_skips_non_jsonl_files(
        self, monkeypatch, tmp_path,
    ):
        """Non-JSONL files in history_dir survive a clear."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            clear_action_outcomes,
            record_action_outcome,
        )
        # Plant a non-JSONL artifact
        stray = tmp_path / "operator_notes.txt"
        stray.write_text("important")
        record_action_outcome(_make(cluster_id="42"))
        ok = clear_action_outcomes()
        assert ok is True
        # Stray file untouched
        assert stray.exists()
        assert stray.read_text() == "important"


# ---------------------------------------------------------------------------
# § 9 — Env-knob clamps
# ---------------------------------------------------------------------------


class TestEnvKnobClamps:
    def test_history_max_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER", "1",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            max_records_per_cluster,
        )
        # Floor is 50
        assert max_records_per_cluster() == 50

    def test_history_max_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER",
            "999999",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            max_records_per_cluster,
        )
        assert max_records_per_cluster() == 100_000

    def test_history_max_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER",
            raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            max_records_per_cluster,
        )
        assert max_records_per_cluster() == 1000

    def test_dedup_window_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_DEDUP_WINDOW_DAYS",
            raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            dedup_window_days,
        )
        assert dedup_window_days() == 30

    def test_dedup_window_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_DEDUP_WINDOW_DAYS", "0",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            dedup_window_days,
        )
        assert dedup_window_days() == 1

    def test_history_dir_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            history_dir,
        )
        assert str(history_dir()).endswith("action_outcomes")
