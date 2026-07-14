"""Task #8 — determinism concurrency / robustness fixes (RSI Gear 1).

Four defects in the determinism substrate that break replay/determinism
under fan-out. Each test here proves the corrected contract AND guards
against a silent re-severing.

  (b) Replay non-idempotency — a REPLAY miss used to APPEND to the very
      ledger it was replaying (decision_runtime.decide), so running
      --replay twice produced different ledgers and corrupted the
      last-write-wins index on the next pass. Replay must be read-only.

  (c) Shared RNG race — entropy_for() returns a SHARED cached
      DeterministicEntropy per (session, op_id); the cache lock guarded
      only the lookup, not the stream draws, so concurrent .random()/
      .randbytes()/.uuid4() raced on the Mersenne-Twister state and
      produced a byte stream no replay could reproduce. Fixed with a
      per-stream draw lock.

  (d) Clock exhaustion livelock — FrozenClock froze monotonic() at the
      last recorded value past trace-end, so any `while now < deadline`
      loop spun forever. Fixed with a strictly-increasing deterministic
      synthetic tail.

  (a) The per-worker ENFORCE flag is graduated-true but INERT (the
      replay index is not worker-keyed because worker_id embeds
      os.getpid(), which no replay process can reproduce). Rather than
      hide the gap, record() surfaces it once — that observability is
      the fix this pass ships for (a); authoritative per-worker replay
      is a total-order-merge design slice.
"""
from __future__ import annotations

import logging
import threading

import pytest

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    decide,
    reset_all_for_tests,
    runtime_for_session,
)
from backend.core.ouroboros.governance.determinism.entropy import (
    DeterministicEntropy,
)
from backend.core.ouroboros.governance.determinism.clock import (
    FrozenClock,
    _exhaustion_tick_s,
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "conc-fix-session")
    reset_all_for_tests()
    yield tmp_path / "det"
    reset_all_for_tests()


# ── (b) replay is read-only / idempotent ─────────────────────────────

@pytest.mark.asyncio
async def test_replay_pass_never_mutates_the_ledger(isolated, monkeypatch):
    """THE bug-b proof: record a session, snapshot the ledger bytes,
    replay it, and assert the ledger file is byte-for-byte unchanged.
    A replay that appends (the old behavior) fails this."""
    # 1) Record two decisions.
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")

    async def c1():
        return {"v": 1}

    async def c2():
        return {"v": 2}

    await decide(op_id="op-A", phase="P", kind="K", inputs={}, compute=c1)
    await decide(op_id="op-A", phase="P", kind="K", inputs={}, compute=c2)

    rt = runtime_for_session()
    ledger_path = rt._resolved_path()  # noqa: SLF001 — test introspection
    before = ledger_path.read_bytes()
    assert before  # sanity: something was written

    # 2) Replay the SAME two decisions — must not write.
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")
    reset_all_for_tests()  # fresh runtime, same on-disk ledger

    async def never():
        raise AssertionError("compute must not run on a replay HIT")

    out1 = await decide(
        op_id="op-A", phase="P", kind="K", inputs={}, compute=never,
    )
    out2 = await decide(
        op_id="op-A", phase="P", kind="K", inputs={}, compute=never,
    )
    assert out1 == {"v": 1} and out2 == {"v": 2}

    after = ledger_path.read_bytes()
    assert after == before  # READ-ONLY replay


@pytest.mark.asyncio
async def test_double_replay_is_idempotent(isolated, monkeypatch):
    """Replaying a session with a MISS twice yields byte-identical
    ledgers each pass (the old append-on-miss diverged them)."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")

    async def rec():
        return {"ok": True}

    await decide(op_id="op-R", phase="P", kind="K", inputs={}, compute=rec)
    rt = runtime_for_session()
    ledger_path = rt._resolved_path()  # noqa: SLF001
    baseline = ledger_path.read_bytes()

    async def live():
        return {"ok": False}

    for _ in range(2):
        monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")
        reset_all_for_tests()
        # A miss (op never recorded) must not append on either pass.
        await decide(
            op_id="op-MISS", phase="P", kind="K", inputs={}, compute=live,
        )
        assert ledger_path.read_bytes() == baseline


# ── (c) shared RNG draw is thread-safe + deterministic ───────────────

def test_concurrent_draws_match_sequential_multiset():
    """THE bug-c proof: N threads drawing K times each from ONE shared
    stream collect exactly the multiset of the first N*K sequential
    draws of a fresh same-seed stream. With the draw lock, each draw is
    atomic so interleaving only permutes the order — the multiset is
    invariant. Without the lock, torn MT state diverges the values."""
    seed = 0xC0FFEE
    n_threads, k = 8, 500
    shared = DeterministicEntropy(seed)
    collected: list = []
    collected_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()  # maximize contention
        local = [shared.random() for _ in range(k)]
        with collected_lock:
            collected.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reference = DeterministicEntropy(seed)
    expected = [reference.random() for _ in range(n_threads * k)]

    assert len(collected) == n_threads * k
    # No torn draws: the value multiset is exactly the sequential one.
    assert sorted(collected) == sorted(expected)
    # And no duplicates beyond what the sequential stream itself has
    # (a corrupted MT typically repeats values).
    assert len(set(collected)) == len(set(expected))


def test_concurrent_uuid4_all_unique_and_wellformed():
    """uuid4() holds the lock across its whole 16-byte draw, so
    concurrent callers never interleave into each other's slice."""
    shared = DeterministicEntropy(0x1234)
    out: list = []
    out_lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        local = [shared.uuid4() for _ in range(200)]
        with out_lock:
            out.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(out) == 1200
    assert len(set(out)) == 1200  # every UUID distinct (no torn slices)
    for u in out:
        assert u.version == 4


# ── (d) clock exhaustion never livelocks ─────────────────────────────

def test_exhausted_clock_deadline_loop_terminates():
    """THE bug-d proof: a `while now < deadline` loop driven by an
    EXHAUSTED FrozenClock MUST terminate. The old frozen-last-value
    behavior spun forever. We bound the loop so a regression fails
    (assert) instead of hanging the suite."""
    fc = FrozenClock(op_id="op-d")
    fc.import_trace(monotonic=[10.0])
    start = fc.monotonic()  # consumes the single recorded value → 10.0
    deadline = start + 0.001  # 1ms; synthetic tick is 1µs → ~1000 steps

    iterations = 0
    while fc.monotonic() < deadline:
        iterations += 1
        assert iterations < 100_000, "clock froze → deadline loop livelocked"
    assert iterations > 0  # it actually looped, then escaped


def test_exhaustion_tail_strictly_monotonic_and_deterministic():
    a = FrozenClock(op_id="op-1")
    b = FrozenClock(op_id="op-2")
    a.import_trace(monotonic=[5.0])
    b.import_trace(monotonic=[5.0])
    a.monotonic()  # consume recorded
    b.monotonic()
    seq_a = [a.monotonic() for _ in range(50)]
    seq_b = [b.monotonic() for _ in range(50)]
    assert seq_a == seq_b  # deterministic: same replay → same tail
    assert all(x < y for x, y in zip(seq_a, seq_a[1:]))  # strictly up


def test_exhaustion_tick_is_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_EXHAUSTION_TICK_S", "0.01")
    assert _exhaustion_tick_s() == 0.01
    fc = FrozenClock(op_id="op-t")
    fc.import_trace(monotonic=[1.0])
    fc.monotonic()
    assert fc.monotonic() == pytest.approx(1.0 + 0.01)


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5", "garbage", ""])
def test_exhaustion_tick_never_zero_or_negative(monkeypatch, bad):
    """A 0 tick re-freezes the clock (livelock); a negative tick makes
    time go backward (non-monotonic). Both must be rejected to a
    positive floor — the actuator fails SAFE."""
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_EXHAUSTION_TICK_S", bad)
    assert _exhaustion_tick_s() > 0.0


# ── (a) inert ENFORCE flag is surfaced, not hidden ───────────────────

@pytest.mark.asyncio
async def test_inert_enforce_flag_is_warned_once(isolated, monkeypatch, caplog):
    """The graduated-but-inert per-worker ENFORCE flag must be surfaced
    exactly once per runtime (observability over silent inertness), and
    must never break the record path."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE", "true")
    reset_all_for_tests()

    with caplog.at_level(logging.WARNING):
        rt = runtime_for_session()
        r1 = await rt.record(op_id="op-w", phase="P", kind="K", inputs={}, output={"n": 1})
        r2 = await rt.record(op_id="op-w", phase="P", kind="K", inputs={}, output={"n": 2})

    assert r1 is not None and r2 is not None  # record path intact
    inert = [
        rec for rec in caplog.records
        if "ENFORCE is set but the replay index is not worker-keyed" in rec.message
    ]
    assert len(inert) == 1  # surfaced, and ONLY once per runtime


def test_enforce_flag_still_has_no_authoritative_wiring():
    """Guard the deliberate design decision: until the total-order
    merge lands, the replay index MUST NOT key on worker_id (a naive
    worker-keyed lookup would miss 100% of records at replay because
    worker_id embeds a non-reproducible pid). If someone wires it, this
    fails and forces them to prove replay still works."""
    import inspect
    from backend.core.ouroboros.governance.determinism import decision_runtime

    src = inspect.getsource(decision_runtime.DecisionRuntime._ensure_index_loaded)
    # The index-build key is the legacy 4-tuple; no worker_id in it.
    assert "(rec.op_id, rec.phase, rec.kind, rec.ordinal)" in src
    assert "rec.worker_id" not in src
