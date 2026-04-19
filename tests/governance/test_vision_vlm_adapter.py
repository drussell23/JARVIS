"""Regression spine for the VLM adapter module (wiring handoff #3).

Scope:

* ``make_sensor_vlm_fn()`` — stub path returns the correct shape to
  trigger Tier 2 dispatch + drop; doubleword path falls back to stub
  gracefully when provider wiring isn't complete.
* ``make_advisory_fn()`` — stub path returns ``aligned`` with zero
  confidence (never triggers L2); doubleword path falls back to stub
  gracefully.
* Env mode parsing — unknown modes fall back to stub with a DEBUG log.
* Integration: wiring into ``VisionSensor`` produces the expected
  Tier 2 skip/drop behavior end-to-end.
* ``IntakeLayerService`` production-source check — the Task 13
  wiring block calls ``make_sensor_vlm_fn`` so Tier 2 stops being
  None after this handoff.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
    FrameData,
    VisionSensor,
)
from backend.core.ouroboros.governance.vision_vlm_adapter import (
    _MODE_ENV,
    _resolve_mode,
    _stub_advisory_fn,
    _stub_sensor_vlm_fn,
    make_advisory_fn,
    make_sensor_vlm_fn,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_MODE_ENV, raising=False)
    yield


class _StubRouter:
    async def ingest(self, envelope):
        return "enqueued"


# ---------------------------------------------------------------------------
# Mode parsing
# ---------------------------------------------------------------------------


def test_default_mode_is_stub():
    assert _resolve_mode() == "stub"


def test_explicit_stub_mode(monkeypatch):
    monkeypatch.setenv(_MODE_ENV, "stub")
    assert _resolve_mode() == "stub"


def test_explicit_doubleword_mode(monkeypatch):
    monkeypatch.setenv(_MODE_ENV, "doubleword")
    assert _resolve_mode() == "doubleword"


def test_unknown_mode_falls_back_to_stub(monkeypatch):
    monkeypatch.setenv(_MODE_ENV, "fabricated")
    assert _resolve_mode() == "stub"


def test_mode_case_insensitive(monkeypatch):
    monkeypatch.setenv(_MODE_ENV, "DOUBLEWORD")
    assert _resolve_mode() == "doubleword"
    monkeypatch.setenv(_MODE_ENV, "  Stub  ")
    assert _resolve_mode() == "stub"


# ---------------------------------------------------------------------------
# Stub sensor VLM fn
# ---------------------------------------------------------------------------


def test_stub_sensor_fn_shape():
    result = _stub_sensor_vlm_fn("/tmp/claude/latest_frame.jpg")
    # Stub returns ``ok`` so no stub-originated signal enters the queue.
    assert result["verdict"] == "ok"
    assert result["confidence"] == 0.0
    assert "stub" in result["model"]
    assert isinstance(result["reasoning"], str)


def test_make_sensor_vlm_fn_returns_stub_by_default():
    fn = make_sensor_vlm_fn()
    result = fn("/tmp/x.jpg")
    assert result["verdict"] == "ok"


def test_make_sensor_vlm_fn_doubleword_falls_back_gracefully(monkeypatch):
    """If the doubleword path is selected but the actual provider
    call isn't implemented yet, the wrapped fn MUST NOT raise — it
    returns the stub payload so the sensor keeps working."""
    monkeypatch.setenv(_MODE_ENV, "doubleword")
    fn = make_sensor_vlm_fn()
    result = fn("/tmp/x.jpg")
    # Real impl not present yet → stub payload.
    assert result["verdict"] == "ok"
    assert "stub" in result["model"]


# ---------------------------------------------------------------------------
# Stub advisory fn
# ---------------------------------------------------------------------------


def test_stub_advisory_fn_shape():
    result = _stub_advisory_fn(b"pre", b"post", "test op")
    assert result["verdict"] == "aligned"
    assert result["confidence"] == 0.0
    assert "stub" in result["model"]


def test_make_advisory_fn_default_stub():
    fn = make_advisory_fn()
    result = fn(b"pre", b"post", "intent")
    assert result["verdict"] == "aligned"


def test_make_advisory_fn_doubleword_falls_back_gracefully(monkeypatch):
    monkeypatch.setenv(_MODE_ENV, "doubleword")
    fn = make_advisory_fn()
    result = fn(b"pre", b"post", "intent")
    # Real impl not implemented → stub.
    assert result["verdict"] == "aligned"


# ---------------------------------------------------------------------------
# Integration: VisionSensor with stub VLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_with_stub_vlm_dispatches_but_drops(tmp_path):
    """The stub returns ``unclear`` — Tier 2 dispatch fires (counter
    increments + cost ledger debited) but no signal emits because
    unclear drops without routing."""
    sensor = VisionSensor(
        router=_StubRouter(),
        session_id="vlm-integration",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        register_shutdown_hooks=False,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        ocr_fn=lambda _p: "",       # Tier 1 quiet
        vlm_fn=make_sensor_vlm_fn(),
        tier2_enabled=True,
        tier2_cost_usd=0.005,
        daily_cost_cap_usd=1.00,
        finding_cooldown_s=0.0,
    )
    env = await sensor._ingest_frame(FrameData(
        frame_path="/tmp/claude/latest_frame.jpg",
        dhash="0123456789abcdef",
        ts=1.0,
        app_id=None,
        window_id=None,
    ))
    # No signal emitted — stub returned unclear.
    assert env is None
    # But Tier 2 WAS called — dispatch wiring exercised end-to-end.
    assert sensor.stats.tier2_calls == 1
    # Cost debited per Task 15 "pay for the attempt" semantics.
    assert sensor._cost_today_usd > 0


@pytest.mark.asyncio
async def test_sensor_with_stub_vlm_does_not_trip_budget_pause(tmp_path):
    """Many stub Tier 2 calls accumulate cost but the sensor stays
    armed until the cascade threshold — confirms the adapter doesn't
    accidentally pause the sensor by overcharging."""
    sensor = VisionSensor(
        router=_StubRouter(),
        session_id="vlm-integration",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        register_shutdown_hooks=False,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        ocr_fn=lambda _p: "",
        vlm_fn=make_sensor_vlm_fn(),
        tier2_enabled=True,
        tier2_cost_usd=0.005,
        daily_cost_cap_usd=1.00,     # cap = 200 calls
        finding_cooldown_s=0.0,
    )
    # 5 distinct frames → 5 Tier 2 calls → $0.025, well below any cascade.
    for i in range(5):
        await sensor._ingest_frame(FrameData(
            frame_path=f"/tmp/claude/frame_{i}.jpg",
            dhash=f"{i:016x}",
            ts=float(i),
            app_id=None,
            window_id=None,
        ))
    assert sensor.stats.tier2_calls == 5
    assert sensor.paused is False


# ---------------------------------------------------------------------------
# Production source check — IntakeLayerService wires the adapter
# ---------------------------------------------------------------------------


def test_intake_layer_wires_vlm_adapter_into_sensor():
    """Structural guard: the VisionSensor construction block passes
    ``vlm_fn=_vlm_fn`` built from ``make_sensor_vlm_fn``. A regression
    where someone omits the adapter silently reverts Tier 2 to None
    (no-op), which would pass unit tests but gut the sensor at boot.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src = (
        repo_root
        / "backend/core/ouroboros/governance/intake/intake_layer_service.py"
    ).read_text(encoding="utf-8")
    # Adapter import + call is present near the VisionSensor wiring.
    assert (
        "from backend.core.ouroboros.governance.vision_vlm_adapter import"
        in src
    )
    assert "make_sensor_vlm_fn()" in src
    # The sensor constructor receives vlm_fn (not bare default None).
    block_start = src.find("_vision_sensor = VisionSensor(")
    block_end = src.find(")", block_start)
    block = src[block_start:block_end + 1]
    assert "vlm_fn=_vlm_fn" in block
