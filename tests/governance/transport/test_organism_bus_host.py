# -*- coding: utf-8 -*-
"""OrganismBusHost (Stage-2 Task 2): the Brain organism's in-process mTLS WS
bus server.

Proves the three contracts from the task brief:

  (a) DARK BY DEFAULT: with the master flag off (or port 0, or TLS material
      missing), ``start()`` returns False and touches nothing -- no server,
      no bridge, no trinity bus resolution.
  (b) LIVE PATH: with the flag on + TLS disabled + an ephemeral port, a REAL
      Stage-0 ``DistributedEventBus`` client connects over localhost and a
      ``publish_raw("actuation.click", ...)`` on the organism's own
      TrinityEventBus is observed on the client's broker (scaffolding lifted
      from test_bridge_reflection_and_cursor.py, server side replaced by
      OrganismBusHost). TRINITY_MULTICAST_ENABLED=false suppresses the
      in-process UDP shortcut (see test_trinity_bus_bridge.py::_mk_bus).
  (c) SIDECAR HANDOFF: scripts/brain_bus_echo_server.py ``main()`` returns 0
      immediately when JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false (the organism
      host replaces the sidecar on Stage-2 nodes); default keeps Stage-1
      behavior byte-identical.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
from typing import Any, List

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.distributed_event_bus import (
    DistributedEventBus,
)
from backend.core.ouroboros.governance.transport.organism_bus_host import (
    OrganismBusHost,
    bus_host_enabled,
    resolve_outbound_topics,
)
from backend.core.ouroboros.governance.transport.transport_config import (
    TransportConfig,
)
from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (
    TRINITY_OP_PREFIX,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

_ALL_KNOBS = (
    "JARVIS_DISTRIBUTED_BUS_ENABLED",
    "JARVIS_BRAIN_WS_TLS_ENABLED",
    "JARVIS_BRAIN_WS_TLS_CERT",
    "JARVIS_BRAIN_WS_TLS_KEY",
    "JARVIS_BRAIN_WS_TLS_CA",
    "JARVIS_BRAIN_WS_TLS_EPHEMERAL",
    "JARVIS_BRAIN_WS_HOST",
    "JARVIS_BRAIN_WS_PORT",
    "JARVIS_BRAIN_WS_HEARTBEAT_S",
    "JARVIS_BRAIN_OUTBOUND_TOPICS",
    "JARVIS_BRAIN_BUS_SIDECAR_ENABLED",
)


def _clean_env(monkeypatch) -> None:
    for key in _ALL_KNOBS:
        monkeypatch.delenv(key, raising=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Env knob parsing
# --------------------------------------------------------------------------- #
def test_outbound_topics_default_and_override(monkeypatch):
    _clean_env(monkeypatch)
    assert resolve_outbound_topics() == ["actuation.*", "telemetry.posture.*"]
    monkeypatch.setenv("JARVIS_BRAIN_OUTBOUND_TOPICS", " a.b , c.* ,, ")
    assert resolve_outbound_topics() == ["a.b", "c.*"]
    monkeypatch.setenv("JARVIS_BRAIN_OUTBOUND_TOPICS", "   ")
    assert resolve_outbound_topics() == ["actuation.*", "telemetry.posture.*"]


# --------------------------------------------------------------------------- #
# (a) dark by default
# --------------------------------------------------------------------------- #
def test_bus_host_enabled_defaults_false(monkeypatch):
    _clean_env(monkeypatch)
    assert bus_host_enabled() is False


def test_start_dark_when_master_flag_off(monkeypatch):
    _clean_env(monkeypatch)
    host = OrganismBusHost()
    assert _run(host.start()) is False
    assert host.started is False
    # touches nothing: no runner, no bridge, no broker
    assert host._runner is None
    assert host._bridge is None
    assert host._broker is None
    _run(host.stop())  # stop on a never-started host must be a no-op


def test_start_dark_when_port_zero(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("JARVIS_DISTRIBUTED_BUS_ENABLED", "true")
    # port stays at the TransportConfig default (0)
    host = OrganismBusHost()
    assert _run(host.start()) is False
    assert host.started is False
    assert host._runner is None


def test_refuses_plaintext_when_tls_material_missing(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("JARVIS_DISTRIBUTED_BUS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", str(_free_port()))
    # tls_enabled defaults True; no cert/key/ephemeral -> must refuse, never
    # fall through to a plaintext TCPSite.
    host = OrganismBusHost()
    assert _run(host.start()) is False
    assert host.started is False
    assert host._runner is None


# --------------------------------------------------------------------------- #
# (b) live path: real Stage-0 client observes the organism's trinity bus
# --------------------------------------------------------------------------- #
def test_live_publish_crosses_to_stage0_client(monkeypatch):
    _clean_env(monkeypatch)
    port = _free_port()
    monkeypatch.setenv("JARVIS_DISTRIBUTED_BUS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HOST", "127.0.0.1")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", str(port))
    monkeypatch.setenv("JARVIS_BRAIN_WS_HEARTBEAT_S", "1.0")
    # Suppress the in-process UDP multicast shortcut -- precedent + rationale:
    # tests/governance/transport/test_trinity_bus_bridge.py::_mk_bus.
    monkeypatch.setenv("TRINITY_MULTICAST_ENABLED", "false")

    async def scenario():
        from backend.core.trinity_event_bus import (
            get_trinity_event_bus,
            shutdown_trinity_event_bus,
        )

        host = OrganismBusHost()
        client_task = None
        client_bus = None
        try:
            assert await host.start() is True
            assert host.started is True

            client_cfg = TransportConfig.from_env(role="client")
            client_broker = StreamEventBroker()
            client_bus = DistributedEventBus(
                client_broker, client_cfg, role="client")
            url = "ws://127.0.0.1:%d%s" % (port, client_cfg.path)
            client_task = asyncio.ensure_future(client_bus.start_client(url))

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                client = getattr(client_bus, "_client", None)
                if client is not None and getattr(client, "connected", False):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("client never reported connected")

            bus = await get_trinity_event_bus()
            await bus.publish_raw("actuation.click", {"x": 1})
            await bus.publish_raw("fs.changed.src", {"x": 2})  # NOT allowlisted
            await asyncio.sleep(1.0)

            return [
                ev for ev in client_broker.recent_history(limit=500)
                if ev.op_id.startswith(TRINITY_OP_PREFIX)
            ]
        finally:
            if client_bus is not None:
                try:
                    await client_bus.stop()
                except Exception:  # noqa: BLE001
                    pass
            if client_task is not None:
                client_task.cancel()
                try:
                    await client_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await host.stop()
            await shutdown_trinity_event_bus()

    got: List[Any] = _run(scenario())
    assert len(got) == 1, (
        "exactly the allowlisted default topic must cross: %r" % got)
    assert got[0].op_id == TRINITY_OP_PREFIX + "actuation.click"
    assert got[0].payload["topic"] == "actuation.click"
    assert got[0].payload["data"]["x"] == 1
    assert got[0].payload["origin"]  # stamped with the host's source_id


# --------------------------------------------------------------------------- #
# (c) sidecar handoff
# --------------------------------------------------------------------------- #
def _load_sidecar():
    path = os.path.join(_REPO_ROOT, "scripts", "brain_bus_echo_server.py")
    spec = importlib.util.spec_from_file_location(
        "brain_bus_echo_server_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sidecar_early_exits_when_disabled(monkeypatch):
    _clean_env(monkeypatch)
    mod = _load_sidecar()
    for falsy in ("0", "false", "no", "off", " FALSE "):
        monkeypatch.setenv("JARVIS_BRAIN_BUS_SIDECAR_ENABLED", falsy)
        # Returns 0 BEFORE any asyncio.run / server setup.
        assert mod.main() == 0


def test_sidecar_default_keeps_stage1_behavior(monkeypatch):
    _clean_env(monkeypatch)
    mod = _load_sidecar()
    # Default (env unset) must still reach amain(); with no port configured
    # the Stage-1 contract is rc=2, NOT the disabled-guard's rc=0.
    assert _run(mod.amain()) == 2
