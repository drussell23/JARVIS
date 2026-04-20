"""Phase B Slice 5 — CrossRepoDriftSensor GitHub push migration (gap #4).

Pins the contract introduced by JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED:
  * Flag off (default): webhook_enabled() -> False; _webhook_mode False;
    poll at legacy 1h interval (no regression).
  * Flag on: ingest_webhook(push) intersects commits[].added+modified
    with _WATCHED_PATHS (contract + protocol files); one or more
    matches triggers scan_once. Non-matching pushes are no-ops.
  * Malformed payloads are clean no-ops (webhook handlers must be
    crash-proof). Scan exceptions are caught.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import cross_repo_drift_sensor as cdm
from backend.core.ouroboros.governance.intake.sensors.cross_repo_drift_sensor import (
    CrossRepoDriftSensor,
)


class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


def _sensor() -> CrossRepoDriftSensor:
    return CrossRepoDriftSensor(
        repo="jarvis",
        router=_SpyRouter(),
        poll_interval_s=3600.0,
        project_root=Path("."),
    )


def _push(
    *,
    ref: str = "refs/heads/main",
    added: List[str] = None,
    modified: List[str] = None,
) -> Dict[str, Any]:
    return {
        "ref": ref,
        "commits": [
            {
                "id": "abc",
                "added": added or [],
                "modified": modified or [],
                "removed": [],
            }
        ],
        "repository": {"full_name": "drussell23/JARVIS-AI-Agent"},
    }


# ---------------------------------------------------------------------------
# Flag helper + init
# ---------------------------------------------------------------------------

def test_webhook_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED", "true")
    assert cdm.webhook_enabled() is True

    monkeypatch.setenv("JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED", "false")
    assert cdm.webhook_enabled() is False


def test_init_captures_webhook_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED", "true")
    sensor = _sensor()
    assert sensor._webhook_mode is True

    monkeypatch.setenv("JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED", "false")
    sensor = _sensor()
    assert sensor._webhook_mode is False


# ---------------------------------------------------------------------------
# ingest_webhook — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_webhook_triggers_scan_on_watched_file() -> None:
    """Push touching a watched contract file -> scan_once called."""
    sensor = _sensor()
    scanned: List[int] = []

    async def _fake_scan() -> list:
        scanned.append(1)
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    # op_context.py is in the watched set (contract file)
    payload = _push(
        modified=["backend/core/ouroboros/governance/op_context.py"],
    )
    emitted = await sensor.ingest_webhook(payload)

    assert scanned == [1]
    assert sensor._webhooks_handled == 1
    # scan returned [] -> no findings -> emitted False
    assert emitted is False


@pytest.mark.asyncio
async def test_ingest_webhook_returns_true_when_findings_emitted() -> None:
    """scan_once returning findings -> emitted=True."""
    sensor = _sensor()

    from backend.core.ouroboros.governance.intake.sensors.cross_repo_drift_sensor import (
        DriftFinding,
    )

    async def _fake_scan() -> list:
        return [DriftFinding(
            category="schema_drift",
            severity="high",
            summary="schema drift detected",
        )]

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    payload = _push(modified=["backend/core/mind_client.py"])  # protocol file
    emitted = await sensor.ingest_webhook(payload)

    assert emitted is True
    assert sensor._webhooks_handled == 1


# ---------------------------------------------------------------------------
# ingest_webhook — non-match paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_webhook_ignores_unwatched_file() -> None:
    sensor = _sensor()
    scanned: List[int] = []

    async def _never_scan() -> list:
        scanned.append(1)
        return []

    sensor.scan_once = _never_scan  # type: ignore[assignment]

    payload = _push(modified=["backend/vision/some_unrelated_file.py"])
    emitted = await sensor.ingest_webhook(payload)

    assert emitted is False
    assert scanned == []  # scan was NOT called
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
async def test_ingest_webhook_ignores_empty_commits() -> None:
    sensor = _sensor()
    emitted = await sensor.ingest_webhook(
        {"commits": [], "ref": "refs/heads/main"},
    )
    assert emitted is False
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_payload", [
    {},
    None,
    "not-a-dict",
    {"commits": None},
    {"commits": "bad"},
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

    payload = _push(modified=["backend/core/mind_client.py"])
    emitted = await sensor.ingest_webhook(payload)

    # Must not raise
    assert emitted is False


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_default_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED", raising=False)
    sensor = CrossRepoDriftSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=3600.0,
    )
    assert sensor._webhook_mode is False
    assert sensor._poll_interval_s == 3600.0


def test_init_webhook_mode_enables_fallback_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_CROSS_REPO_DRIFT_WEBHOOK_ENABLED", "true")
    sensor = CrossRepoDriftSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=3600.0,
    )
    assert sensor._webhook_mode is True
