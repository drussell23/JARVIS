"""Tests for Slice 2.1 — PostMergeAuditor: commit consequence tracker.

Coverage matrix:
  1. Record commit → deferred observations scheduled at correct intervals
  2. Observation fires → evaluate_commit called
  3. Revert detection (synthetic revert commit)
  4. Test-count delta computation
  5. MergeOutcome verdict classification
  6. Lesson text generation
  7. StrategicDirection prompt section composition
  8. Deliberately-bad-commit detection (§24.10.2 failure-mode test)
  9. Posture gate (HARDEN → skip)
  10. Memory pressure gate (HIGH/CRITICAL → skip)
  11. Master flag matrix
  12. Never-raises smoke
  13. Bounded sizes (ledger caps, file caps)
  14. JSONL persistence round-trip
  15. Cage authority invariants (no banned imports)
"""
from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_auditor(monkeypatch, tmp_path):
    """Enable the master flag and redirect paths to tmp_path."""
    monkeypatch.setenv("JARVIS_POST_MERGE_AUDITOR_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_POST_MERGE_AUDITOR_PATH",
        str(tmp_path / "merge_outcomes.jsonl"),
    )
    monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DEFERRED_OBSERVATION_PATH",
        str(tmp_path / "deferred_observations.jsonl"),
    )


@pytest.fixture
def obs_queue(tmp_path):
    from backend.core.ouroboros.governance.observability.deferred_observation import (
        DeferredObservationQueue,
    )
    return DeferredObservationQueue(
        path=tmp_path / "deferred_observations.jsonl",
    )


@pytest.fixture
def auditor(tmp_path, obs_queue):
    from backend.core.ouroboros.governance.observability.post_merge_auditor import (
        PostMergeAuditor,
    )
    return PostMergeAuditor(
        repo_root=tmp_path,  # not a real git repo — git calls will fail safely
        observation_queue=obs_queue,
        outcomes_path=tmp_path / "merge_outcomes.jsonl",
    )


# ---------------------------------------------------------------------------
# 1. Record commit → deferred observations scheduled
# ---------------------------------------------------------------------------

class TestRecordCommit:

    def test_record_returns_ok(self, auditor):
        ok, detail = auditor.record_commit(
            commit_sha="abc123def456",
            op_id="op-test-001",
        )
        assert ok is True
        assert "scheduled" in detail

    def test_observations_scheduled_at_intervals(self, auditor, obs_queue):
        auditor.record_commit(
            commit_sha="abc123def456",
            op_id="op-test-001",
        )
        pending = obs_queue.read_pending()
        assert len(pending) == 3  # 24h, 72h, 168h default
        labels = [p.metadata.get("interval") for p in pending]
        assert "24h" in labels
        assert "72h" in labels
        assert "168h" in labels

    def test_custom_intervals(self, auditor, obs_queue, monkeypatch):
        monkeypatch.setenv("JARVIS_POST_MERGE_AUDIT_INTERVALS", "1h,6h")
        auditor.record_commit(
            commit_sha="abc123def456",
            op_id="op-test-001",
        )
        pending = obs_queue.read_pending()
        assert len(pending) == 2
        labels = [p.metadata.get("interval") for p in pending]
        assert "1h" in labels
        assert "6h" in labels

    def test_metadata_propagated_to_intent(self, auditor, obs_queue):
        auditor.record_commit(
            commit_sha="sha123",
            op_id="op-meta",
            target_files=("file_a.py", "file_b.py"),
            risk_tier="SAFE_AUTO",
            signal_source="test_failure",
        )
        pending = obs_queue.read_pending()
        assert len(pending) > 0
        meta = pending[0].metadata
        assert meta["commit_sha"] == "sha123"
        assert meta["op_id"] == "op-meta"
        assert meta["risk_tier"] == "SAFE_AUTO"

    def test_empty_sha_returns_error(self, auditor):
        ok, detail = auditor.record_commit(commit_sha="", op_id="op-1")
        assert ok is False
        assert detail == "empty_commit_sha"

    def test_empty_op_id_returns_error(self, auditor):
        ok, detail = auditor.record_commit(commit_sha="abc", op_id="")
        assert ok is False
        assert detail == "empty_op_id"


# ---------------------------------------------------------------------------
# 2. Observation fires → evaluate_commit
# ---------------------------------------------------------------------------

class TestEvaluateCommit:

    def test_evaluate_returns_merge_outcome(self, auditor):
        outcome = auditor.evaluate_commit(
            commit_sha="abc123",
            op_id="op-eval",
            interval="24h",
        )
        assert outcome.commit_sha == "abc123"
        assert outcome.op_id == "op-eval"
        assert outcome.observation_interval == "24h"
        assert outcome.verdict in ("beneficial", "neutral", "harmful", "reverted")

    def test_make_observer_callback(self, auditor):
        observer = auditor.make_observer()
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            make_intent,
        )
        intent = make_intent(
            origin="post_merge_auditor",
            observation_target="commit:xyz789",
            hypothesis="no regressions at 24h",
            due_unix=time.time(),
            metadata={
                "commit_sha": "xyz789",
                "op_id": "op-obs",
                "interval": "24h",
            },
        )
        result = observer(intent)
        # Should return valid JSON
        parsed = json.loads(result)
        assert parsed["commit_sha"] == "xyz789"
        assert parsed["op_id"] == "op-obs"


# ---------------------------------------------------------------------------
# 3-4. Verdict classification and test delta
# ---------------------------------------------------------------------------

class TestVerdictClassification:

    def test_reverted_verdict(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _classify_verdict,
        )
        assert _classify_verdict(
            was_reverted=True, downstream_failures=0, test_count_delta=0,
        ) == "reverted"

    def test_harmful_verdict(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _classify_verdict,
        )
        assert _classify_verdict(
            was_reverted=False, downstream_failures=3, test_count_delta=0,
        ) == "harmful"

    def test_beneficial_verdict(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _classify_verdict,
        )
        assert _classify_verdict(
            was_reverted=False, downstream_failures=0, test_count_delta=2,
        ) == "beneficial"

    def test_neutral_verdict(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _classify_verdict,
        )
        assert _classify_verdict(
            was_reverted=False, downstream_failures=0, test_count_delta=0,
        ) == "neutral"

    def test_reverted_takes_priority(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _classify_verdict,
        )
        # Reverted should override even if there are downstream failures.
        assert _classify_verdict(
            was_reverted=True, downstream_failures=5, test_count_delta=3,
        ) == "reverted"


# ---------------------------------------------------------------------------
# 5. Lesson text generation
# ---------------------------------------------------------------------------

class TestLessonComposition:

    def test_lesson_contains_sha(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _compose_lesson,
        )
        lesson = _compose_lesson(
            commit_sha="abc123def456",
            op_id="op-lesson",
            interval="24h",
            verdict="beneficial",
            downstream_failures=0,
            was_reverted=False,
            test_count_delta=3,
            files_changed=5,
        )
        assert "abc123de" in lesson
        assert "op-lesson" in lesson

    def test_lesson_for_reverted_commit(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _compose_lesson,
        )
        lesson = _compose_lesson(
            commit_sha="rev123",
            op_id="op-rev",
            interval="72h",
            verdict="reverted",
            downstream_failures=0,
            was_reverted=True,
            test_count_delta=0,
            files_changed=2,
        )
        assert "REVERTED" in lesson

    def test_lesson_for_harmful_commit(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _compose_lesson,
        )
        lesson = _compose_lesson(
            commit_sha="harm123",
            op_id="op-harm",
            interval="24h",
            verdict="harmful",
            downstream_failures=5,
            was_reverted=False,
            test_count_delta=0,
            files_changed=3,
        )
        assert "5 downstream test failure" in lesson

    def test_lesson_bounded(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            MAX_LESSON_CHARS,
            _compose_lesson,
        )
        lesson = _compose_lesson(
            commit_sha="x" * 100,
            op_id="y" * 100,
            interval="168h",
            verdict="neutral",
            downstream_failures=0,
            was_reverted=False,
            test_count_delta=0,
            files_changed=99,
        )
        assert len(lesson) <= MAX_LESSON_CHARS


# ---------------------------------------------------------------------------
# 6. StrategicDirection prompt section
# ---------------------------------------------------------------------------

class TestPromptSection:

    def test_format_prompt_section_none_when_empty(self, auditor):
        result = auditor.format_prompt_section()
        assert result is None

    def test_format_prompt_section_with_outcomes(self, auditor):
        # Inject a synthetic outcome.
        auditor.evaluate_commit(
            commit_sha="abc123",
            op_id="op-prompt",
            interval="24h",
        )
        result = auditor.format_prompt_section()
        assert result is not None
        assert "## Recent Merge Outcomes" in result
        assert "abc123" in result

    def test_prompt_section_contains_verdict_icons(self, auditor):
        auditor.evaluate_commit(
            commit_sha="xyz",
            op_id="op-icon",
            interval="24h",
        )
        result = auditor.format_prompt_section()
        assert result is not None
        # Should contain at least one icon
        assert any(icon in result for icon in ("✅", "➖", "⚠️", "🔄"))


# ---------------------------------------------------------------------------
# 7. §24.10.2 failure-mode test: bad commits MUST be detected
# ---------------------------------------------------------------------------

class TestBadCommitDetection:

    def test_harmful_verdict_for_downstream_failures(self, auditor):
        """Simulate a commit with downstream failures."""
        # We mock _estimate_downstream_failures to return > 0.
        with mock.patch.object(
            auditor, "_estimate_downstream_failures", return_value=3,
        ):
            outcome = auditor.evaluate_commit(
                commit_sha="bad_commit",
                op_id="op-bad",
                interval="24h",
            )
        assert outcome.verdict == "harmful"
        assert outcome.downstream_failures == 3
        assert "downstream test failure" in outcome.lesson

    def test_reverted_verdict_detected(self, auditor):
        """Simulate a reverted commit."""
        with mock.patch.object(
            auditor, "_check_revert", return_value=True,
        ):
            outcome = auditor.evaluate_commit(
                commit_sha="reverted_sha",
                op_id="op-revert",
                interval="72h",
            )
        assert outcome.verdict == "reverted"
        assert outcome.was_reverted is True
        assert "REVERTED" in outcome.lesson


# ---------------------------------------------------------------------------
# 8. Posture gate
# ---------------------------------------------------------------------------

class TestPostureGate:

    def test_harden_posture_skips_evaluation(self, tmp_path, obs_queue):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            PostMergeAuditor,
        )
        auditor = PostMergeAuditor(
            repo_root=tmp_path,
            observation_queue=obs_queue,
            posture_provider=lambda: "HARDEN",
        )
        outcome = auditor.evaluate_commit(
            commit_sha="abc", op_id="op-1", interval="24h",
        )
        assert "posture_blocked" in outcome.lesson

    def test_explore_posture_allows(self, tmp_path, obs_queue):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            PostMergeAuditor,
        )
        auditor = PostMergeAuditor(
            repo_root=tmp_path,
            observation_queue=obs_queue,
            posture_provider=lambda: "EXPLORE",
        )
        outcome = auditor.evaluate_commit(
            commit_sha="abc", op_id="op-1", interval="24h",
        )
        assert "posture_blocked" not in outcome.lesson


# ---------------------------------------------------------------------------
# 9. Memory pressure gate
# ---------------------------------------------------------------------------

class TestPressureGate:

    def test_critical_pressure_skips(self, tmp_path, obs_queue):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            PostMergeAuditor,
        )
        auditor = PostMergeAuditor(
            repo_root=tmp_path,
            observation_queue=obs_queue,
            pressure_provider=lambda: "CRITICAL",
        )
        outcome = auditor.evaluate_commit(
            commit_sha="abc", op_id="op-1", interval="24h",
        )
        assert "pressure_blocked" in outcome.lesson

    def test_ok_pressure_allows(self, tmp_path, obs_queue):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            PostMergeAuditor,
        )
        auditor = PostMergeAuditor(
            repo_root=tmp_path,
            observation_queue=obs_queue,
            pressure_provider=lambda: "OK",
        )
        outcome = auditor.evaluate_commit(
            commit_sha="abc", op_id="op-1", interval="24h",
        )
        assert "pressure_blocked" not in outcome.lesson


# ---------------------------------------------------------------------------
# 10. Master flag matrix
# ---------------------------------------------------------------------------

class TestMasterFlag:

    def test_record_when_disabled(self, auditor, monkeypatch):
        monkeypatch.setenv("JARVIS_POST_MERGE_AUDITOR_ENABLED", "false")
        ok, detail = auditor.record_commit(
            commit_sha="abc", op_id="op-1",
        )
        assert ok is False
        assert detail == "master_off"

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy_values_enable(self, monkeypatch, val):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            is_auditor_enabled,
        )
        monkeypatch.setenv("JARVIS_POST_MERGE_AUDITOR_ENABLED", val)
        assert is_auditor_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy_values_disable(self, monkeypatch, val):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            is_auditor_enabled,
        )
        monkeypatch.setenv("JARVIS_POST_MERGE_AUDITOR_ENABLED", val)
        assert is_auditor_enabled() is False

    def test_default_is_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            is_auditor_enabled,
        )
        monkeypatch.delenv("JARVIS_POST_MERGE_AUDITOR_ENABLED", raising=False)
        assert is_auditor_enabled() is False


# ---------------------------------------------------------------------------
# 11. Never-raises smoke
# ---------------------------------------------------------------------------

class TestNeverRaises:

    def test_evaluate_with_no_git_repo(self, auditor):
        """evaluate_commit should not raise even with no git repo."""
        outcome = auditor.evaluate_commit(
            commit_sha="nonexistent",
            op_id="op-smoke",
            interval="24h",
        )
        assert isinstance(outcome.verdict, str)

    def test_record_with_no_queue(self, tmp_path):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            PostMergeAuditor,
        )
        auditor = PostMergeAuditor(
            repo_root=tmp_path,
            observation_queue=None,  # no queue
        )
        ok, detail = auditor.record_commit(
            commit_sha="abc", op_id="op-1",
        )
        assert ok is True
        # Should still succeed (just no observations scheduled).

    def test_crashing_posture_provider(self, tmp_path, obs_queue):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            PostMergeAuditor,
        )
        auditor = PostMergeAuditor(
            repo_root=tmp_path,
            observation_queue=obs_queue,
            posture_provider=lambda: 1/0,  # crash
        )
        outcome = auditor.evaluate_commit(
            commit_sha="abc", op_id="op-1", interval="24h",
        )
        # Should not crash — gate failure is treated as "allow".
        assert isinstance(outcome.verdict, str)


# ---------------------------------------------------------------------------
# 12. JSONL persistence round-trip
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_outcome_persists_to_jsonl(self, auditor, tmp_path):
        auditor.evaluate_commit(
            commit_sha="persist_sha",
            op_id="op-persist",
            interval="24h",
        )
        path = tmp_path / "merge_outcomes.jsonl"
        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["commit_sha"] == "persist_sha"
        assert obj["op_id"] == "op-persist"

    def test_load_recent_outcomes(self, auditor):
        for i in range(5):
            auditor.evaluate_commit(
                commit_sha=f"sha_{i}",
                op_id=f"op_{i}",
                interval="24h",
                now_unix=1000000 + i * 100,
            )
        outcomes = auditor.load_recent_outcomes(limit=3)
        assert len(outcomes) == 3
        # Should be newest first.
        assert outcomes[0].ts_unix >= outcomes[1].ts_unix

    def test_load_recent_empty_file(self, auditor):
        outcomes = auditor.load_recent_outcomes()
        assert outcomes == []


# ---------------------------------------------------------------------------
# 13. Interval parsing
# ---------------------------------------------------------------------------

class TestIntervalParsing:

    def test_default_intervals(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _parse_intervals,
        )
        intervals = _parse_intervals()
        assert len(intervals) == 3
        labels = [label for label, _ in intervals]
        assert "24h" in labels
        assert "72h" in labels
        assert "168h" in labels

    def test_custom_intervals_env(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            _parse_intervals,
        )
        monkeypatch.setenv("JARVIS_POST_MERGE_AUDIT_INTERVALS", "1h,2d,30m")
        intervals = _parse_intervals()
        assert len(intervals) == 3
        labels = [label for label, _ in intervals]
        assert "1h" in labels
        assert "2d" in labels
        assert "30m" in labels
        # Verify seconds conversion.
        seconds = {label: secs for label, secs in intervals}
        assert seconds["1h"] == 3600.0
        assert seconds["2d"] == 172800.0
        assert seconds["30m"] == 1800.0


# ---------------------------------------------------------------------------
# 14. Cage authority invariants
# ---------------------------------------------------------------------------

class TestCageAuthority:

    _BANNED_IMPORTS = frozenset({
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate",
        "semantic_guardian",
    })

    def test_no_banned_imports(self):
        src = Path(
            "backend/core/ouroboros/governance/observability/"
            "post_merge_auditor.py"
        )
        if not src.exists():
            pytest.skip("source file not found")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name
                for banned in self._BANNED_IMPORTS:
                    assert banned not in module, (
                        f"Banned import '{banned}' found in post_merge_auditor.py"
                    )


# ---------------------------------------------------------------------------
# 15. Module constants pinned
# ---------------------------------------------------------------------------

class TestModuleConstants:

    def test_constants_pinned(self):
        from backend.core.ouroboros.governance.observability import post_merge_auditor as mod
        assert mod.MAX_OUTCOMES_FILE_BYTES == 16 * 1024 * 1024
        assert mod.MAX_OUTCOMES_LOADED == 10_000
        assert mod.MAX_LESSON_CHARS == 1_000
        assert mod.MAX_TARGET_FILES == 100
        assert mod.MAX_RECENT_OUTCOMES == 10

    def test_merge_outcome_to_dict_round_trip(self):
        from backend.core.ouroboros.governance.observability.post_merge_auditor import (
            MergeOutcome,
        )
        outcome = MergeOutcome(
            commit_sha="abc",
            op_id="op-1",
            observation_interval="24h",
            downstream_failures=2,
            was_reverted=False,
            latency_delta_s=1.5,
            test_count_delta=3,
            files_changed=5,
            lesson="test lesson",
            verdict="beneficial",
            ts_unix=1234567890.0,
        )
        d = outcome.to_dict()
        assert d["commit_sha"] == "abc"
        assert d["verdict"] == "beneficial"
        assert d["downstream_failures"] == 2
        # Verify it's JSON-serializable.
        json_str = json.dumps(d)
        assert isinstance(json_str, str)
