from __future__ import annotations

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceKind, SurfaceVerdict, SurfaceHealthLedger,
)
from backend.core.ouroboros.governance.dw_surface_probes import (
    probe_auth_sync,
    run_surface_sweep,
)


async def test_probe_auth_sync_healthy(monkeypatch):
    import backend.core.ouroboros.governance.dw_surface_probes as mod

    async def fake_header():
        return {"Authorization": "Bearer abc"}

    monkeypatch.setattr(mod, "dw_session_auth_header", fake_header)
    verdict, diag = await probe_auth_sync()
    assert verdict is SurfaceVerdict.HEALTHY


async def test_probe_auth_sync_missing_bearer(monkeypatch):
    import backend.core.ouroboros.governance.dw_surface_probes as mod

    async def fake_header():
        return {}

    monkeypatch.setattr(mod, "dw_session_auth_header", fake_header)
    verdict, diag = await probe_auth_sync()
    assert verdict is SurfaceVerdict.AUTH_FAILED


async def test_run_surface_sweep_records_all_three(monkeypatch, tmp_path):
    led = SurfaceHealthLedger(path=tmp_path / "h.json")

    async def fake_batch(provider, model_id):
        return SurfaceVerdict.HEALTHY, "file_id=x", 700

    async def fake_stream(provider, model_id):
        return SurfaceVerdict.UPSTREAM_DEGRADED, "done_before_content", 712

    async def fake_auth():
        return SurfaceVerdict.HEALTHY, ""

    import backend.core.ouroboros.governance.dw_surface_probes as mod
    monkeypatch.setattr(mod, "probe_batch_storage", fake_batch)
    monkeypatch.setattr(mod, "probe_direct_streaming", fake_stream)
    monkeypatch.setattr(mod, "probe_auth_sync", fake_auth)

    snap = await run_surface_sweep(provider=object(), model_id="m", ledger=led)
    assert snap[SurfaceKind.BATCH_STORAGE].verdict is SurfaceVerdict.HEALTHY
    assert snap[SurfaceKind.DIRECT_STREAMING].verdict is SurfaceVerdict.UPSTREAM_DEGRADED
    assert snap[SurfaceKind.AUTH_SYNC].verdict is SurfaceVerdict.HEALTHY
    assert led.verdict_for(SurfaceKind.DIRECT_STREAMING).diagnostic == "done_before_content"


async def test_run_surface_sweep_timeout_sentinel(monkeypatch, tmp_path):
    import asyncio

    led = SurfaceHealthLedger(path=tmp_path / "h.json")

    async def slow_batch(provider, model_id):
        await asyncio.sleep(5)
        return SurfaceVerdict.HEALTHY, "", 0

    async def ok_stream(provider, model_id):
        return SurfaceVerdict.HEALTHY, "", 1

    async def ok_auth():
        return SurfaceVerdict.HEALTHY, ""

    import backend.core.ouroboros.governance.dw_surface_probes as mod
    monkeypatch.setattr(mod, "probe_batch_storage", slow_batch)
    monkeypatch.setattr(mod, "probe_direct_streaming", ok_stream)
    monkeypatch.setattr(mod, "probe_auth_sync", ok_auth)

    # Tiny timeout forces the slow batch probe into the timeout sentinel.
    snap = await run_surface_sweep(
        provider=object(), model_id="m", ledger=led, timeout_s=0.05,
    )
    rec = snap[SurfaceKind.BATCH_STORAGE]
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED
    assert rec.diagnostic == "probe_timeout"


async def test_run_surface_sweep_error_sentinel(monkeypatch, tmp_path):
    led = SurfaceHealthLedger(path=tmp_path / "h.json")

    async def boom_stream(provider, model_id):
        raise RuntimeError("kaboom")

    async def ok_batch(provider, model_id):
        return SurfaceVerdict.HEALTHY, "", 0

    async def ok_auth():
        return SurfaceVerdict.HEALTHY, ""

    import backend.core.ouroboros.governance.dw_surface_probes as mod
    monkeypatch.setattr(mod, "probe_batch_storage", ok_batch)
    monkeypatch.setattr(mod, "probe_direct_streaming", boom_stream)
    monkeypatch.setattr(mod, "probe_auth_sync", ok_auth)

    snap = await run_surface_sweep(provider=object(), model_id="m", ledger=led)
    rec = snap[SurfaceKind.DIRECT_STREAMING]
    assert rec.verdict is SurfaceVerdict.ERROR_OTHER
    assert "RuntimeError" in rec.diagnostic
