"""Multi-dimensional DW surface health (2026-07-17).

bt-2026-07-17-080507 bypassed the DW-RT tier on consecutive_failures=254 from
DIRECT_STREAMING — the SSE surface that complete_sync was PURPOSE-BUILT to
avoid (it stalls post-accept; bt-2026-04-14-182446). Cross-contaminated
telemetry neutralizes the opportunistic tier. These pin the isolation, the two
failure DIMENSIONS (transport vs inference), and the recovery door.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger, SurfaceKind, SurfaceVerdict,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    lg = SurfaceHealthLedger(path=tmp_path / "surf.json", autosave=False)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_surface_health.SurfaceHealthLedger",
        lambda *a, **k: lg,
    )
    return lg


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---- surface isolation (the core fix) -------------------------------------


def test_surfaces_are_independent_health_domains(ledger):
    """A broken SSE surface must NOT contaminate the completions verdict."""
    for _ in range(254):
        ledger.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
    ledger.record(SurfaceKind.DIRECT_COMPLETION, SurfaceVerdict.HEALTHY)

    sse = ledger.verdict_for(SurfaceKind.DIRECT_STREAMING)
    comp = ledger.verdict_for(SurfaceKind.DIRECT_COMPLETION)
    assert sse.consecutive_failures == 254            # SSE chronically broken
    assert comp.verdict is SurfaceVerdict.HEALTHY     # completions unaffected
    assert comp.consecutive_failures == 0


def test_direct_completion_surface_is_registered():
    assert SurfaceKind.DIRECT_COMPLETION.value == "direct_completion"
    assert SurfaceVerdict.INFERENCE_DEGRADED.value == "inference_degraded"


def test_bypass_reads_completion_not_streaming(ledger, monkeypatch, tmp_path):
    """THE decoupling: 254 SSE failures must not bypass the DW-RT tier."""
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    for _ in range(254):
        ledger.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
    ledger.record(SurfaceKind.DIRECT_COMPLETION, SurfaceVerdict.HEALTHY)
    assert DreamEngine._dw_health_bypass() is False   # SSE no longer contaminates


def test_bypass_fires_on_completion_degradation(ledger):
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    for _ in range(2):
        ledger.record(SurfaceKind.DIRECT_COMPLETION, SurfaceVerdict.UPSTREAM_DEGRADED)
    assert DreamEngine._dw_health_bypass() is True


def test_no_verdict_is_not_a_verdict(ledger):
    """Never probed → attempt DW (don't strand the tier on silence)."""
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    assert DreamEngine._dw_health_bypass() is False


def test_single_failure_below_streak_does_not_bypass(ledger):
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    ledger.record(SurfaceKind.DIRECT_COMPLETION, SurfaceVerdict.UPSTREAM_DEGRADED)
    assert DreamEngine._dw_health_bypass() is False   # streak=1 < 2


def test_healthy_record_resets_and_lifts_bypass(ledger):
    """Recovery: a HEALTHY record zeroes the streak → tier re-opens."""
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    for _ in range(5):
        ledger.record(SurfaceKind.DIRECT_COMPLETION, SurfaceVerdict.UPSTREAM_DEGRADED)
    assert DreamEngine._dw_health_bypass() is True
    ledger.record(SurfaceKind.DIRECT_COMPLETION, SurfaceVerdict.HEALTHY)
    assert DreamEngine._dw_health_bypass() is False   # cheap tokens resume


# ---- the two failure DIMENSIONS -------------------------------------------


def _provider():
    from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider
    return DoublewordProvider


def test_empty_200_is_inference_not_healthy(ledger):
    """A 200 with 0 chars is a LIE the transport can't see."""
    _provider()._record_completion_surface(
        healthy=False, latency_s=30.25, model="Qwen/Qwen3.5-397B-A17B-FP8",
        output_tokens=2048,
    )
    rec = ledger.verdict_for(SurfaceKind.DIRECT_COMPLETION)
    assert rec.verdict is SurfaceVerdict.INFERENCE_DEGRADED   # not HEALTHY
    assert "empty_generation" in rec.diagnostic


def test_content_records_healthy(ledger):
    _provider()._record_completion_surface(
        healthy=True, latency_s=1.2, model="m", output_tokens=12,
    )
    assert ledger.verdict_for(SurfaceKind.DIRECT_COMPLETION).verdict is SurfaceVerdict.HEALTHY


def test_transport_verdicts_are_distinct(ledger):
    """502 (upstream) vs 403 (auth) must be different facts."""
    _provider()._record_completion_surface(
        healthy=False, latency_s=0.0, model="m",
        verdict=SurfaceVerdict.UPSTREAM_DEGRADED, diagnostic="http_502:unreachable")
    assert ledger.verdict_for(SurfaceKind.DIRECT_COMPLETION).verdict is SurfaceVerdict.UPSTREAM_DEGRADED
    _provider()._record_completion_surface(
        healthy=False, latency_s=0.0, model="m",
        verdict=SurfaceVerdict.AUTH_FAILED, diagnostic="http_403")
    assert ledger.verdict_for(SurfaceKind.DIRECT_COMPLETION).verdict is SurfaceVerdict.AUTH_FAILED


def test_health_record_never_raises(ledger, monkeypatch):
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_surface_health.SurfaceHealthLedger",
        MagicMock(side_effect=RuntimeError("ledger down")))
    _provider()._record_completion_surface(healthy=True, latency_s=1.0, model="m")


# ---- Synthetic Tracer -----------------------------------------------------


class _Res:
    def __init__(self, content):
        self.content = content
        self.model = "m"
        self.latency_s = 0.4


def test_tracer_demands_generated_content_not_just_200(ledger):
    """Mandate 2: an empty 200 must NOT score healthy."""
    from backend.core.ouroboros.governance.dw_capacity_probe import trace_direct_completion
    p = MagicMock()
    p.complete_sync = AsyncMock(return_value=_Res(""))     # perfect transport, no inference
    assert _run(trace_direct_completion(p)) == "inference_degraded"


def test_tracer_healthy_on_real_generation(ledger):
    from backend.core.ouroboros.governance.dw_capacity_probe import trace_direct_completion
    p = MagicMock()
    p.complete_sync = AsyncMock(return_value=_Res("ok"))
    assert _run(trace_direct_completion(p)) == "healthy"


def test_tracer_handles_502_transport_failure(ledger):
    from backend.core.ouroboros.governance.dw_capacity_probe import trace_direct_completion
    p = MagicMock()
    p.complete_sync = AsyncMock(side_effect=RuntimeError("HTTP 502 upstream_unreachable"))
    assert _run(trace_direct_completion(p)) == "transport_degraded"


def test_tracer_records_timeout_to_ledger(ledger):
    from backend.core.ouroboros.governance.dw_capacity_probe import trace_direct_completion
    p = MagicMock()
    p.complete_sync = AsyncMock(side_effect=asyncio.TimeoutError())
    assert _run(trace_direct_completion(p)) == "transport_degraded"
    rec = ledger.verdict_for(SurfaceKind.DIRECT_COMPLETION)
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED and rec.diagnostic == "tracer_timeout"


def test_tracer_uses_a_reasoning_safe_budget():
    """A tiny budget would reproduce the exhaustion the probe exists to detect
    (the 397B effort FLOOR means it always thinks) → false INFERENCE_DEGRADED."""
    from backend.core.ouroboros.governance.dw_capacity_probe import _tracer_max_tokens
    assert _tracer_max_tokens() >= 64


def test_tracer_skips_without_provider(ledger):
    from backend.core.ouroboros.governance.dw_capacity_probe import trace_direct_completion
    assert _run(trace_direct_completion(None)) == "skipped"


def test_tracer_disabled_is_inert(ledger, monkeypatch):
    from backend.core.ouroboros.governance.dw_capacity_probe import trace_direct_completion
    monkeypatch.setenv("JARVIS_DW_COMPLETION_TRACER_ENABLED", "false")
    p = MagicMock(); p.complete_sync = AsyncMock(return_value=_Res("ok"))
    assert _run(trace_direct_completion(p)) == "skipped"
    p.complete_sync.assert_not_called()
