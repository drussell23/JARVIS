"""The three crash oracles for the durable transcript.

Each oracle is honest about the one thing it can prove; see
``transcript_kill_harness`` for why one is not enough. Nothing here
asserts on source text — every assertion is against bytes that a real
process actually wrote, or against the reader's behaviour on bytes that
were really truncated.
"""
from __future__ import annotations

import os
import zlib
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.transcript_kill_harness import (
    HarnessError,
    build_records,
    run_kill_scenario,
    seam_after_record,
    seam_after_sync,
    seam_mid_record,
)
from backend.core.ouroboros.battle_test.transcript_log import (
    FRAME_VERSION,
    RejectReason,
    decode_frame,
    encode_record,
    recover_log,
)
from backend.core.ouroboros.governance import durable_io


# ===========================================================================
# Codec — the contract all three oracles rest on
# ===========================================================================


def test_frame_roundtrip_is_byte_deterministic():
    rec = {"seq": 1, "kind": "diff", "ref": "d-1", "op_id": "op-1"}
    a, b = encode_record(rec), encode_record(dict(reversed(list(rec.items()))))
    assert a == b, "key order must not change the bytes"
    assert a.endswith(b"\n")
    decoded, reason = decode_frame(a[:-1])
    assert reason is RejectReason.NONE
    assert decoded is not None and decoded["ref"] == "d-1"
    assert decoded["v"] == FRAME_VERSION


def test_payload_can_never_contain_the_framing_bytes():
    """The delimiter argument: a payload holding a raw newline or tab
    would make the frame self-confusing. json escapes both."""
    rec = {"seq": 1, "kind": "k", "ref": "r", "note": "a\nb\tcé"}
    frame = encode_record(rec)
    assert frame.count(b"\n") == 1, "only the terminator may be a newline"
    assert frame.count(b"\t") == 1, "only the separator may be a tab"
    decoded, reason = decode_frame(frame[:-1])
    assert reason is RejectReason.NONE
    assert decoded is not None and decoded["note"] == "a\nb\tcé"


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda f: f[:4], RejectReason.SHORT_FRAME),
        (lambda f: b"zzzzzzzz" + f[8:], RejectReason.BAD_CRC_FIELD),
        (lambda f: f[:8] + b" " + f[9:], RejectReason.NO_SEPARATOR),
        (lambda f: f[:20] + bytes([f[20] ^ 0xFF]) + f[21:],
         RejectReason.CRC_MISMATCH),
    ],
)
def test_each_corruption_shape_has_its_own_reason(mutate, expected):
    frame = encode_record(
        {"seq": 1, "kind": "diff", "ref": "d-1", "pad": "x" * 40},
    )[:-1]
    record, reason = decode_frame(mutate(frame))
    assert record is None
    assert reason is expected


def test_intact_crc_over_a_wrong_shape_is_still_rejected():
    """CRC says 'unmodified'. It does not say 'usable'."""
    body = b'{"v":1,"seq":1,"kind":"diff"}'          # no "ref"
    crc = ("%08x" % (zlib.crc32(body) & 0xFFFFFFFF)).encode()
    record, reason = decode_frame(crc + b"\t" + body)
    assert record is None and reason is RejectReason.MISSING_FIELD


# ===========================================================================
# ORACLE A — exhaustive truncation. Proves the READER.
# ===========================================================================


def _write_clean_log(path: Path, count: int = 12) -> bytes:
    blob = b"".join(encode_record(r) for r in build_records(count))
    path.write_bytes(blob)
    return blob


def test_oracle_a_every_truncation_point_yields_a_valid_prefix(tmp_path):
    """Truncate at EVERY byte offset, not a sampled few.

    Three invariants must hold at all N+1 offsets:
      1. recovery never raises;
      2. the records returned are exactly the frames wholly contained in
         the surviving bytes -- never a partial one;
      3. durable_bytes lands exactly on a frame boundary.
    """
    blob = _write_clean_log(tmp_path / "full.log")
    boundaries, acc = set(), 0
    for rec in build_records(12):
        acc += len(encode_record(rec))
        boundaries.add(acc)
    boundaries.add(0)

    target = tmp_path / "cut.log"
    for cut in range(len(blob) + 1):
        target.write_bytes(blob[:cut])
        result = recover_log(target)

        assert result.durable_bytes in boundaries, (
            f"cut={cut} stopped mid-frame at {result.durable_bytes}"
        )
        assert result.durable_bytes <= cut
        assert result.trailing_bytes == cut - result.durable_bytes
        # Every returned record is whole and in order.
        assert [r["seq"] for r in result.records] == list(
            range(1, len(result.records) + 1),
        )
        # A cut on a boundary is clean; anywhere else is a torn tail.
        assert result.clean is (cut in boundaries)
        if cut not in boundaries:
            assert result.stop_reason is RejectReason.TRUNCATED


def test_oracle_a_single_bit_flip_anywhere_stops_the_prefix(tmp_path):
    """A flip inside frame k must never yield frame k+1: continuing past
    a bad record is what silently reorders a transcript."""
    blob = _write_clean_log(tmp_path / "full.log", count=6)
    target = tmp_path / "flip.log"
    first_len = len(encode_record(build_records(1)[0]))

    for pos in range(first_len, len(blob)):
        if blob[pos] == 0x0A:              # a flipped terminator is its
            continue                       # own (also-handled) case
        corrupt = bytearray(blob)
        corrupt[pos] ^= 0x01
        target.write_bytes(bytes(corrupt))
        result = recover_log(target)
        assert result.stop_reason is not RejectReason.NONE
        # Records stop strictly before the damaged frame.
        assert result.durable_bytes <= pos
        assert all(r["seq"] <= result.stop_frame for r in result.records)


def test_oracle_a_missing_and_empty_are_not_errors(tmp_path):
    absent = recover_log(tmp_path / "nope.log")
    assert absent.records == () and absent.exists is False
    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    got = recover_log(empty)
    assert got.records == () and got.exists is True and got.clean


def test_oracle_a_rewound_writer_is_caught_by_sequence(tmp_path):
    """CRC cannot see a duplicated or reordered append. The order IS the
    transcript, so the prefix must end at the violation."""
    path = tmp_path / "rewound.log"
    frames = [encode_record(r) for r in build_records(4)]
    path.write_bytes(b"".join(frames) + frames[1])   # seq 2 again
    result = recover_log(path)
    assert len(result.records) == 4
    assert result.stop_reason is RejectReason.NON_MONOTONIC_SEQ


def test_oracle_a_appending_past_a_torn_tail_orphans_and_is_detected(tmp_path):
    """THE PROPERTY STEP 5 DEPENDS ON.

    This is the shape a writer that LIMPS instead of SEALING produces:
    it crashed mid-frame, restarted, and appended on top of the tear.
    The bytes after the tear are unreachable -- a forward scan cannot
    pass a torn record without inventing an order that never happened.

    Pinning it here means the reader can never be 'fixed' into skipping
    the hole, which would turn a loud corruption into a silent one and
    make the writer's seal untestable.
    """
    path = tmp_path / "limped.log"
    good = b"".join(encode_record(r) for r in build_records(3))
    torn = encode_record(build_records(1, start=4)[0])[:9]   # no terminator
    after = b"".join(encode_record(r) for r in build_records(4, start=5))
    path.write_bytes(good + torn + after)

    result = recover_log(path)
    assert [r["seq"] for r in result.records] == [1, 2, 3]
    assert result.durable_bytes == len(good)
    # The orphaned region is counted honestly, never parsed.
    assert result.trailing_bytes == len(torn) + len(after)
    assert result.stop_reason is not RejectReason.NONE


def test_oracle_a_no_newline_at_all_is_bounded(tmp_path, monkeypatch):
    """A corrupt file with no terminator must be rejected, not buffered
    into memory: a disk fault may not become an OOM."""
    monkeypatch.setenv("JARVIS_TRANSCRIPT_MAX_RECORD_BYTES", "512")
    path = tmp_path / "garbage.log"
    path.write_bytes(b"A" * 4096)
    result = recover_log(path)
    assert result.records == ()
    assert result.stop_reason is RejectReason.OVERSIZE


# ===========================================================================
# ORACLE B — real SIGKILL. Proves the WRITER under process death.
# ===========================================================================


@pytest.mark.parametrize("n", [1, 4, 8])
def test_oracle_b_kill_after_a_record_loses_nothing_before_it(tmp_path, n):
    """SIGKILL leaves the page cache intact, so every frame written
    before the seam must be fully recoverable by another process."""
    path = tmp_path / f"after-{n}.log"
    outcome = run_kill_scenario(path=path, seam=seam_after_record(n),
                                records=12)
    assert outcome.died_by_sigkill, f"rc={outcome.returncode}"

    result = recover_log(path)
    assert result.clean, result.to_dict()
    assert [r["seq"] for r in result.records] == list(range(1, n + 1))


@pytest.mark.parametrize("n", [2, 5])
def test_oracle_b_kill_mid_record_leaves_a_recoverable_prefix(tmp_path, n):
    """The torn-record case, produced by a real kill between two chunks
    of one frame. The partial frame must be invisible to the reader and
    everything before it must survive intact."""
    path = tmp_path / f"mid-{n}.log"
    outcome = run_kill_scenario(path=path, seam=seam_mid_record(n),
                                records=12, split_bytes=7)
    assert outcome.died_by_sigkill

    result = recover_log(path)
    assert [r["seq"] for r in result.records] == list(range(1, n))
    assert result.stop_reason is RejectReason.TRUNCATED
    assert result.trailing_bytes == 7, "the torn chunk, counted not parsed"
    assert result.durable_bytes == outcome.file_size - 7


def test_oracle_b_torn_tail_is_exactly_recoverable_to_a_boundary(tmp_path):
    """Truncating to durable_bytes must produce a byte-identical clean
    log -- this is the operation the writer performs at open, and it is
    what makes a crash LOOP idempotent instead of cumulative."""
    path = tmp_path / "loop.log"
    run_kill_scenario(path=path, seam=seam_mid_record(3), records=6,
                      split_bytes=5)
    first = recover_log(path)
    assert first.trailing_bytes > 0

    with open(path, "r+b") as fh:
        fh.truncate(first.durable_bytes)
        os.fsync(fh.fileno())

    second = recover_log(path)
    assert second.clean
    assert [r["seq"] for r in second.records] == \
           [r["seq"] for r in first.records]


def test_oracle_b_kill_after_sync_survives(tmp_path):
    path = tmp_path / "synced.log"
    outcome = run_kill_scenario(path=path, seam=seam_after_sync(4),
                                records=12, sync_every=2)
    assert outcome.died_by_sigkill
    result = recover_log(path)
    assert result.clean
    assert [r["seq"] for r in result.records] == [1, 2, 3, 4]


def test_oracle_b_unreachable_seam_is_a_harness_failure_not_a_pass(tmp_path):
    """A scenario that cannot be staged has proved nothing. It must be
    impossible to mistake for a green result."""
    with pytest.raises(HarnessError):
        run_kill_scenario(path=tmp_path / "x.log",
                          seam=seam_after_record(99), records=3)


# ===========================================================================
# ORACLE C — simulated power loss. Proves the DURABILITY BOUNDARY.
# ===========================================================================


class _RecordingDevice:
    """Models a device whose page cache is lost on power failure.

    Writes accumulate in a volatile buffer; only a sync moves them to the
    'platter'. ``platter()`` is what a machine would still have after
    losing power at that instant -- the state a SIGKILL can never show us,
    because the kernel keeps the cache when only a process dies.
    """

    def __init__(self) -> None:
        self._platter = b""
        self._volatile = b""
        self.barriers: list = []           # (byte_len, highest acked seq)
        self._acked = 0

    def write(self, data: bytes) -> None:
        self._volatile += data

    def ack(self, seq: int) -> None:
        self._acked = seq

    def sync(self) -> None:
        self._platter += self._volatile
        self._volatile = b""
        self.barriers.append((len(self._platter), self._acked))

    def platter(self) -> bytes:
        return self._platter

    def everything(self) -> bytes:
        return self._platter + self._volatile


def test_oracle_c_every_barrier_survives_a_power_cut(tmp_path):
    """The durability contract: anything acknowledged before a barrier is
    recoverable after the machine dies at that barrier."""
    dev = _RecordingDevice()
    for rec in build_records(20):
        dev.write(encode_record(rec))
        dev.ack(int(rec["seq"]))
        if int(rec["seq"]) % 4 == 0:
            dev.sync()

    assert dev.barriers, "the corpus must cross at least one barrier"
    path = tmp_path / "power.log"
    replay = _RecordingDevice()

    for rec in build_records(20):
        replay.write(encode_record(rec))
        replay.ack(int(rec["seq"]))
        if int(rec["seq"]) % 4 == 0:
            replay.sync()
            path.write_bytes(replay.platter())
            result = recover_log(path)
            assert result.clean, result.to_dict()
            assert result.head_seq >= int(rec["seq"]), (
                "a record acked before the barrier did not survive it"
            )


def test_oracle_c_unsynced_tail_is_lost_but_never_corrupting(tmp_path):
    """Records written after the last barrier MAY be lost -- that is the
    honest contract. What must never happen is a torn or reordered
    prefix: what survives is always a valid transcript."""
    path = tmp_path / "cut.log"
    for tail in range(1, 8):
        dev = _RecordingDevice()
        for rec in build_records(4):
            dev.write(encode_record(rec))
            dev.ack(int(rec["seq"]))
        dev.sync()
        durable_seq = dev.barriers[-1][1]
        for rec in build_records(tail, start=5):
            dev.write(encode_record(rec))       # never synced

        path.write_bytes(dev.platter())
        result = recover_log(path)
        assert result.clean
        assert result.head_seq == durable_seq


def test_oracle_c_partial_sector_at_the_barrier_is_still_a_valid_prefix(
    tmp_path,
):
    """POSIX does not promise that one >1-sector write lands atomically
    under power loss. The reader must not depend on it: truncate the
    platter itself at every offset and the prefix must stay valid."""
    dev = _RecordingDevice()
    for rec in build_records(6):
        dev.write(encode_record(rec))
        dev.ack(int(rec["seq"]))
    dev.sync()
    blob = dev.platter()

    path = tmp_path / "sector.log"
    for cut in range(len(blob) + 1):
        path.write_bytes(blob[:cut])
        result = recover_log(path)
        assert [r["seq"] for r in result.records] == list(
            range(1, len(result.records) + 1),
        )


# ===========================================================================
# Platform abstraction
# ===========================================================================


def test_full_fsync_is_used_on_darwin_and_a_real_sync_elsewhere(tmp_path):
    """The macOS gap this arc exists to close: plain fsync() does not
    flush the drive's own write cache, F_FULLFSYNC does."""
    import sys

    assert durable_io.full_fsync_available() is (sys.platform == "darwin")

    path = tmp_path / "sync.bin"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"durable")
        durable_io.fsync_file(fd)          # must not raise on any platform
    finally:
        os.close(fd)
    assert path.read_bytes() == b"durable"


def test_atomic_replace_publishes_and_syncs_the_directory(tmp_path):
    src, dst = tmp_path / "t.tmp", tmp_path / "final.json"
    src.write_bytes(b'{"ok":true}')
    durable_io.atomic_replace(src, dst)
    assert dst.read_bytes() == b'{"ok":true}'
    assert not src.exists(), "the temp name must not survive the publish"


def test_space_exhaustion_classifier_covers_inode_exhaustion():
    """Inode exhaustion arrives as ENOSPC from open(), the same errno as
    a full data area -- one recovery path, classified in one place."""
    import errno

    assert durable_io.is_space_exhaustion(OSError(errno.ENOSPC, "no space"))
    assert durable_io.is_space_exhaustion(OSError(errno.EDQUOT, "quota"))
    assert not durable_io.is_space_exhaustion(OSError(errno.ENOENT, "gone"))
    assert not durable_io.is_space_exhaustion(ValueError("not an OSError"))
