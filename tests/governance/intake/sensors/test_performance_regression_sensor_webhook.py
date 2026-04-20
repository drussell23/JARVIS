"""Phase B Slice 6 — PerformanceRegressionSensor CI webhook migration.

Pins the contract introduced by JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED:
  * Flag off (default): webhook_enabled() -> False; _webhook_mode False;
    poll at legacy 1h interval (no regression).
  * Flag on: ingest_webhook(ci_payload) triggers scan_once() for
    interesting events (status ∈ {failure/failed/error} OR payload has
    benchmark/metrics/perf_data OR workflow_run.conclusion=failure).
    Benign events (passing builds with no perf data) are no-ops.
  * Malformed payloads are clean no-ops. Scan exceptions are caught.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import performance_regression_sensor as pm
from backend.core.ouroboros.governance.intake.sensors.performance_regression_sensor import (
    PerformanceRegressionSensor,
)


class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


def _sensor() -> PerformanceRegressionSensor:
    return PerformanceRegressionSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=3600.0,
    )


# ---------------------------------------------------------------------------
# Flag helper + init
# ---------------------------------------------------------------------------

def test_webhook_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "true")
    assert pm.webhook_enabled() is True

    monkeypatch.setenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "false")
    assert pm.webhook_enabled() is False

    # Graduated 2026-04-20 — default is now "true" (CI-webhook-primary).
    monkeypatch.delenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", raising=False)
    assert pm.webhook_enabled() is True


def test_init_captures_webhook_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "true")
    sensor = _sensor()
    assert sensor._webhook_mode is True

    monkeypatch.setenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "false")
    sensor = _sensor()
    assert sensor._webhook_mode is False


# ---------------------------------------------------------------------------
# ingest_webhook — interesting events trigger scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failure", "failed", "error", "FAILURE", "FAILED"])
async def test_ingest_webhook_failed_status_triggers_scan(status: str) -> None:
    """Any CI failure status (case-insensitive) triggers scan_once."""
    sensor = _sensor()
    scanned: List[int] = []

    async def _fake_scan() -> list:
        scanned.append(1)
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    emitted = await sensor.ingest_webhook({
        "status": status,
        "name": "test-build",
        "message": "pipeline failed on stage 2",
    })

    assert scanned == [1]
    assert sensor._webhooks_handled == 1
    # scan returned [] → emitted False (no new findings), but "handled" counts
    assert emitted is False


@pytest.mark.asyncio
async def test_ingest_webhook_benchmark_payload_triggers_scan() -> None:
    """Explicit benchmark metrics always trigger a scan."""
    sensor = _sensor()
    scanned: List[int] = []

    async def _fake_scan() -> list:
        scanned.append(1)
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    # Passing status but with benchmark data — interesting
    emitted = await sensor.ingest_webhook({
        "status": "success",
        "benchmark": {"latency_p50_ms": 45.2, "latency_p99_ms": 182.0},
    })

    assert scanned == [1]
    assert sensor._webhooks_handled == 1


@pytest.mark.asyncio
async def test_ingest_webhook_github_actions_workflow_run_failure() -> None:
    """GitHub Actions-as-CI wraps status under workflow_run.conclusion."""
    sensor = _sensor()
    scanned: List[int] = []

    async def _fake_scan() -> list:
        scanned.append(1)
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    emitted = await sensor.ingest_webhook({
        "event": "workflow_run",
        "workflow_run": {
            "conclusion": "failure",
            "name": "benchmark-suite",
            "id": 12345,
        },
    })

    assert scanned == [1]
    assert sensor._webhooks_handled == 1


@pytest.mark.asyncio
async def test_ingest_webhook_returns_true_when_scan_produces_findings() -> None:
    """scan_once returning findings -> emitted=True."""
    sensor = _sensor()

    from backend.core.ouroboros.governance.intake.sensors.performance_regression_sensor import (
        RegressionFinding,
    )

    async def _fake_scan() -> list:
        return [RegressionFinding(
            metric="latency", severity="high",
            summary="p99 latency regression detected",
            baseline_value=120.0, current_value=210.0,
            delta_pct=75.0, task_type="immediate",
        )]

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    emitted = await sensor.ingest_webhook({"status": "failed"})

    assert emitted is True
    assert sensor._webhooks_handled == 1


# ---------------------------------------------------------------------------
# ingest_webhook — benign events ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("benign_payload", [
    {"status": "success"},
    {"status": "passed", "name": "build-42"},
    {"conclusion": "success"},
    {"event": "workflow_run", "workflow_run": {"conclusion": "success"}},
    {"status": "in_progress"},
    {"event": "ping"},
])
async def test_ingest_webhook_benign_events_are_noop(benign_payload: Dict) -> None:
    """Passing/pending/non-regression events do NOT trigger scan."""
    sensor = _sensor()
    scanned: List[int] = []

    async def _never_scan() -> list:
        scanned.append(1)
        return []

    sensor.scan_once = _never_scan  # type: ignore[assignment]

    emitted = await sensor.ingest_webhook(benign_payload)

    assert emitted is False
    assert scanned == []
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_payload", [
    None,
    "not-a-dict",
    42,
    [],
])
async def test_ingest_webhook_malformed_payloads_are_noop(bad_payload: Any) -> None:
    sensor = _sensor()
    emitted = await sensor.ingest_webhook(bad_payload)
    assert emitted is False
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
async def test_ingest_webhook_never_raises_on_scan_exception() -> None:
    sensor = _sensor()

    async def _exploding() -> list:
        raise RuntimeError("simulated scan failure")

    sensor.scan_once = _exploding  # type: ignore[assignment]

    emitted = await sensor.ingest_webhook({"status": "failed"})
    # Must not raise
    assert emitted is False


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_default_when_flag_off(monkeypatch: Any) -> None:
    # Graduated 2026-04-20 — default is now "true"; opt-out must be explicit.
    monkeypatch.setenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "false")
    sensor = PerformanceRegressionSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=3600.0,
    )
    assert sensor._webhook_mode is False
    assert sensor._poll_interval_s == 3600.0


def test_init_webhook_mode_enables_fallback_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "true")
    sensor = PerformanceRegressionSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=3600.0,
    )
    assert sensor._webhook_mode is True
