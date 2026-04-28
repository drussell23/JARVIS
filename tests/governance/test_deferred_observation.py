"""Tests for Slice 2.3 — DeferredObservation queue.

Coverage matrix:
  1. Schedule lifecycle (happy path)
  2. Content-addressed dedup (same intent = no-op)
  3. Expiration (max_wait_s exceeded = status expired)
  4. Bounded queue (MAX_PENDING_OBSERVATIONS cap)
  5. Persistence round-trip (write → reload → state preserved)
  6. Master flag matrix (off / on / env permutations)
  7. Never-raises smoke (bad inputs)
  8. Cage authority invariants (no banned imports)
  9. Tick-driven observer callback lifecycle
  10. Complete intent lifecycle (fired → completed)
  11. Intent factory helpers
  12. Edge cases (empty strings, zero timestamps, negative max_wait)
"""
from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_deferred_observation(monkeypatch, tmp_path):
    """Enable the master flag and redirect JSONL to tmp_path for all tests."""
    monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DEFERRED_OBSERVATION_PATH",
        str(tmp_path / "deferred_observations.jsonl"),
    )


@pytest.fixture
def queue(tmp_path):
    from backend.core.ouroboros.governance.observability.deferred_observation import (
        DeferredObservationQueue,
    )
    return DeferredObservationQueue(
        path=tmp_path / "deferred_observations.jsonl",
    )


@pytest.fixture
def sample_intent():
    from backend.core.ouroboros.governance.observability.deferred_observation import (
        make_intent,
    )
    return make_intent(
        origin="test_producer",
        observation_target="commit:abc123",
        hypothesis="no new test failures",
        due_unix=time.time() + 3600,
        max_wait_s=7200.0,
        metadata={"op_id": "op-test-001", "commit_sha": "abc123"},
        now_unix=time.time(),
    )


# ---------------------------------------------------------------------------
# 1. Schedule lifecycle (happy path)
# ---------------------------------------------------------------------------

class TestScheduleLifecycle:

    def test_schedule_returns_ok(self, queue, sample_intent):
        ok, detail = queue.schedule(sample_intent)
        assert ok is True
        assert detail == "ok"

    def test_scheduled_intent_is_pending(self, queue, sample_intent):
        queue.schedule(sample_intent)
        intent = queue.get_intent(sample_intent.intent_id)
        assert intent is not None
        assert intent.status == "pending"

    def test_pending_count_increments(self, queue, sample_intent):
        assert queue.pending_count() == 0
        queue.schedule(sample_intent)
        assert queue.pending_count() == 1

    def test_read_all_returns_scheduled(self, queue, sample_intent):
        queue.schedule(sample_intent)
        all_intents = queue.read_all()
        assert len(all_intents) == 1
        assert all_intents[0].intent_id == sample_intent.intent_id

    def test_read_pending_returns_only_pending(self, queue, sample_intent):
        queue.schedule(sample_intent)
        pending = queue.read_pending()
        assert len(pending) == 1
        assert pending[0].status == "pending"


# ---------------------------------------------------------------------------
# 2. Content-addressed dedup
# ---------------------------------------------------------------------------

class TestContentAddressedDedup:

    def test_duplicate_schedule_returns_duplicate(self, queue, sample_intent):
        ok1, _ = queue.schedule(sample_intent)
        assert ok1 is True
        ok2, detail = queue.schedule(sample_intent)
        assert ok2 is False
        assert detail == "duplicate"

    def test_pending_count_not_incremented_on_dup(self, queue, sample_intent):
        queue.schedule(sample_intent)
        queue.schedule(sample_intent)
        assert queue.pending_count() == 1

    def test_same_content_produces_same_id(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            compute_intent_id,
        )
        id1 = compute_intent_id("origin_a", "target_b", "hypo_c")
        id2 = compute_intent_id("origin_a", "target_b", "hypo_c")
        assert id1 == id2
        assert len(id1) == 16

    def test_different_content_produces_different_id(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            compute_intent_id,
        )
        id1 = compute_intent_id("origin_a", "target_b", "hypo_c")
        id2 = compute_intent_id("origin_a", "target_b", "hypo_d")
        assert id1 != id2

    def test_rescheduling_terminal_intent_succeeds(self, queue, sample_intent):
        """Once an intent reaches terminal status, re-scheduling with the
        same ID should succeed (new observation cycle)."""
        queue.schedule(sample_intent)
        # Fire the intent
        now = sample_intent.due_unix + 1
        queue.tick(now, observer=lambda _: "observed")
        assert queue.pending_count() == 0
        # Re-schedule
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            make_intent,
        )
        new_intent = make_intent(
            origin=sample_intent.origin,
            observation_target=sample_intent.observation_target,
            hypothesis=sample_intent.hypothesis,
            due_unix=now + 3600,
            now_unix=now,
        )
        ok, detail = queue.schedule(new_intent)
        assert ok is True


# ---------------------------------------------------------------------------
# 3. Expiration
# ---------------------------------------------------------------------------

class TestExpiration:

    def test_expired_intent_is_marked_expired(self, queue, sample_intent):
        queue.schedule(sample_intent)
        # Jump past due + max_wait
        future = sample_intent.due_unix + sample_intent.max_wait_s + 1
        results = queue.tick(future, observer=lambda _: "should_not_fire")
        assert len(results) == 1
        assert results[0].intent.status == "expired"
        assert results[0].success is False

    def test_expire_stale_counts(self, queue, sample_intent):
        queue.schedule(sample_intent)
        future = sample_intent.due_unix + sample_intent.max_wait_s + 1
        count = queue.expire_stale(future)
        assert count == 1
        assert queue.pending_count() == 0

    def test_is_expired_logic(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            ObservationIntent,
        )
        intent = ObservationIntent(
            intent_id="test",
            origin="test",
            observation_target="x",
            hypothesis="y",
            due_unix=100.0,
            created_unix=50.0,
            max_wait_s=200.0,
        )
        # Not yet due
        assert intent.is_expired(99.0) is False
        # Due but within grace window
        assert intent.is_expired(200.0) is False
        # Past grace window
        assert intent.is_expired(301.0) is True


# ---------------------------------------------------------------------------
# 4. Bounded queue
# ---------------------------------------------------------------------------

class TestBoundedQueue:

    def test_queue_full_returns_error(self, queue):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            MAX_PENDING_OBSERVATIONS,
            make_intent,
        )
        now = time.time()
        for i in range(MAX_PENDING_OBSERVATIONS):
            intent = make_intent(
                origin=f"producer_{i}",
                observation_target=f"target_{i}",
                hypothesis=f"hypothesis_{i}",
                due_unix=now + 3600 + i,
                now_unix=now,
            )
            ok, _ = queue.schedule(intent)
            assert ok is True

        # One more should fail.
        overflow = make_intent(
            origin="overflow",
            observation_target="overflow_target",
            hypothesis="overflow_hypo",
            due_unix=now + 99999,
            now_unix=now,
        )
        ok, detail = queue.schedule(overflow)
        assert ok is False
        assert detail == "queue_full"


# ---------------------------------------------------------------------------
# 5. Persistence round-trip
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_write_reload_preserves_state(self, tmp_path, sample_intent):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            DeferredObservationQueue,
        )
        path = tmp_path / "persist_test.jsonl"

        # Write
        q1 = DeferredObservationQueue(path=path)
        q1.schedule(sample_intent)
        assert q1.pending_count() == 1

        # Reload from disk
        q2 = DeferredObservationQueue(path=path)
        assert q2.pending_count() == 1
        reloaded = q2.get_intent(sample_intent.intent_id)
        assert reloaded is not None
        assert reloaded.origin == sample_intent.origin
        assert reloaded.observation_target == sample_intent.observation_target

    def test_jsonl_file_is_valid(self, tmp_path, sample_intent):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            DeferredObservationQueue,
        )
        path = tmp_path / "valid_json.jsonl"
        q = DeferredObservationQueue(path=path)
        q.schedule(sample_intent)

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["intent_id"] == sample_intent.intent_id
        assert obj["status"] == "pending"


# ---------------------------------------------------------------------------
# 6. Master flag matrix
# ---------------------------------------------------------------------------

class TestMasterFlag:

    def test_schedule_when_disabled(self, queue, sample_intent, monkeypatch):
        monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", "false")
        ok, detail = queue.schedule(sample_intent)
        assert ok is False
        assert detail == "master_off"

    def test_tick_when_disabled(self, queue, monkeypatch):
        monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", "false")
        results = queue.tick(time.time(), observer=lambda _: "x")
        assert results == []

    def test_expire_stale_when_disabled(self, queue, monkeypatch):
        monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", "false")
        count = queue.expire_stale(time.time())
        assert count == 0

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "True"])
    def test_truthy_values_enable(self, monkeypatch, val):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            is_deferred_observation_enabled,
        )
        monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", val)
        assert is_deferred_observation_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "random"])
    def test_falsy_values_disable(self, monkeypatch, val):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            is_deferred_observation_enabled,
        )
        monkeypatch.setenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", val)
        assert is_deferred_observation_enabled() is False

    def test_default_is_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            is_deferred_observation_enabled,
        )
        monkeypatch.delenv("JARVIS_DEFERRED_OBSERVATION_ENABLED", raising=False)
        assert is_deferred_observation_enabled() is False


# ---------------------------------------------------------------------------
# 7. Never-raises smoke
# ---------------------------------------------------------------------------

class TestNeverRaises:

    def test_schedule_empty_intent_id(self, queue):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            ObservationIntent,
        )
        intent = ObservationIntent(
            intent_id="",
            origin="test",
            observation_target="x",
            hypothesis="y",
            due_unix=0,
            created_unix=0,
        )
        ok, detail = queue.schedule(intent)
        assert ok is False
        assert detail == "empty_intent_id"

    def test_schedule_empty_origin(self, queue):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            ObservationIntent,
        )
        intent = ObservationIntent(
            intent_id="abc",
            origin="",
            observation_target="x",
            hypothesis="y",
            due_unix=0,
            created_unix=0,
        )
        ok, detail = queue.schedule(intent)
        assert ok is False
        assert detail == "empty_origin"

    def test_schedule_empty_target(self, queue):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            ObservationIntent,
        )
        intent = ObservationIntent(
            intent_id="abc",
            origin="test",
            observation_target="",
            hypothesis="y",
            due_unix=0,
            created_unix=0,
        )
        ok, detail = queue.schedule(intent)
        assert ok is False
        assert detail == "empty_target"

    def test_tick_with_crashing_observer(self, queue, sample_intent):
        queue.schedule(sample_intent)
        now = sample_intent.due_unix + 1

        def crashing_observer(_):
            raise RuntimeError("observer crashed")

        results = queue.tick(now, observer=crashing_observer)
        assert len(results) == 1
        assert results[0].success is False
        assert "RuntimeError" in results[0].error

    def test_get_intent_nonexistent(self, queue):
        result = queue.get_intent("nonexistent")
        assert result is None

    def test_complete_nonexistent(self, queue):
        ok, detail = queue.complete_intent("nonexistent")
        assert ok is False
        assert detail == "not_found"


# ---------------------------------------------------------------------------
# 8. Cage authority invariants
# ---------------------------------------------------------------------------

class TestCageAuthority:

    _BANNED_IMPORTS = frozenset({
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate",
        "semantic_guardian",
    })

    def test_no_banned_imports(self):
        """The module must not import any authority module."""
        src = Path(
            "backend/core/ouroboros/governance/observability/"
            "deferred_observation.py"
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
                        f"Banned import '{banned}' found in deferred_observation.py"
                    )


# ---------------------------------------------------------------------------
# 9. Tick-driven observer callback lifecycle
# ---------------------------------------------------------------------------

class TestTickLifecycle:

    def test_tick_fires_due_intent(self, queue, sample_intent):
        queue.schedule(sample_intent)
        now = sample_intent.due_unix + 1
        results = queue.tick(now, observer=lambda _: "observation_complete")
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].result_text == "observation_complete"
        assert results[0].intent.status == "fired"

    def test_tick_does_not_fire_future_intent(self, queue, sample_intent):
        queue.schedule(sample_intent)
        now = sample_intent.due_unix - 100  # not yet due
        results = queue.tick(now, observer=lambda _: "should_not_fire")
        assert len(results) == 0
        assert queue.pending_count() == 1

    def test_tick_no_observer_auto_expires(self, queue, sample_intent):
        queue.schedule(sample_intent)
        now = sample_intent.due_unix + 1
        results = queue.tick(now, observer=None)
        assert len(results) == 1
        assert results[0].intent.status == "expired"

    def test_tick_multiple_due_intents(self, queue):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            make_intent,
        )
        now = time.time()
        for i in range(3):
            intent = make_intent(
                origin=f"producer_{i}",
                observation_target=f"target_{i}",
                hypothesis=f"hypo_{i}",
                due_unix=now - 10,  # already due
                now_unix=now - 100,
            )
            queue.schedule(intent)

        results = queue.tick(now, observer=lambda i: f"result_{i.origin}")
        assert len(results) == 3
        assert all(r.success for r in results)

    def test_observer_receives_intent(self, queue, sample_intent):
        queue.schedule(sample_intent)
        now = sample_intent.due_unix + 1
        received = []

        def capture_observer(intent):
            received.append(intent)
            return "captured"

        queue.tick(now, observer=capture_observer)
        assert len(received) == 1
        assert received[0].intent_id == sample_intent.intent_id


# ---------------------------------------------------------------------------
# 10. Complete intent lifecycle
# ---------------------------------------------------------------------------

class TestCompleteLifecycle:

    def test_complete_fired_intent(self, queue, sample_intent):
        queue.schedule(sample_intent)
        now = sample_intent.due_unix + 1
        queue.tick(now, observer=lambda _: "intermediate_result")

        ok, detail = queue.complete_intent(
            sample_intent.intent_id, "final_outcome: beneficial",
        )
        assert ok is True
        assert detail == "ok"

        intent = queue.get_intent(sample_intent.intent_id)
        assert intent is not None
        assert intent.status == "completed"
        assert "beneficial" in intent.result

    def test_complete_pending_intent_fails(self, queue, sample_intent):
        queue.schedule(sample_intent)
        ok, detail = queue.complete_intent(sample_intent.intent_id, "too_early")
        assert ok is False
        assert "wrong_status" in detail


# ---------------------------------------------------------------------------
# 11. Intent factory helpers
# ---------------------------------------------------------------------------

class TestIntentFactory:

    def test_make_intent_computes_id(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            compute_intent_id,
            make_intent,
        )
        intent = make_intent(
            origin="auditor",
            observation_target="commit:xyz",
            hypothesis="tests pass",
            due_unix=9999.0,
        )
        expected_id = compute_intent_id("auditor", "commit:xyz", "tests pass")
        assert intent.intent_id == expected_id

    def test_make_intent_default_max_wait(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            make_intent,
        )
        intent = make_intent(
            origin="a",
            observation_target="b",
            hypothesis="c",
            due_unix=0.0,
        )
        assert intent.max_wait_s == 3600.0

    def test_make_intent_custom_metadata(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            make_intent,
        )
        intent = make_intent(
            origin="a",
            observation_target="b",
            hypothesis="c",
            due_unix=0.0,
            metadata={"key": "value"},
        )
        assert intent.metadata == {"key": "value"}


# ---------------------------------------------------------------------------
# 12. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_with_status_preserves_fields(self, sample_intent):
        updated = sample_intent.with_status("fired", "result_text")
        assert updated.intent_id == sample_intent.intent_id
        assert updated.origin == sample_intent.origin
        assert updated.observation_target == sample_intent.observation_target
        assert updated.hypothesis == sample_intent.hypothesis
        assert updated.status == "fired"
        assert updated.result == "result_text"

    def test_to_dict_round_trip(self, sample_intent):
        d = sample_intent.to_dict()
        assert isinstance(d, dict)
        assert d["intent_id"] == sample_intent.intent_id
        assert d["status"] == "pending"

    def test_is_due_logic(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            ObservationIntent,
        )
        intent = ObservationIntent(
            intent_id="test",
            origin="test",
            observation_target="x",
            hypothesis="y",
            due_unix=100.0,
            created_unix=50.0,
        )
        assert intent.is_due(99.0) is False
        assert intent.is_due(100.0) is True
        assert intent.is_due(200.0) is True

    def test_is_terminal_states(self):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            ObservationIntent,
        )
        for status in ("fired", "expired", "completed"):
            intent = ObservationIntent(
                intent_id="t",
                origin="t",
                observation_target="x",
                hypothesis="y",
                due_unix=0,
                created_unix=0,
                status=status,
            )
            assert intent.is_terminal() is True

        pending = ObservationIntent(
            intent_id="t",
            origin="t",
            observation_target="x",
            hypothesis="y",
            due_unix=0,
            created_unix=0,
            status="pending",
        )
        assert pending.is_terminal() is False

    def test_result_truncation(self, queue, sample_intent):
        from backend.core.ouroboros.governance.observability.deferred_observation import (
            MAX_RESULT_CHARS,
        )
        queue.schedule(sample_intent)
        now = sample_intent.due_unix + 1
        long_result = "x" * (MAX_RESULT_CHARS + 500)
        results = queue.tick(now, observer=lambda _: long_result)
        assert len(results) == 1
        assert len(results[0].result_text) <= MAX_RESULT_CHARS

    def test_module_constants_pinned(self):
        from backend.core.ouroboros.governance.observability import deferred_observation as mod
        assert mod.MAX_PENDING_OBSERVATIONS == 100
        assert mod.MAX_INTENT_METADATA_KEYS == 32
        assert mod.MAX_HYPOTHESIS_CHARS == 500
        assert mod.MAX_TARGET_CHARS == 500
        assert mod.MAX_RESULT_CHARS == 2_000
        assert mod.MAX_LEDGER_FILE_BYTES == 8 * 1024 * 1024
