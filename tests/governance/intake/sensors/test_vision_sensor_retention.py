"""Regression spine for VisionSensor retention (Task 9).

Pins:

* Retained frame directory: ``.jarvis/vision_frames/<session_id>/``.
* Retention fires only on frames that produced a signal.
* Memory-only mode (TTL <= 0) skips retention and keeps the volatile
  Ferrari path in the evidence ``frame_path``.
* Retained path flows into ``evidence["vision_signal"]["frame_path"]``.
* Idempotent retention — same ``dhash`` reuses an existing copy.
* Best-effort failure (missing source / locked dir) does NOT break the
  signal emit; evidence falls back to the volatile source path.
* TTL purge removes expired retained frames, preserves fresh ones.
* ``atexit`` + ``SIGTERM`` purge hooks nuke the session directory.
* Opt-out (``register_shutdown_hooks=False``) leaves the process-level
  signal handler and ``atexit`` registry untouched.
* Session ID scoping — two sensors with distinct session IDs never
  clobber each other's retained files.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Retention + §Threat Model T7.
"""
from __future__ import annotations

import atexit
import json
import os
import signal as _signal
import time
from pathlib import Path
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import IntentEnvelope
from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
    _TTL_PURGE_INTERVAL_S,
    FrameData,
    VisionSensor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubRouter:
    async def ingest(self, envelope: IntentEnvelope) -> str:
        return "enqueued"


def _make_frame_file(tmp_path: Path, name: str = "latest_frame.jpg", payload: bytes = b"\x89PNG" + b"\x00" * 64) -> str:
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def _make_frame(
    frame_path: str,
    dhash: str = "0123456789abcdef",
    ts: float = 1.0,
    app_id: Optional[str] = None,
    window_id: Optional[int] = None,
) -> FrameData:
    return FrameData(
        frame_path=frame_path,
        dhash=dhash,
        ts=ts,
        app_id=app_id,
        window_id=window_id,
    )


def _make_sensor(
    tmp_path: Path,
    *,
    session_id: str = "test-session-9",
    frame_ttl_s: Optional[float] = 600.0,
    ocr_fn=None,
    register_shutdown_hooks: bool = False,
) -> VisionSensor:
    retention_root = str(tmp_path / ".jarvis" / "vision_frames")
    return VisionSensor(
        router=_StubRouter(),
        session_id=session_id,
        retention_root=retention_root,
        frame_ttl_s=frame_ttl_s,
        ocr_fn=ocr_fn,
        register_shutdown_hooks=register_shutdown_hooks,
    )


@pytest.fixture
def traceback_ocr():
    return lambda _path: "Traceback (most recent call last):\n  File 'x.py'"


# ---------------------------------------------------------------------------
# Session dir construction
# ---------------------------------------------------------------------------


def test_session_id_is_generated_when_omitted(tmp_path):
    # Two sensors without explicit session_id get distinct IDs.
    s1 = VisionSensor(
        router=_StubRouter(),
        retention_root=str(tmp_path),
        frame_ttl_s=60.0,
        register_shutdown_hooks=False,
    )
    s2 = VisionSensor(
        router=_StubRouter(),
        retention_root=str(tmp_path),
        frame_ttl_s=60.0,
        register_shutdown_hooks=False,
    )
    assert s1._session_id != s2._session_id


def test_session_dir_is_retention_root_plus_session_id(tmp_path):
    sensor = _make_sensor(tmp_path, session_id="abc123")
    expected = Path(tmp_path) / ".jarvis" / "vision_frames" / "abc123"
    assert sensor._session_retention_dir == expected


def test_session_dir_not_created_until_first_retain(tmp_path):
    sensor = _make_sensor(tmp_path)
    # Construction alone must not create the directory — sensor that
    # never fires leaves zero disk footprint.
    assert not sensor._session_retention_dir.exists()


# ---------------------------------------------------------------------------
# Retention on signal emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retain_saves_copy_when_signal_emits(tmp_path, traceback_ocr):
    src = _make_frame_file(tmp_path, payload=b"frame_bytes_v1")
    sensor = _make_sensor(tmp_path, ocr_fn=traceback_ocr)
    envelope = await sensor._ingest_frame(_make_frame(src, dhash="aaaaaaaaaaaaaaaa"))
    assert envelope is not None
    # A retained file exists under the session dir named by dhash.
    retained = sensor._session_retention_dir / "aaaaaaaaaaaaaaaa.jpg"
    assert retained.exists()
    assert retained.read_bytes() == b"frame_bytes_v1"
    # Counter bumped.
    assert sensor.stats.frames_retained == 1


@pytest.mark.asyncio
async def test_retained_path_flows_into_evidence(tmp_path, traceback_ocr):
    src = _make_frame_file(tmp_path)
    sensor = _make_sensor(tmp_path, ocr_fn=traceback_ocr)
    envelope = await sensor._ingest_frame(_make_frame(src, dhash="bbbbbbbbbbbbbbbb"))
    assert envelope is not None
    ev_path = envelope.evidence["vision_signal"]["frame_path"]
    # Path points to the retained copy, not the volatile Ferrari source.
    assert ev_path != src
    assert ev_path.endswith("bbbbbbbbbbbbbbbb.jpg")
    assert ev_path.startswith(str(sensor._session_retention_dir))


@pytest.mark.asyncio
async def test_clean_screen_does_not_retain(tmp_path):
    # OCR returns non-trigger text → no signal → no retention.
    src = _make_frame_file(tmp_path)
    sensor = _make_sensor(tmp_path, ocr_fn=lambda _p: "Welcome to the app")
    envelope = await sensor._ingest_frame(_make_frame(src, dhash="1234567890abcdef"))
    assert envelope is None
    assert sensor.stats.frames_retained == 0
    assert not sensor._session_retention_dir.exists()


@pytest.mark.asyncio
async def test_retain_idempotent_same_dhash(tmp_path, traceback_ocr):
    """Two distinct frames with the same dhash (shouldn't happen in
    practice, but defensively tested) reuse the existing retained file
    instead of overwriting — the original bytes are authoritative for
    the signal that was emitted first."""
    src_a = _make_frame_file(tmp_path, name="a.jpg", payload=b"original")
    src_b = _make_frame_file(tmp_path, name="b.jpg", payload=b"different")
    sensor = _make_sensor(
        tmp_path,
        ocr_fn=traceback_ocr,
        frame_ttl_s=600.0,
    )
    await sensor._ingest_frame(_make_frame(src_a, dhash="deadbeefdeadbeef"))
    # Force a fresh ingress (clear dedup) — same dhash second time.
    sensor._recent_hashes.clear()
    await sensor._ingest_frame(_make_frame(src_b, dhash="deadbeefdeadbeef"))
    retained = sensor._session_retention_dir / "deadbeefdeadbeef.jpg"
    # Content is the FIRST frame's bytes — idempotent reuse.
    assert retained.read_bytes() == b"original"


# ---------------------------------------------------------------------------
# Memory-only mode — TTL <= 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_only_mode_skips_retention(tmp_path, traceback_ocr):
    src = _make_frame_file(tmp_path)
    sensor = _make_sensor(
        tmp_path,
        ocr_fn=traceback_ocr,
        frame_ttl_s=0.0,      # memory-only
    )
    envelope = await sensor._ingest_frame(_make_frame(src, dhash="abcdef1234567890"))
    assert envelope is not None
    # No disk footprint.
    assert not sensor._session_retention_dir.exists()
    # Evidence frame_path is the volatile Ferrari path.
    assert envelope.evidence["vision_signal"]["frame_path"] == src
    assert sensor.stats.frames_retained == 0


@pytest.mark.asyncio
async def test_memory_only_mode_also_skips_ttl_purge(tmp_path, traceback_ocr):
    # Set up a manually-created retention dir with an old file, confirm
    # the sensor doesn't touch it when TTL <= 0.
    src = _make_frame_file(tmp_path)
    sensor = _make_sensor(tmp_path, ocr_fn=traceback_ocr, frame_ttl_s=0.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    stale = sensor._session_retention_dir / "old.jpg"
    stale.write_bytes(b"ancient")
    removed = sensor._purge_expired_frames()
    assert removed == 0
    assert stale.exists()


# ---------------------------------------------------------------------------
# Best-effort failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retain_missing_source_falls_back_to_volatile_path(tmp_path, traceback_ocr):
    # Source file referenced by frame.frame_path does not exist on disk.
    sensor = _make_sensor(tmp_path, ocr_fn=traceback_ocr)
    missing_path = str(tmp_path / "never_existed.jpg")
    envelope = await sensor._ingest_frame(
        _make_frame(missing_path, dhash="1111222233334444")
    )
    assert envelope is not None
    # Retention failed → evidence falls back to the source path.
    assert envelope.evidence["vision_signal"]["frame_path"] == missing_path
    assert sensor.stats.frames_retained == 0


# ---------------------------------------------------------------------------
# TTL purge
# ---------------------------------------------------------------------------


def test_ttl_purge_removes_files_older_than_ttl(tmp_path):
    sensor = _make_sensor(tmp_path, frame_ttl_s=5.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    f = sensor._session_retention_dir / "aaaa0000.jpg"
    f.write_bytes(b"x")
    # Backdate the mtime well past the 5s TTL.
    past = time.time() - 60.0
    os.utime(f, (past, past))
    removed = sensor._purge_expired_frames()
    assert removed == 1
    assert not f.exists()
    assert sensor.stats.frames_purged_ttl == 1


def test_ttl_purge_preserves_fresh_files(tmp_path):
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    f = sensor._session_retention_dir / "fresh0001.jpg"
    f.write_bytes(b"x")
    # Current mtime — well inside TTL window.
    removed = sensor._purge_expired_frames()
    assert removed == 0
    assert f.exists()


def test_ttl_purge_tolerates_missing_directory(tmp_path):
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    # Directory never created — purge is a no-op, never raises.
    assert sensor._purge_expired_frames() == 0


def test_ttl_purge_rate_limited_by_interval(tmp_path, monkeypatch):
    """``_maybe_ttl_purge`` respects the 60s inter-purge window — two
    back-to-back calls within the window only run the purge once."""
    sensor = _make_sensor(tmp_path, frame_ttl_s=60.0)
    calls: List[int] = []
    monkeypatch.setattr(
        sensor, "_purge_expired_frames",
        lambda *a, **k: (calls.append(1), 0)[1],
    )
    sensor._maybe_ttl_purge()   # first call — purges
    sensor._maybe_ttl_purge()   # within window — skipped
    sensor._maybe_ttl_purge()   # still within — skipped
    assert len(calls) == 1


def test_ttl_purge_skipped_in_memory_only_mode(tmp_path):
    sensor = _make_sensor(tmp_path, frame_ttl_s=0.0)
    # Even with the interval elapsed, the memory-only check short-
    # circuits the purge attempt entirely.
    sensor._maybe_ttl_purge()
    # Nothing to assert beyond "did not crash". State untouched.
    assert sensor.stats.frames_purged_ttl == 0


# ---------------------------------------------------------------------------
# Shutdown purge — atexit + SIGTERM
# ---------------------------------------------------------------------------


def test_purge_session_dir_removes_files_and_dir(tmp_path):
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    (sensor._session_retention_dir / "a.jpg").write_bytes(b"x")
    (sensor._session_retention_dir / "b.jpg").write_bytes(b"y")
    removed = sensor._purge_session_dir_safe()
    assert removed == 2
    assert not sensor._session_retention_dir.exists()
    assert sensor.stats.frames_purged_shutdown == 2


def test_purge_session_dir_idempotent(tmp_path):
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    # Never created — first call is a no-op.
    assert sensor._purge_session_dir_safe() == 0
    # Create + purge + purge again — second is a no-op.
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    (sensor._session_retention_dir / "a.jpg").write_bytes(b"x")
    assert sensor._purge_session_dir_safe() == 1
    assert sensor._purge_session_dir_safe() == 0


def test_purge_session_dir_never_raises(tmp_path, monkeypatch):
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)

    def _boom(*args, **kwargs):
        raise RuntimeError("intentional failure")

    # Make the impl raise — the safe wrapper must swallow it.
    monkeypatch.setattr(sensor, "_purge_session_dir_impl", _boom)
    assert sensor._purge_session_dir_safe() == 0


def test_atexit_hook_registered_when_requested(tmp_path, monkeypatch):
    registered = []
    real_register = atexit.register
    monkeypatch.setattr(
        atexit, "register",
        lambda fn, *a, **k: (registered.append(fn), real_register(fn, *a, **k))[1],
    )
    sensor = VisionSensor(
        router=_StubRouter(),
        session_id="atexit-test",
        retention_root=str(tmp_path),
        frame_ttl_s=60.0,
        register_shutdown_hooks=True,
    )
    assert sensor._purge_session_dir_safe in registered
    # Clean up — don't leak the atexit entry into the test runner's
    # shutdown sequence.
    try:
        atexit.unregister(sensor._purge_session_dir_safe)
    except Exception:
        pass


def test_atexit_hook_not_registered_when_opted_out(tmp_path, monkeypatch):
    registered = []
    real_register = atexit.register
    monkeypatch.setattr(
        atexit, "register",
        lambda fn, *a, **k: (registered.append(fn), real_register(fn, *a, **k))[1],
    )
    sensor = VisionSensor(
        router=_StubRouter(),
        session_id="no-atexit",
        retention_root=str(tmp_path),
        frame_ttl_s=60.0,
        register_shutdown_hooks=False,
    )
    assert sensor._purge_session_dir_safe not in registered
    assert sensor._shutdown_hooks_registered is False


def test_sigterm_handler_purges_session_dir(tmp_path, monkeypatch):
    # We do NOT actually register the handler on the process — that
    # would clobber pytest's own signal handling. Instead we construct
    # the sensor with register_shutdown_hooks=False and drive
    # ``_on_sigterm`` directly.
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    (sensor._session_retention_dir / "a.jpg").write_bytes(b"x")

    # No prior handler chain — the handler should purge then attempt
    # to re-raise SIGTERM (which we intercept via monkeypatching
    # ``signal.raise_signal`` to avoid killing the test runner).
    raised = []
    monkeypatch.setattr(
        _signal, "raise_signal",
        lambda sig: raised.append(sig),
    )
    # SIG_DFL install is OK on a non-main thread because we also patch
    # ``signal.signal`` to be a no-op.
    monkeypatch.setattr(_signal, "signal", lambda *a, **k: None)
    sensor._prev_sigterm_handler = None   # simulate default-handler chain
    sensor._on_sigterm(_signal.SIGTERM, None)
    # Session dir purged.
    assert not sensor._session_retention_dir.exists()
    assert sensor.stats.frames_purged_shutdown == 1
    # And the handler re-raised SIGTERM exactly once.
    assert raised == [_signal.SIGTERM]


def test_sigterm_handler_chains_to_previous_callable(tmp_path, monkeypatch):
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    (sensor._session_retention_dir / "a.jpg").write_bytes(b"x")

    chained = []

    def _prev(signum, frame):
        chained.append(signum)

    sensor._prev_sigterm_handler = _prev
    # raise_signal must NOT fire when a callable chain exists.
    raised = []
    monkeypatch.setattr(_signal, "raise_signal", lambda sig: raised.append(sig))
    sensor._on_sigterm(_signal.SIGTERM, None)
    assert chained == [_signal.SIGTERM]
    assert raised == []


def test_sigterm_handler_tolerates_prev_raising(tmp_path, monkeypatch):
    """A misbehaving previous handler must not prevent purge from
    completing, and must not bubble."""
    sensor = _make_sensor(tmp_path, frame_ttl_s=600.0)
    sensor._session_retention_dir.mkdir(parents=True, exist_ok=True)
    (sensor._session_retention_dir / "a.jpg").write_bytes(b"x")

    def _bad(signum, frame):
        raise RuntimeError("prev handler exploded")

    sensor._prev_sigterm_handler = _bad
    # Should return without raising.
    sensor._on_sigterm(_signal.SIGTERM, None)
    assert not sensor._session_retention_dir.exists()


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def test_two_sessions_do_not_clobber_each_other(tmp_path):
    s1 = _make_sensor(tmp_path, session_id="session-one", frame_ttl_s=600.0)
    s2 = _make_sensor(tmp_path, session_id="session-two", frame_ttl_s=600.0)
    s1._session_retention_dir.mkdir(parents=True, exist_ok=True)
    s2._session_retention_dir.mkdir(parents=True, exist_ok=True)
    (s1._session_retention_dir / "a.jpg").write_bytes(b"from_s1")
    (s2._session_retention_dir / "a.jpg").write_bytes(b"from_s2")
    # s1's shutdown purge must not touch s2's directory.
    s1._purge_session_dir_safe()
    assert not s1._session_retention_dir.exists()
    assert s2._session_retention_dir.exists()
    assert (s2._session_retention_dir / "a.jpg").read_bytes() == b"from_s2"


# ---------------------------------------------------------------------------
# Pinned module constants
# ---------------------------------------------------------------------------


def test_ttl_purge_interval_pinned():
    assert _TTL_PURGE_INTERVAL_S == 60.0


def test_gitignore_covers_retention_root():
    """Belt-and-braces: ensure the repo's .gitignore actually excludes
    the retention root. A regression where someone removes these
    entries would silently commit screen captures."""
    # tests/governance/intake/sensors/test_*.py → parents[4] = repo root
    gi = Path(__file__).resolve().parents[4] / ".gitignore"
    text = gi.read_text(encoding="utf-8")
    assert ".jarvis/vision_frames/" in text
    assert ".jarvis/vision_sensor_fp_ledger.json" in text
    assert ".jarvis/vision_cost_ledger.json" in text
