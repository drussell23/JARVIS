"""Tests for the DEEP INFERENCE PROBE upgrade to provider_heartbeat.py.

Run-#13 blindspot (2026-06-28 soak bt-2026-06-29-032526)
-------------------------------------------------------
The legacy heartbeat probed ``GET {base}/models`` -- a CONTROL-PLANE reachability
check. In the failover soak DW's control plane answered 200 the entire session
while the *data plane* (batch/generation) deadlocked at 180s per op. The probe
stayed GREEN, ``is_degrading()`` never fired, J-Prime never pre-warmed.

The fix upgrades the probe to a DEEP INFERENCE PROBE: a deterministic,
ultra-low-latency generation (``Return the digit 1``, ``max_tokens=1``) dispatched
straight at the DW *data plane*, wrapped in an ``asyncio.wait_for`` whose timeout
is DYNAMICALLY resolved (env override OR a baseline-latency metric -- never a
hardcoded literal). A deadlocked data plane TIMES OUT the probe in a fraction of
a second and instantly flips ``is_degrading()`` -- long before the 180s real-op
budget burns.

TDD with injected fakes -- ZERO real network. The inference dispatch boundary is
injectable; the ledger is an in-process SurfaceHealthLedger.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import backend.core.ouroboros.governance.provider_heartbeat as ph
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_DEGRADE_STREAK", "2")
    monkeypatch.delenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", raising=False)
    # Isolate the ledger from the repo's real .jarvis surface-health file
    # (otherwise a prior soak's degrade streak leaks into the test).
    monkeypatch.setenv(
        "JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "surface_health.json")
    )
    ph._reset_singleton_for_tests()
    yield
    ph._reset_singleton_for_tests()


def _ledger() -> SurfaceHealthLedger:
    return SurfaceHealthLedger(autosave=False)


# ---------------------------------------------------------------------------
# Dynamic timeout resolution -- NOT a hardcoded literal
# ---------------------------------------------------------------------------

def test_deep_probe_timeout_honors_env_override(monkeypatch):
    """An explicit env override wins -- the baseline metric is ignored."""
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", "7.5")
    # Even with a tiny baseline, the explicit operator pin is authoritative.
    assert ph._resolve_deep_probe_timeout(baseline_s=0.01) == pytest.approx(7.5)


def test_deep_probe_timeout_is_metric_driven_not_hardcoded(monkeypatch):
    """No env override -> the timeout SCALES with the observed baseline latency.

    The whole point: a slower-but-healthy DW gets a proportionally larger probe
    deadline (no false degrade), a fast DW a tight one. A hardcoded constant
    could not do this -- so a larger baseline MUST yield a larger timeout.
    """
    monkeypatch.delenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", raising=False)
    fast = ph._resolve_deep_probe_timeout(baseline_s=0.05)
    slow = ph._resolve_deep_probe_timeout(baseline_s=2.0)
    assert slow > fast  # metric-driven, not a constant


def test_deep_probe_timeout_is_clamped(monkeypatch):
    """The dynamic timeout is bounded -- never 0, never unbounded."""
    monkeypatch.delenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_FLOOR_S", "0.5")
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_CEIL_S", "30")
    # A near-zero baseline is floored.
    assert ph._resolve_deep_probe_timeout(baseline_s=0.0) >= 0.5
    # An enormous baseline is ceiled.
    assert ph._resolve_deep_probe_timeout(baseline_s=10_000.0) <= 30.0


# ---------------------------------------------------------------------------
# The deep inference probe itself
# ---------------------------------------------------------------------------

async def test_deep_probe_healthy_dispatch_returns_true():
    """A dispatch that returns a token within the deadline -> healthy (True)."""
    async def dispatch():
        return "1"

    hb = ph.DWHeartbeat(ledger=_ledger(), inference_dispatch_fn=dispatch)
    assert await hb._deep_inference_probe() is True


async def test_deep_probe_deadlock_returns_false_fast(monkeypatch):
    """A DEADLOCKED data plane (dispatch hangs far past the dynamic timeout)
    must return False in a fraction of a second -- NOT wait 180s."""
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", "0.05")

    async def dispatch():
        await asyncio.sleep(5.0)  # simulate a wedged DW inference queue
        return "1"

    hb = ph.DWHeartbeat(ledger=_ledger(), inference_dispatch_fn=dispatch)
    t0 = time.monotonic()
    verdict = await hb._deep_inference_probe()
    elapsed = time.monotonic() - t0
    assert verdict is False
    assert elapsed < 1.0  # killed by asyncio.wait_for, not the 5s sleep


async def test_deep_probe_dispatch_error_is_false(monkeypatch):
    """A dispatch error is a degrade signal -> False (never raises)."""
    async def dispatch():
        raise RuntimeError("DoublewordInfraError: 503")

    hb = ph.DWHeartbeat(ledger=_ledger(), inference_dispatch_fn=dispatch)
    assert await hb._deep_inference_probe() is False


async def test_deep_probe_empty_result_is_false():
    """An empty/blank generation is NOT proof of a live data plane -> False."""
    async def dispatch():
        return ""

    hb = ph.DWHeartbeat(ledger=_ledger(), inference_dispatch_fn=dispatch)
    assert await hb._deep_inference_probe() is False


# ---------------------------------------------------------------------------
# The deep probe drives is_degrading() -- the early signal, fast.
# ---------------------------------------------------------------------------

async def test_deep_probe_deadlock_drives_is_degrading(monkeypatch):
    """End to end: a deadlocked data plane flips is_degrading() at the streak,
    in well under a second -- the control-plane GET /models trap is gone."""
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", "0.02")

    async def dispatch():
        await asyncio.sleep(5.0)
        return "1"

    led = _ledger()
    hb = ph.DWHeartbeat(ledger=led, inference_dispatch_fn=dispatch)
    t0 = time.monotonic()
    await hb.beat()  # streak 1
    assert hb.is_degrading() is False
    await hb.beat()  # streak 2 -> degrading
    elapsed = time.monotonic() - t0
    assert hb.is_degrading() is True
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED
    assert elapsed < 1.0  # two deadlocked beats still finished in a blink


# ---------------------------------------------------------------------------
# Gate: deep probe OFF -> legacy control-plane probe (rollback path)
# ---------------------------------------------------------------------------

def test_deep_probe_gate_default_on():
    assert ph.deep_probe_enabled() is True


def test_deep_probe_gate_off_falls_back_to_models_probe(monkeypatch):
    """JARVIS_DW_DEEP_PROBE_ENABLED=false -> the heartbeat's default probe is the
    legacy GET /models reachability check (byte-identical rollback)."""
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_ENABLED", "false")
    assert ph.deep_probe_enabled() is False
    hb = ph.DWHeartbeat(ledger=_ledger())
    assert hb._probe_fn is ph._default_dw_probe


def test_deep_probe_gate_on_uses_deep_probe_by_default(monkeypatch):
    """Default (deep ON) -> the heartbeat's default probe is the deep inference
    probe, not GET /models."""
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_ENABLED", "true")
    hb = ph.DWHeartbeat(ledger=_ledger())
    assert hb._probe_fn is not ph._default_dw_probe
