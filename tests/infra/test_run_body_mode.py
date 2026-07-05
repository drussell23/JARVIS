"""Unit tests for scripts/run_body_mode.py (Stage-2 Body-mode driver).

Pure-logic -- NO real sockets, buses, or GCP. Every collaborator (discover /
bus stack / bridge / shim / sensor / watchdog) is a fake injected via the
driver's seams (the ``BrainIgnitionDriver`` seam pattern). The tests prove:

  (a) --dry-run prints the plan and touches NOTHING (injected discover seam is
      never called).
  (b) Injected-seam happy path: fake discover returns a URL; the connect gate
      clears against a fake client; ``--inject-test-signal 3`` produces exactly
      3 ``shim.ingest`` calls whose envelopes carry 3 DISTINCT dedup_keys; the
      run exits 0 and emits the ``[BodyMode] SUMMARY`` line.
  (c) Discovery failure -> exit 2 with the "Brain offline" log.
  (d) Connect-gate timeout -> exit 3.

The live discovery/bus/bridge paths are proven by the Stage-2 live-fire
acceptance, not here.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from typing import Any, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_DRIVER_PATH = os.path.join(_REPO_ROOT, "scripts", "run_body_mode.py")


def _load_driver() -> Any:
    """Load the Body-mode driver as a module by path (script, not a package)."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    if "run_body_mode" in sys.modules:
        return sys.modules["run_body_mode"]
    spec = importlib.util.spec_from_file_location("run_body_mode", _DRIVER_PATH)
    assert spec and spec.loader, "cannot load run_body_mode"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_body_mode"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


bm = _load_driver()


@pytest.fixture(autouse=True)
def _no_multicast(monkeypatch):
    # Established precedent (tests/governance/transport/test_trinity_bus_bridge.py
    # _mk_bus): suppress the in-process UDP multicast artifact. Belt-and-braces
    # here -- every collaborator is a fake, so no real bus is ever constructed.
    monkeypatch.setenv("TRINITY_MULTICAST_ENABLED", "false")


# ---------------------------------------------------------------------------
# Fakes (record-only; no I/O).
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected


class _FakeDistBus:
    """Duck-types the DistributedEventBus client surface the driver uses:
    ``start_client`` (long-running) + ``_client`` attr with ``connected``."""

    def __init__(self, connect: bool = True) -> None:
        self._client = _FakeClient(connected=True) if connect else None
        self.started_urls: List[str] = []
        self.stopped = False
        self._forever = asyncio.Event()

    async def start_client(self, url: str) -> None:
        self.started_urls.append(url)
        await self._forever.wait()  # long-running, like the real client task

    async def stop(self) -> None:
        self.stopped = True
        self._forever.set()


class _FakeTrinityBus:
    pass


class _FakeBroker:
    pass


class _FakeBridge:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeShim:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


class _FakeWatchdog:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.lag_event_count = 0
        self.threshold_ms = 200.0

    def start(self) -> bool:
        self.started = True
        return True

    async def stop(self) -> None:
        self.stopped = True

    def recent_lag_records(self, limit: Optional[int] = None) -> List[Any]:
        return []


def _seams(*, url: Optional[str] = "wss://10.0.0.5:8770/ws/trinity-bus",
           connect: bool = True):
    """A full fake seam kit; returns (kwargs, recorder namespace)."""
    calls = {"discover": 0}
    dist = _FakeDistBus(connect=connect)
    bridge = _FakeBridge()
    shim = _FakeShim()
    wd = _FakeWatchdog()

    async def _discover() -> Optional[str]:
        calls["discover"] += 1
        return url

    async def _bus_factory():
        return _FakeTrinityBus(), _FakeBroker(), dist

    def _bridge_factory(trinity_bus: Any, broker: Any) -> _FakeBridge:
        return bridge

    def _shim_factory(trinity_bus: Any) -> _FakeShim:
        return shim

    def _sensor_factory(shim_: Any) -> Any:
        return object()

    def _watchdog_factory() -> _FakeWatchdog:
        return wd

    kwargs = dict(
        discover_fn=_discover,
        bus_factory=_bus_factory,
        bridge_factory=_bridge_factory,
        shim_factory=_shim_factory,
        sensor_factory=_sensor_factory,
        watchdog_factory=_watchdog_factory,
    )
    ns = type("NS", (), dict(calls=calls, dist=dist, bridge=bridge,
                             shim=shim, wd=wd))
    return kwargs, ns


# ---------------------------------------------------------------------------
# (a) --dry-run: plan printed, no seam touched.
# ---------------------------------------------------------------------------


def test_dry_run_prints_plan_and_touches_nothing(capsys):
    kwargs, ns = _seams()
    driver = bm.BodyModeDriver(dry_run=True, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out
    assert "[BodyMode]" in out
    assert ns.calls["discover"] == 0, "dry-run must not discover"
    assert ns.dist.started_urls == [], "dry-run must not connect"
    assert not ns.bridge.started and not ns.wd.started


def test_dry_run_via_cli_main(capsys):
    rc = bm.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out


# ---------------------------------------------------------------------------
# (b) Injected-seam happy path: 3 signals -> 3 ingests, distinct dedup_keys.
# ---------------------------------------------------------------------------


def test_inject_three_signals_distinct_dedup_keys(capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_CONNECT_GATE_S", "2")
    kwargs, ns = _seams()
    driver = bm.BodyModeDriver(
        inject_test_signals=3, duration_s=0.05, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out

    assert rc == 0
    assert len(ns.shim.envelopes) == 3, "exactly 3 shim.ingest calls"
    keys = [e.dedup_key for e in ns.shim.envelopes]
    assert len(set(keys)) == 3, "dedup_keys must be DISTINCT: %r" % keys
    for e in ns.shim.envelopes:
        # ``cadence_synthetic`` is the whitelisted honest-source token for
        # synthetic test workload (intent_envelope.py _VALID_SOURCES) -- the
        # Body-mode identity travels in evidence.origin.
        assert e.source == "cadence_synthetic"
        assert e.evidence.get("origin") == "body_test"
        assert "stage2 acceptance signal" in e.description

    # Full lifecycle: connected + bridged + watched + cleanly stopped.
    assert ns.dist.started_urls == ["wss://10.0.0.5:8770/ws/trinity-bus"]
    assert ns.bridge.started and ns.bridge.stopped
    assert ns.wd.started and ns.wd.stopped

    # Census + summary lines.
    assert "[BodyMode] SUMMARY" in out
    assert "signals_sent=3" in out
    assert "lag_events=0" in out


# ---------------------------------------------------------------------------
# (c) Discovery failure -> exit 2 + "Brain offline".
# ---------------------------------------------------------------------------


def test_discovery_failure_exits_2_brain_offline(capsys):
    kwargs, ns = _seams(url=None)
    driver = bm.BodyModeDriver(**kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out
    assert rc == 2
    assert "Brain offline" in out
    assert ns.calls["discover"] == 1
    assert ns.dist.started_urls == [], "no connect attempt after failed discovery"
    assert not ns.bridge.started


# ---------------------------------------------------------------------------
# (d) Connect-gate timeout -> exit 3.
# ---------------------------------------------------------------------------


def test_connect_gate_timeout_exits_3(capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_CONNECT_GATE_S", "0.2")
    kwargs, ns = _seams(connect=False)  # _client never materializes
    driver = bm.BodyModeDriver(inject_test_signals=1, duration_s=0.05, **kwargs)
    rc = asyncio.run(driver.run())
    assert rc == 3
    assert ns.shim.envelopes == [], "no signals on a dark link"
    assert not ns.bridge.started, "bridge must not start on a dark link"
