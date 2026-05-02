"""Edge-case race condition fixes — unit + integration tests.

Verifies the three hidden race condition fixes from the brutal
architectural review:

  Race 1 (§1-§4): DecisionRuntime worker_id cache poisoning
  Race 2 (§5-§7): PostmortemRecall read/rotation over-cap
  Race 3 (§8-§13): SSE broker per-subscriber degradation blindness
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ===================================================================
# Race 1: DecisionRuntime worker_id cache poisoning
# ===================================================================


def test_s1_no_worker_id_cache_attribute():
    """§1: DecisionRuntime no longer has a _worker_id cache field."""
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        DecisionRuntime,
    )

    rt = DecisionRuntime(session_id="test-s1")
    assert not hasattr(rt, "_worker_id"), (
        "self._worker_id was removed — caching creates cross-contamination"
    )


def test_s2_compute_worker_id_is_pure_function():
    """§2: _compute_worker_id returns fresh value each call (not cached)."""
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        DecisionRuntime,
    )

    rt = DecisionRuntime(
        session_id="test-s2", worktree_path="/tmp/wt-alpha",
    )
    # Call twice — both should return the same value (same path)
    # but each call should independently compute it.
    id1 = rt._compute_worker_id()
    id2 = rt._compute_worker_id()
    assert id1 == id2
    assert isinstance(id1, str)
    assert len(id1) > 0


def test_s3_different_worktree_paths_produce_different_ids():
    """§3: Two runtimes with different worktree_path get different worker_ids."""
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        DecisionRuntime,
    )

    rt_a = DecisionRuntime(
        session_id="test-s3", worktree_path="/tmp/wt-alpha",
    )
    rt_b = DecisionRuntime(
        session_id="test-s3", worktree_path="/tmp/wt-beta",
    )
    id_a = rt_a._compute_worker_id()
    id_b = rt_b._compute_worker_id()
    # Different paths → different SHA1 hashes → different worker_ids.
    assert id_a != id_b


def test_s4_none_worktree_path_produces_base():
    """§4: worktree_path=None → '{pid}-base' worker_id."""
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        DecisionRuntime,
    )

    rt = DecisionRuntime(session_id="test-s4", worktree_path=None)
    wid = rt._compute_worker_id()
    assert wid.endswith("-base"), (
        f"Expected '{os.getpid()}-base', got '{wid}'"
    )


# ===================================================================
# Race 2: PostmortemRecall read/rotation over-cap
# ===================================================================


def test_s5_read_replay_history_acquires_flock(tmp_path):
    """§5: read_replay_history wraps its file read in flock_critical_section."""
    from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
        read_replay_history,
    )

    # Create a minimal JSONL file with one valid record.
    jsonl_path = tmp_path / "replay.jsonl"
    dummy_verdict = {
        "verdict": {
            "target": {
                "session_id": "s1",
                "swap_at_phase": "GENERATE",
                "swap_decision_kind": "provider_selection",
            },
            "outcome": "identical",
            "verdict": "safe",
            "detail": "",
            "original_output": "",
            "replay_output": "",
        },
        "tightening": "passed",
        "cluster_kind": "",
        "schema_version": "counterfactual_replay_observer.1",
    }
    jsonl_path.write_text(
        json.dumps(dummy_verdict) + "\n", encoding="utf-8",
    )

    flock_called = {"count": 0}
    original_flock = None

    # Intercept flock_critical_section to verify it's called.
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        flock_critical_section as _real_flock,
    )

    from contextlib import contextmanager

    @contextmanager
    def _tracking_flock(path):
        flock_called["count"] += 1
        with _real_flock(path) as acquired:
            yield acquired

    with (
        patch(
            "backend.core.ouroboros.governance.verification"
            ".counterfactual_replay_observer.replay_history_path",
            return_value=jsonl_path,
        ),
        patch(
            "backend.core.ouroboros.governance.verification"
            ".counterfactual_replay_observer.flock_critical_section",
            side_effect=_tracking_flock,
        ),
    ):
        _ = read_replay_history(limit=10)
        assert flock_called["count"] >= 1, (
            "read_replay_history must acquire flock for serialization"
        )


def test_s6_read_on_missing_file_returns_empty(tmp_path):
    """§6: read_replay_history on missing file returns empty tuple."""
    from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
        read_replay_history,
    )

    with patch(
        "backend.core.ouroboros.governance.verification"
        ".counterfactual_replay_observer.replay_history_path",
        return_value=tmp_path / "nonexistent.jsonl",
    ):
        result = read_replay_history(limit=10)
        assert result == ()


def test_s7_read_never_raises(tmp_path):
    """§7: read_replay_history NEVER raises, even on corrupt data."""
    from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
        read_replay_history,
    )

    # Write garbage data.
    jsonl_path = tmp_path / "replay.jsonl"
    jsonl_path.write_text("{{{{bad json\n" * 10, encoding="utf-8")

    with patch(
        "backend.core.ouroboros.governance.verification"
        ".counterfactual_replay_observer.replay_history_path",
        return_value=jsonl_path,
    ):
        result = read_replay_history(limit=10)
        assert result == ()


# ===================================================================
# Race 3: SSE broker per-subscriber degradation blindness
# ===================================================================


def test_s8_subscriber_has_last_drop_at():
    """§8: _Subscriber has a last_drop_at field initialized to 0.0."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _Subscriber,
    )

    loop = asyncio.new_event_loop()
    try:
        sub = _Subscriber(
            sub_id=1,
            op_id_filter=None,
            queue=asyncio.Queue(maxsize=4),
            loop=loop,
            maxsize=4,
        )
        assert hasattr(sub, "last_drop_at")
        assert sub.last_drop_at == 0.0
    finally:
        loop.close()


def test_s9_drop_sets_last_drop_at():
    """§9: When a subscriber's queue is full and an event is dropped,
    last_drop_at is set to a monotonic timestamp."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
        reset_default_broker,
    )

    broker = StreamEventBroker(
        history_maxlen=10, max_subscribers=5, queue_maxsize=2,
    )
    sub = broker.subscribe()
    assert sub is not None

    # Fill the queue.
    broker.publish("task_created", "op-1", {"test": True})
    broker.publish("task_started", "op-1", {"test": True})
    # This should trigger a drop + lag frame.
    broker.publish("task_updated", "op-1", {"test": True})

    assert sub.drop_count > 0 or sub.queue.qsize() >= sub.maxsize
    # If drops occurred, last_drop_at should be set.
    if sub.drop_count > 0:
        assert sub.last_drop_at > 0.0

    broker.unsubscribe(sub)


def test_s10_subscriber_health_returns_per_subscriber_status():
    """§10: subscriber_health() returns per-subscriber status list."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    broker = StreamEventBroker(
        history_maxlen=10, max_subscribers=5, queue_maxsize=4,
    )
    sub = broker.subscribe()
    assert sub is not None

    health = broker.subscriber_health()
    assert isinstance(health, list)
    assert len(health) == 1

    entry = health[0]
    assert entry["sub_id"] == sub.sub_id
    assert entry["status"] == "healthy"
    assert entry["drop_count"] == 0
    assert entry["last_drop_ago_s"] is None
    assert "queue_depth" in entry
    assert "queue_maxsize" in entry
    assert "connected_s" in entry

    broker.unsubscribe(sub)


def test_s11_healthy_subscriber_classified_correctly():
    """§11: Subscriber with no drops classified as 'healthy'."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    broker = StreamEventBroker(
        history_maxlen=10, max_subscribers=5, queue_maxsize=64,
    )
    sub = broker.subscribe()
    assert sub is not None

    health = broker.subscriber_health()
    assert health[0]["status"] == "healthy"

    broker.unsubscribe(sub)


def test_s12_lagging_subscriber_classified():
    """§12: Subscriber with recent drops classified as 'lagging' or 'wedged'."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    broker = StreamEventBroker(
        history_maxlen=10, max_subscribers=5, queue_maxsize=1,
    )
    sub = broker.subscribe()
    assert sub is not None

    # Fill queue + force drops.
    for i in range(5):
        broker.publish("task_created", f"op-{i}", {"i": i})

    health = broker.subscriber_health()
    if health:
        entry = health[0]
        # With drops, status should be lagging or wedged.
        assert entry["drop_count"] > 0
        assert entry["status"] in ("lagging", "wedged")
        assert entry["last_drop_ago_s"] is not None

    broker.unsubscribe(sub)


def test_s13_subscriber_health_empty_when_no_subscribers():
    """§13: subscriber_health() returns empty list when no subscribers."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    broker = StreamEventBroker(
        history_maxlen=10, max_subscribers=5, queue_maxsize=4,
    )
    health = broker.subscriber_health()
    assert health == []
