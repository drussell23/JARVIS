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


# ---------------------------------------------------------------------------
# Slice 39 Task 8 — run_surface_health_sweep orchestration + AST pin
# ---------------------------------------------------------------------------


def test_surface_health_enabled_default_true_after_slice40(monkeypatch):
    # Slice 40 graduated the master flag default to TRUE.
    monkeypatch.delenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        is_surface_health_enabled,
    )
    assert is_surface_health_enabled() is True


async def test_surface_health_sweep_disabled_when_flag_false(monkeypatch):
    # Post-graduation, disabling requires an explicit =false opt-out.
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "false")
    from backend.core.ouroboros.governance.preflight_probe import (
        run_surface_health_sweep,
    )
    out = await run_surface_health_sweep(provider=object(), model_id="m")
    assert out is None


async def test_surface_health_sweep_runs_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "h.json"))
    import backend.core.ouroboros.governance.dw_surface_probes as probes

    async def fake_sweep(*, provider, model_id, ledger, timeout_s=None):
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceKind, SurfaceVerdict,
        )
        ledger.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
        return ledger.snapshot()

    monkeypatch.setattr(probes, "run_surface_sweep", fake_sweep)
    from backend.core.ouroboros.governance.preflight_probe import (
        run_surface_health_sweep,
    )
    out = await run_surface_health_sweep(provider=object(), model_id="m")
    assert out is not None


def test_ast_pin_flush_bypass_on_upstream():
    """AST pin: disambiguate_and_recover must NOT call flush in the
    UPSTREAM branch. Guards the load-bearing Slice 39 invariant."""
    import ast
    import pathlib
    src = pathlib.Path(
        "backend/core/ouroboros/governance/dw_transport_disambiguator.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "disambiguate_and_recover"
    )
    upstream_blocks = [
        node for node in ast.walk(func)
        if isinstance(node, ast.If) and "UPSTREAM" in ast.dump(node.test)
    ]
    assert upstream_blocks, "UPSTREAM branch not found"
    for blk in upstream_blocks:
        for sub in ast.walk(blk):
            if isinstance(sub, ast.Attribute):
                assert sub.attr != "flush_transport_pool", (
                    "flush_transport_pool must NEVER be called in the "
                    "UPSTREAM branch (Slice 39 invariant — PRD §49.6.2)"
                )


# ---------------------------------------------------------------------------
# Slice 40 — boot wiring: run_boot_surface_health_sweep + model resolution
# ---------------------------------------------------------------------------


class _AvailProv:
    is_available = True


async def test_boot_sweep_skipped_when_flag_false(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "false")
    from backend.core.ouroboros.governance.preflight_probe import (
        run_boot_surface_health_sweep,
    )
    assert await run_boot_surface_health_sweep(dw_provider=_AvailProv()) is None


async def test_boot_sweep_skipped_when_provider_unavailable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")

    class _Unavail:
        is_available = False

    from backend.core.ouroboros.governance.preflight_probe import (
        run_boot_surface_health_sweep,
    )
    assert await run_boot_surface_health_sweep(dw_provider=_Unavail()) is None
    assert await run_boot_surface_health_sweep(dw_provider=None) is None


async def test_boot_sweep_skipped_when_no_model(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    monkeypatch.delenv("JARVIS_DW_SURFACE_PROBE_MODEL", raising=False)
    import backend.core.ouroboros.governance.preflight_probe as pp
    monkeypatch.setattr(pp, "_resolve_surface_probe_model", lambda: None)
    assert await pp.run_boot_surface_health_sweep(dw_provider=_AvailProv()) is None


async def test_boot_sweep_degraded_streaming_trips_breaker(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_SURFACE_PROBE_MODEL", "vendor/m")
    import backend.core.ouroboros.governance.preflight_probe as pp
    from backend.core.ouroboros.governance.dw_surface_health import (
        SurfaceKind, SurfaceVerdict, SurfaceHealthRecord,
    )

    async def fake_sweep(*, provider, model_id):
        return {SurfaceKind.DIRECT_STREAMING: SurfaceHealthRecord(
            surface=SurfaceKind.DIRECT_STREAMING,
            verdict=SurfaceVerdict.UPSTREAM_DEGRADED,
            diagnostic="done_before_content")}

    flips = []
    monkeypatch.setattr(pp, "run_surface_health_sweep", fake_sweep)
    import backend.core.ouroboros.governance.dw_transport_disambiguator as dis
    monkeypatch.setattr(
        dis, "_flip_topology_breaker",
        lambda mid, diag: flips.append((mid, diag)),
    )
    snap = await pp.run_boot_surface_health_sweep(dw_provider=_AvailProv())
    assert snap is not None
    assert flips == [("vendor/m", "done_before_content")]


async def test_boot_sweep_healthy_streaming_no_breaker(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_SURFACE_PROBE_MODEL", "vendor/m")
    import backend.core.ouroboros.governance.preflight_probe as pp
    from backend.core.ouroboros.governance.dw_surface_health import (
        SurfaceKind, SurfaceVerdict, SurfaceHealthRecord,
    )

    async def fake_sweep(*, provider, model_id):
        return {SurfaceKind.DIRECT_STREAMING: SurfaceHealthRecord(
            surface=SurfaceKind.DIRECT_STREAMING,
            verdict=SurfaceVerdict.HEALTHY)}

    flips = []
    monkeypatch.setattr(pp, "run_surface_health_sweep", fake_sweep)
    import backend.core.ouroboros.governance.dw_transport_disambiguator as dis
    monkeypatch.setattr(
        dis, "_flip_topology_breaker",
        lambda mid, diag: flips.append((mid, diag)),
    )
    await pp.run_boot_surface_health_sweep(dw_provider=_AvailProv())
    assert flips == []


def test_resolve_surface_probe_model_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_PROBE_MODEL", "vendor/explicit")
    from backend.core.ouroboros.governance.preflight_probe import (
        _resolve_surface_probe_model,
    )
    assert _resolve_surface_probe_model() == "vendor/explicit"


def test_gls_wires_boot_surface_sweep_before_pool_start():
    """AST/source pin: GLS boot calls run_boot_surface_health_sweep, and
    the call appears BEFORE bg_pool.start() so the first op sees the
    populated ledger."""
    import pathlib
    src = pathlib.Path(
        "backend/core/ouroboros/governance/governed_loop_service.py"
    ).read_text(encoding="utf-8")
    assert "run_boot_surface_health_sweep" in src, "Slice 40 sweep not wired into GLS"
    sweep_at = src.index("run_boot_surface_health_sweep(")
    pool_at = src.index("self._bg_pool.start()")
    assert sweep_at < pool_at, "sweep must be awaited before the worker fleet starts"
