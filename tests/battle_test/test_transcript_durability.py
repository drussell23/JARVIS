"""Steps 5 + 6 — the writer that seals, and the spine that persists.

Fault injection is done by making the REAL syscall fail (patching
``os.write`` / ``os.fsync`` for one call), never by adding a test-only
branch to the writer. A seam that exists only for tests proves the seam,
not the code.
"""
from __future__ import annotations

import errno
import json
import os
import threading

import pytest

from backend.core.ouroboros.battle_test import transcript_writer as tw
from backend.core.ouroboros.battle_test.transcript_kill_harness import (
    build_records,
)
from backend.core.ouroboros.battle_test.transcript_log import (
    RejectReason,
    encode_record,
    recover_log,
)
from backend.core.ouroboros.battle_test.transcript_spine import (
    TranscriptSpine,
)
from backend.core.ouroboros.battle_test.transcript_writer import (
    DurableLogWriter,
    LogHealth,
)


@pytest.fixture
def writer(tmp_path):
    w = DurableLogWriter(tmp_path / "t.log", sync_every_n=4)
    w.start()
    yield w
    w.close()


def _drain(w: DurableLogWriter) -> None:
    """Wait for the io thread by round-tripping through it — the queue is
    FIFO with one worker, so a completed barrier means every earlier
    append has run. No sleep, and no polling."""
    w.barrier(timeout=10)


# ===========================================================================
# Group commit
# ===========================================================================


def test_appends_are_visible_before_they_are_durable(writer):
    """The distinction the whole state machine rests on: accepted for
    writing is not the same claim as survived a power cut."""
    for rec in build_records(3):
        assert writer.submit(rec) is True
    writer._executor.submit(lambda: None).result(timeout=10)  # order barrier

    stats = writer.snapshot_stats()
    assert stats["head_seq"] == 3
    assert stats["durable_through_seq"] == 0, "nothing synced yet"
    assert stats["undurable_records"] == 3

    _drain(writer)
    after = writer.snapshot_stats()
    assert after["durable_through_seq"] == 3
    assert after["undurable_records"] == 0


def test_group_commit_fires_on_the_count_threshold(writer):
    for rec in build_records(4):          # sync_every_n=4
        writer.submit(rec)
    writer._executor.submit(lambda: None).result(timeout=10)
    stats = writer.snapshot_stats()
    assert stats["syncs"] == 1, "the 4th append must trigger the barrier"
    assert stats["durable_through_seq"] == 4


def test_a_barrier_is_a_no_op_when_nothing_is_pending(writer):
    _drain(writer)
    before = writer.snapshot_stats()["syncs"]
    assert writer.barrier(timeout=10) is True
    assert writer.snapshot_stats()["syncs"] == before, \
        "an empty barrier must not cost an fsync"


@pytest.mark.asyncio
async def test_flusher_syncs_a_quiet_transcript_without_blocking_the_loop(
    tmp_path,
):
    """A quiet transcript still has to reach disk, and the interval that
    makes that happen must live on the loop, not on the io thread."""
    import asyncio

    w = DurableLogWriter(tmp_path / "quiet.log", sync_every_n=1000)
    w.start()
    try:
        for rec in build_records(2):
            w.submit(rec)
        task = asyncio.create_task(w.run_flusher(interval_s=0.05))

        loop_alive = 0
        for _ in range(20):
            await asyncio.sleep(0.01)
            loop_alive += 1            # the loop kept running throughout
            if w.durable_through_seq >= 2:
                break
        task.cancel()
        assert w.durable_through_seq == 2
        assert loop_alive >= 2
    finally:
        w.close()


# ===========================================================================
# Seal, don't limp
# ===========================================================================


def test_write_failure_truncates_to_the_last_good_offset_and_seals(
    tmp_path, monkeypatch,
):
    """ENOSPC mid-frame. The partial bytes must be gone, the prefix must
    be intact, and no further record may ever be appended."""
    w = DurableLogWriter(tmp_path / "enospc.log", sync_every_n=1000)
    w.start()
    try:
        for rec in build_records(3):
            w.submit(rec)
        _drain(w)
        good = w.snapshot_stats()["good_offset"]
        assert good > 0

        real_write = os.write
        state = {"torn": False}

        def _short_then_enospc(fd, data):
            if fd == w._fd and not state["torn"]:
                state["torn"] = True
                real_write(fd, data[:5])          # a real partial frame
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", _short_then_enospc)
        w.submit(build_records(1, start=4)[0])
        w._executor.submit(lambda: None).result(timeout=10)
        monkeypatch.undo()

        assert w.health is LogHealth.SEALED
        stats = w.snapshot_stats()
        assert stats["seal_errno"] == errno.ENOSPC
        assert "write_failed" in stats["seal_reason"]
        assert stats["good_offset"] == good

        # The torn bytes are gone: the file is a clean prefix.
        result = recover_log(tmp_path / "enospc.log")
        assert result.clean, result.to_dict()
        assert [r["seq"] for r in result.records] == [1, 2, 3]
    finally:
        w.close()


def test_a_sealed_writer_refuses_every_further_append(tmp_path, monkeypatch):
    """The limp this class exists to refuse: appending after a tear
    orphans everything that follows it."""
    w = DurableLogWriter(tmp_path / "sealed.log", sync_every_n=1000)
    w.start()
    try:
        w.submit(build_records(1)[0])
        _drain(w)
        w._seal("test", OSError(errno.ENOSPC, "full"))

        for rec in build_records(5, start=2):
            assert w.submit(rec) is False, "sealed must accept nothing"
        w.barrier(timeout=10)

        result = recover_log(tmp_path / "sealed.log")
        assert [r["seq"] for r in result.records] == [1]
        assert result.clean
    finally:
        w.close()


def test_seal_survives_a_crash_loop_idempotently(tmp_path, monkeypatch):
    """Re-opening onto a torn tail must truncate it, not append past it.
    Ten crash-and-reopen cycles must converge, never accumulate."""
    path = tmp_path / "loop.log"
    path.write_bytes(
        b"".join(encode_record(r) for r in build_records(3))
        + encode_record(build_records(1, start=4)[0])[:6],   # torn
    )
    for _ in range(10):
        w = DurableLogWriter(path, sync_every_n=1)
        result = w.start()
        try:
            assert [r["seq"] for r in result.records] == [1, 2, 3]
            assert recover_log(path).clean, "reopen must leave a clean file"
        finally:
            w.close()
    assert [r["seq"] for r in recover_log(path).records] == [1, 2, 3]


def _break_sync(monkeypatch, err: int) -> None:
    """Make BOTH sync paths fail — F_FULLFSYNC and the os.fsync it falls
    back to — so the failure reaches the writer rather than the fallback."""
    import fcntl

    def _boom(*_a, **_k):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(fcntl, "fcntl", _boom)
    monkeypatch.setattr(os, "fsync", _boom)


def test_fsync_eio_seals_because_writeback_actually_lost_data(
    tmp_path, monkeypatch,
):
    """EIO from fsync is the 'fsyncgate' case: writeback failed and the
    kernel may already have dropped the dirty pages. Data we believed
    written is gone, so continuing to append while still claiming
    durability would be a lie. Seal."""
    w = DurableLogWriter(tmp_path / "eio.log", sync_every_n=1000)
    w.start()
    try:
        w.submit(build_records(1)[0])
        _break_sync(monkeypatch, errno.EIO)
        assert w.barrier(timeout=10) is False
        assert w.health is LogHealth.SEALED
        assert w.snapshot_stats()["sync_failures"] == 1
        assert w.snapshot_stats()["seal_errno"] == errno.EIO
        monkeypatch.undo()
        assert w.submit(build_records(1, start=2)[0]) is False
    finally:
        w.close()


def test_a_filesystem_that_cannot_fsync_degrades_and_then_heals(
    tmp_path, monkeypatch,
):
    """EINVAL means this mount does not support the call — the data is
    fine, the PROMISE is weaker. Degrade, keep appending, and say so."""
    w = DurableLogWriter(tmp_path / "einval.log", sync_every_n=1000)
    w.start()
    try:
        w.submit(build_records(1)[0])
        _break_sync(monkeypatch, errno.EINVAL)
        assert w.barrier(timeout=10) is False
        assert w.health is LogHealth.DEGRADED, "EINVAL must not seal"
        assert w.snapshot_stats()["durable_through_seq"] == 0
        monkeypatch.undo()

        # A later successful barrier heals -- but the history of having
        # been degraded survives the healing.
        w.submit(build_records(1, start=2)[0])
        assert w.barrier(timeout=10) is True
        stats = w.snapshot_stats()
        assert stats["health"] == LogHealth.DURABLE.value
        assert stats["degraded_events"] == 1
        assert stats["durable_through_seq"] == 2
    finally:
        w.close()


def test_reserve_exhaustion_degrades_without_tearing_anything(
    tmp_path, monkeypatch,
):
    """Refusing early is not sealing: nothing is torn, so a later barrier
    can still heal."""
    w = DurableLogWriter(tmp_path / "reserve.log", sync_every_n=1000,
                         reserve_bytes=1 << 62)
    w.start()
    try:
        w.submit(build_records(1)[0])
        w._executor.submit(lambda: None).result(timeout=10)
        assert w.health is LogHealth.DEGRADED
        assert w.snapshot_stats()["appended"] == 0
        assert recover_log(tmp_path / "reserve.log").records == ()
    finally:
        w.close()


def test_queue_depth_is_bounded_and_drops_are_counted(tmp_path):
    """A stuck disk must not become an OOM, and the loss must be a
    readable number rather than an inference."""
    w = DurableLogWriter(tmp_path / "q.log", max_queue=4)
    w.start()
    try:
        gate = threading.Event()
        w._executor.submit(gate.wait)          # wedge the io thread
        accepted = [w.submit(r) for r in build_records(20)]
        assert accepted.count(True) == 4
        assert w.snapshot_stats()["dropped"] == 16
    finally:
        gate.set()
        w.close()


def test_unrepresentable_record_is_rejected_not_sealed(writer):
    """A record that cannot encode never reaches the file, so it is a
    caller error — not a corruption, and not grounds to seal."""
    assert writer.submit({"seq": 1, "kind": "k", "ref": "r",
                          "bad": object()}) is False
    assert writer.health is LogHealth.DURABLE
    assert writer.snapshot_stats()["rejected"] == 1


# ===========================================================================
# Oracle B against the REAL writer — the loop the whole arc exists to close
# ===========================================================================


@pytest.mark.parametrize("n", [1, 5])
def test_real_writer_survives_a_kill_mid_frame_and_reopens_clean(tmp_path, n):
    """END TO END. A real process, running the real DurableLogWriter, is
    SIGKILLed between two chunks of one frame -- so the writer never gets
    to seal, because it is dead. The tear is on disk.

    The claim under test is the one the arc exists for: the NEXT writer
    truncates that tear to a clean prefix and the transcript continues,
    rather than appending past it and orphaning everything after.
    """
    from backend.core.ouroboros.battle_test.transcript_kill_harness import (
        run_kill_scenario, seam_mid_record as mid,
    )

    path = tmp_path / f"real-{n}.log"
    outcome = run_kill_scenario(path=path, seam=mid(n), records=10,
                                split_bytes=9, writer="durable")
    assert outcome.died_by_sigkill, f"rc={outcome.returncode}"

    torn = recover_log(path)
    assert [r["seq"] for r in torn.records] == list(range(1, n))
    assert torn.trailing_bytes == 9, "a real torn frame, from a real kill"

    # The next boot heals it, and appends land in order after the seam.
    w = DurableLogWriter(path, sync_every_n=1)
    recovered = w.start()
    try:
        assert [r["seq"] for r in recovered.records] == list(range(1, n))
        assert recover_log(path).clean, "reopen must leave a clean file"
        w.submit({"seq": 999, "kind": "diff", "ref": "d-999"})
        _drain(w)
        final = recover_log(path)
        assert final.clean
        assert [r["seq"] for r in final.records] == \
               list(range(1, n)) + [999]
    finally:
        w.close()


def test_real_writer_kill_after_a_frame_loses_nothing_before_it(tmp_path):
    """SIGKILL leaves the page cache intact, so frames written before the
    seam are recoverable even though no barrier ran."""
    from backend.core.ouroboros.battle_test.transcript_kill_harness import (
        run_kill_scenario, seam_after_record as after,
    )

    path = tmp_path / "real-after.log"
    outcome = run_kill_scenario(path=path, seam=after(6), records=10,
                                writer="durable")
    assert outcome.died_by_sigkill
    result = recover_log(path)
    assert result.clean
    assert [r["seq"] for r in result.records] == list(range(1, 7))


# ===========================================================================
# Compaction
# ===========================================================================


def test_compaction_is_atomic_and_leaves_no_temp(tmp_path, writer):
    for rec in build_records(6):
        writer.submit(rec)
    _drain(writer)
    assert writer.compact(build_records(2, start=5)) is True

    result = recover_log(writer._path)
    assert result.clean
    assert [r["seq"] for r in result.records] == [5, 6]
    assert not writer._tmp_path.exists()

    # The re-opened fd must point at the NEW inode, not the unlinked one.
    writer.submit(build_records(1, start=7)[0])
    _drain(writer)
    assert [r["seq"] for r in recover_log(writer._path).records] == [5, 6, 7]


def test_compaction_refuses_when_space_is_tight_and_keeps_the_live_log(
    tmp_path,
):
    """Compaction doubles usage, so running it when space is tight would
    cause the very failure it prevents."""
    w = DurableLogWriter(tmp_path / "c.log", sync_every_n=1)
    w.start()
    try:
        for rec in build_records(3):
            w.submit(rec)
        _drain(w)
        w._reserve_bytes = 1 << 62
        w._free_bytes = -1
        assert w.compact(build_records(1)) is False
        assert [r["seq"] for r in recover_log(w._path).records] == [1, 2, 3]
    finally:
        w.close()


def test_compaction_temp_name_is_fixed_so_a_crash_loop_leaks_no_inodes(
    tmp_path,
):
    """Random temp names spray inodes until the filesystem runs out —
    and inode exhaustion arrives as the same ENOSPC we are surviving."""
    w = DurableLogWriter(tmp_path / "fixed.log")
    w.start()
    try:
        names = set()
        for i in range(5):
            w.compact(build_records(2, start=i + 1))
            names.add(w._tmp_path.name)
            names.update(p.name for p in tmp_path.glob("*.compact"))
        assert names == {w._tmp_path.name}, "exactly one temp name, ever"
        assert not w._tmp_path.exists()
    finally:
        w.close()


def test_a_stale_temp_from_a_crash_is_reaped_at_open(tmp_path):
    stale = tmp_path / "x.log.compact"
    stale.write_bytes(b"half a compaction")
    w = DurableLogWriter(tmp_path / "x.log")
    w.start()
    try:
        assert not stale.exists()
    finally:
        w.close()


# ===========================================================================
# Step 6 — spine wiring
# ===========================================================================


def test_spine_appends_reach_the_log_in_sequence_order(tmp_path):
    w = DurableLogWriter(tmp_path / "spine.log", sync_every_n=1)
    w.start()
    spine = TranscriptSpine()
    spine.attach_sink(tw.build_durable_sink(w))
    try:
        for i in range(1, 11):
            spine.append("diff", f"d-{i}")
        _drain(w)
        result = recover_log(tmp_path / "spine.log")
        assert result.clean
        assert [r["seq"] for r in result.records] == list(range(1, 11))
        assert [r["ref"] for r in result.records] == [
            f"d-{i}" for i in range(1, 11)
        ]
    finally:
        spine.attach_sink(None)
        w.close()


def test_concurrent_appends_never_produce_a_non_monotonic_log(tmp_path):
    """The reason the sink is called INSIDE the append lock. Outside it,
    two threads can reach the log in the opposite order to their seq,
    and recover_log correctly ends the prefix at the inversion."""
    w = DurableLogWriter(tmp_path / "race.log", sync_every_n=64)
    w.start()
    spine = TranscriptSpine()
    spine.attach_sink(tw.build_durable_sink(w))
    try:
        def _hammer(base: int) -> None:
            for i in range(50):
                spine.append("diff", f"d-{base}-{i}")

        threads = [threading.Thread(target=_hammer, args=(t,))
                   for t in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _drain(w)

        result = recover_log(tmp_path / "race.log")
        assert result.stop_reason is not RejectReason.NON_MONOTONIC_SEQ
        assert len(result.records) == 300
        seqs = [r["seq"] for r in result.records]
        assert seqs == sorted(seqs) == list(range(1, 301))
    finally:
        spine.attach_sink(None)
        w.close()


def test_a_failing_sink_never_breaks_the_transcript(tmp_path):
    spine = TranscriptSpine()

    def _boom(_rec):
        raise OSError("disk on fire")

    spine.attach_sink(_boom)
    rec = spine.append("diff", "d-1")
    assert rec is not None, "recording must survive a persistence failure"
    assert spine.sink_failures == 1


def test_oversize_payload_is_marked_not_silently_truncated(tmp_path):
    w = DurableLogWriter(tmp_path / "big.log", sync_every_n=1)
    w.start()
    spine = TranscriptSpine()
    spine.attach_sink(tw.build_durable_sink(w, max_payload_bytes=64))
    try:
        class _Big:
            def to_dict(self):
                return {"body": "x" * 5000}

        spine.append("diff", "d-1", payload=_Big())
        spine.append("diff", "d-2", payload={"small": "ok"})
        _drain(w)

        records = recover_log(tmp_path / "big.log").records
        assert records[0]["payload"] == {"__truncated__": True,
                                         "bytes": len(json.dumps(
                                             {"body": "x" * 5000},
                                             separators=(",", ":")))}
        assert records[1]["payload"] == {"small": "ok"}
    finally:
        spine.attach_sink(None)
        w.close()


def test_install_is_off_by_default_and_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv(tw.DURABLE_ENABLED_ENV_VAR, raising=False)
    assert tw.durable_enabled() is False
    assert tw.install_durable_transcript(tmp_path / "off.log") is None
    assert not (tmp_path / "off.log").exists()

    monkeypatch.setenv(tw.DURABLE_ENABLED_ENV_VAR, "true")
    spine = TranscriptSpine()
    w = tw.install_durable_transcript(tmp_path / "on.log", spine=spine)
    assert w is not None
    try:
        spine.append("diff", "d-1")
        _drain(w)
        assert [r["ref"] for r in recover_log(tmp_path / "on.log").records] \
            == ["d-1"]
    finally:
        spine.attach_sink(None)
        w.close()


# ===========================================================================
# Step 6 — the milestone feed
# ===========================================================================


def test_milestones_share_the_spine_order_with_everything_else(tmp_path):
    """The gap this closes: a transcript could show d-7 and the tool
    calls around it, and have no position for 'and then it was
    committed'."""
    from backend.core.ouroboros.battle_test import transcript_milestones as tm
    from backend.core.ouroboros.governance import ops_digest_observer as odo

    odo.reset_ops_digest_observer()
    tm.uninstall()
    spine = TranscriptSpine()
    listener = tm.install(spine)
    assert listener is not None
    try:
        spine.append("diff", "d-1")
        observer = odo.get_ops_digest_observer()
        observer.on_apply_succeeded(op_id="op-1", mode="single", files=2)
        spine.append("tool_body", "t-1")
        observer.on_verify_completed(op_id="op-1", passed=4, total=4,
                                     scoped_to_applied_op=False)
        observer.on_commit_succeeded(op_id="op-1", commit_hash="deadbeef")

        kinds = [(r.kind, r.ref) for r in spine]
        assert kinds == [
            ("diff", "d-1"), ("milestone", "m-1"),
            ("tool_body", "t-1"), ("milestone", "m-2"), ("milestone", "m-3"),
        ]
        verify = spine.resolve("m-2")
        assert verify is not None
        # The qualifier is carried verbatim: "4/4 passed" means something
        # different when the counts are repo-wide.
        assert verify.payload["scoped_to_applied_op"] is False
        assert spine.resolve("m-3").payload["commit"] == "deadbeef"
        assert listener.recorded == 3 and listener.failures == 0
    finally:
        tm.uninstall()
        odo.reset_ops_digest_observer()


def test_milestone_listener_is_additive_not_displacing(tmp_path):
    """It must subscribe alongside SessionRecorder, never replace it."""
    from backend.core.ouroboros.battle_test import transcript_milestones as tm
    from backend.core.ouroboros.governance import ops_digest_observer as odo

    odo.reset_ops_digest_observer()
    tm.uninstall()

    seen = []

    class _Primary:
        def on_apply_succeeded(self, *, op_id, mode, files):
            seen.append(op_id)

        def on_verify_completed(self, *, op_id, passed, total,
                                scoped_to_applied_op=True):
            pass

        def on_commit_succeeded(self, *, op_id, commit_hash):
            pass

    odo.register_ops_digest_observer(_Primary())
    spine = TranscriptSpine()
    tm.install(spine)
    try:
        odo.get_ops_digest_observer().on_apply_succeeded(
            op_id="op-9", mode="multi", files=3,
        )
        assert seen == ["op-9"], "the primary observer must still be called"
        assert len(list(spine)) == 1
    finally:
        tm.uninstall()
        odo.reset_ops_digest_observer()


def test_milestones_add_capacity_rather_than_stealing_it():
    """A fifth producer that added no budget would make the spine evict
    sooner than the union it promises."""
    from backend.core.ouroboros.battle_test import transcript_spine as ts
    from backend.core.ouroboros.battle_test import transcript_milestones as tm

    assert ts.known_prefixes().get(tm.REF_PREFIX) == "milestone"
    assert ts._store_capacity("transcript_milestones") > 0
    assert ts._derived_capacity() >= ts._store_capacity(
        "transcript_milestones",
    )
