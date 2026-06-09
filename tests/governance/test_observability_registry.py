"""Slice 193 — Sovereign Telemetry Registry (structured hedge observability).

The Slice 192 verdict was readable only by cross-referencing the aegis proxy's
HTTP log: the hedge's win/loss/swallow telemetry is logger.info (invisible at
the soak's WARNING threshold) and the Slice 190 counters are in-memory only.
This slice gives the organism a structured, durable, unsuppressed metrics
surface — counters, not log lines (Manifesto §7).

Pins:
  * ObservabilityRegistry — thread-safe, mmap-backed atomic counters that
    survive process restart (.jarvis/observability_registry.bin).
  * 10 concurrent hedge victories update the registry EXACTLY (no lost incr).
  * Corrupt/unwritable backing file → fail-soft in-memory fallback, NEVER
    raises into the dispatch throat.
  * record_hedge_dispatch / record_hedge_outcome module helpers are wired
    into the doubleword_provider hedge block (source-pinned).
  * GET /observability/registry — 403 master-off, structured payload
    master-on, Cache-Control: no-store. Mounted in EventChannelServer
    (source-pinned).
  * Authority invariant: the module never imports orchestrator / policy /
    iron_gate / change_engine / candidate_generator (grep-pinned).
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.observability_registry import (
    HEDGE_BATCH_VICTORIES,
    HEDGE_CONCURRENCY_DISPATCHES,
    HEDGE_RT_VICTORIES,
    HEDGE_RUPTURES_SWALLOWED,
    REGISTRY_SCHEMA_VERSION,
    ObservabilityRegistry,
    _reset_singleton_for_tests,
    get_observability_registry,
    observability_registry_enabled,
    record_hedge_dispatch,
    record_hedge_outcome,
    register_registry_routes,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    """Every test gets an isolated backing file + a fresh singleton."""
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "reg.bin"),
    )
    monkeypatch.delenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", raising=False)
    _reset_singleton_for_tests()
    yield
    _reset_singleton_for_tests()


# ===========================================================================
# A — master gate
# ===========================================================================

def test_enabled_default_true():
    """Authority-free observability defaults ON (economic_telemetry
    precedent, Slice 171)."""
    assert observability_registry_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE"])
def test_enabled_falsy_values_disable(monkeypatch, val):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", val)
    assert observability_registry_enabled() is False


# ===========================================================================
# B — counter core
# ===========================================================================

def test_hedge_counters_preregistered_at_zero():
    """The four hedge counters exist from birth so the soak payload always
    shows them — a 0 is a verdict, a missing key is a question."""
    snap = get_observability_registry().snapshot()
    for name in (
        HEDGE_CONCURRENCY_DISPATCHES,
        HEDGE_RT_VICTORIES,
        HEDGE_BATCH_VICTORIES,
        HEDGE_RUPTURES_SWALLOWED,
    ):
        assert snap[name] == 0


def test_incr_and_get():
    reg = get_observability_registry()
    reg.incr(HEDGE_RT_VICTORIES)
    reg.incr(HEDGE_RT_VICTORIES, 4)
    assert reg.get(HEDGE_RT_VICTORIES) == 5


def test_incr_auto_registers_new_counter():
    reg = get_observability_registry()
    reg.incr("custom_sensor_emissions", 7)
    assert reg.snapshot()["custom_sensor_emissions"] == 7


def test_disabled_incr_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", "false")
    _reset_singleton_for_tests()
    reg = get_observability_registry()
    reg.incr(HEDGE_RT_VICTORIES)
    assert reg.snapshot() == {}


# ===========================================================================
# C — atomicity under concurrency (Phase 4 acceptance criterion)
# ===========================================================================

def test_ten_concurrent_hedge_victories_exact():
    """10 threads each record one hedge victory concurrently → registry
    state is EXACTLY 10. The user-pinned Slice 193 acceptance test."""
    barrier = threading.Barrier(10)

    def _win(i):
        barrier.wait()
        record_hedge_outcome("rt" if i % 2 == 0 else "batch",
                             rupture_swallowed=(i % 3 == 0))

    threads = [threading.Thread(target=_win, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = get_observability_registry().snapshot()
    assert snap[HEDGE_RT_VICTORIES] == 5
    assert snap[HEDGE_BATCH_VICTORIES] == 5
    assert snap[HEDGE_RT_VICTORIES] + snap[HEDGE_BATCH_VICTORIES] == 10
    assert snap[HEDGE_RUPTURES_SWALLOWED] == 4  # i in {0, 3, 6, 9}


def test_hammer_no_lost_increments():
    """8 threads × 1000 increments → exactly 8000 (no torn mmap writes)."""
    reg = get_observability_registry()

    def _hammer():
        for _ in range(1000):
            reg.incr(HEDGE_CONCURRENCY_DISPATCHES)

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert reg.get(HEDGE_CONCURRENCY_DISPATCHES) == 8000


# ===========================================================================
# D — mmap durability + fail-soft
# ===========================================================================

def test_counters_survive_reopen(tmp_path):
    path = tmp_path / "durable.bin"
    reg = ObservabilityRegistry(path=path)
    reg.incr(HEDGE_BATCH_VICTORIES, 42)
    reg.incr("custom_counter", 3)
    reg.close()

    reborn = ObservabilityRegistry(path=path)
    assert reborn.get(HEDGE_BATCH_VICTORIES) == 42
    assert reborn.get("custom_counter") == 3
    assert reborn.backend_kind == "mmap"
    reborn.close()


def test_corrupt_backing_file_fails_soft(tmp_path):
    """Garbage backing file → in-memory fallback; counting still works and
    nothing raises into the dispatch throat."""
    path = tmp_path / "corrupt.bin"
    path.write_bytes(b"\x00garbage" * 7)
    reg = ObservabilityRegistry(path=path)
    reg.incr(HEDGE_RT_VICTORIES)
    assert reg.get(HEDGE_RT_VICTORIES) == 1
    assert reg.backend_kind == "memory"
    reg.close()


def test_unwritable_path_fails_soft():
    reg = ObservabilityRegistry(path=Path("/nonexistent-dir-x9/reg.bin"))
    reg.incr(HEDGE_RT_VICTORIES, 2)
    assert reg.get(HEDGE_RT_VICTORIES) == 2
    assert reg.backend_kind == "memory"
    reg.close()


def test_singleton_uses_env_path(tmp_path, monkeypatch):
    p = tmp_path / "envpath.bin"
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_PATH", str(p))
    _reset_singleton_for_tests()
    get_observability_registry().incr(HEDGE_RT_VICTORIES)
    assert p.exists()


# ===========================================================================
# E — hedge recording helpers (the dispatch-throat API)
# ===========================================================================

def test_record_hedge_dispatch_increments():
    record_hedge_dispatch()
    record_hedge_dispatch()
    assert get_observability_registry().get(HEDGE_CONCURRENCY_DISPATCHES) == 2


def test_record_hedge_outcome_rt_no_rupture():
    record_hedge_outcome("rt", rupture_swallowed=False)
    snap = get_observability_registry().snapshot()
    assert snap[HEDGE_RT_VICTORIES] == 1
    assert snap[HEDGE_BATCH_VICTORIES] == 0
    assert snap[HEDGE_RUPTURES_SWALLOWED] == 0


def test_record_hedge_outcome_batch_with_swallow():
    record_hedge_outcome("batch", rupture_swallowed=True)
    snap = get_observability_registry().snapshot()
    assert snap[HEDGE_BATCH_VICTORIES] == 1
    assert snap[HEDGE_RUPTURES_SWALLOWED] == 1


def test_helpers_never_raise_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", "0")
    _reset_singleton_for_tests()
    record_hedge_dispatch()
    record_hedge_outcome("rt", rupture_swallowed=True)


# ===========================================================================
# F — dispatch-throat wiring (source-pinned, repo precedent)
# ===========================================================================

def test_provider_hedge_block_wired_to_registry():
    src = (_GOV / "doubleword_provider.py").read_text(encoding="utf-8")
    assert "record_hedge_dispatch" in src, (
        "the hedge race site must count hedge_concurrency_dispatches"
    )
    assert src.count("record_hedge_outcome") >= 2, (
        "the win lambda must feed BOTH the economic ledger (190) and the "
        "registry (193)"
    )


def test_event_channel_mounts_registry_routes():
    src = (_GOV / "event_channel.py").read_text(encoding="utf-8")
    assert "register_registry_routes" in src


def test_authority_invariant_no_wide_imports():
    src = (_GOV / "observability_registry.py").read_text(encoding="utf-8")
    for forbidden in (
        "import orchestrator", "from backend.core.ouroboros.governance.orchestrator",
        "iron_gate", "change_engine", "candidate_generator",
        "semantic_guardian", "risk_tier",
    ):
        assert forbidden not in src, f"authority leak: {forbidden}"


# ===========================================================================
# G — GET /observability/registry
# ===========================================================================

async def _get(app, path):
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    async with aiohttp_test.TestServer(app) as server:
        async with aiohttp_test.TestClient(server) as client:
            resp = await client.get(path)
            return resp.status, await resp.json(), dict(resp.headers)


def _make_app():
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_registry_routes(
        app,
        rate_limit_check=lambda req: True,
        cors_headers=lambda req: {"Access-Control-Allow-Origin": "x"},
    )
    return app


def test_endpoint_disabled_returns_403(monkeypatch):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", "false")
    _reset_singleton_for_tests()
    app = _make_app()

    async def _run():
        status, body, _ = await _get(app, "/observability/registry")
        assert status == 403
        assert body["error"] is True
        assert body["reason_code"] == "ide_observability.disabled"
    asyncio.run(_run())


def test_endpoint_returns_structured_counters():
    record_hedge_dispatch()
    record_hedge_outcome("rt", rupture_swallowed=False)
    app = _make_app()

    async def _run():
        status, body, headers = await _get(app, "/observability/registry")
        assert status == 200
        assert body["schema_version"] == REGISTRY_SCHEMA_VERSION
        assert body["backend"] in ("mmap", "memory")
        c = body["counters"]
        assert c[HEDGE_CONCURRENCY_DISPATCHES] == 1
        assert c[HEDGE_RT_VICTORIES] == 1
        assert c[HEDGE_BATCH_VICTORIES] == 0
        assert c[HEDGE_RUPTURES_SWALLOWED] == 0
        assert headers.get("Cache-Control") == "no-store"
    asyncio.run(_run())


def test_endpoint_rate_limited_returns_429():
    app = pytest.importorskip("aiohttp.web").Application()
    register_registry_routes(app, rate_limit_check=lambda req: False)

    async def _run():
        status, body, _ = await _get(app, "/observability/registry")
        assert status == 429
    asyncio.run(_run())
