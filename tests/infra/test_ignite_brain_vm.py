"""Unit tests for scripts/ignite_brain_vm.py (Stage-1 Brain-VM ignition driver).

NO live GCP -- every collaborator (create_instance / discover / firewall / delete
/ list) is a fake injected via the driver's seams. The tests prove:

  (a) PROVISION: reuse an existing brain when persistent+running; provision new
      otherwise.
  (b) TEARDOWN fires on the normal ``finally`` AND on a simulated SIGTERM AND on
      an exception mid-run; the reaper is idempotent (called twice == one
      effective delete).
  (c) The VALIDATION EXCHANGE logic (the Stage-1 acceptance): bidirectional
      observe + Last-Event-ID reconnect-replay-exact against a fake in-proc bus,
      asserting zero gap / zero dup.
  (d) Lifecycle: non-persistent => VM deleted; persistent => VM left running,
      firewall still closed.
  (e) The post-teardown $0 verify detects a leaked resource (injected) and
      reports non-$0.

These exercise the driver's exchange + provision + teardown LOGIC only; the live
GCP provision/connect/teardown are proven in Task 5 (live-fire).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_DRIVER_PATH = os.path.join(_REPO_ROOT, "scripts", "ignite_brain_vm.py")


def _load_driver() -> Any:
    """Load the ignition driver as a module by path (script, not a package)."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    if "ignite_brain_vm" in sys.modules:
        return sys.modules["ignite_brain_vm"]
    spec = importlib.util.spec_from_file_location("ignite_brain_vm", _DRIVER_PATH)
    assert spec and spec.loader, "cannot load ignite_brain_vm"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ignite_brain_vm"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ign = _load_driver()


# ---------------------------------------------------------------------------
# Fake in-proc bidirectional bus wire for the exchange-logic tests.
# ---------------------------------------------------------------------------


class _FakeWire:
    """A shared monotonic event log linking two ``_FakeEndpoint``s. Publishing on
    one endpoint delivers to the other with a contiguous hex event_id. Supports a
    mid-stream sever + Last-Event-ID replay of exactly the missed contiguous span
    (zero gap, zero dup) -- the cross-host Stage-0 guarantee, in-proc."""

    def __init__(self) -> None:
        self._seq = 0
        self._log: List[Tuple[str, str, str]] = []  # (event_id, dst_name, op_id)
        self.connected = True

    def next_id(self) -> str:
        self._seq += 1
        return format(self._seq, "012x")


class _FakeEndpoint:
    def __init__(self, name: str, wire: _FakeWire, peer_name: str) -> None:
        self.name = name
        self._wire = wire
        self._peer_name = peer_name
        self._observed: List[Tuple[str, str]] = []  # (op_id, event_id)
        self._last_event_id: Optional[str] = None
        self._peer: Optional["_FakeEndpoint"] = None

    def bind_peer(self, peer: "_FakeEndpoint") -> None:
        self._peer = peer

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last_event_id

    def observed(self) -> List[Tuple[str, str]]:
        return list(self._observed)

    def publish(self, event_type: str, op_id: str, payload: Dict[str, Any]) -> str:
        event_id = self._wire.next_id()
        self._wire._log.append((event_id, self._peer_name, op_id))
        if self._wire.connected and self._peer is not None:
            self._peer._deliver(event_id, op_id)
        return event_id

    def _deliver(self, event_id: str, op_id: str) -> None:
        self._observed.append((op_id, event_id))
        self._last_event_id = event_id

    def replay_from(self, last_event_id: Optional[str]) -> None:
        """Deliver every logged event destined for THIS endpoint whose id is
        strictly greater than ``last_event_id`` -- exactly once, in order."""
        floor = int(last_event_id, 16) if last_event_id else 0
        for event_id, dst, op_id in self._wire._log:
            if dst != self.name:
                continue
            if int(event_id, 16) <= floor:
                continue
            already = any(eid == event_id for _op, eid in self._observed)
            if already:
                continue
            self._deliver(event_id, op_id)


def _make_wire_pair() -> Tuple[_FakeEndpoint, _FakeEndpoint, _FakeWire]:
    wire = _FakeWire()
    mac = _FakeEndpoint("mac", wire, peer_name="brain")
    brain = _FakeEndpoint("brain", wire, peer_name="mac")
    mac.bind_peer(brain)
    brain.bind_peer(mac)
    return mac, brain, wire


# ---------------------------------------------------------------------------
# (c) Validation-exchange logic: bidirectional + reconnect-replay-exact.
# ---------------------------------------------------------------------------


def test_exchange_bidirectional_observes_both_directions() -> None:
    mac, brain, _wire = _make_wire_pair()

    async def _reconnect() -> Any:
        return mac

    exchange = ign.ValidationExchange(mac=mac, brain=brain, reconnect=_reconnect,
                                      settle_s=0.0)
    result = asyncio.run(exchange.run_bidirectional(n=4))

    assert result["mac_to_brain"]["ok"] is True
    assert result["brain_to_mac"]["ok"] is True
    assert result["mac_to_brain"]["gap"] == [] and result["mac_to_brain"]["dup"] == []
    assert result["brain_to_mac"]["gap"] == [] and result["brain_to_mac"]["dup"] == []


def test_exchange_reconnect_replay_exact_zero_gap_zero_dup() -> None:
    """Replay is LOAD-BEARING here: the post span is published WHILE SEVERED
    (``connected=False`` -> buffered at the source, never delivered live), so the
    only way those events reach the Mac is the reconnect's Last-Event-ID replay.
    A no-op reconnect stub would leave a gap and fail (proven by the sibling
    negative control below)."""
    mac, brain, wire = _make_wire_pair()

    async def _sever() -> Any:
        wire.connected = False  # drop the link BEFORE the post span is published

    async def _reconnect() -> Any:
        # Stateless reconnect: replay exactly the contiguous span > last acked id
        # (what a real reconnect's ``hello`` Last-Event-ID frame does). The link
        # stays down for live delivery -- replay is the ONLY delivery route.
        mac.replay_from(mac.last_event_id)
        return mac

    exchange = ign.ValidationExchange(mac=mac, brain=brain, reconnect=_reconnect,
                                      sever=_sever, settle_s=0.0)
    result = asyncio.run(exchange.run_reconnect_replay(n=3))

    assert result["ok"] is True, result
    assert result["observed"] == 6, "replay must deliver all pre+post: %r" % (result,)
    assert result["gap"] == [], "replay left a gap: %r" % (result,)
    assert result["dup"] == [], "replay duplicated an event: %r" % (result,)


def test_exchange_reconnect_replay_detects_a_dropped_event() -> None:
    """Negative control -- proves replay is the delivery route: the post span is
    published WHILE SEVERED, and the reconnect does NOT replay. The buffered span
    is therefore never delivered -> the exchange MUST report a gap (a no-op replay
    stub cannot pass the positive test above)."""
    mac, brain, wire = _make_wire_pair()

    async def _sever() -> Any:
        wire.connected = False

    async def _reconnect() -> Any:
        wire.connected = True  # link back up, but NO replay -> severed span is lost
        return mac

    exchange = ign.ValidationExchange(mac=mac, brain=brain, reconnect=_reconnect,
                                      sever=_sever, settle_s=0.0)
    result = asyncio.run(exchange.run_reconnect_replay(n=3))
    assert result["ok"] is False
    assert result["gap"], "a dropped span must surface as a gap"


def test_gap_dup_dup_side_has_teeth() -> None:
    """The ``_gap_dup`` dup detector must flag a repeated event id (so a broken
    replay that double-delivers cannot masquerade as success)."""
    gap, dup = ign._gap_dup(["a", "b", "c"], ["a", "b", "b", "c"])
    assert gap == []
    assert dup == ["b"], "a repeated observed id must surface as a dup"


def test_replay_dedup_drops_reoffered_already_seen_span() -> None:
    """DUP negative control: re-offering an already-observed span (a redundant
    replay) MUST be dropped by the endpoint's high-water / already-seen dedup --
    zero duplicates surface. Proves the dedup logic has teeth, not just the
    detector."""
    mac, brain, _wire = _make_wire_pair()
    eid = brain.publish("task_started", "op-dup-1", {})  # delivered live to mac
    baseline = mac.observed()
    assert baseline == [("op-dup-1", eid)]
    # Re-offer the SAME logged span from the very start -> dedup drops it.
    mac.replay_from(None)
    mac.replay_from(None)
    assert mac.observed() == baseline, "re-offered already-seen events must be dropped"
    _gap, dup = ign._gap_dup([eid], [e for _op, e in mac.observed()])
    assert dup == [], "dedup must prevent any duplicate observation"


# ---------------------------------------------------------------------------
# (Fix 3) Loopback guard: mac IS brain -> proven gated False, loopback_only True.
# ---------------------------------------------------------------------------


class _LoopbackEndpoint:
    """A single endpoint that observes its OWN publishes -- mirrors the live
    ``_LiveMacEndpoint`` whose local broker fans published events back to its own
    subscription when no Brain echo subscriber exists. This is the exact 'proves
    nothing cross-host' trap Fix 3 guards against."""

    def __init__(self) -> None:
        self.name = "mac"
        self._observed: List[Tuple[str, str]] = []
        self._last: Optional[str] = None
        self._seq = 0
        self._log: List[Tuple[str, str]] = []
        self.connected = True

    def publish(self, event_type: str, op_id: str, payload: Dict[str, Any]) -> str:
        self._seq += 1
        eid = format(self._seq, "012x")
        self._log.append((eid, op_id))
        if self.connected:
            self._deliver(eid, op_id)
        return eid

    def _deliver(self, eid: str, op_id: str) -> None:
        self._observed.append((op_id, eid))
        self._last = eid

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last

    def observed(self) -> List[Tuple[str, str]]:
        return list(self._observed)

    def replay_from(self, last_event_id: Optional[str]) -> None:
        floor = int(last_event_id, 16) if last_event_id else 0
        for eid, op_id in self._log:
            if int(eid, 16) <= floor:
                continue
            if any(e == eid for _o, e in self._observed):
                continue
            self._deliver(eid, op_id)


def test_exchange_loopback_cannot_report_cross_host_proven() -> None:
    ep = _LoopbackEndpoint()

    async def _sever() -> Any:
        ep.connected = False

    async def _reconnect() -> Any:
        ep.replay_from(ep.last_event_id)
        return ep

    exchange = ign.ValidationExchange(mac=ep, brain=ep, reconnect=_reconnect,
                                      sever=_sever, settle_s=0.0)
    result = asyncio.run(exchange.run_all(n=2))
    assert result["loopback_only"] is True
    assert result["logic_ok"] is True, "self-observation still exercises the logic"
    assert result["proven"] is False, "loopback can NEVER report cross-host-proven"


def test_exchange_distinct_endpoints_are_not_loopback() -> None:
    """The real bidirectional path (distinct mac/brain) is NOT loopback and CAN
    report proven -- so the guard is specific to the same-endpoint trap."""
    mac, brain, wire = _make_wire_pair()

    async def _sever() -> Any:
        wire.connected = False

    async def _reconnect() -> Any:
        mac.replay_from(mac.last_event_id)
        return mac

    exchange = ign.ValidationExchange(mac=mac, brain=brain, reconnect=_reconnect,
                                      sever=_sever, settle_s=0.0)
    result = asyncio.run(exchange.run_all(n=2))
    assert result["loopback_only"] is False
    assert result["proven"] is True, "distinct cross-host endpoints prove acceptance"


# ---------------------------------------------------------------------------
# (Fix 2) mTLS is REQUIRED (not merely accepted): a certless client is rejected
# by the Brain WS server. Mirrors Stage-0 test_two_process_loopback.py's negative
# branch -- the SAME build_*_ssl_context builders brain_discovery wraps.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brain_ws_requires_mtls_certless_client_rejected() -> None:
    ssl = pytest.importorskip("ssl")
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from backend.core.ouroboros.governance.transport import transport_security as ts
    from backend.core.ouroboros.governance.transport.transport_config import (
        TransportConfig,
    )
    from backend.core.ouroboros.governance.transport.bus_bridge_server import (
        BusBridgeServer,
    )
    from backend.core.ouroboros.governance.transport.transport_security import (
        build_server_ssl_context,
        build_client_ssl_context,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )

    # Shared ephemeral material stands in for a shared CA on loopback (same shape
    # brain_discovery.build_brain_{client,server}_ssl_context resolve from env).
    cert, key = ts.generate_ephemeral_material()
    cfg = TransportConfig(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=True, tls_cert=cert, tls_key=key, tls_ca=cert,
        tls_ephemeral=False, source_id="brain",
    )

    broker = StreamEventBroker(history_maxlen=100)
    app = web.Application()
    BusBridgeServer(broker, cfg).register_routes(app)
    tserver = TestServer(app)
    await tserver.start_server(ssl=build_server_ssl_context(cfg))
    host, port = tserver.host, tserver.port
    ws_url = "wss://%s:%d/ws/trinity-bus" % (host, port)
    try:
        # (a) A proper mTLS client (our cert) is ACCEPTED.
        client_ssl = build_client_ssl_context(cfg)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url, ssl=client_ssl) as ws:
                assert not ws.closed

        # (b) A CERTLESS client (CERT_NONE, no client cert loaded) is REJECTED --
        #     the server's CERT_REQUIRED expectation makes mTLS REQUIRED, not
        #     merely accepted. This is the required-rejection proof Fix 2 demands.
        with pytest.raises((aiohttp.ClientConnectorError, ssl.SSLError,
                            aiohttp.ClientError, ConnectionResetError,
                            aiohttp.WSServerHandshakeError)):
            no_cert = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            no_cert.check_hostname = False
            no_cert.verify_mode = ssl.CERT_NONE
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(ws_url, ssl=no_cert) as ws:
                    await ws.receive()
    finally:
        await tserver.close()


# ---------------------------------------------------------------------------
# Fake GCP collaborators for the provision + teardown tests.
# ---------------------------------------------------------------------------


class _FakeGCP:
    def __init__(self, *, existing_url: Optional[str] = None,
                 leaked_instances: Optional[List[Dict[str, Any]]] = None,
                 firewall_present: bool = False) -> None:
        self.create_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[Tuple[str, Optional[str]]] = []
        self.open_fw_calls = 0
        self.close_fw_calls = 0
        self._existing_url = existing_url
        self._leaked_instances = leaked_instances or []
        self._firewall_present = firewall_present
        self.discover_count = 0

    async def create_instance(self, **kwargs: Any) -> Tuple[bool, str]:
        self.create_calls.append(kwargs)
        return (True, "created:done")

    async def discover(self, **kwargs: Any) -> Optional[str]:
        self.discover_count += 1
        # After a create, discovery resolves the freshly-provisioned URL.
        if self._existing_url:
            return self._existing_url
        if self.create_calls:
            return "wss://10.0.0.9:8443/ws/trinity-bus"
        return None

    async def open_firewall(self, *args: Any, **kwargs: Any) -> Tuple[bool, str]:
        self.open_fw_calls += 1
        return (True, "created:200")

    async def close_firewall(self, *args: Any, **kwargs: Any) -> Tuple[bool, str]:
        self.close_fw_calls += 1
        self._firewall_present = False
        return (True, "deleted:404")

    async def delete_instance(self, name: Optional[str] = None,
                              *, zone: Optional[str] = None) -> Tuple[bool, str]:
        self.delete_calls.append((name or "", zone))
        self._leaked_instances = []
        return (True, "deleted:200")

    async def list_brain(self) -> List[Dict[str, Any]]:
        return list(self._leaked_instances)

    async def firewall_exists(self, rule_name: str) -> bool:
        return self._firewall_present


def _driver(gcp: _FakeGCP, *, persistent: bool,
            exchange_result: Optional[Dict[str, Any]] = None,
            exchange_raises: bool = False) -> Any:
    async def _run_exchange() -> Dict[str, Any]:
        if exchange_raises:
            raise RuntimeError("boom mid-exchange")
        return exchange_result or {"proven": True}

    return ign.BrainIgnitionDriver(
        persistent=persistent,
        create_instance_fn=gcp.create_instance,
        discover_fn=gcp.discover,
        open_firewall_fn=gcp.open_firewall,
        close_firewall_fn=gcp.close_firewall,
        delete_instance_fn=gcp.delete_instance,
        list_brain_fn=gcp.list_brain,
        firewall_exists_fn=gcp.firewall_exists,
        run_exchange_fn=_run_exchange,
        node_name="jarvis-brain-test",
    )


def _reset_registry() -> None:
    ign._ACTIVE_BRAIN_RESOURCES.clear()


# ---------------------------------------------------------------------------
# (a) PROVISION: reuse when persistent+running; provision new otherwise.
# ---------------------------------------------------------------------------


def test_provision_reuses_existing_when_persistent_and_running() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url="wss://10.0.0.5:8443/ws/trinity-bus")
    drv = _driver(gcp, persistent=True)
    rc = asyncio.run(drv.run())
    assert rc == 0
    assert gcp.create_calls == [], "must REUSE the running brain, not provision"


def test_provision_creates_new_when_none_exists() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url=None)
    drv = _driver(gcp, persistent=False)
    rc = asyncio.run(drv.run())
    assert rc == 0
    assert len(gcp.create_calls) == 1, "must provision a fresh brain"


# ---------------------------------------------------------------------------
# (d) Lifecycle: non-persistent => VM deleted; persistent => VM left running,
#     firewall still closed.
# ---------------------------------------------------------------------------


def test_non_persistent_deletes_vm_and_closes_firewall() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url=None)
    drv = _driver(gcp, persistent=False)
    asyncio.run(drv.run())
    assert gcp.delete_calls, "non-persistent run must delete the VM"
    assert gcp.close_fw_calls >= 1, "firewall must be closed"


def test_persistent_leaves_vm_but_still_closes_firewall() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url="wss://10.0.0.5:8443/ws/trinity-bus")
    drv = _driver(gcp, persistent=True)
    asyncio.run(drv.run())
    assert gcp.delete_calls == [], "persistent run must LEAVE the VM running"
    assert gcp.close_fw_calls >= 1, "firewall must be closed even when persistent"


# ---------------------------------------------------------------------------
# (b) TEARDOWN on finally / SIGTERM / exception; idempotent reaper.
# ---------------------------------------------------------------------------


def test_teardown_fires_on_exception_midrun() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url=None)
    drv = _driver(gcp, persistent=False, exchange_raises=True)
    rc = asyncio.run(drv.run())
    assert rc != 0, "a mid-run exception must surface as a nonzero rc"
    assert gcp.close_fw_calls >= 1, "firewall reaped even on exception"
    assert gcp.delete_calls, "VM reaped even on exception"


def test_reaper_is_idempotent_second_call_is_noop() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url=None)
    ign._register_brain_resource(node="jarvis-brain-test", fw_rule="jarvis-brain-mtls",
                                 persistent=False)
    asyncio.run(ign._reap_brain_resources_async(
        close_firewall_fn=gcp.close_firewall,
        delete_instance_fn=gcp.delete_instance,
    ))
    first_deletes = len(gcp.delete_calls)
    first_closes = gcp.close_fw_calls
    assert first_deletes >= 1 and first_closes >= 1
    # Second reap: registry is drained -> no additional effect.
    asyncio.run(ign._reap_brain_resources_async(
        close_firewall_fn=gcp.close_firewall,
        delete_instance_fn=gcp.delete_instance,
    ))
    assert len(gcp.delete_calls) == first_deletes, "second reap must be a no-op"
    assert gcp.close_fw_calls == first_closes, "second reap must be a no-op"


def test_signal_handler_reaps_registered_resources() -> None:
    _reset_registry()
    gcp = _FakeGCP(existing_url=None)
    ign._register_brain_resource(node="jarvis-brain-test", fw_rule="jarvis-brain-mtls",
                                 persistent=False)
    # Point the module reaper at our fakes, then fire the SIGTERM handler body.
    ign._set_reap_collaborators(close_firewall_fn=gcp.close_firewall,
                                delete_instance_fn=gcp.delete_instance)
    try:
        ign._reap_brain_resources()  # sync wrapper the signal handler calls
    finally:
        ign._set_reap_collaborators(close_firewall_fn=None, delete_instance_fn=None)
    assert gcp.close_fw_calls >= 1
    assert gcp.delete_calls, "SIGTERM-path reap must delete the VM"
    assert ign._ACTIVE_BRAIN_RESOURCES == [], "registry drained after reap"


# ---------------------------------------------------------------------------
# (e) Post-teardown $0 verify detects a leaked resource.
# ---------------------------------------------------------------------------


def test_teardown_verify_zero_cost_when_clean() -> None:
    gcp = _FakeGCP(existing_url=None, leaked_instances=[], firewall_present=False)
    verdict = asyncio.run(ign._teardown_verify(
        persistent=False,
        list_brain_fn=gcp.list_brain,
        firewall_exists_fn=gcp.firewall_exists,
        rule_name="jarvis-brain-mtls",
    ))
    assert verdict["zero_cost"] is True
    assert verdict["leaked"] == []


def test_teardown_verify_flags_leaked_instance_and_firewall() -> None:
    gcp = _FakeGCP(
        existing_url=None,
        leaked_instances=[{"name": "jarvis-brain-test", "status": "RUNNING"}],
        firewall_present=True,
    )
    verdict = asyncio.run(ign._teardown_verify(
        persistent=False,
        list_brain_fn=gcp.list_brain,
        firewall_exists_fn=gcp.firewall_exists,
        rule_name="jarvis-brain-mtls",
    ))
    assert verdict["zero_cost"] is False
    assert any("instance:" in x for x in verdict["leaked"])
    assert any("firewall:" in x for x in verdict["leaked"])


def test_teardown_verify_ignores_instance_leak_when_persistent() -> None:
    """A persistent run INTENDS to leave the VM running -- verify must not flag
    the instance, but MUST still flag a leaked firewall."""
    gcp = _FakeGCP(
        existing_url=None,
        leaked_instances=[{"name": "jarvis-brain-test", "status": "RUNNING"}],
        firewall_present=True,
    )
    verdict = asyncio.run(ign._teardown_verify(
        persistent=True,
        list_brain_fn=gcp.list_brain,
        firewall_exists_fn=gcp.firewall_exists,
        rule_name="jarvis-brain-mtls",
    ))
    assert any("firewall:" in x for x in verdict["leaked"])
    assert not any("instance:" in x for x in verdict["leaked"])
    assert verdict["zero_cost"] is False  # firewall still leaked
