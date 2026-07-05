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
  (c) Discovery failure -> exit 2 with the "Brain offline" log (STRICT
      contract: applies because these seams arm no durable WAL).
  (d) Connect-gate timeout -> exit 3 (same strict contract).

Stage-3 Task 4 (durable degrade) additions:

  (e) Census line includes ``queued=N`` from the injected durable's
      ``pending_count()``; SUMMARY gains ``queued_at_exit=N``.
  (f) Offline-at-start with the WAL armed -> NO exit 2: the driver
      degrades, signals journal via the fake durable, and the canonical
      "Brain offline" line is logged exactly once.
  (g) --require-brain restores the strict exit-2 contract even with the
      WAL armed.
  (h) Edge-transition determinism: the degrade UI state derives ONLY
      from the client's ``connected`` property, polled exactly once per
      census tick -- one offline line + one reconnected line per
      episode, no repeats while steady.

The live discovery/bus/bridge/WAL paths are proven by the Stage-2/3
live-fire acceptance and the real-component transport suites, not here.
The fake shim->durable wiring below simulates the live journal chain
(shim -> trinity bus -> bridge -> broker -> DurableOutbound), which is
itself proven by test_trinity_bus_bridge.py + test_durable_outbound.py;
these tests prove the DRIVER's composition and surfacing only.
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


class _ScriptedClient:
    """``connected`` consumes one scripted value per read and holds the
    last value once the script is exhausted. Determinism contract with
    the driver: the connect gate reads ``connected`` until True; the
    census loop reads it EXACTLY once per tick (a single poll feeds both
    the edge machine and the census line)."""

    def __init__(self, script: List[bool]) -> None:
        self._script = list(script)
        self._last = self._script[-1] if self._script else False

    @property
    def connected(self) -> bool:
        if self._script:
            self._last = self._script.pop(0)
        return self._last


class _FakeDurable:
    """Duck-types the DurableOutbound surface the driver composes:
    ``start``/``stop`` lifecycle + ``pending_count`` (census / summary)
    + ``on_ack`` (threaded by the real DistributedEventBus, unused by
    the fakes). ``record`` is the fake journal-chain terminus."""

    def __init__(self, queued: Optional[int] = None) -> None:
        self.started = False
        self.stopped = False
        self.journaled: List[str] = []
        self._queued = queued

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def record(self, dedup_key: str) -> None:
        self.journaled.append(dedup_key)

    def pending_count(self) -> int:
        if self._queued is not None:
            return self._queued
        return len(self.journaled)

    def on_ack(self, acked_event_id: str) -> None:  # pragma: no cover
        pass


class _FakeDistBus:
    """Duck-types the DistributedEventBus client surface the driver uses:
    ``start_client`` (long-running) + ``_client`` attr with ``connected``."""

    def __init__(self, connect: bool = True, client: Any = None) -> None:
        if client is not None:
            self._client = client
        else:
            self._client = _FakeClient(connected=True) if connect else None
        self.started_urls: List[Any] = []
        self.stopped = False
        self._forever = asyncio.Event()

    async def start_client(self, url: Optional[str]) -> None:
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
    def __init__(self, durable: Optional[_FakeDurable] = None) -> None:
        self.envelopes: List[Any] = []
        # Simulates the live journal chain terminus (shim -> trinity bus
        # -> bridge -> broker -> DurableOutbound); the real chain is
        # proven by the transport suites -- here we test the driver.
        self._durable = durable

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        if self._durable is not None:
            self._durable.record(envelope.dedup_key)
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
           connect: bool = True,
           durable: Optional[_FakeDurable] = None,
           client: Any = None):
    """A full fake seam kit; returns (kwargs, recorder namespace).

    ``durable`` arms the WAL seam (Stage-3 degrade contract); without it
    the driver stays on the strict Stage-2 exit-2/exit-3 contract.
    ``client`` overrides the dist bus's ``_client`` (edge scripting).
    """
    calls = {"discover": 0}
    dist = _FakeDistBus(connect=connect, client=client)
    bridge = _FakeBridge()
    shim = _FakeShim(durable=durable)
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
    if durable is not None:
        kwargs["durable_factory"] = lambda broker: durable
    ns = type("NS", (), dict(calls=calls, dist=dist, bridge=bridge,
                             shim=shim, wd=wd, durable=durable))
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


# ---------------------------------------------------------------------------
# (e) Task 4: census line includes queued= from the injected durable;
#     SUMMARY gains queued_at_exit=.
# ---------------------------------------------------------------------------


def test_census_includes_queued_from_fake_durable(capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_CONNECT_GATE_S", "2")
    monkeypatch.setenv("JARVIS_BODY_MODE_CENSUS_S", "0.01")
    durable = _FakeDurable(queued=7)
    kwargs, ns = _seams(durable=durable)
    driver = bm.BodyModeDriver(duration_s=0.05, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out
    assert rc == 0
    assert "queued=7" in out, "census must surface durable.pending_count()"
    assert "connected=True" in out
    assert "queued_at_exit=7" in out, "SUMMARY must carry the exit depth"
    assert durable.started, "driver must arm the durable WAL"
    assert durable.stopped, "driver must disarm the durable WAL on teardown"


# ---------------------------------------------------------------------------
# (f) Task 4: offline-at-start with the WAL armed -> NO exit 2. The driver
#     degrades: signals journal via the (fake) durable chain, the client
#     starts with no static url (re-racing via url_resolver), and the
#     canonical "Brain offline" line is logged exactly once.
# ---------------------------------------------------------------------------


def test_offline_at_start_with_wal_armed_degrades_without_exit_2(
        capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_BODY_MODE_CENSUS_S", "0.01")
    durable = _FakeDurable()
    kwargs, ns = _seams(url=None, connect=False, durable=durable)
    driver = bm.BodyModeDriver(inject_test_signals=5, duration_s=0.1, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out

    assert rc == 0, "WAL armed: discovery failure must NOT exit 2"
    assert out.count("Brain offline") == 1, (
        "the offline episode must surface exactly once: %r" % out)
    assert "signals queued (durable)" in out
    assert len(ns.shim.envelopes) == 5, "signals still flow while offline"
    assert durable.journaled == [e.dedup_key for e in ns.shim.envelopes], (
        "every accepted signal must journal durably")
    assert "queued=5" in out and "connected=False" in out
    assert "queued_at_exit=5" in out
    # The client is started with no static url -- it re-races discovery
    # via the url_resolver seam instead of the driver exiting.
    assert ns.dist.started_urls == [None]
    assert ns.bridge.started, "bridge runs in degraded mode (WAL is the sink)"


# ---------------------------------------------------------------------------
# (g) Task 4: --require-brain restores the strict exit-2 contract even
#     with the WAL armed.
# ---------------------------------------------------------------------------


def test_require_brain_restores_exit_2(capsys):
    durable = _FakeDurable()
    kwargs, ns = _seams(url=None, durable=durable)
    driver = bm.BodyModeDriver(require_brain=True, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out
    assert rc == 2
    assert "Brain offline" in out and "(exit 2)" in out
    assert ns.dist.started_urls == [], "strict contract: no connect attempt"
    assert not durable.started, "strict exit precedes WAL arming"


def test_require_brain_restores_exit_3_on_gate_timeout(capsys, monkeypatch):
    """Symmetric strict pin (review round): WAL armed + --require-brain +
    a never-connecting client -> the connect-gate timeout still exits 3
    (no durable degrade)."""
    monkeypatch.setenv("JARVIS_BRAIN_CONNECT_GATE_S", "0.2")
    durable = _FakeDurable()
    kwargs, ns = _seams(connect=False, durable=durable)  # _client never appears
    driver = bm.BodyModeDriver(
        require_brain=True, inject_test_signals=1, duration_s=0.05, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out
    assert rc == 3
    assert "(exit 3)" in out
    assert ns.shim.envelopes == [], "no signals on a dark link"
    assert not ns.bridge.started, "bridge must not start on a dark link"
    # The WAL arms before the gate (journal-at-publish coverage) and is
    # disarmed on the exit-3 teardown path.
    assert durable.started and durable.stopped


def test_require_brain_cli_flag_parses():
    parser = bm.build_arg_parser()
    assert parser.parse_args(["--require-brain"]).require_brain is True
    assert parser.parse_args([]).require_brain is False


# ---------------------------------------------------------------------------
# (h) Task 4: edge-transition determinism. The degrade UI state derives
#     ONLY from the client's `connected` property (set/cleared at WS
#     establishment/teardown -- the transport closure), polled exactly
#     once per census tick. True->False->True across ticks -> exactly one
#     offline line + one reconnected line, no repeats while steady.
# ---------------------------------------------------------------------------


def test_edge_transitions_log_exactly_once_per_episode(capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_CONNECT_GATE_S", "2")
    monkeypatch.setenv("JARVIS_BODY_MODE_CENSUS_S", "0.01")
    durable = _FakeDurable(queued=3)
    # Read schedule: gate(True), initial(True), tick1(True: steady),
    # tick2(False: offline edge), tick3(False: steady -- no repeat),
    # tick4(True: reconnect edge), then holds True for remaining ticks.
    client = _ScriptedClient([True, True, True, False, False, True])
    kwargs, ns = _seams(durable=durable, client=client)
    driver = bm.BodyModeDriver(duration_s=0.25, **kwargs)
    rc = asyncio.run(driver.run())
    out = capsys.readouterr().out

    assert rc == 0
    assert out.count("Brain offline") == 1, (
        "exactly one offline line per episode: %r" % out)
    assert out.count("Brain reconnected") == 1, (
        "exactly one reconnected line per episode: %r" % out)
    # Both census states were surfaced during the run.
    assert "connected=False" in out and "connected=True" in out
