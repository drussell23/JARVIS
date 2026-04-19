"""Regression spine for ``VisionSensor`` — Task 8 (Tier 0 + Tier 1 skeleton).

Scope: pure-inner-unit ``_ingest_frame`` (dhash dedup + OCR regex), disk
ingress ``_read_frame`` (fail-closed I8), ``scan_once`` integration,
verdict+severity mapping per §Sensor Contract, evidence schema v1
emission.

Out of scope (later tasks):
* Tier 2 VLM classifier — Task 15.
* FP budget / finding cooldowns / chain cap — Task 11.
* Retention directory + atexit purge — Task 9.
* Full T1–T7 threat-model spine + I8 static source grep — Task 10/12.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Sensor Contract + §Invariant I8.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import IntentEnvelope
from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
    _ADAPTIVE_MAX_INTERVAL_S,
    _ADAPTIVE_STATIC_BEFORE_DOWNSHIFT,
    _BUG_PATTERNS,
    _ERROR_PATTERNS,
    _HASH_COOLDOWN_S,
    _INJECTION_PATTERNS,
    _OCR_SNIPPET_LEN,
    FrameData,
    VisionSensor,
    _classify_from_matches,
    _run_deterministic_patterns,
    _truncate_snippet,
)


# ---------------------------------------------------------------------------
# Autouse isolation — keep the FP ledger (Task 11) from leaking
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_vision_sensor_disk_state(tmp_path, monkeypatch):
    """Chdir into ``tmp_path`` so every ``VisionSensor(...)`` instance in
    this file writes its default-path disk artifacts (FP ledger, retention
    directory) into an isolated tmp dir — NOT the repo's ``.jarvis/``.

    Without this, the disk-persisted FP ledger introduced in Task 11
    leaks finding-cooldown state across tests and across test files.
    """
    monkeypatch.chdir(tmp_path)
    yield


# ---------------------------------------------------------------------------
# Test router stub
# ---------------------------------------------------------------------------


class _StubRouter:
    """Records every envelope passed through ``ingest``."""

    def __init__(self) -> None:
        self.ingested: List[IntentEnvelope] = []
        self.raise_on_next: Optional[Exception] = None

    async def ingest(self, envelope: IntentEnvelope) -> str:
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        self.ingested.append(envelope)
        return "enqueued"


def _make_frame(
    dhash: str = "a7b9c2d4e5f6abcd",
    frame_path: str = "/tmp/claude/latest_frame.jpg",
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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_injection_patterns_contain_all_five():
    assert set(_INJECTION_PATTERNS.keys()) == {
        "traceback", "panic", "segfault", "modal_error", "linter_red",
    }


def test_error_and_bug_pattern_sets_partition_injection_patterns():
    # Every pattern name belongs to exactly one tier bucket.
    assert _ERROR_PATTERNS | _BUG_PATTERNS == set(_INJECTION_PATTERNS.keys())
    assert not (_ERROR_PATTERNS & _BUG_PATTERNS)


@pytest.mark.parametrize(
    "text,expected",
    [
        # --- traceback ---
        ("Traceback (most recent call last):\n  File ...", ["traceback"]),
        ("some noise\nTraceback (most recent call last):\nmore", ["traceback"]),
        # --- panic ---
        ("panic: runtime error: index out of range", ["panic"]),
        ("PANIC: rust panicked at 'oops'", ["panic"]),
        # --- segfault ---
        ("segmentation fault (core dumped)", ["segfault"]),
        ("process died with SIGSEGV", ["segfault"]),
        # --- modal_error ---
        ("Error\n\nSomething broke\n\n[OK]", ["modal_error"]),
        ("Failed to save\n[Retry]", ["modal_error"]),
        # --- linter_red ---
        ("TypeError: unsupported operand", ["linter_red"]),
        ("ReferenceError: x is not defined", ["linter_red"]),
        ("ImportError: no module named foo", ["linter_red"]),
        # --- multiple hits ---
        (
            "Traceback (most recent call last):\nTypeError: x",
            ["traceback", "linter_red"],
        ),
    ],
)
def test_run_deterministic_patterns_matches_expected(text, expected):
    assert _run_deterministic_patterns(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Welcome to My App\nAll systems nominal",
        "File operation completed",
        "Just a README with the word error",  # 'error' alone is NOT modal_error
    ],
)
def test_run_deterministic_patterns_no_match_clean_screen(text):
    assert _run_deterministic_patterns(text) == []


def test_classify_from_matches_empty_returns_none():
    assert _classify_from_matches([]) is None


@pytest.mark.parametrize(
    "matches,expected_verdict,expected_severity,expected_urgency",
    [
        (["traceback"], "error_visible", "error", "high"),
        (["panic"], "error_visible", "error", "high"),
        (["segfault"], "error_visible", "error", "high"),
        (["modal_error"], "bug_visible", "warning", "normal"),
        (["linter_red"], "bug_visible", "warning", "normal"),
        # Error bucket dominates when both buckets hit simultaneously.
        (["modal_error", "traceback"], "error_visible", "error", "high"),
    ],
)
def test_classify_from_matches_bucket_mapping(
    matches, expected_verdict, expected_severity, expected_urgency,
):
    verdict = _classify_from_matches(matches)
    assert verdict == {
        "classifier_verdict": expected_verdict,
        "severity": expected_severity,
        "urgency": expected_urgency,
    }


def test_truncate_snippet_respects_max_len():
    long = "x" * (_OCR_SNIPPET_LEN + 100)
    assert len(_truncate_snippet(long)) == _OCR_SNIPPET_LEN


def test_truncate_snippet_empty_safe():
    assert _truncate_snippet("") == ""


# ---------------------------------------------------------------------------
# _ingest_frame — Tier 0 hash dedup
# ---------------------------------------------------------------------------


@pytest.fixture
def sensor():
    """Plain sensor with no OCR and no router side-effect."""
    return VisionSensor(router=_StubRouter(), ocr_fn=None)


@pytest.fixture
def sensor_with_ocr():
    """Sensor whose OCR returns whatever callable is patched in."""
    return VisionSensor(router=_StubRouter(), ocr_fn=lambda _p: "")


@pytest.mark.asyncio
async def test_tier0_dedup_drops_repeat_hash(sensor_with_ocr):
    sensor_with_ocr._ocr_fn = lambda _p: "Traceback (most recent call last):"
    f1 = _make_frame(dhash="abcdef0123456789")
    f2 = _make_frame(dhash="abcdef0123456789", ts=1.5)   # same hash
    first = await sensor_with_ocr._ingest_frame(f1)
    second = await sensor_with_ocr._ingest_frame(f2)
    assert first is not None
    assert second is None
    assert sensor_with_ocr.stats.dropped_hash_dedup == 1


@pytest.mark.asyncio
async def test_tier0_accepts_distinct_hash(sensor_with_ocr):
    sensor_with_ocr._ocr_fn = lambda _p: "Traceback (most recent call last):"
    f1 = _make_frame(dhash="abcdef0123456789")
    f2 = _make_frame(dhash="1111222233334444")
    first = await sensor_with_ocr._ingest_frame(f1)
    second = await sensor_with_ocr._ingest_frame(f2)
    assert first is not None
    assert second is not None
    assert sensor_with_ocr.stats.dropped_hash_dedup == 0


@pytest.mark.asyncio
async def test_tier0_dedup_expires_after_cooldown(monkeypatch):
    s = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
        hash_cooldown_s=0.01,   # expire almost immediately
    )
    f = _make_frame(dhash="bbbbbbbbbbbbbbbb")
    first = await s._ingest_frame(f)
    # Sleep past cooldown
    await asyncio.sleep(0.02)
    second = await s._ingest_frame(f)
    assert first is not None
    assert second is not None
    assert s.stats.dropped_hash_dedup == 0


# ---------------------------------------------------------------------------
# _ingest_frame — Tier 1 OCR + regex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_traceback_emits_error_visible_signal():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):\n  File 'x.py', line 1",
    )
    envelope = await sensor._ingest_frame(_make_frame(dhash="c0ffeec0ffeec0ff"))
    assert envelope is not None
    assert envelope.source == "vision_sensor"
    assert envelope.urgency == "high"
    vs = envelope.evidence["vision_signal"]
    assert vs["classifier_verdict"] == "error_visible"
    assert vs["severity"] == "error"
    assert "traceback" in vs["deterministic_matches"]
    assert vs["classifier_model"] == "deterministic"
    assert vs["classifier_confidence"] == 1.0


@pytest.mark.asyncio
async def test_tier1_linter_red_emits_bug_visible_signal():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "TypeError: cannot concatenate str and int",
    )
    envelope = await sensor._ingest_frame(_make_frame(dhash="deadbeef12345678"))
    assert envelope is not None
    assert envelope.urgency == "normal"
    vs = envelope.evidence["vision_signal"]
    assert vs["classifier_verdict"] == "bug_visible"
    assert vs["severity"] == "warning"
    assert "linter_red" in vs["deterministic_matches"]


@pytest.mark.asyncio
async def test_tier1_clean_screen_emits_nothing():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Welcome to My App",
    )
    envelope = await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa"))
    assert envelope is None
    assert sensor.stats.dropped_no_match == 1


@pytest.mark.asyncio
async def test_tier1_ocr_none_means_no_emit(sensor):
    # ocr_fn is None → OCR text empty → no regex hits → no signal.
    envelope = await sensor._ingest_frame(_make_frame(dhash="1234567890abcdef"))
    assert envelope is None


@pytest.mark.asyncio
async def test_tier1_ocr_exception_treated_as_empty():
    def _raises(_path):
        raise RuntimeError("OCR server crashed")

    sensor = VisionSensor(router=_StubRouter(), ocr_fn=_raises)
    envelope = await sensor._ingest_frame(_make_frame(dhash="1234567890abcdef"))
    assert envelope is None
    assert sensor.stats.dropped_no_match == 1


# ---------------------------------------------------------------------------
# Evidence schema v1 — every field populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_schema_v1_all_fields_populated():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    envelope = await sensor._ingest_frame(_make_frame(
        dhash="0123456789abcdef",
        ts=12345.678,
        app_id="com.apple.Terminal",
        window_id=42,
    ))
    assert envelope is not None
    e = envelope.evidence["vision_signal"]
    # Schema version locked
    assert e["schema_version"] == 1
    # Every field populated with the expected shape
    assert e["frame_hash"] == "0123456789abcdef"
    assert e["frame_ts"] == 12345.678
    assert e["frame_path"] == "/tmp/claude/latest_frame.jpg"
    assert e["app_id"] == "com.apple.Terminal"
    assert e["window_id"] == 42
    assert e["classifier_verdict"] == "error_visible"
    assert e["classifier_model"] == "deterministic"
    assert e["classifier_confidence"] == 1.0
    assert e["deterministic_matches"] == ("traceback",)
    assert "Traceback" in e["ocr_snippet"]
    assert e["severity"] == "error"


@pytest.mark.asyncio
async def test_evidence_ocr_snippet_truncated_at_max_len():
    big = "Traceback (most recent call last):\n" + ("x" * 1000)
    sensor = VisionSensor(router=_StubRouter(), ocr_fn=lambda _p: big)
    envelope = await sensor._ingest_frame(_make_frame(dhash="1234567890abcdef"))
    assert envelope is not None
    assert len(envelope.evidence["vision_signal"]["ocr_snippet"]) <= _OCR_SNIPPET_LEN


@pytest.mark.asyncio
async def test_envelope_signature_groups_by_verdict_and_app():
    # Disable Task 11 finding cooldown so both emissions actually fire
    # — this test exercises signature-building logic, not cooldowns.
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
        finding_cooldown_s=0.0,
    )
    e1 = await sensor._ingest_frame(_make_frame(dhash="1111222233334444", app_id="com.apple.Terminal"))
    # Different hash, same verdict+app → same signature (intake dedup unit).
    e2 = await sensor._ingest_frame(_make_frame(dhash="5555666677778888", app_id="com.apple.Terminal"))
    assert e1.evidence["signature"] == e2.evidence["signature"]


@pytest.mark.asyncio
async def test_envelope_signature_differs_when_app_differs():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    e1 = await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa", app_id="com.apple.Terminal"))
    e2 = await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb", app_id="com.apple.Safari"))
    assert e1.evidence["signature"] != e2.evidence["signature"]


# ---------------------------------------------------------------------------
# _read_frame — disk ingress + fail-closed I8
# ---------------------------------------------------------------------------


def _write_sidecar(tmp_path: Path, **meta) -> tuple[str, str]:
    frame_path = tmp_path / "latest_frame.jpg"
    frame_path.write_bytes(b"\x89PNG" + b"\x00" * 32)
    meta_path = tmp_path / "latest_frame.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return str(frame_path), str(meta_path)


def test_read_frame_returns_none_when_frame_missing(tmp_path):
    # Only the sidecar exists.
    meta_path = tmp_path / "latest_frame.json"
    meta_path.write_text('{"dhash":"abcd","ts":1.0}', encoding="utf-8")
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(meta_path),
    )
    assert sensor._read_frame() is None
    assert sensor.stats.dropped_ferrari_absent == 1


def test_read_frame_returns_none_when_metadata_missing(tmp_path):
    frame_path = tmp_path / "latest_frame.jpg"
    frame_path.write_bytes(b"\x89PNG" + b"\x00" * 32)
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=str(frame_path),
        metadata_path=str(tmp_path / "missing.json"),
    )
    assert sensor._read_frame() is None
    assert sensor.stats.dropped_ferrari_absent == 1


def test_read_frame_returns_none_when_sidecar_malformed(tmp_path):
    frame_path, meta_path = _write_sidecar(tmp_path, dhash="abcd")  # missing ts
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=frame_path,
        metadata_path=meta_path,
    )
    assert sensor._read_frame() is None


def test_read_frame_returns_none_when_sidecar_invalid_json(tmp_path):
    frame_path = tmp_path / "latest_frame.jpg"
    frame_path.write_bytes(b"\x89PNG")
    meta_path = tmp_path / "latest_frame.json"
    meta_path.write_text("{not valid json", encoding="utf-8")
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=str(frame_path),
        metadata_path=str(meta_path),
    )
    assert sensor._read_frame() is None


def test_read_frame_parses_valid_sidecar(tmp_path):
    frame_path, meta_path = _write_sidecar(
        tmp_path,
        dhash="0011223344556677",
        ts=12345.6,
        app_id="com.apple.Terminal",
        window_id=99,
    )
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=frame_path,
        metadata_path=meta_path,
    )
    frame = sensor._read_frame()
    assert frame is not None
    assert frame.dhash == "0011223344556677"
    assert frame.ts == 12345.6
    assert frame.app_id == "com.apple.Terminal"
    assert frame.window_id == 99


def test_read_frame_rejects_bool_window_id(tmp_path):
    # bool is a subclass of int — must NOT slip through as window_id.
    frame_path, meta_path = _write_sidecar(
        tmp_path,
        dhash="aabbccddeeff0011",
        ts=1.0,
        window_id=True,
    )
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=frame_path,
        metadata_path=meta_path,
    )
    frame = sensor._read_frame()
    assert frame is not None
    assert frame.window_id is None  # bool silently coerced to None


def test_degraded_breadcrumb_is_rate_limited(tmp_path):
    """Rate-limited breadcrumb: three back-to-back reads with Ferrari
    absent must bump the ``degraded_ticks`` counter exactly once in
    under the 60-second window.
    """
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    sensor._read_frame()
    sensor._read_frame()
    sensor._read_frame()
    assert sensor.stats.degraded_ticks == 1
    # But every read still increments the "ferrari absent" drop counter.
    assert sensor.stats.dropped_ferrari_absent == 3


# ---------------------------------------------------------------------------
# I8 — structural "no capture authority" check
# ---------------------------------------------------------------------------


def test_i8_module_does_not_reference_forbidden_capture_symbols():
    """Static AST check: the sensor file must never *execute* anything
    that implies capture authority — no imports of Quartz / SCK /
    AVFoundation, no calls to ``_ensure_frame_server`` /
    ``CGWindowListCreateImage``.

    The check walks the AST rather than greps the raw source, so
    docstrings and comments that *name* the forbidden symbols for
    documentation (spec cross-references, §Sensor Contract prose)
    do not trip the check. Only live identifier / import nodes do.

    Task 10 layers a more thorough threat-model spine on top; this is
    the earlier, tighter per-module check so any regression in Task
    8+ is caught here first.
    """
    import ast
    import pathlib

    # Resolve from ``__file__`` (absolute) so the autouse chdir fixture
    # doesn't break this lookup. ``tests/governance/intake/sensors/
    # test_vision_sensor.py`` → parents[4] = repo root.
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    src = (
        repo_root
        / "backend/core/ouroboros/governance/intake/sensors/vision_sensor.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    forbidden_identifiers = {
        "_ensure_frame_server",
        "CGWindowListCreateImage",
        "Quartz",
        "ScreenCaptureKit",
        "AVFoundation",
    }
    forbidden_import_modules = {
        "Quartz",
        "ScreenCaptureKit",
        "AVFoundation",
        "objc",          # PyObjC gateway
    }

    hits = []
    for node in ast.walk(tree):
        # Name references (e.g. ``Quartz.CGWindowListCreateImage(...)``
        # would expose ``Quartz`` as a Name).
        if isinstance(node, ast.Name) and node.id in forbidden_identifiers:
            hits.append(f"Name:{node.id}")
        # Attribute reads (e.g. ``self._ensure_frame_server``,
        # ``lean_loop._ensure_frame_server``).
        if isinstance(node, ast.Attribute) and node.attr in forbidden_identifiers:
            hits.append(f"Attribute:{node.attr}")
        # Calls to a Name whose id is forbidden.
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in forbidden_identifiers:
                hits.append(f"Call:{fn.id}")
        # Imports.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_import_modules:
                    hits.append(f"Import:{alias.name}")
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_import_modules:
                hits.append(f"ImportFrom:{node.module}")

    assert not hits, (
        f"I8 violation — VisionSensor module has live references to "
        f"forbidden capture symbols: {hits}. The sensor is a read-only "
        f"consumer of the Ferrari frame stream; it must never spawn "
        f"capture or import capture APIs."
    )


# ---------------------------------------------------------------------------
# scan_once integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_returns_empty_when_ferrari_absent(tmp_path):
    sensor = VisionSensor(
        router=_StubRouter(),
        frame_path=str(tmp_path / "missing.jpg"),
        metadata_path=str(tmp_path / "missing.json"),
    )
    out = await sensor.scan_once()
    assert out == []


@pytest.mark.asyncio
async def test_scan_once_emits_envelope_via_router(tmp_path):
    frame_path, meta_path = _write_sidecar(
        tmp_path,
        dhash="feedbeeffeedbeef",
        ts=1.0,
        app_id="com.apple.Terminal",
    )
    router = _StubRouter()
    sensor = VisionSensor(
        router=router,
        frame_path=frame_path,
        metadata_path=meta_path,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    envs = await sensor.scan_once()
    assert len(envs) == 1
    assert len(router.ingested) == 1
    assert router.ingested[0].source == "vision_sensor"


@pytest.mark.asyncio
async def test_scan_once_swallows_router_ingest_exception(tmp_path, caplog):
    frame_path, meta_path = _write_sidecar(tmp_path, dhash="1212121212121212", ts=1.0)
    router = _StubRouter()
    router.raise_on_next = RuntimeError("router down")
    sensor = VisionSensor(
        router=router,
        frame_path=frame_path,
        metadata_path=meta_path,
        ocr_fn=lambda _p: "Traceback (most recent call last):",
    )
    with caplog.at_level("ERROR"):
        envs = await sensor.scan_once()
    # Returns empty on router failure (sensor does not retry inline) but
    # does not crash the poll loop.
    assert envs == []
    assert any("router.ingest raised" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Adaptive throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adaptive_downshifts_after_consecutive_unchanged_frames():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
        poll_interval_s=1.0,
    )
    assert sensor._current_poll_interval_s == 1.0
    # Emit one fresh frame, then repeat the same hash enough times to
    # cross the downshift threshold.
    await sensor._ingest_frame(_make_frame(dhash="f11511cf11511cff"))
    for _ in range(_ADAPTIVE_STATIC_BEFORE_DOWNSHIFT):
        await sensor._ingest_frame(_make_frame(dhash="f11511cf11511cff"))
    sensor._adjust_adaptive_interval()
    assert sensor._current_poll_interval_s == 2.0   # one doubling


@pytest.mark.asyncio
async def test_adaptive_caps_at_max_interval():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
        poll_interval_s=1.0,
    )
    await sensor._ingest_frame(_make_frame(dhash="abcdefabcdefabcd"))
    # Saturate the downshift — repeat dedup + adjust many times.
    for _ in range(20):
        for _ in range(_ADAPTIVE_STATIC_BEFORE_DOWNSHIFT):
            await sensor._ingest_frame(_make_frame(dhash="abcdefabcdefabcd"))
        sensor._adjust_adaptive_interval()
    assert sensor._current_poll_interval_s == _ADAPTIVE_MAX_INTERVAL_S


@pytest.mark.asyncio
async def test_adaptive_resets_on_change():
    sensor = VisionSensor(
        router=_StubRouter(),
        ocr_fn=lambda _p: "Traceback (most recent call last):",
        poll_interval_s=1.0,
    )
    # Force downshift first.
    await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa"))
    for _ in range(_ADAPTIVE_STATIC_BEFORE_DOWNSHIFT):
        await sensor._ingest_frame(_make_frame(dhash="aaaaaaaaaaaaaaaa"))
    sensor._adjust_adaptive_interval()
    assert sensor._current_poll_interval_s > 1.0

    # Now a different hash → counter resets → next adjust goes back to base.
    await sensor._ingest_frame(_make_frame(dhash="bbbbbbbbbbbbbbbb"))
    sensor._adjust_adaptive_interval()
    assert sensor._current_poll_interval_s == 1.0


# ---------------------------------------------------------------------------
# Module-level constants pinned
# ---------------------------------------------------------------------------


def test_defaults_pinned():
    assert _HASH_COOLDOWN_S == 10.0
    assert _ADAPTIVE_MAX_INTERVAL_S == 8.0
    assert _ADAPTIVE_STATIC_BEFORE_DOWNSHIFT == 3
    assert _OCR_SNIPPET_LEN == 256
