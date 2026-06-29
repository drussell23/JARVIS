"""The Safety Law -- strict semantic classification of probe errors.

Soak bt-2026-06-29-061928 PROVISIONED A REAL GCE NODE on a false 401: the deep
probe couldn't authenticate, every beat counted as a "degrade", the streak hit
the hard-outage threshold, and Gap 2 forcefully awakened J-Prime. A CONFIG error
must NEVER trigger infrastructure.

The fix: classify the probe outcome.
  * Data-plane outage (timeout / 5xx / connection drop) -> degrade (streak++).
  * Client/auth error (401 / 403) -> AegisConfigurationError: FREEZE the loop
    (stop spamming the proxy), emit critical to the DLQ, and is_degrading=False
    + streak forced to 0. A misconfig can NEVER awaken.

TDD with injected fakes -- ZERO real network.
"""
from __future__ import annotations

import asyncio
import urllib.error

import pytest

import backend.core.ouroboros.governance.provider_heartbeat as ph
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_DEGRADE_STREAK", "2")
    monkeypatch.setenv(
        "JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "surface_health.json")
    )
    ph._reset_singleton_for_tests()
    yield
    ph._reset_singleton_for_tests()


def _led():
    return SurfaceHealthLedger(autosave=False)


def _http_error(code):
    return urllib.error.HTTPError("http://aegis/v1/chat/completions", code, "x", {}, None)


# ---------------------------------------------------------------------------
# Classification of the raw transport error
# ---------------------------------------------------------------------------

def test_classify_401_is_auth():
    assert ph._classify_probe_exception(_http_error(401)) == "auth"


def test_classify_403_is_auth():
    assert ph._classify_probe_exception(_http_error(403)) == "auth"


def test_classify_500_is_outage():
    assert ph._classify_probe_exception(_http_error(503)) == "outage"


def test_classify_timeout_is_outage():
    assert ph._classify_probe_exception(asyncio.TimeoutError()) == "outage"


def test_classify_connection_error_is_outage():
    assert ph._classify_probe_exception(urllib.error.URLError("refused")) == "outage"


# ---------------------------------------------------------------------------
# The deep probe RAISES AegisConfigurationError on auth (does NOT degrade)
# ---------------------------------------------------------------------------

async def test_deep_probe_auth_error_raises_config_error():
    async def dispatch():
        raise _http_error(401)

    hb = ph.DWHeartbeat(ledger=_led(), inference_dispatch_fn=dispatch)
    with pytest.raises(ph.AegisConfigurationError):
        await hb._deep_inference_probe()


async def test_deep_probe_5xx_is_outage_false():
    async def dispatch():
        raise _http_error(502)

    hb = ph.DWHeartbeat(ledger=_led(), inference_dispatch_fn=dispatch)
    assert await hb._deep_inference_probe() is False  # outage -> degrade


# ---------------------------------------------------------------------------
# beat(): auth error FREEZES + no streak + is_degrading False + DLQ emit
# ---------------------------------------------------------------------------

async def test_beat_auth_error_freezes_and_never_degrades():
    dlq = []
    async def dispatch():
        raise _http_error(401)

    hb = ph.DWHeartbeat(
        ledger=_led(), inference_dispatch_fn=dispatch,
        dlq_emit_fn=lambda payload: dlq.append(payload),
    )
    await hb.beat()
    assert hb.is_frozen() is True
    assert hb.is_degrading() is False        # a misconfig must NOT awaken
    assert hb.consecutive_failures() == 0    # streak forced to 0 (Gap 2 safe)
    assert len(dlq) == 1
    assert "AegisConfiguration" in str(dlq[0]) or dlq[0].get("error_class") == "AegisConfigurationError"


async def test_beat_outage_still_increments_streak():
    async def dispatch():
        raise _http_error(503)

    hb = ph.DWHeartbeat(ledger=_led(), inference_dispatch_fn=dispatch)
    await hb.beat()
    await hb.beat()
    assert hb.is_frozen() is False
    assert hb.consecutive_failures() == 2   # real outage DOES degrade
    assert hb.is_degrading() is True


async def test_frozen_heartbeat_is_degrading_stays_false_forever():
    async def dispatch():
        raise _http_error(403)

    hb = ph.DWHeartbeat(ledger=_led(), inference_dispatch_fn=dispatch)
    await hb.beat()
    await hb.beat()  # would be streak 2 if counted -- but auth never counts
    assert hb.is_frozen() is True
    assert hb.is_degrading() is False
    assert hb.consecutive_failures() == 0


# ---------------------------------------------------------------------------
# The run() loop STOPS once frozen (no proxy spam)
# ---------------------------------------------------------------------------

async def test_run_loop_stops_on_auth_freeze(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_INTERVAL_S", "0.01")
    beats = {"n": 0}
    async def dispatch():
        beats["n"] += 1
        raise _http_error(401)

    hb = ph.DWHeartbeat(ledger=_led(), inference_dispatch_fn=dispatch)
    await asyncio.wait_for(hb.run(), timeout=2.0)  # must EXIT, not spin forever
    assert hb.is_frozen() is True
    assert beats["n"] >= 1  # it probed, hit auth, froze, and stopped
