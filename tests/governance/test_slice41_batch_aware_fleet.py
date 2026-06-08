"""Slice 41 — batch-aware fleet eligibility + ledger-driven routing.

A model whose STREAMING probe returns ``done_before_content`` (transport-
specific empty generation) must stay ELIGIBLE — verdict ACTIVE_BATCH_ONLY,
counted as active, FORCE_BATCH marker, NOT reported as a sentinel failure —
*when the surface ledger shows batch_storage healthy* (batch generation was
empirically confirmed working while streaming is degraded). This prevents the
streaming-based preflight from emptying the fleet and halting dispatch.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.preflight_probe import (
    ProbeOutcome,
    PreflightVerdict,
    _is_streaming_only_degradation,
    _batch_surface_healthy,
    run_preflight,
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)


def _done_before_content(model_id="vendor/m"):
    # Mirrors the real adapter shape for a streaming empty-completion.
    return ProbeOutcome(
        model_id=model_id, success=False, status_code=0,
        error_message="done_before_content",
    )


# ---------------------------------------------------------------------------
# _is_streaming_only_degradation (pure)
# ---------------------------------------------------------------------------


def test_done_before_content_is_streaming_only():
    assert _is_streaming_only_degradation(_done_before_content()) is True


def test_timeout_is_not_streaming_only():
    assert _is_streaming_only_degradation(
        ProbeOutcome(model_id="m", success=False, timeout=True,
                     error_message="ttft_timeout")
    ) is False


def test_5xx_is_not_streaming_only():
    assert _is_streaming_only_degradation(
        ProbeOutcome(model_id="m", success=False, status_code=503,
                     error_message="status_503", error_body="Service Unavailable")
    ) is False


def test_success_is_not_streaming_only():
    assert _is_streaming_only_degradation(
        ProbeOutcome(model_id="m", success=True, status_code=200)
    ) is False


# ---------------------------------------------------------------------------
# _batch_surface_healthy (ledger read; gated; never raises)
# ---------------------------------------------------------------------------


def test_batch_surface_healthy_true(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    SurfaceHealthLedger(path=p).record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.HEALTHY,
    )
    assert _batch_surface_healthy() is True


def test_batch_surface_unhealthy_false(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    SurfaceHealthLedger(path=p).record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.UPSTREAM_DEGRADED,
    )
    assert _batch_surface_healthy() is False


def test_batch_surface_missing_ledger_false(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "none.json"))
    assert _batch_surface_healthy() is False


# ---------------------------------------------------------------------------
# run_preflight batch-aware upgrade
# ---------------------------------------------------------------------------


class _SentinelSpy:
    def __init__(self):
        self.failures = []

    def report_failure(self, model_id, source, detail="", *,
                       status_code=None, response_body="", is_terminal=False):
        self.failures.append(model_id)


async def _probe_done(model_id):
    return _done_before_content(model_id)


async def test_done_before_content_upgrades_to_active_batch_only(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    SurfaceHealthLedger(path=p).record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.HEALTHY,
    )
    spy = _SentinelSpy()
    report = await run_preflight(
        model_ids=("vendor/m",), probe_fn=_probe_done,
        sentinel=spy, halt_on_all_fail=False,
    )
    res = report.results[0]
    assert res.verdict is PreflightVerdict.ACTIVE_BATCH_ONLY
    assert res.entitlement_marker == "FORCE_BATCH"
    # Counts as active → fleet NOT empty → no halt.
    assert report.active_count == 1
    assert report.active_batch_only_count == 1
    assert report.all_failed is False
    # NOT reported as a failure — the model is usable via batch.
    assert spy.failures == []


async def test_done_before_content_stays_degraded_when_batch_unhealthy(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    SurfaceHealthLedger(path=p).record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.UPSTREAM_DEGRADED,
    )
    spy = _SentinelSpy()
    report = await run_preflight(
        model_ids=("vendor/m",), probe_fn=_probe_done,
        sentinel=spy, halt_on_all_fail=False,
    )
    assert report.results[0].verdict is PreflightVerdict.DEGRADED_5XX
    assert report.active_count == 0
    assert report.all_failed is True
    assert spy.failures == ["vendor/m"]


async def test_done_before_content_stays_degraded_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "false")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    SurfaceHealthLedger(path=p).record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.HEALTHY,
    )
    report = await run_preflight(
        model_ids=("vendor/m",), probe_fn=_probe_done,
        sentinel=_SentinelSpy(), halt_on_all_fail=False,
    )
    assert report.results[0].verdict is PreflightVerdict.DEGRADED_5XX
    assert report.all_failed is True


# ---------------------------------------------------------------------------
# Phase 2 — ledger-driven batch routing (_slice36_should_force_batch)
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.doubleword_provider import (  # noqa: E402
    _slice36_should_force_batch,
    _slice41_ledger_force_batch,
)


class _Ctx:
    def __init__(self, route):
        self.provider_route = route


def _seed_ledger(monkeypatch, tmp_path, *, stream, batch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    led = SurfaceHealthLedger(path=p)
    led.record(SurfaceKind.DIRECT_STREAMING, stream)
    led.record(SurfaceKind.BATCH_STORAGE, batch)


def test_ledger_force_batch_true_on_degraded_stream_healthy_batch(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert _slice41_ledger_force_batch() is True


def test_ledger_force_batch_false_when_batch_unhealthy(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.UPSTREAM_DEGRADED)
    assert _slice41_ledger_force_batch() is False


def test_ledger_force_batch_false_when_stream_healthy(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.HEALTHY)
    assert _slice41_ledger_force_batch() is False


def test_ledger_force_batch_false_when_flag_off(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "false")
    assert _slice41_ledger_force_batch() is False


def test_force_batch_ledger_overrides_static_optout(monkeypatch, tmp_path):
    # Static Slice 36 opt-out OFF, but the ledger shows streaming degraded +
    # batch healthy → Slice 41 still forces batch (don't fail closed).
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.setenv("JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX", "0")
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert _slice36_should_force_batch(_Ctx("standard")) is True
    assert _slice36_should_force_batch(_Ctx("complex")) is True
    # Non-standard/complex route never forces batch.
    assert _slice36_should_force_batch(_Ctx("immediate")) is False


def test_degraded_stream_forces_batch_even_with_claude_available(monkeypatch, tmp_path):
    # Slice 170 — intra-DW transport failover. A degraded streaming wire + healthy batch
    # lane now forces DW-batch EVEN when Claude is available, so a transport rupture fails
    # over within DW instead of cascading to the expensive Claude fallback. (Pre-170 this
    # asserted False — "force_batch requires Claude disabled" — which was the cost leak.)
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "false")
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert _slice36_should_force_batch(_Ctx("standard")) is True


def test_healthy_stream_still_requires_claude_disabled(monkeypatch, tmp_path):
    # The legacy invariant still holds for a HEALTHY stream: with Claude available and no
    # degradation, RT failures may cascade to Claude (Slice 170 fires only on a rupture).
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "false")
    _seed_ledger(monkeypatch, tmp_path,
                 stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.HEALTHY)
    assert _slice36_should_force_batch(_Ctx("standard")) is False
