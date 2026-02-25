"""Tests for v272.x Phase 10: Split-brain guard and idempotency registry.

Validates:
1. SplitBrainGuard — canonical lock dir resolution, canary, sweep, fencing token
2. IdempotencyRegistry — dedup window, eviction, thread safety
3. OperationTracker — in-flight guard, timeout reaping, thread safety
4. Integration — wiring in supervisor_singleton, DLM, gcp_vm_manager, prime_router
"""

import importlib
import os
import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module(dotted_name: str):
    try:
        return importlib.import_module(dotted_name)
    except Exception:
        return None


# ===========================================================================
# 1. SplitBrainGuard Module
# ===========================================================================

class TestSplitBrainGuardModule:
    """Verify split_brain_guard.py module structure."""

    def test_module_imports(self):
        mod = _import_module("backend.core.split_brain_guard")
        assert mod is not None, "split_brain_guard must be importable"

    def test_canonical_lock_dir_returns_path(self):
        from backend.core.split_brain_guard import canonical_lock_dir
        result = canonical_lock_dir()
        assert result is not None
        assert result.exists()

    def test_canonical_lock_dir_consistent(self):
        from backend.core.split_brain_guard import canonical_lock_dir
        a = canonical_lock_dir()
        b = canonical_lock_dir()
        assert a == b

    def test_canonical_cross_repo_derived(self):
        from backend.core.split_brain_guard import (
            canonical_lock_dir, canonical_cross_repo_lock_dir,
        )
        cross = canonical_cross_repo_lock_dir()
        base = canonical_lock_dir()
        assert str(cross).startswith(str(base))
        assert cross.name == "cross_repo"

    def test_validate_lock_dir_writeable(self, tmp_path):
        from backend.core.split_brain_guard import validate_lock_dir_writeable
        assert validate_lock_dir_writeable(tmp_path) is True

    def test_validate_unwriteable_dir(self):
        from backend.core.split_brain_guard import validate_lock_dir_writeable
        # A path that definitely doesn't exist and can't be created
        bad = __import__("pathlib").Path("/nonexistent_root_dir_xyz/locks")
        assert validate_lock_dir_writeable(bad) is False


# ===========================================================================
# 2. LockCanary
# ===========================================================================

class TestLockCanary:
    """Verify canary write/verify/cleanup."""

    def test_write_and_verify(self, tmp_path):
        from backend.core.split_brain_guard import LockCanary
        lock_file = tmp_path / "test.lock"
        lock_file.write_text("1234")

        token = LockCanary.write(lock_file)
        assert isinstance(token, str)
        assert len(token) == 32  # uuid4 hex

        assert LockCanary.verify(lock_file, token) is True

    def test_verify_fails_on_mismatch(self, tmp_path):
        from backend.core.split_brain_guard import LockCanary
        lock_file = tmp_path / "test.lock"
        lock_file.write_text("1234")

        LockCanary.write(lock_file)
        assert LockCanary.verify(lock_file, "wrong_token") is False

    def test_verify_fails_when_missing(self, tmp_path):
        from backend.core.split_brain_guard import LockCanary
        lock_file = tmp_path / "nonexistent.lock"
        assert LockCanary.verify(lock_file, "anything") is False

    def test_cleanup_removes_canary(self, tmp_path):
        from backend.core.split_brain_guard import LockCanary
        lock_file = tmp_path / "test.lock"
        lock_file.write_text("1234")

        LockCanary.write(lock_file)
        canary_path = lock_file.with_name(lock_file.name + ".canary")
        assert canary_path.exists()

        LockCanary.cleanup(lock_file)
        assert not canary_path.exists()


# ===========================================================================
# 3. CrossDirectorySweep
# ===========================================================================

class TestCrossDirectorySweep:
    """Verify cross-directory sweep behavior."""

    def test_sweep_no_conflict_returns_none(self):
        from backend.core.split_brain_guard import CrossDirectorySweep
        # Use a lock name that definitely doesn't exist
        result = CrossDirectorySweep.sweep(
            f"nonexistent_lock_{os.getpid()}", os.getpid()
        )
        assert result is None

    def test_sweep_detects_competing_lock(self, tmp_path, monkeypatch):
        from backend.core.split_brain_guard import CrossDirectorySweep
        # Create a fake lock file with PID 1 (init, always alive)
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        lock_file = lock_dir / "supervisor.lock"
        lock_file.write_text("1\n")  # PID 1

        # Monkeypatch _build_candidate_dirs to include our tmp dir
        import backend.core.split_brain_guard as sbg
        original = sbg._build_candidate_dirs

        def patched():
            dirs = original()
            dirs.insert(0, lock_dir)
            return dirs

        monkeypatch.setattr(sbg, "_build_candidate_dirs", patched)

        result = CrossDirectorySweep.sweep("supervisor.lock", os.getpid())
        assert result is not None
        assert result["competing_pid"] == 1


# ===========================================================================
# 4. PersistentFencingToken
# ===========================================================================

class TestPersistentFencingToken:
    """Verify persistent fencing token monotonicity and persistence."""

    def test_monotonic_increment(self, tmp_path):
        from backend.core.split_brain_guard import PersistentFencingToken
        pft = PersistentFencingToken(path=tmp_path / ".fencing")
        values = [pft.next_token() for _ in range(10)]
        assert values == list(range(1, 11))

    def test_survives_reinstantiation(self, tmp_path):
        from backend.core.split_brain_guard import PersistentFencingToken
        path = tmp_path / ".fencing"
        pft1 = PersistentFencingToken(path=path)
        for _ in range(5):
            pft1.next_token()  # 1, 2, 3, 4, 5

        # New instance, same path
        pft2 = PersistentFencingToken(path=path)
        assert pft2.next_token() == 6  # Continues from 5

    def test_current_value_no_increment(self, tmp_path):
        from backend.core.split_brain_guard import PersistentFencingToken
        pft = PersistentFencingToken(path=tmp_path / ".fencing")
        pft.next_token()  # 1
        pft.next_token()  # 2
        assert pft.current_value() == 2
        assert pft.current_value() == 2  # Still 2, no increment

    def test_thread_safety(self, tmp_path):
        from backend.core.split_brain_guard import PersistentFencingToken
        pft = PersistentFencingToken(path=tmp_path / ".fencing")
        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            for _ in range(10):
                results.append(pft.next_token())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 10 threads * 10 increments = 100 tokens, all unique
        assert len(results) == 100
        assert len(set(results)) == 100
        assert max(results) == 100


# ===========================================================================
# 5. KeepaliveBreachSignal
# ===========================================================================

class TestKeepaliveBreachSignal:
    """Verify breach signal roundtrip."""

    def test_signal_check_clear(self, tmp_path):
        from backend.core.split_brain_guard import KeepaliveBreachSignal
        kbs = KeepaliveBreachSignal(lock_dir=tmp_path)

        assert kbs.check_breach("test_lock") is False
        kbs.signal_breach("test_lock")
        assert kbs.check_breach("test_lock") is True

        data = kbs.read_breach("test_lock")
        assert data is not None
        assert data["lock_name"] == "test_lock"
        assert data["pid"] == os.getpid()

        kbs.clear_breach("test_lock")
        assert kbs.check_breach("test_lock") is False


# ===========================================================================
# 6. IdempotencyRegistry
# ===========================================================================

class TestIdempotencyRegistry:
    """Verify dedup window, eviction, and thread safety."""

    def test_module_imports(self):
        mod = _import_module("backend.core.idempotency_registry")
        assert mod is not None, "idempotency_registry must be importable"

    def test_check_and_record_new_returns_true(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=10.0)
        key = IdempotencyKey("test", "resource_1")
        assert reg.check_and_record(key) is True

    def test_check_and_record_duplicate_returns_false(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=10.0)
        key = IdempotencyKey("test", "resource_1")
        reg.check_and_record(key)
        assert reg.check_and_record(key) is False

    def test_check_after_window_returns_true(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=0.05)  # 50ms window
        key = IdempotencyKey("test", "resource_1")
        reg.check_and_record(key)
        time.sleep(0.1)  # Wait for window to expire
        assert reg.check_and_record(key) is True

    def test_max_entries_eviction(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=60.0, max_entries=5)
        for i in range(6):
            reg.check_and_record(IdempotencyKey("test", f"r_{i}"))

        stats = reg.stats()
        assert stats["count"] <= 5

    def test_clear_allows_re_record(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=60.0)
        key = IdempotencyKey("test", "resource_1")
        reg.check_and_record(key)
        assert reg.check_and_record(key) is False

        reg.clear(key)
        assert reg.check_and_record(key) is True

    def test_is_duplicate_read_only(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=60.0)
        key = IdempotencyKey("test", "resource_1")
        assert reg.is_duplicate(key) is False
        reg.check_and_record(key)
        assert reg.is_duplicate(key) is True

    def test_thread_safety(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=60.0)
        key = IdempotencyKey("test", "shared_resource")
        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            results.append(reg.check_and_record(key))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 1 thread should get True, rest False
        assert sum(results) == 1

    def test_convenience_function_never_raises(self):
        from backend.core.idempotency_registry import check_idempotent
        # Should work without error even on first call
        result = check_idempotent("test_conv", "resource_conv")
        assert isinstance(result, bool)

    def test_nonce_differentiates_keys(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, IdempotencyRegistry,
        )
        reg = IdempotencyRegistry(dedup_window_s=60.0)
        k1 = IdempotencyKey("test", "resource_1", nonce="aaa")
        k2 = IdempotencyKey("test", "resource_1", nonce="bbb")
        assert reg.check_and_record(k1) is True
        assert reg.check_and_record(k2) is True  # Different nonce = new op


# ===========================================================================
# 7. OperationTracker
# ===========================================================================

class TestOperationTracker:
    """Verify in-flight operation tracking."""

    def test_start_returns_token(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("create_vm", "vm-1")
        token = tracker.start_operation(key)
        assert token is not None
        assert isinstance(token, str)

    def test_start_duplicate_returns_none(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("create_vm", "vm-1")
        tracker.start_operation(key)
        assert tracker.start_operation(key) is None

    def test_complete_allows_restart(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("create_vm", "vm-1")
        token = tracker.start_operation(key)
        tracker.complete_operation(token)
        # Should now allow a new start
        new_token = tracker.start_operation(key)
        assert new_token is not None
        assert new_token != token

    def test_fail_allows_restart(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("terminate_vm", "vm-1")
        token = tracker.start_operation(key)
        tracker.fail_operation(token)
        new_token = tracker.start_operation(key)
        assert new_token is not None

    def test_timeout_reaps_stale(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("create_vm", "vm-1")
        tracker.start_operation(key, timeout_s=0.05)  # 50ms timeout
        time.sleep(0.1)  # Wait for timeout
        # Should allow restart because stale entry was reaped
        new_token = tracker.start_operation(key)
        assert new_token is not None

    def test_is_in_flight(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("create_vm", "vm-1")
        assert tracker.is_in_flight(key) is False
        token = tracker.start_operation(key)
        assert tracker.is_in_flight(key) is True
        tracker.complete_operation(token)
        assert tracker.is_in_flight(key) is False

    def test_active_count(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        assert tracker.active_count() == 0
        tracker.start_operation(IdempotencyKey("a", "1"))
        tracker.start_operation(IdempotencyKey("b", "2"))
        assert tracker.active_count() == 2

    def test_thread_safety(self):
        from backend.core.idempotency_registry import (
            IdempotencyKey, OperationTracker,
        )
        tracker = OperationTracker()
        key = IdempotencyKey("create_vm", "vm-shared")
        tokens = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            token = tracker.start_operation(key)
            tokens.append(token)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 1 thread should get a real token, rest None
        real_tokens = [t for t in tokens if t is not None]
        assert len(real_tokens) == 1

    def test_convenience_functions_never_raise(self):
        from backend.core.idempotency_registry import (
            start_tracked_operation,
            complete_tracked_operation,
            fail_tracked_operation,
        )
        token = start_tracked_operation("test_conv", "resource_conv")
        assert token is not None
        complete_tracked_operation(token)
        fail_tracked_operation("nonexistent_token")  # Should not raise


# ===========================================================================
# 8. Integration Wiring
# ===========================================================================

class TestIntegrationWiring:
    """Verify wiring in consumer files."""

    def test_supervisor_singleton_has_canonical_lock_dir(self):
        with open("backend/core/supervisor_singleton.py", "r") as f:
            content = f.read()
        assert "canonical_lock_dir" in content

    def test_supervisor_singleton_has_canary(self):
        with open("backend/core/supervisor_singleton.py", "r") as f:
            content = f.read()
        assert "LockCanary" in content

    def test_dlm_has_canonical_cross_repo(self):
        with open("backend/core/distributed_lock_manager.py", "r") as f:
            content = f.read()
        assert "canonical_cross_repo_lock_dir" in content or "split_brain_guard" in content

    def test_dlm_has_persistent_fencing(self):
        with open("backend/core/distributed_lock_manager.py", "r") as f:
            content = f.read()
        assert "PersistentFencingToken" in content

    def test_gcp_vm_manager_has_idempotency(self):
        with open("backend/core/gcp_vm_manager.py", "r") as f:
            content = f.read()
        assert "check_idempotent" in content or "start_tracked_operation" in content

    def test_prime_router_has_idempotency(self):
        with open("backend/core/prime_router.py", "r") as f:
            content = f.read()
        assert "check_idempotent" in content

    def test_supervisor_has_broadcast_dedup(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert "_last_broadcast_cache" in content or "broadcast_dedup" in content
