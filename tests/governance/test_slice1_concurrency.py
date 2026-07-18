"""Slice 1 concurrency remediation — lessons lock + atomic file-op claim.

The audit found two unguarded shared-state seams under the 3-worker
BackgroundAgentPool sharing ONE orchestrator/GLS:

  * ``_session_lessons`` — compound append+cap+rebind and the convergence-negative
    clear+counter-reset interleave across workers (and real threads: wall-clock
    watchdog, embed pool). Now serialized by ``_session_lessons_lock``
    (threading.RLock — the mutation sites are sync methods, so an asyncio.Lock is
    structurally unusable there; RLock also covers real threads, matching the
    in_flight_registry precedent).
  * ``_active_file_ops`` — the split-brain guard's membership CHECK sat ~40 lines
    before the ADD (TOCTOU): correct only while no await sat between them. Now
    ``_try_claim_file_ops`` performs check-and-set in ONE synchronous,
    all-or-nothing operation the event loop cannot interleave.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator


# ---------------------------------------------------------------------------
# Minimal instances — object.__new__ + just the attrs the methods touch
# (same fixture discipline as the provider _entitlement_filter tests).
# ---------------------------------------------------------------------------


def _orch():
    o = object.__new__(GovernedOrchestrator)
    o._state = SimpleNamespace(session_lessons=[])
    o._session_lessons_max = 20
    o._session_lessons_lock = threading.RLock()
    o._stack = SimpleNamespace(comm=SimpleNamespace(_transports=[]))
    return o


def _gls():
    g = object.__new__(GovernedLoopService)
    g._active_file_ops = set()
    return g


# ===========================================================================
# A. _session_lessons — serialized compound mutations
# ===========================================================================


def test_lessons_lock_exists_and_is_reentrant():
    o = _orch()
    with o._session_lessons_lock:
        with o._session_lessons_lock:      # RLock: reentrant acquire must not deadlock
            o._add_session_lesson("code", "lesson")
    assert len(o._session_lessons) == 1


def test_concurrent_threaded_appends_never_exceed_cap_or_corrupt():
    """8 real threads × 200 appends + 2 clear threads hammering the buffer.
    Under the lock: no exception, cap invariant holds at every observation,
    every element stays a well-formed (type, text) tuple."""
    o = _orch()
    errors: list = []
    stop = threading.Event()

    def _writer(n):
        try:
            for i in range(200):
                o._add_session_lesson("code", f"w{n}-{i}")
                snap_len = len(o._session_lessons)
                assert snap_len <= o._session_lessons_max + 8  # bounded even mid-interleave
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _clearer():
        try:
            while not stop.is_set():
                with o._session_lessons_lock:
                    o._session_lessons.clear()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    writers = [threading.Thread(target=_writer, args=(n,)) for n in range(8)]
    clearers = [threading.Thread(target=_clearer) for _ in range(2)]
    for t in writers + clearers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in clearers:
        t.join()

    assert not errors, errors
    assert len(o._session_lessons) <= o._session_lessons_max
    assert all(isinstance(x, tuple) and len(x) == 2 for x in o._session_lessons)


def test_cap_rebind_is_atomic_with_reads():
    """The cap path REBINDS the list (slice assignment through the property).
    Interleaved threaded readers must always see a list, never a torn state."""
    o = _orch()
    o._session_lessons_max = 5
    errors: list = []

    def _reader():
        try:
            for _ in range(500):
                with o._session_lessons_lock:
                    snapshot = list(o._session_lessons)
                assert len(snapshot) <= 5
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _writer():
        try:
            for i in range(500):
                o._add_session_lesson("infra", f"x{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=_reader) for _ in range(4)] + [
        threading.Thread(target=_writer) for _ in range(4)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    assert len(o._session_lessons) <= 5


# ===========================================================================
# B. _try_claim_file_ops — atomic all-or-nothing check-and-set
# ===========================================================================


def test_claim_success_then_conflict(tmp_path):
    g = _gls()
    f1, f2 = tmp_path / "a.py", tmp_path / "b.py"
    claimed, conflict = g._try_claim_file_ops([str(f1), str(f2)])
    assert conflict is None and claimed is not None and len(claimed) == 2
    # Second claim on an overlapping set → conflict, and (all-or-nothing)
    # the NON-overlapping file must NOT be claimed.
    f3 = tmp_path / "c.py"
    claimed2, conflict2 = g._try_claim_file_ops([str(f3), str(f1)])
    assert claimed2 is None and conflict2 == str(f1.resolve())
    assert str(f3.resolve()) not in g._active_file_ops   # nothing partially claimed


def test_claim_release_reclaim(tmp_path):
    g = _gls()
    f = tmp_path / "a.py"
    claimed, _ = g._try_claim_file_ops([str(f)])
    for c in claimed:
        g._active_file_ops.discard(c)                    # the finally-loop's release
    reclaimed, conflict = g._try_claim_file_ops([str(f)])
    assert conflict is None and reclaimed


def test_empty_targets_claim_trivially():
    g = _gls()
    claimed, conflict = g._try_claim_file_ops([])
    assert claimed == [] and conflict is None


@pytest.mark.asyncio
async def test_concurrent_asyncio_tasks_exactly_one_winner(tmp_path):
    """The REAL concurrency model: N asyncio tasks (the worker pool) racing to
    claim the same file. The claim is synchronous — the event loop structurally
    cannot interleave check and set — so EXACTLY one task wins, regardless of
    scheduling order. This is the split-brain double-apply the TOCTOU allowed."""
    g = _gls()
    f = str(tmp_path / "hot.py")
    results: list = []

    async def _racer():
        await asyncio.sleep(0)                    # force scheduling interleave
        claimed, conflict = g._try_claim_file_ops([f])
        results.append(claimed is not None)
        await asyncio.sleep(0)

    await asyncio.gather(*[_racer() for _ in range(16)])
    assert results.count(True) == 1, f"{results.count(True)} winners — split-brain!"
    assert results.count(False) == 15


def test_claim_canonicalizes_paths(tmp_path):
    """Two spellings of the same file must conflict (resolve() canonicalization
    — the guard's original intent)."""
    g = _gls()
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    spelled_direct = str(f)
    spelled_dotted = str(tmp_path / "." / "a.py")
    claimed, _ = g._try_claim_file_ops([spelled_direct])
    assert claimed
    claimed2, conflict = g._try_claim_file_ops([spelled_dotted])
    assert claimed2 is None and conflict == str(f.resolve())
