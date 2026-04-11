"""Tests for ForwardProgressDetector."""
from __future__ import annotations

import hashlib
import time

from backend.core.ouroboros.governance.forward_progress import (
    ForwardProgressConfig,
    ForwardProgressDetector,
    candidate_content_hash,
)


# ---------------------------------------------------------------------------
# candidate_content_hash()
# ---------------------------------------------------------------------------

class TestCandidateContentHash:
    def test_trusts_upstream_hash(self):
        h = candidate_content_hash({"candidate_hash": "abc123", "full_content": "xyz"})
        assert h == "abc123"

    def test_single_file_full_content(self):
        cand = {"full_content": "def foo(): return 1"}
        h = candidate_content_hash(cand)
        expected = hashlib.sha256(b"def foo(): return 1").hexdigest()
        assert h == expected

    def test_single_file_raw_content_fallback(self):
        cand = {"raw_content": "def bar(): return 2"}
        h = candidate_content_hash(cand)
        expected = hashlib.sha256(b"def bar(): return 2").hexdigest()
        assert h == expected

    def test_multi_file_determinism(self):
        cand_a = {"files": [
            {"file_path": "a.py", "full_content": "x = 1"},
            {"file_path": "b.py", "full_content": "y = 2"},
        ]}
        cand_b = {"files": [
            {"file_path": "a.py", "full_content": "x = 1"},
            {"file_path": "b.py", "full_content": "y = 2"},
        ]}
        assert candidate_content_hash(cand_a) == candidate_content_hash(cand_b)

    def test_multi_file_different_content_differs(self):
        cand_a = {"files": [{"file_path": "a.py", "full_content": "x = 1"}]}
        cand_b = {"files": [{"file_path": "a.py", "full_content": "x = 2"}]}
        assert candidate_content_hash(cand_a) != candidate_content_hash(cand_b)

    def test_multi_file_file_path_matters(self):
        cand_a = {"files": [{"file_path": "a.py", "full_content": "x = 1"}]}
        cand_b = {"files": [{"file_path": "b.py", "full_content": "x = 1"}]}
        assert candidate_content_hash(cand_a) != candidate_content_hash(cand_b)

    def test_empty_candidate_returns_empty(self):
        assert candidate_content_hash(None) == ""
        assert candidate_content_hash({}) == ""
        assert candidate_content_hash({"other_key": "value"}) == ""

    def test_duck_typed_object(self):
        class _Fake:
            full_content = "def baz(): pass"
        h = candidate_content_hash(_Fake())
        expected = hashlib.sha256(b"def baz(): pass").hexdigest()
        assert h == expected


# ---------------------------------------------------------------------------
# ForwardProgressDetector.observe() + repeat semantics
# ---------------------------------------------------------------------------

class TestDetectorObserve:
    def _det(self, max_repeats: int = 2) -> ForwardProgressDetector:
        return ForwardProgressDetector(config=ForwardProgressConfig(
            max_repeats=max_repeats, ttl_s=3600.0, enabled=True,
        ))

    def test_first_observation_not_stuck(self):
        det = self._det()
        assert not det.observe("op-1", "hash-a")

    def test_two_identical_hashes_trips(self):
        det = self._det(max_repeats=2)
        assert not det.observe("op-2", "hash-a")  # 1st
        assert det.observe("op-2", "hash-a")       # 2nd → stuck

    def test_different_hashes_reset_counter(self):
        det = self._det(max_repeats=2)
        det.observe("op-3", "hash-a")  # 1
        det.observe("op-3", "hash-b")  # reset, 1
        # Not stuck because counter reset
        assert not det.is_tripped("op-3")
        assert det.observe("op-3", "hash-b")  # 2 → stuck

    def test_higher_max_repeats(self):
        det = self._det(max_repeats=5)
        # 4 repeats is fine
        for _ in range(4):
            det.observe("op-4", "hash-a")
        assert not det.is_tripped("op-4")
        # 5th trips
        assert det.observe("op-4", "hash-a")

    def test_empty_hash_is_noop(self):
        det = self._det()
        det.observe("op-5", "")
        det.observe("op-5", "")
        det.observe("op-5", "")
        assert not det.is_tripped("op-5")
        assert det.active_op_count() == 0  # no entry created

    def test_stays_tripped_after_trip(self):
        det = self._det(max_repeats=2)
        det.observe("op-6", "hash-a")
        det.observe("op-6", "hash-a")  # trip
        # Subsequent different hash doesn't un-trip
        assert det.observe("op-6", "hash-b")  # still stuck
        assert det.is_tripped("op-6")

    def test_multiple_ops_independent(self):
        det = self._det(max_repeats=2)
        det.observe("op-7a", "hash-x")
        det.observe("op-7a", "hash-x")  # 7a stuck
        assert det.is_tripped("op-7a")
        assert not det.is_tripped("op-7b")
        det.observe("op-7b", "hash-y")
        assert not det.is_tripped("op-7b")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_finish_removes_entry_and_returns_summary(self):
        det = ForwardProgressDetector()
        det.observe("op-f1", "hash-a")
        det.observe("op-f1", "hash-a")
        summary = det.finish("op-f1")
        assert summary is not None
        assert summary["tripped"] is True
        assert summary["repeat_count"] == 2
        assert det.active_op_count() == 0

    def test_finish_unknown_op_returns_none(self):
        det = ForwardProgressDetector()
        assert det.finish("nonexistent") is None

    def test_summary_without_removal(self):
        det = ForwardProgressDetector()
        det.observe("op-f2", "hash-a")
        s1 = det.summary("op-f2")
        s2 = det.summary("op-f2")
        assert s1 is not None
        assert s2 is not None
        assert det.active_op_count() == 1


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------

class TestTTL:
    def test_stale_entries_pruned_on_observe(self):
        det = ForwardProgressDetector(config=ForwardProgressConfig(
            max_repeats=2, ttl_s=0.01, enabled=True,
        ))
        det.observe("op-t1", "hash-a")
        det.observe("op-t2", "hash-b")
        assert det.active_op_count() == 2
        time.sleep(0.02)
        det.observe("op-t3", "hash-c")  # triggers prune
        assert det.summary("op-t1") is None
        assert det.summary("op-t2") is None
        assert det.summary("op-t3") is not None


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------

class TestDisabled:
    def test_disabled_detector_never_trips(self):
        det = ForwardProgressDetector(config=ForwardProgressConfig(
            max_repeats=1, enabled=False,
        ))
        for _ in range(10):
            assert not det.observe("op-d", "hash-a")
        assert not det.is_tripped("op-d")


# ---------------------------------------------------------------------------
# Orchestrator integration — the detector is wired onto GovernedOrchestrator
# ---------------------------------------------------------------------------

class TestOrchestratorWiring:
    def test_orchestrator_has_detector_attribute(self, tmp_path):
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        cfg = OrchestratorConfig(project_root=tmp_path)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=cfg,
        )
        assert hasattr(orch, "_forward_progress")
        assert isinstance(orch._forward_progress, ForwardProgressDetector)
