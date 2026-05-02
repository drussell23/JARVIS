"""Cascading state vector fixes — unit + integration tests.

Verifies the three cascading state vector fixes from the brutal
architectural review:

  Issue 1 (§1-§4): Semantic gradient drift watcher
  Issue 2 (§5-§7): Worktree failure classification pinning
  Issue 3 (§8-§11): Lock file zombie signaling
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ===================================================================
# Issue 1: Semantic Gradient Drift Watcher
# ===================================================================


def test_s1_gradient_drift_detected_is_valid_drift_kind():
    """§1: GRADIENT_DRIFT_DETECTED is a valid DriftKind member."""
    from backend.core.ouroboros.governance.invariant_drift_auditor import (
        DriftKind,
    )

    assert hasattr(DriftKind, "GRADIENT_DRIFT_DETECTED")
    assert DriftKind.GRADIENT_DRIFT_DETECTED.value == (
        "gradient_drift_detected"
    )


def test_s2_gradient_with_stable_history_returns_empty(tmp_path):
    """§2: _compute_gradient_drift with stable history returns no records."""
    from backend.core.ouroboros.governance.invariant_drift_auditor import (
        InvariantSnapshot,
    )
    from backend.core.ouroboros.governance.invariant_drift_observer import (
        InvariantDriftObserver,
    )
    from backend.core.ouroboros.governance.invariant_drift_store import (
        InvariantDriftStore,
    )

    store = InvariantDriftStore(tmp_path, history_size=64)
    observer = InvariantDriftObserver(store)

    # Write 10 identical snapshots — stable, no gradient drift.
    for i in range(10):
        snap = InvariantSnapshot(
            snapshot_id=f"snap-{i}",
            captured_at_utc=float(i),
            shipped_invariant_names=("a", "b"),
            shipped_violation_signature="abc123",
            shipped_violation_count=0,
            flag_registry_hash="flaghash_stable",
            flag_count=10,
            exploration_floor_pins=(),
            posture_value="HARDEN",
            posture_confidence=0.9,
        )
        store.append_history(snap)

    records = observer._compute_gradient_drift()
    assert records == [], (
        "Stable history should produce zero gradient drift records"
    )


def test_s3_gradient_with_churning_hashes_returns_record(tmp_path):
    """§3: _compute_gradient_drift with many distinct hashes returns record."""
    from backend.core.ouroboros.governance.invariant_drift_auditor import (
        DriftKind,
        InvariantSnapshot,
    )
    from backend.core.ouroboros.governance.invariant_drift_observer import (
        InvariantDriftObserver,
    )
    from backend.core.ouroboros.governance.invariant_drift_store import (
        InvariantDriftStore,
    )

    store = InvariantDriftStore(tmp_path, history_size=64)
    observer = InvariantDriftObserver(store)

    # Write 5 snapshots with 5 different flag_registry_hash values.
    for i in range(5):
        snap = InvariantSnapshot(
            snapshot_id=f"snap-{i}",
            captured_at_utc=float(i),
            shipped_invariant_names=("a",),
            shipped_violation_signature="same",
            shipped_violation_count=0,
            flag_registry_hash=f"hash_{i}",
            flag_count=10,
            exploration_floor_pins=(),
            posture_value="HARDEN",
            posture_confidence=0.9,
        )
        store.append_history(snap)

    # Default threshold=3, window=10. 5 distinct hashes > 3.
    records = observer._compute_gradient_drift()
    assert len(records) >= 1
    kinds = [r.drift_kind for r in records]
    assert DriftKind.GRADIENT_DRIFT_DETECTED in kinds
    # Verify the record references the flag_registry surface.
    flag_records = [
        r for r in records
        if "flag_registry" in r.detail
    ]
    assert len(flag_records) >= 1


def test_s4_gradient_window_env_knob():
    """§4: JARVIS_INVARIANT_DRIFT_GRADIENT_WINDOW env knob is respected."""
    from backend.core.ouroboros.governance.invariant_drift_observer import (
        gradient_window,
    )

    # Default
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_INVARIANT_DRIFT_GRADIENT_WINDOW", None)
        assert gradient_window() == 10  # default

    # Override
    with patch.dict(os.environ, {
        "JARVIS_INVARIANT_DRIFT_GRADIENT_WINDOW": "20",
    }):
        assert gradient_window() == 20

    # Below floor
    with patch.dict(os.environ, {
        "JARVIS_INVARIANT_DRIFT_GRADIENT_WINDOW": "1",
    }):
        assert gradient_window() == 3  # floor

    # Above ceiling
    with patch.dict(os.environ, {
        "JARVIS_INVARIANT_DRIFT_GRADIENT_WINDOW": "999",
    }):
        assert gradient_window() == 100  # ceiling


# ===================================================================
# Issue 2: Worktree Failure Classification
# ===================================================================


def test_s5_worktree_create_failed_uses_worktree_isolation():
    """§5: worktree_create_failed failure_class is 'worktree_isolation'."""
    import inspect

    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        GenerationSubagentExecutor,
    )

    source = inspect.getsource(GenerationSubagentExecutor.execute)

    # The worktree_create_failed path must use the new failure_class.
    assert 'failure_class="worktree_isolation"' in source, (
        "GenerationSubagentExecutor.execute must use "
        "failure_class='worktree_isolation' for worktree_create_failed"
    )
    # And the error string should reference worktree_create_failed.
    assert "worktree_create_failed" in source


def test_s6_worktree_isolation_in_infra_failure_classes():
    """§6: 'worktree_isolation' is in _INFRA_FAILURE_CLASSES."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        _INFRA_FAILURE_CLASSES,
    )

    assert "worktree_isolation" in _INFRA_FAILURE_CLASSES


def test_s7_generic_executor_exception_still_uses_infra():
    """§7: Generic executor exceptions (not worktree) still use 'infra'."""
    import inspect

    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        GenerationSubagentExecutor,
    )

    source = inspect.getsource(GenerationSubagentExecutor.execute)
    # The generic except block at the bottom should still have "infra".
    assert 'failure_class="infra"' in source, (
        "Generic executor exception path must still use "
        "failure_class='infra'"
    )


# ===================================================================
# Issue 3: Lock File Zombie Signaling
# ===================================================================


def test_s8_stale_lock_emits_warning(tmp_path, caplog):
    """§8: Stale lock file (mtime > threshold) emits WARNING log."""
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        flock_append_line,
    )

    target_file = tmp_path / "test.jsonl"
    lock_file = target_file.with_suffix(".jsonl.lock")

    # Create a lock file with mtime 600s in the past.
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch()
    old_mtime = time.time() - 600
    os.utime(str(lock_file), (old_mtime, old_mtime))

    with patch.dict(os.environ, {
        "JARVIS_STALE_LOCK_AGE_S": "100",
    }):
        with caplog.at_level(logging.WARNING):
            flock_append_line(target_file, '{"test": true}')

    # Check for the stale_lock_detected log.
    stale_logs = [
        r for r in caplog.records
        if "stale_lock_detected" in r.message
    ]
    assert len(stale_logs) >= 1, (
        "Expected stale_lock_detected WARNING for lock file "
        f"with age > threshold. Got logs: {[r.message for r in caplog.records]}"
    )


def test_s9_fresh_lock_no_warning(tmp_path, caplog):
    """§9: Fresh lock file emits no stale lock warning."""
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        flock_append_line,
    )

    target_file = tmp_path / "test_fresh.jsonl"

    with patch.dict(os.environ, {
        "JARVIS_STALE_LOCK_AGE_S": "100",
    }):
        with caplog.at_level(logging.WARNING):
            flock_append_line(target_file, '{"fresh": true}')

    stale_logs = [
        r for r in caplog.records
        if "stale_lock_detected" in r.message
    ]
    assert len(stale_logs) == 0, (
        "Fresh lock file should not emit stale_lock_detected"
    )


def test_s10_successful_acquire_updates_mtime(tmp_path):
    """§10: Successful flock acquire updates the .lock file mtime."""
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        flock_append_line,
    )

    target_file = tmp_path / "test_mtime.jsonl"
    lock_file = target_file.with_suffix(".jsonl.lock")

    # Create lock file with old mtime.
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch()
    old_mtime = time.time() - 600
    os.utime(str(lock_file), (old_mtime, old_mtime))

    before_write = time.time()
    flock_append_line(target_file, '{"update_test": true}')

    # After acquire, mtime should be updated to ~now.
    if lock_file.exists():
        new_mtime = lock_file.stat().st_mtime
        assert new_mtime >= before_write - 1.0, (
            f"Lock file mtime should have been updated to ~now. "
            f"Got {new_mtime}, expected >= {before_write - 1.0}"
        )


def test_s11_stale_lock_age_env_knob():
    """§11: JARVIS_STALE_LOCK_AGE_S env knob is respected."""
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        stale_lock_age_s,
    )

    # Default
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_STALE_LOCK_AGE_S", None)
        assert stale_lock_age_s() == 300.0  # default

    # Override
    with patch.dict(os.environ, {"JARVIS_STALE_LOCK_AGE_S": "60"}):
        assert stale_lock_age_s() == 60.0

    # Below floor
    with patch.dict(os.environ, {"JARVIS_STALE_LOCK_AGE_S": "1"}):
        assert stale_lock_age_s() == 10.0  # floor

    # Above ceiling
    with patch.dict(os.environ, {
        "JARVIS_STALE_LOCK_AGE_S": "999999",
    }):
        assert stale_lock_age_s() == 86400.0  # ceiling
