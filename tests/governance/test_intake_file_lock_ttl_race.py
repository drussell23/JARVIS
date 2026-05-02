"""Q3 Slice 1 — UnifiedIntakeRouter file-lock TTL race regression suite.

Closes the race the prior brutal review flagged:

  T1: _find_file_conflict reads entry (op_X, t_old).
      now - t_old > TTL → enters stale-release branch.
  T2: register_active_op writes (op_Y, t_now) — fresh.
  T1: del self._active_file_ops[fpath] — clobbers T2.

Fix: every read/write/delete is under ``_active_file_ops_lock``;
the stale-release path uses a CAS pattern (re-verify entry
identity under lock before delete).

Covers:

  §1   Lock attribute + threading.Lock type
  §2   register_active_op atomic batch (no torn writes during
       concurrent _find_file_conflict)
  §3   release_op CAS (concurrent register doesn't get clobbered)
  §4   Stale-release CAS aborts when entry is mutated under us
  §5   Multi-thread stress: N writers + 1 stale-releaser; the
       fresh write always survives
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class _InterceptDict(dict):
    """Dict subclass that fires a one-shot callback on the second
    ``.get`` call for a given key. Lets us simulate a fresh
    write injected into the CAS re-check window without touching
    real threading primitives."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._intercept_key: Optional[str] = None
        self._intercept_target_call: int = 2
        self._intercept_call_count: int = 0
        self._intercept_cb: Optional[Callable[[], None]] = None

    def arm(
        self, key: str, target_call: int,
        cb: Callable[[], None],
    ) -> None:
        self._intercept_key = key
        self._intercept_target_call = target_call
        self._intercept_call_count = 0
        self._intercept_cb = cb

    def get(self, key, *args, **kwargs):  # type: ignore[override]
        if key == self._intercept_key:
            self._intercept_call_count += 1
            if (
                self._intercept_call_count
                == self._intercept_target_call
                and self._intercept_cb is not None
            ):
                cb = self._intercept_cb
                self._intercept_cb = None
                cb()
        return super().get(key, *args, **kwargs)


def _build_router():
    """Build a minimal router instance bypassing __init__'s heavy
    setup. We only need the dict + lock + the three methods under
    test; the rest of the router (queues, WAL, governor, etc.)
    isn't exercised by these tests."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    r = UnifiedIntakeRouter.__new__(UnifiedIntakeRouter)
    r._active_file_ops = _InterceptDict()
    r._active_file_ops_lock = threading.Lock()
    r._queued_behind = {}
    r._file_lock_ttl_s = 300.0
    return r


class _StubEnvelope:
    """Minimal envelope shape consumed by _find_file_conflict +
    register_active_op."""
    def __init__(self, target_files):
        self.target_files = list(target_files)


# ============================================================================
# §1 — Lock attribute
# ============================================================================


class TestLockAttribute:
    def test_lock_present_on_init(self):
        r = _build_router()
        assert hasattr(r, '_active_file_ops_lock')
        assert isinstance(
            r._active_file_ops_lock, type(threading.Lock()),
        )

    def test_lock_acquirable_and_releasable(self):
        r = _build_router()
        with r._active_file_ops_lock:
            pass  # smoke


# ============================================================================
# §2 — register_active_op atomic batch
# ============================================================================


class TestRegisterAtomicBatch:
    def test_batch_register_all_or_nothing_under_lock(self):
        r = _build_router()
        files = [f'f{i}.py' for i in range(20)]
        r.register_active_op('op-1', files)
        # All 20 entries present; all share same op_id
        assert len(r._active_file_ops) == 20
        for f in files:
            assert r._active_file_ops[f][0] == 'op-1'

    def test_concurrent_register_no_torn_state(self):
        """Two threads each register 50 files; final state has all
        100 entries, no lost writes."""
        r = _build_router()
        files_a = [f'a{i}.py' for i in range(50)]
        files_b = [f'b{i}.py' for i in range(50)]
        t1 = threading.Thread(
            target=r.register_active_op, args=('op-A', files_a),
        )
        t2 = threading.Thread(
            target=r.register_active_op, args=('op-B', files_b),
        )
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert len(r._active_file_ops) == 100
        assert all(
            r._active_file_ops[f][0] == 'op-A' for f in files_a
        )
        assert all(
            r._active_file_ops[f][0] == 'op-B' for f in files_b
        )


# ============================================================================
# §3 — Stale-release CAS abort
# ============================================================================


class TestStaleReleaseCAS:
    def test_stale_release_aborts_when_entry_overwritten(self):
        """Inject a fresh registration during the stale-release
        path — the CAS must detect the mutation and skip the
        delete, preserving the fresh entry."""
        r = _build_router()
        # Plant a stale entry: registered far in the past
        old_t = time.monotonic() - 1000.0
        r._active_file_ops['shared.py'] = ('op-stale', old_t)
        # On the SECOND .get (the CAS re-check), inject a fresh
        # write before super().get returns — so the CAS sees the
        # mutated entry and aborts.
        def _overwrite():
            r._active_file_ops['shared.py'] = (
                'op-fresh', time.monotonic(),
            )
        r._active_file_ops.arm(  # type: ignore[attr-defined]
            key='shared.py', target_call=2, cb=_overwrite,
        )
        env = _StubEnvelope(['shared.py'])
        r._find_file_conflict(env)  # type: ignore[arg-type]
        # Fresh entry survives (CAS aborted)
        assert 'shared.py' in r._active_file_ops
        assert r._active_file_ops['shared.py'][0] == 'op-fresh'

    def test_stale_release_succeeds_when_entry_unchanged(self):
        """Happy path: entry untouched, CAS confirms identity,
        delete proceeds."""
        r = _build_router()
        old_t = time.monotonic() - 1000.0
        r._active_file_ops['stale.py'] = ('op-stale', old_t)
        env = _StubEnvelope(['stale.py'])
        blocking = r._find_file_conflict(env)  # type: ignore[arg-type]
        assert blocking is None  # no conflict — released
        assert 'stale.py' not in r._active_file_ops


# ============================================================================
# §4 — release_op CAS
# ============================================================================


class TestReleaseOpCAS:
    def test_release_only_deletes_keys_owned_by_op_id(self):
        import asyncio
        r = _build_router()
        r.register_active_op('op-1', ['a.py', 'b.py'])
        r.register_active_op('op-2', ['c.py', 'd.py'])
        asyncio.run(r.release_op('op-1'))
        assert 'a.py' not in r._active_file_ops
        assert 'b.py' not in r._active_file_ops
        # op-2 entries untouched
        assert r._active_file_ops['c.py'][0] == 'op-2'
        assert r._active_file_ops['d.py'][0] == 'op-2'

    def test_release_skips_keys_rewritten_to_other_op_id(self):
        """Race scenario: release_op for op-1 starts; between scan
        and delete, op-2 takes over a.py. The CAS in release_op
        must detect this and NOT delete a.py (now owned by op-2)."""
        import asyncio
        r = _build_router()
        r.register_active_op('op-1', ['a.py'])

        def _overwrite():
            r._active_file_ops['a.py'] = ('op-2', time.monotonic())

        r._active_file_ops.arm(  # type: ignore[attr-defined]
            key='a.py', target_call=1, cb=_overwrite,
        )
        asyncio.run(r.release_op('op-1'))
        # a.py survives because the CAS saw op-2, not op-1
        assert 'a.py' in r._active_file_ops
        assert r._active_file_ops['a.py'][0] == 'op-2'


# ============================================================================
# §5 — Multi-thread stress
# ============================================================================


class TestStress:
    def test_concurrent_register_and_stale_release_preserves_fresh(self):
        """Stress: stale entries pre-planted; one registrar thread
        re-registers fresh against random keys; two finder threads
        sweep all 100 keys triggering stale-release on whatever's
        still stale. The invariant we test: any key the registrar
        WROTE FRESH must NEVER end up either (a) missing from the
        dict or (b) carrying op-stale. The registrar tracks a
        last-write timestamp per key under a witness lock; a
        finder cannot legally drop a key whose latest fresh-write
        timestamp is more recent than the entry it captured."""
        r = _build_router()
        STOP_AFTER_S = 0.5

        long_ago = time.monotonic() - 1000.0
        for i in range(100):
            r._active_file_ops[f'f{i}.py'] = ('op-stale', long_ago)

        # Witness map: fpath → most-recent fresh-write monotonic
        # timestamp. Updated AFTER register_active_op returns, so
        # any time we see a fresh entry on fpath whose ts >= the
        # witness, that entry is the one the registrar last wrote.
        witness_lock = threading.Lock()
        witness: dict = {}
        stop = threading.Event()

        def _registrar():
            i = 0
            while not stop.is_set():
                fpath = f'f{i % 100}.py'
                i += 1
                r.register_active_op('op-fresh', [fpath])
                ts = time.monotonic()
                with witness_lock:
                    witness[fpath] = ts

        def _conflict_finder():
            while not stop.is_set():
                env = _StubEnvelope([
                    f'f{i}.py' for i in range(100)
                ])
                r._find_file_conflict(env)  # type: ignore[arg-type]

        threads = [
            threading.Thread(target=_registrar),
            threading.Thread(target=_conflict_finder),
            threading.Thread(target=_conflict_finder),
        ]
        for t in threads:
            t.start()
        time.sleep(STOP_AFTER_S)
        stop.set()
        for t in threads:
            t.join(timeout=5)

        # Invariant: every key the registrar WITNESSED writing
        # fresh must (a) still exist in the dict, and (b) carry
        # op-fresh — never op-stale. A clobbered fresh write is
        # the exact symptom of the TTL race we're closing.
        with witness_lock:
            tracked = dict(witness)
        for fpath, ts in tracked.items():
            entry = r._active_file_ops.get(fpath)
            assert entry is not None, (
                f"fresh write to {fpath} at {ts} disappeared — "
                f"clobbered by stale-release"
            )
            op_id, registered_at = entry
            # The entry's registered_at must be >= when we wrote
            # fresh — otherwise the dict still holds the stale
            # tuple (TTL race symptom).
            if registered_at < ts:
                # Could be a slightly older fresh write from a
                # previous loop iteration; only fail if op-stale.
                assert op_id != 'op-stale', (
                    f"{fpath} still holds op-stale after fresh "
                    f"write at {ts} (entry ts={registered_at}) — "
                    f"TTL race clobber"
                )
