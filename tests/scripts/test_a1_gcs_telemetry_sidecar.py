from __future__ import annotations

import sys
from pathlib import Path

# Allow import from scripts/ (repo root -> scripts/).
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Constraint 2 — Append-Only Chunked Streaming.
# Immutable, monotonically-indexed chunks (never overwrite the same object).
# A dead node's log is perfectly reconstructable by ordering the chunks.
# ---------------------------------------------------------------------------


def test_streamer_emits_monotonic_immutable_chunks():
    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer

    writes = []
    s = AppendOnlyChunkStreamer(
        session_id="bt-X", sink=lambda name, data: writes.append((name, data))
    )

    s.stream_new_bytes(b"hello ")
    s.stream_new_bytes(b"world")

    names = [w[0] for w in writes]
    # Distinct, monotonic, zero-padded — never the same object twice.
    assert names == ["bt-X/chunk_00001.log", "bt-X/chunk_00002.log"]
    # Reconstructable in order, byte-for-byte.
    assert b"".join(w[1] for w in writes) == b"hello world"


def test_streamer_tracks_byte_offset():
    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer

    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda name, data: None)
    assert s.offset() == 0
    s.stream_new_bytes(b"12345")
    assert s.offset() == 5
    s.stream_new_bytes(b"678")
    assert s.offset() == 8


def test_streamer_skips_empty_flush():
    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer

    writes = []
    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda name, data: writes.append(name))
    s.stream_new_bytes(b"")
    assert writes == []


def test_streamer_is_fail_soft_on_sink_error():
    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer

    def bad_sink(name, data):
        raise RuntimeError("gcs unreachable")

    s = AppendOnlyChunkStreamer(session_id="x", sink=bad_sink)
    # MUST NOT raise — the soak must never die because telemetry failed.
    s.stream_new_bytes(b"payload")
    # The failed chunk is counted but swallowed.
    assert s.failed_chunks() == 1


# ---------------------------------------------------------------------------
# GCS-Vault chunk sink — reuses the native google-cloud-storage SDK pattern
# (ADC), uploading each immutable chunk to gs://bucket/prefix/<object_name>.
# ---------------------------------------------------------------------------


class _FakeBlob:
    def __init__(self, name, store):
        self.name = name
        self._store = store

    def upload_from_string(self, data):
        self._store.append((self.name, data))


class _FakeBucket:
    def __init__(self, name, store):
        self.name = name
        self._store = store

    def blob(self, name):
        return _FakeBlob(name, self._store)


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def bucket(self, name):
        return _FakeBucket(name, self._store)


def test_gcs_chunk_sink_uploads_to_prefixed_immutable_blob():
    from a1_gcs_telemetry_sidecar import make_gcs_chunk_sink

    uploaded = []
    sink = make_gcs_chunk_sink(
        "gs://mybucket/a1/logs", client_factory=lambda: _FakeClient(uploaded)
    )
    assert sink is not None

    sink("bt-X/chunk_00001.log", b"hello")

    assert uploaded == [("a1/logs/bt-X/chunk_00001.log", b"hello")]


def test_make_gcs_chunk_sink_none_for_non_gs_uri():
    from a1_gcs_telemetry_sidecar import make_gcs_chunk_sink

    # No target / local path -> no sink -> sidecar stays disabled (fail-soft).
    assert make_gcs_chunk_sink("", client_factory=lambda: None) is None
    assert make_gcs_chunk_sink("/local/only", client_factory=lambda: None) is None


def test_gcs_chunk_sink_is_fail_soft_on_upload_error():
    from a1_gcs_telemetry_sidecar import make_gcs_chunk_sink

    class _BoomClient:
        def bucket(self, name):
            raise RuntimeError("gcs auth failed")

    sink = make_gcs_chunk_sink("gs://b/p", client_factory=lambda: _BoomClient())
    # A sink failure must never propagate (it runs under the fail-soft streamer,
    # but the sink itself must also be defensive).
    sink("bt-X/chunk_00001.log", b"data")


# ---------------------------------------------------------------------------
# Tail/flush loop — reads the growing debug.log and feeds new bytes to the
# streamer on each tick, with a guaranteed final flush on stop.
# ---------------------------------------------------------------------------


def test_flush_tick_streams_only_new_bytes(tmp_path):
    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer, flush_tick

    p = tmp_path / "debug.log"
    p.write_bytes(b"abc")
    got = []
    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda n, d: got.append(d))

    flush_tick(str(p), s)
    assert b"".join(got) == b"abc"
    assert s.offset() == 3

    p.write_bytes(b"abcde")  # the log grew (append-only)
    flush_tick(str(p), s)
    assert b"".join(got) == b"abcde"  # only the new bytes were streamed
    assert s.offset() == 5


def test_flush_tick_is_fail_soft_on_missing_file():
    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer, flush_tick

    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda n, d: None)
    flush_tick("/no/such/path.log", s)  # must not raise
    assert s.offset() == 0


def test_run_sidecar_does_a_final_flush_on_stop(tmp_path):
    import asyncio

    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer, run_sidecar

    p = tmp_path / "d.log"
    p.write_bytes(b"hello")
    got = []
    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda n, d: got.append(d))

    async def drive():
        stop = asyncio.Event()
        stop.set()  # already stopped -> loop skips, but the final flush must run
        await run_sidecar(str(p), s, interval_s=0.01, stop_event=stop)

    asyncio.run(drive())
    # Even though stop was pre-set, the dying node's last bytes are captured.
    assert b"".join(got) == b"hello"


# ---------------------------------------------------------------------------
# Managed Async Daemon Lifecycle — bound to the caller's lifecycle; aclose()
# awaits a guaranteed final flush so the last 'APPLIED' chunk is never lost.
# ---------------------------------------------------------------------------


def test_managed_sidecar_awaits_final_flush_on_aclose(tmp_path):
    import asyncio

    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer, ManagedSidecar

    p = tmp_path / "debug.log"
    p.write_bytes(b"boot")
    got = []
    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda n, d: got.append(d))

    async def drive():
        m = ManagedSidecar(str(p), s, interval_s=0.01).start()
        await asyncio.sleep(0.03)  # let it tick at least once
        p.write_bytes(b"bootAPPLIED")  # the final critical chunk lands late
        await m.aclose()  # MUST await the final flush before returning

    asyncio.run(drive())
    # The terminal APPLIED bytes were captured despite the late write + teardown.
    assert b"".join(got) == b"bootAPPLIED"


def test_managed_sidecar_aclose_is_safe_without_start():
    import asyncio

    from a1_gcs_telemetry_sidecar import AppendOnlyChunkStreamer, ManagedSidecar

    s = AppendOnlyChunkStreamer(session_id="x", sink=lambda n, d: None)
    m = ManagedSidecar("/no/such.log", s, interval_s=0.01)
    # aclose() before start() must not raise.
    asyncio.run(m.aclose())
