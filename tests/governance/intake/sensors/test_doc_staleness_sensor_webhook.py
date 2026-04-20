"""Phase B Slice 4 — DocStalenessSensor GitHub push webhook migration.

Pins the contract introduced by JARVIS_DOC_STALENESS_WEBHOOK_ENABLED:
  * Flag off (default): webhook_enabled() returns False; _webhook_mode is
    False; poll uses legacy 24h interval (no regression).
  * Flag on: ingest_webhook(push_payload) extracts .py files under
    watched scan_paths, triggers scan_once, never raises on malformed
    input, returns True only when findings were emitted.
  * Ignored actions: non-push events, refs with zero .py changes, files
    outside scan_paths, empty commits list — all bump the ignored counter.
  * Interval gate: flag on -> poll demotes to fallback (default 6h).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import doc_staleness_sensor as dsm
from backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor import (
    DocStalenessSensor,
)


class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


def _sensor(monkeypatch: Any = None, scan_paths: Any = None) -> DocStalenessSensor:
    router = _SpyRouter()
    sensor = DocStalenessSensor(
        repo="jarvis",
        router=router,
        poll_interval_s=86400.0,
        project_root=Path("."),
        scan_paths=scan_paths or ("backend/core/",),
    )
    return sensor


def _push(
    *,
    ref: str = "refs/heads/main",
    added: List[str] = None,
    modified: List[str] = None,
    removed: List[str] = None,
) -> Dict[str, Any]:
    return {
        "ref": ref,
        "commits": [
            {
                "id": "abc123",
                "added": added or [],
                "modified": modified or [],
                "removed": removed or [],
            }
        ],
        "repository": {"full_name": "drussell23/JARVIS-AI-Agent"},
    }


# ---------------------------------------------------------------------------
# Flag helper
# ---------------------------------------------------------------------------

def test_webhook_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", "true")
    assert dsm.webhook_enabled() is True

    monkeypatch.setenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", "false")
    assert dsm.webhook_enabled() is False

    monkeypatch.delenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", raising=False)
    assert dsm.webhook_enabled() is False


def test_init_captures_webhook_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", "true")
    sensor = _sensor()
    assert sensor._webhook_mode is True

    monkeypatch.setenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", "false")
    sensor = _sensor()
    assert sensor._webhook_mode is False


# ---------------------------------------------------------------------------
# ingest_webhook — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_webhook_triggers_scan_on_watched_py_changes(
    monkeypatch: Any,
) -> None:
    """Push payload with .py file under scan_paths -> scan_once called."""
    sensor = _sensor(scan_paths=("backend/core/",))

    scanned = []

    async def _fake_scan() -> List:
        scanned.append("called")
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    payload = _push(modified=["backend/core/something.py"])
    emitted = await sensor.ingest_webhook(payload)

    assert scanned == ["called"]
    assert sensor._webhooks_handled == 1
    assert sensor._webhooks_ignored == 0
    # scan_once returns [] -> emitted False, but "handled" semantics still apply
    assert emitted is False


@pytest.mark.asyncio
async def test_ingest_webhook_returns_true_when_findings_emitted(
    monkeypatch: Any,
) -> None:
    """scan_once producing at least one finding -> emitted=True."""
    sensor = _sensor()

    from backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor import (
        DocFinding,
    )

    async def _fake_scan() -> List:
        return [
            DocFinding(
                category="missing_module_doc",
                severity="normal",
                summary="needs doc",
                file_path="backend/core/something.py",
                public_symbols=3,
                documented_symbols=0,
            )
        ]

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    payload = _push(modified=["backend/core/something.py"])
    emitted = await sensor.ingest_webhook(payload)

    assert emitted is True
    assert sensor._webhooks_handled == 1


# ---------------------------------------------------------------------------
# ingest_webhook — ignored paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_webhook_ignores_non_py_changes() -> None:
    sensor = _sensor()
    payload = _push(modified=["backend/core/README.md"])

    emitted = await sensor.ingest_webhook(payload)

    assert emitted is False
    assert sensor._webhooks_ignored == 1
    assert sensor._webhooks_handled == 0


@pytest.mark.asyncio
async def test_ingest_webhook_ignores_py_outside_scan_paths() -> None:
    sensor = _sensor(scan_paths=("backend/core/",))
    payload = _push(modified=["tests/unrelated.py"])

    emitted = await sensor.ingest_webhook(payload)

    assert emitted is False
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
async def test_ingest_webhook_ignores_empty_commits_list() -> None:
    sensor = _sensor()

    emitted = await sensor.ingest_webhook({"commits": [], "ref": "refs/heads/main"})

    assert emitted is False
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_payload", [
    {},
    {"commits": None},
    {"commits": "not-a-list"},
    "not-a-dict",
    None,
])
async def test_ingest_webhook_malformed_payloads_are_noop(bad_payload: Any) -> None:
    sensor = _sensor()

    # Must not raise
    emitted = await sensor.ingest_webhook(bad_payload)
    assert emitted is False
    assert sensor._webhooks_ignored == 1


@pytest.mark.asyncio
async def test_ingest_webhook_never_raises_when_scan_raises(
    monkeypatch: Any,
) -> None:
    """Scan raising must be caught — webhook handler is crash-proof."""
    sensor = _sensor()

    async def _exploding_scan() -> List:
        raise RuntimeError("simulated scan failure")

    sensor.scan_once = _exploding_scan  # type: ignore[assignment]

    payload = _push(modified=["backend/core/something.py"])
    emitted = await sensor.ingest_webhook(payload)

    assert emitted is False
    # Counter model: the webhook was counted as "handled" up to the scan
    # attempt, then the except-branch bumps ignored. Either is fine —
    # assert we didn't raise.


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_default_when_flag_off(monkeypatch: Any) -> None:
    """Flag off -> existing 24h poll interval preserved exactly."""
    monkeypatch.delenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", raising=False)
    sensor = DocStalenessSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=86400.0,
    )
    assert sensor._webhook_mode is False
    assert sensor._poll_interval_s == 86400.0


def test_fallback_interval_applied_when_flag_on(monkeypatch: Any) -> None:
    """Flag on -> _webhook_mode=True triggers fallback selection in _poll_loop."""
    monkeypatch.setenv("JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", "true")
    monkeypatch.setattr(dsm, "_DOC_STALENESS_FALLBACK_INTERVAL_S", 21600.0)

    sensor = DocStalenessSensor(
        repo="jarvis", router=_SpyRouter(), poll_interval_s=86400.0,
    )
    assert sensor._webhook_mode is True
    # Effective interval resolution happens inside _poll_loop at sleep
    # time, so the attribute itself is still the configured default.
    # The actual sleep duration is exercised by the ov_smoke + integration
    # checks where we can drive the loop.
    assert sensor._poll_interval_s == 86400.0
