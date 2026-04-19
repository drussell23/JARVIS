"""Cross-cutting threat-model regression spine for the VisionSensor.

This file is the canonical home for the invariant + threat tests named
in the spec — it grows across the implementation plan:

    Task 10 (this file's initial scope):
        I8 — No capture authority. The sensor is a **read-only**
        consumer of the Ferrari frame stream owned by VisionCortex;
        if the stream is absent the sensor fails closed (no signals,
        rate-limited telemetry, zero capture-API calls).

    Task 12 (extends this file):
        T1 — Prompt injection via screen text.
        T2 — Credential leak via screenshot (hard-coded denylist +
        FORBIDDEN_APP + credential-shape regex).
        T3 — Flicker cost runaway contained by inter-signal cooldown.
        T4 — Stale signal caught at pre-APPLY re-capture.
        T5 — Sensor loop prevented by chain cap + post-APPLY cooldown.
        T6 — Visual VERIFY UX-state guard (Task 17 interaction).
        T7 — Retention purged on shutdown (overlap with Task 9 spine).

Every I8 check here is either **structural** (AST-walked) or **runtime**
(driven through the real ``VisionSensor`` instance against a mocked
filesystem). We do NOT rely on text-grep against the module source —
docstrings legitimately name forbidden symbols for spec cross-reference,
and a grep would false-fire.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Invariant I8 + §Threat Model.
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import json
import logging
import pathlib
import time
from typing import List

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import IntentEnvelope
from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
    FrameData,
    VisionSensor,
)


_SENSOR_SOURCE_PATH = pathlib.Path(
    "backend/core/ouroboros/governance/intake/sensors/vision_sensor.py"
)


class _StubRouter:
    async def ingest(self, envelope: IntentEnvelope) -> str:
        return "enqueued"


def _make_sensor(**overrides) -> VisionSensor:
    """Build a sensor with deterministic defaults for threat-model tests.

    ``register_shutdown_hooks`` is forced off so the process-level
    ``atexit`` + ``SIGTERM`` registry doesn't accumulate fixtures.
    """
    kwargs = dict(
        router=_StubRouter(),
        session_id="threat-model",
        retention_root="/tmp/claude-test-threat-model",
        frame_ttl_s=0.0,              # memory-only — keep the filesystem quiet
        register_shutdown_hooks=False,
        frame_path="/tmp/claude/nonexistent_frame.jpg",
        metadata_path="/tmp/claude/nonexistent_frame.json",
    )
    kwargs.update(overrides)
    return VisionSensor(**kwargs)


# =========================================================================
# I8 — Structural checks (AST walk, no grep)
# =========================================================================


def _read_sensor_ast() -> ast.Module:
    src = _SENSOR_SOURCE_PATH.read_text(encoding="utf-8")
    return ast.parse(src)


_FORBIDDEN_IDENTIFIERS = frozenset({
    "_ensure_frame_server",
    "CGWindowListCreateImage",
    "CGWindowListCopyWindowInfo",    # enumerating windows is Visual VERIFY's job, not the sensor's
    "CGImageCreate",
    "CGWindowListCreateImageFromArray",
    "Quartz",
    "ScreenCaptureKit",
    "AVFoundation",
    "SCStream",                      # ScreenCaptureKit stream type
})

# Python-visible module names that, if imported, imply capture authority.
# ``Quartz`` / ``AVFoundation`` also appear above as identifiers because
# a native import like ``import Quartz`` creates both a name binding and
# an import node, and we check both surfaces.
_FORBIDDEN_IMPORT_MODULES = frozenset({
    "Quartz",
    "ScreenCaptureKit",
    "AVFoundation",
    "objc",                          # PyObjC gateway to native frameworks
    "mss",                           # cross-platform screen grabber lib
    "pyscreeze",                     # PyAutoGUI's capture backend
    "PIL.ImageGrab",                 # Pillow's capture helper
})


def test_i8_no_forbidden_imports_in_sensor_module():
    tree = _read_sensor_ast()
    hits: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORT_MODULES:
                    hits.append(f"Import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in _FORBIDDEN_IMPORT_MODULES:
                hits.append(f"ImportFrom:{node.module}")
    assert not hits, (
        f"I8 violation — VisionSensor imports capture libraries: {hits}. "
        f"The sensor is a read-only consumer of the Ferrari frame stream; "
        f"it must never open a capture device itself."
    )


def test_i8_no_forbidden_identifier_references_in_sensor_module():
    tree = _read_sensor_ast()
    hits: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_IDENTIFIERS:
            hits.append(f"Name:{node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_IDENTIFIERS:
            hits.append(f"Attribute:{node.attr}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_IDENTIFIERS:
                hits.append(f"Call:{fn.id}")
            elif isinstance(fn, ast.Attribute) and fn.attr in _FORBIDDEN_IDENTIFIERS:
                hits.append(f"Call.Attribute:{fn.attr}")
    assert not hits, (
        f"I8 violation — VisionSensor module has live references to "
        f"forbidden capture symbols: {hits}. Docstrings mentioning these "
        f"names for spec cross-reference are fine (strings are not AST "
        f"Name nodes); only executable references fail this check."
    )


def test_i8_vision_sensor_class_exposes_no_capture_methods():
    """Every method on ``VisionSensor`` must be recognisably a *read*
    operation. This guards against a future refactor that silently
    introduces e.g. ``_capture_fresh_frame`` or ``_spawn_frame_server``
    on the class surface.
    """
    tree = _read_sensor_ast()
    method_names: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "VisionSensor":
            continue
        for body_node in node.body:
            if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_names.append(body_node.name)
    # Sanity: we actually found methods.
    assert method_names, "VisionSensor class not found — test setup issue"
    # No method name should imply capture authority.
    forbidden_prefixes = (
        "capture_", "_capture_",
        "grab_", "_grab_",
        "spawn_frame", "_spawn_frame",
        "start_frame_server", "_start_frame_server",
        "ensure_frame_server", "_ensure_frame_server",
    )
    offenders = [
        m for m in method_names
        if any(m.startswith(p) for p in forbidden_prefixes)
    ]
    assert not offenders, (
        f"I8 violation — VisionSensor class has methods implying "
        f"capture authority: {offenders}"
    )


# =========================================================================
# I8 — Runtime checks (real sensor against mocked filesystem)
# =========================================================================


def test_i8_fails_closed_when_frame_file_absent(tmp_path):
    """Only the sidecar exists — no JPEG frame. Fail closed."""
    meta = tmp_path / "latest_frame.json"
    meta.write_text('{"dhash":"abcdef0123456789","ts":1.0}', encoding="utf-8")
    sensor = _make_sensor(
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(meta),
    )
    frame = sensor._read_frame()
    assert frame is None
    assert sensor.stats.dropped_ferrari_absent == 1
    assert sensor.stats.frames_polled == 0
    assert sensor.stats.signals_emitted == 0


def test_i8_fails_closed_when_metadata_absent(tmp_path):
    """Only the JPEG exists — no sidecar. Fail closed."""
    frame_path = tmp_path / "latest_frame.jpg"
    frame_path.write_bytes(b"\x89PNG" + b"\x00" * 32)
    sensor = _make_sensor(
        frame_path=str(frame_path),
        metadata_path=str(tmp_path / "missing.json"),
    )
    frame = sensor._read_frame()
    assert frame is None
    assert sensor.stats.dropped_ferrari_absent == 1
    assert sensor.stats.signals_emitted == 0


def test_i8_fails_closed_when_both_absent(tmp_path):
    sensor = _make_sensor(
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    for _ in range(10):
        assert sensor._read_frame() is None
    assert sensor.stats.dropped_ferrari_absent == 10
    assert sensor.stats.frames_polled == 0
    assert sensor.stats.signals_emitted == 0
    assert sensor.stats.frames_retained == 0


@pytest.mark.asyncio
async def test_i8_scan_once_emits_nothing_when_ferrari_absent(tmp_path):
    sensor = _make_sensor(
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    out = await sensor.scan_once()
    assert out == []


# =========================================================================
# I8 — Rate-limited degraded telemetry
# =========================================================================


def test_i8_degraded_breadcrumb_rate_limited_across_many_polls(tmp_path):
    sensor = _make_sensor(
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    # Fire many reads — only the first should bump ``degraded_ticks``.
    for _ in range(20):
        sensor._read_frame()
    assert sensor.stats.degraded_ticks == 1
    assert sensor.stats.dropped_ferrari_absent == 20


def test_i8_degraded_breadcrumb_logs_at_info_with_both_paths(tmp_path, caplog):
    sensor = _make_sensor(
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    with caplog.at_level(logging.INFO, logger="Ouroboros.VisionSensor"):
        sensor._read_frame()
    msgs = [rec.message for rec in caplog.records]
    # The breadcrumb mentions the canonical reason and both paths so the
    # operator can debug why Ferrari isn't producing output.
    relevant = [m for m in msgs if "degraded reason=ferrari_absent" in m]
    assert len(relevant) == 1
    assert "frame_path=" in relevant[0]
    assert "metadata_path=" in relevant[0]


# =========================================================================
# I8 — No writes to the Ferrari frame / metadata paths
# =========================================================================


def test_i8_sensor_never_opens_ferrari_paths_for_write(tmp_path, monkeypatch):
    """Wrap ``builtins.open`` and fail the test if the sensor ever opens
    the Ferrari frame / metadata paths in a write mode.

    The sensor IS allowed to open these paths read-only — that's how it
    *consumes* Ferrari's output. Writing would imply capture authority.
    """
    frame_path = str(tmp_path / "latest_frame.jpg")
    meta_path = str(tmp_path / "latest_frame.json")
    # Create the files so reads succeed.
    pathlib.Path(frame_path).write_bytes(b"\x89PNG" + b"\x00" * 32)
    pathlib.Path(meta_path).write_text(
        '{"dhash":"abcdef0123456789","ts":1.0}', encoding="utf-8",
    )

    real_open = builtins.open
    bad_opens: List[tuple] = []

    def _tracking_open(path, mode="r", *args, **kwargs):
        # Any write-mode access to the Ferrari output paths is forbidden.
        if "w" in mode or "a" in mode or "x" in mode or "+" in mode:
            if str(path) in (frame_path, meta_path):
                bad_opens.append((str(path), mode))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _tracking_open)

    sensor = _make_sensor(frame_path=frame_path, metadata_path=meta_path)
    # Exercise the disk-ingress path several times.
    for _ in range(5):
        sensor._read_frame()

    assert not bad_opens, (
        f"I8 violation — sensor opened Ferrari output paths for write: "
        f"{bad_opens}. The sensor must never mutate Ferrari's stream."
    )


# =========================================================================
# I8 — Schema drift / malformed sidecar
# =========================================================================


def _write_pair(tmp_path, sidecar: dict) -> tuple:
    frame_path = tmp_path / "latest_frame.jpg"
    frame_path.write_bytes(b"\x89PNG" + b"\x00" * 32)
    meta_path = tmp_path / "latest_frame.json"
    meta_path.write_text(json.dumps(sidecar), encoding="utf-8")
    return str(frame_path), str(meta_path)


def test_i8_malformed_sidecar_missing_dhash_is_dropped(tmp_path):
    fp, mp = _write_pair(tmp_path, {"ts": 1.0})
    sensor = _make_sensor(frame_path=fp, metadata_path=mp)
    assert sensor._read_frame() is None
    assert sensor.stats.signals_emitted == 0


def test_i8_malformed_sidecar_missing_ts_is_dropped(tmp_path):
    fp, mp = _write_pair(tmp_path, {"dhash": "abcd"})
    sensor = _make_sensor(frame_path=fp, metadata_path=mp)
    assert sensor._read_frame() is None
    assert sensor.stats.signals_emitted == 0


def test_i8_invalid_json_sidecar_is_dropped(tmp_path):
    frame_path = tmp_path / "latest_frame.jpg"
    frame_path.write_bytes(b"\x89PNG" + b"\x00" * 32)
    meta_path = tmp_path / "latest_frame.json"
    meta_path.write_text("{broken", encoding="utf-8")
    sensor = _make_sensor(frame_path=str(frame_path), metadata_path=str(meta_path))
    assert sensor._read_frame() is None


@pytest.mark.asyncio
async def test_i8_schema_malformed_at_build_evidence_fails_closed():
    """If ``build_vision_signal_evidence`` rejects the frame (e.g. a bad
    dhash format makes it past ``_read_frame`` because Ferrari's sidecar
    schema drifts), ``_ingest_frame`` must fail closed: drop, bump the
    malformed counter, never raise.
    """
    sensor = _make_sensor(
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    # FrameData is permissive — it accepts anything. ``build_vision_
    # signal_evidence`` then rejects because dhash must be 16 hex.
    bad_frame = FrameData(
        frame_path="/tmp/claude/latest_frame.jpg",
        dhash="NOT_HEX_xxxxxxxx",       # invalid per schema v1 regex
        ts=1.0,
        app_id=None,
        window_id=None,
    )
    envelope = await sensor._ingest_frame(bad_frame)
    assert envelope is None
    assert sensor.stats.dropped_schema_malformed == 1
    assert sensor.stats.signals_emitted == 0


@pytest.mark.asyncio
async def test_i8_negative_ts_fails_closed():
    sensor = _make_sensor(
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    bad_frame = FrameData(
        frame_path="/tmp/claude/latest_frame.jpg",
        dhash="abcdef0123456789",
        ts=-1.0,                        # violates I1 non-negative
        app_id=None,
        window_id=None,
    )
    envelope = await sensor._ingest_frame(bad_frame)
    assert envelope is None
    assert sensor.stats.dropped_schema_malformed == 1


# =========================================================================
# I8 — Idempotency + bounded state across Ferrari-absent polls
# =========================================================================


@pytest.mark.asyncio
async def test_i8_many_ferrari_absent_polls_leave_memory_bounded(tmp_path):
    """1000 consecutive failed reads must not grow the dedup-hash table,
    the stats-counter dataclass, or any private collection beyond a
    small constant."""
    sensor = _make_sensor(
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    for _ in range(1000):
        assert await sensor.scan_once() == []
    assert sensor.stats.dropped_ferrari_absent == 1000
    # No hash has been observed — dedup table stays empty.
    assert len(sensor._recent_hashes) == 0
    # Rate-limited breadcrumb fired once, not 1000 times.
    assert sensor.stats.degraded_ticks == 1
    assert sensor.stats.signals_emitted == 0
