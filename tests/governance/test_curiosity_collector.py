"""M9 Slice 2 — CuriosityCollector tests (PRD §30.5.1).

Pins:
  § 1 — Master flag gate (record returns None when off)
  § 2 — Three record_* methods (one per source)
  § 3 — Atomic frozen-swap mutation (concurrent ops don't tear)
  § 4 — Per-cluster JSONL persistence (flock'd, idempotent)
  § 5 — JSONL replay via read_observations_for_cluster
  § 6 — Pull-side score_for_cluster query
  § 7 — Auto-decay: STALE_FOCUS / RECURRENCE_LOOP
  § 8 — Operator-explicit reset_cluster (OPERATOR_RESET)
  § 9 — resolve_cluster_id (Decision A3 SemanticIndex-optional)
  § 10 — snapshot_all observability projection
  § 11 — Authority floor (no orchestrator/iron_gate/governor imports)
  § 12 — Public exports (__all__)
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
    )


def _isolate_history(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_HISTORY_DIR", str(tmp_path / "cur"),
    )


# ---------------------------------------------------------------------------
# § 1 — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    def test_record_returns_none_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        assert c.record_logprob_entropy("backend", 0.5) is None
        assert c.record_prophecy_error("backend", 0.5) is None
        assert c.record_recurrence_drift("backend", 5) is None
        assert len(c) == 0

    def test_score_for_cluster_returns_disabled_when_off(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
        )
        c = CuriosityCollector()
        score = c.score_for_cluster("backend")
        assert score.dominant_source is CuriositySource.DISABLED


# ---------------------------------------------------------------------------
# § 2 — Three record_* methods
# ---------------------------------------------------------------------------


class TestRecordMethods:
    def test_logprob_entropy_records_sample(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        score = c.record_logprob_entropy(
            "backend", 0.5, op_id="op-x", at_unix=time.time(),
        )
        assert score is not None
        assert score.cluster_id == "backend"
        assert score.samples_count == 1

    def test_prophecy_error_records_sample(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        score = c.record_prophecy_error(
            "backend", 0.7, op_id="op-y",
        )
        assert score is not None
        assert score.samples_count == 1

    def test_recurrence_drift_normalizes_via_weight_score(
        self, monkeypatch, tmp_path,
    ):
        """Recurrence_count should be normalized via
        _scoring_primitives.weight_score (log-scale saturating).
        Verify 1 recurrence < 5 recurrences < 50 (compressed)."""
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c1 = CuriosityCollector()
        c1.record_recurrence_drift("c", 1, at_unix=time.time())
        s1 = c1._states["c"].observations[-1].value

        c2 = CuriosityCollector()
        c2.record_recurrence_drift("c", 5, at_unix=time.time())
        s5 = c2._states["c"].observations[-1].value

        c3 = CuriosityCollector()
        c3.record_recurrence_drift("c", 50, at_unix=time.time())
        s50 = c3._states["c"].observations[-1].value

        assert s1 < s5 < s50
        # Log-scale saturation: 50/5 = 10× recurrence count, but
        # the value ratio should be much less than 10×
        assert (s50 / s5) < 5.0

    def test_value_clamped_at_ingest(
        self, monkeypatch, tmp_path,
    ):
        """Adversarial value > 1.0 must clamp at ingest, not
        propagate."""
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        c.record_logprob_entropy("backend", 5.0)
        obs = c._states["backend"].observations[-1]
        assert obs.value == 1.0  # clamped to ceiling

        c.record_logprob_entropy("backend", -3.0)
        obs = c._states["backend"].observations[-1]
        assert obs.value == 0.0  # clamped to floor

    def test_nan_value_silently_dropped(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        result = c.record_logprob_entropy(
            "backend", float("nan"),
        )
        assert result is None
        # No state should have been created
        assert "backend" not in c._states


# ---------------------------------------------------------------------------
# § 3 — Atomic frozen-swap (thread safety)
# ---------------------------------------------------------------------------


class TestAtomicMutation:
    def test_concurrent_records_never_tear(
        self, monkeypatch, tmp_path,
    ):
        """5 threads × 20 records each = 100 atomic increments."""
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        # Disable persistence — JSONL append latency would
        # dominate the test
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_PERSIST_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        errors = []

        def worker():
            try:
                for i in range(20):
                    c.record_logprob_entropy(
                        "backend", 0.5,
                        op_id=f"op-{threading.get_ident()}-{i}",
                        at_unix=time.time(),
                    )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=worker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # Total observations should be exactly 100 (or capped
        # at window_size if smaller)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            curiosity_window_size,
        )
        cap = curiosity_window_size()
        actual = len(c._states["backend"].observations)
        assert actual == min(100, cap)

    def test_score_after_concurrent_records_consistent(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_PERSIST_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()

        def worker():
            for _ in range(10):
                c.record_logprob_entropy(
                    "backend", 0.6, at_unix=time.time(),
                )
                c.score_for_cluster("backend")

        threads = [
            threading.Thread(target=worker) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Final score should be well-formed (no exceptions)
        score = c.score_for_cluster("backend")
        assert 0.0 <= score.magnitude <= 1.0


# ---------------------------------------------------------------------------
# § 4 — Per-cluster JSONL persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_jsonl_written_to_per_cluster_file(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        c.record_logprob_entropy(
            "backend", 0.6, op_id="op-A", at_unix=1000.0,
        )
        c.record_prophecy_error(
            "frontend", 0.8, op_id="op-B", at_unix=1001.0,
        )

        backend_jsonl = tmp_path / "cur" / "backend.jsonl"
        frontend_jsonl = tmp_path / "cur" / "frontend.jsonl"
        assert backend_jsonl.exists()
        assert frontend_jsonl.exists()

        backend_lines = backend_jsonl.read_text().strip().split("\n")
        assert len(backend_lines) == 1
        row = json.loads(backend_lines[0])
        assert row["source"] == "logprob_entropy"
        assert row["cluster_id"] == "backend"
        assert row["value"] == 0.6
        assert row["op_id"] == "op-A"

    def test_persist_disabled_skips_jsonl_write(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_PERSIST_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        c.record_logprob_entropy("backend", 0.6)
        # In-memory state populated
        assert "backend" in c._states
        # JSONL not created
        assert not (tmp_path / "cur" / "backend.jsonl").exists()


# ---------------------------------------------------------------------------
# § 5 — JSONL replay
# ---------------------------------------------------------------------------


class TestJSONLReplay:
    def test_read_observations_round_trip(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
            read_observations_for_cluster,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
        )
        c = CuriosityCollector()
        for i in range(5):
            c.record_logprob_entropy(
                "backend", 0.5 + i * 0.05,
                op_id=f"op-{i}", at_unix=1000.0 + i,
            )
        observed = read_observations_for_cluster(
            "backend", history_dir=tmp_path / "cur",
        )
        assert len(observed) == 5
        assert all(
            o.source is CuriositySource.LOGPROB_ENTROPY
            for o in observed
        )
        assert observed[0].value == 0.5
        assert observed[4].value == pytest.approx(0.7)

    def test_read_observations_returns_empty_on_missing(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            read_observations_for_cluster,
        )
        result = read_observations_for_cluster(
            "nonexistent", history_dir=tmp_path / "cur",
        )
        assert result == ()

    def test_read_observations_skips_garbage_lines(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        # Manually write a JSONL with one good line + one
        # corrupted line
        history = tmp_path / "cur"
        history.mkdir(parents=True, exist_ok=True)
        path = history / "x.jsonl"
        path.write_text(
            json.dumps({
                "source": "logprob_entropy",
                "cluster_id": "x",
                "value": 0.5,
                "at_unix": 1000.0,
                "op_id": "good",
            }) + "\n"
            "{bad json{\n"
            + json.dumps({
                "source": "unknown_source",  # Invalid enum
                "cluster_id": "x",
                "value": 0.5,
                "at_unix": 1001.0,
                "op_id": "bad-source",
            }) + "\n"
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            read_observations_for_cluster,
        )
        observed = read_observations_for_cluster(
            "x", history_dir=history,
        )
        # Only the well-formed + valid-enum row survives
        assert len(observed) == 1
        assert observed[0].op_id == "good"


# ---------------------------------------------------------------------------
# § 6 — Pull-side score query
# ---------------------------------------------------------------------------


class TestScoreForCluster:
    def test_below_min_samples_returns_insufficient(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "8",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
        )
        c = CuriosityCollector()
        for _ in range(3):
            c.record_logprob_entropy(
                "backend", 0.5, at_unix=time.time(),
            )
        score = c.score_for_cluster("backend")
        assert score.dominant_source is (
            CuriositySource.INSUFFICIENT_DATA
        )

    def test_above_min_samples_aggregates(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "3",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
        )
        c = CuriosityCollector()
        now = time.time()
        for i in range(5):
            c.record_logprob_entropy(
                "backend", 0.6, at_unix=now + i * 0.001,
            )
        score = c.score_for_cluster("backend")
        assert score.dominant_source is (
            CuriositySource.LOGPROB_ENTROPY
        )
        assert score.magnitude == pytest.approx(0.6, abs=0.01)
        assert score.samples_count == 5

    def test_unknown_cluster_returns_cold_start(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriositySource,
        )
        c = CuriosityCollector()
        score = c.score_for_cluster("never-recorded")
        assert score.dominant_source is (
            CuriositySource.INSUFFICIENT_DATA
        )


# ---------------------------------------------------------------------------
# § 7 — Auto-decay
# ---------------------------------------------------------------------------


class TestAutoDecay:
    def test_stale_focus_fires_after_threshold(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        # 1-hour stale threshold so we can simulate it
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_STALE_FOCUS_HOURS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "3",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        c = CuriosityCollector()
        # Record 3 observations 2 hours ago
        two_hours_ago = time.time() - (2 * 3600)
        for _ in range(3):
            c.record_logprob_entropy(
                "backend", 0.6, at_unix=two_hours_ago,
            )
        # Score now → STALE_FOCUS
        score = c.score_for_cluster("backend")
        assert score.decay_reason is (
            CuriosityDecayReason.STALE_FOCUS
        )

    def test_recurrence_loop_fires_at_threshold(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_RECURRENCE_LOOP_THRESHOLD", "3",
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "1",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        c = CuriosityCollector()
        now = time.time()
        # Three consecutive same-source observations triggers
        # the loop
        for i in range(3):
            c.record_logprob_entropy(
                "backend", 0.6, at_unix=now + i,
            )
        score = c.score_for_cluster("backend")
        assert score.decay_reason is (
            CuriosityDecayReason.RECURRENCE_LOOP
        )

    def test_alternating_sources_dont_trigger_recurrence_loop(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_RECURRENCE_LOOP_THRESHOLD", "3",
        )
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "1",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        c = CuriosityCollector()
        now = time.time()
        # Alternate logprob and prophecy - not all same source
        c.record_logprob_entropy("backend", 0.6, at_unix=now)
        c.record_prophecy_error("backend", 0.6, at_unix=now + 1)
        c.record_logprob_entropy("backend", 0.6, at_unix=now + 2)
        score = c.score_for_cluster("backend")
        assert score.decay_reason is CuriosityDecayReason.NONE


# ---------------------------------------------------------------------------
# § 8 — Operator-explicit reset
# ---------------------------------------------------------------------------


class TestOperatorReset:
    def test_reset_marks_cluster_for_decay(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "1",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        c = CuriosityCollector()
        c.record_logprob_entropy(
            "backend", 0.9, at_unix=time.time(),
        )
        # Reset
        assert c.reset_cluster("backend") is True
        # Next score reflects OPERATOR_RESET
        score = c.score_for_cluster("backend")
        assert score.decay_reason is (
            CuriosityDecayReason.OPERATOR_RESET
        )

    def test_reset_returns_false_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        assert c.reset_cluster("backend") is False

    def test_reset_consumed_after_one_score_call(
        self, monkeypatch, tmp_path,
    ):
        """OPERATOR_RESET is one-shot — Slice 4's /curiosity
        reset behavior. Next score after reset shows
        OPERATOR_RESET; subsequent scores resume normal decay
        evaluation."""
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_MIN_SAMPLES", "1",
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        c = CuriosityCollector()
        c.record_logprob_entropy(
            "backend", 0.9, at_unix=time.time(),
        )
        c.reset_cluster("backend")
        score1 = c.score_for_cluster("backend")
        assert score1.decay_reason is (
            CuriosityDecayReason.OPERATOR_RESET
        )
        score2 = c.score_for_cluster("backend")
        # After consumption, override is gone — back to NONE
        # (no stale-focus, no recurrence-loop in this scenario)
        assert score2.decay_reason is CuriosityDecayReason.NONE


# ---------------------------------------------------------------------------
# § 9 — resolve_cluster_id (Decision A3)
# ---------------------------------------------------------------------------


class TestResolveClusterId:
    def test_explicit_label_passes_through(self):
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            resolve_cluster_id,
        )
        # No path separator, no dot → explicit label
        assert resolve_cluster_id("backend") == "backend"
        assert resolve_cluster_id("verification") == "verification"

    def test_empty_input_falls_through_to_global(self):
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            resolve_cluster_id,
        )
        assert resolve_cluster_id("") == "_global"
        assert resolve_cluster_id(None) == "_global"

    def test_path_without_index_falls_to_global(self):
        """No SemanticIndex provided → path falls to _global
        (Decision A3 graceful fallback)."""
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            resolve_cluster_id,
        )
        assert (
            resolve_cluster_id("backend/foo.py") == "_global"
        )

    def test_path_with_matching_index_returns_sem_id(self):
        """When SemanticIndex provides clusters with matching
        representative_paths, return ``sem-{id}``."""
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            resolve_cluster_id,
        )

        class FakeCluster:
            def __init__(self, cluster_id, paths):
                self.cluster_id = cluster_id
                self.representative_paths = paths

        class FakeIndex:
            clusters = (
                FakeCluster(
                    7,
                    ("backend/orchestrator.py", "backend/policy.py"),
                ),
                FakeCluster(
                    11,
                    ("frontend/app.py",),
                ),
            )

        idx = FakeIndex()
        # Direct match
        result = resolve_cluster_id(
            "backend/orchestrator.py", semantic_index=idx,
        )
        assert result == "sem-7"
        # Tail match
        result = resolve_cluster_id(
            "/abs/path/backend/policy.py", semantic_index=idx,
        )
        assert result == "sem-7"

    def test_index_with_no_match_falls_to_global(self):
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            resolve_cluster_id,
        )

        class FakeCluster:
            cluster_id = 1
            representative_paths = ("other/foo.py",)

        class FakeIndex:
            clusters = (FakeCluster(),)

        result = resolve_cluster_id(
            "backend/orchestrator.py",
            semantic_index=FakeIndex(),
        )
        assert result == "_global"

    def test_corrupted_index_falls_to_global(self):
        """Defensive — broken SemanticIndex shouldn't propagate
        up."""
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            resolve_cluster_id,
        )

        class BrokenIndex:
            @property
            def clusters(self):
                raise RuntimeError("synthetic")

        result = resolve_cluster_id(
            "backend/foo.py",
            semantic_index=BrokenIndex(),
        )
        assert result == "_global"


# ---------------------------------------------------------------------------
# § 10 — snapshot_all
# ---------------------------------------------------------------------------


class TestSnapshotAll:
    def test_empty_collector_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        assert c.snapshot_all() == ()

    def test_snapshot_contains_all_tracked_clusters(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        _isolate_history(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        c.record_logprob_entropy("a", 0.5, at_unix=time.time())
        c.record_logprob_entropy("b", 0.6, at_unix=time.time())
        c.record_logprob_entropy("c", 0.7, at_unix=time.time())
        snap = c.snapshot_all()
        assert len(snap) == 3
        cids = {s.cluster_id for s in snap}
        assert cids == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# § 11 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
        "from backend.core.ouroboros.governance.sensor_governor",
        "import anthropic",
        "from anthropic",
    )

    def test_imports_narrow_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "curiosity_collector.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"curiosity_collector.py must NOT import "
                f"{forbidden} — pure observer layer"
            )

    def test_uses_shared_scoring_primitives(self):
        """Decision E1 — recurrence count normalization MUST
        defer to _scoring_primitives.weight_score, NOT a local
        re-implementation."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "curiosity_collector.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "weight_score" in source
        assert (
            "_scoring_primitives" in source
        )

    def test_uses_cross_process_jsonl(self):
        """Decision A1 — persistence MUST use cross_process_jsonl
        (flock'd), not raw file I/O."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "curiosity_collector.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "flock_append_line" in source
        assert "flock_critical_section" in source


# ---------------------------------------------------------------------------
# § 12 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance import (
            curiosity_collector as cc,
        )
        expected = sorted([
            "CuriosityCollector",
            "curiosity_history_dir",
            "curiosity_persist_enabled",
            "curiosity_recurrence_loop_threshold",
            "curiosity_window_size",
            "get_default_collector",
            "read_observations_for_cluster",
            "reset_default_collector_for_tests",
            "resolve_cluster_id",
        ])
        assert sorted(cc.__all__) == expected

    def test_default_collector_is_singleton(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            get_default_collector,
            reset_default_collector_for_tests,
        )
        reset_default_collector_for_tests()
        c1 = get_default_collector()
        c2 = get_default_collector()
        assert c1 is c2
        reset_default_collector_for_tests()
        c3 = get_default_collector()
        assert c3 is not c1
