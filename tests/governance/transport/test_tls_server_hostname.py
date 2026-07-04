# -*- coding: utf-8 -*-
"""tls_server_hostname wiring: config field + client + discovery-probe passthrough.

Live-fire attempt-1 pre-flight finding (2026-07-04): the Brain server cert's SAN
is the DNS identity ``jarvis-brain`` (never an IP), but both the discovery
health-probe and the WS client connect to a raw IP -- with ``check_hostname``
enabled on the CA path the handshake can NEVER succeed unless the intended
identity is passed as ``server_hostname``. Default None = legacy behavior
(loopback tests connect by resolvable hostname and need no override).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.transport.transport_config import TransportConfig


def _clear_ws_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_BRAIN_WS_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# (a) config field: default None, env-overridable.
# --------------------------------------------------------------------------- #
def test_tls_server_hostname_defaults_none(monkeypatch):
    _clear_ws_env(monkeypatch)
    cfg = TransportConfig.from_env(role="client")
    assert cfg.tls_server_hostname is None


def test_tls_server_hostname_env_overridable(monkeypatch):
    _clear_ws_env(monkeypatch)
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_SERVER_HOSTNAME", "jarvis-brain")
    cfg = TransportConfig.from_env(role="client")
    assert cfg.tls_server_hostname == "jarvis-brain"


# --------------------------------------------------------------------------- #
# (b) BusBridgeClient passes server_hostname to ws_connect when set (and omits
#     it when unset -- legacy call shape preserved).
# --------------------------------------------------------------------------- #
class _CaptureSession:
    """Fake aiohttp session: capture ws_connect kwargs, then abort the connect."""

    def __init__(self, captured: Dict[str, Any]) -> None:
        self._captured = captured

    def ws_connect(self, url: str, **kwargs: Any):
        self._captured["url"] = url
        self._captured.update(kwargs)

        class _Ctx:
            async def __aenter__(self_inner):
                raise RuntimeError("capture-abort")

            async def __aexit__(self_inner, *exc: Any) -> bool:
                return False

        return _Ctx()

    async def close(self) -> None:
        return None


def _mk_cfg(monkeypatch, hostname: str | None) -> TransportConfig:
    _clear_ws_env(monkeypatch)
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    if hostname is not None:
        monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_SERVER_HOSTNAME", hostname)
    return TransportConfig.from_env(role="client")


def test_client_passes_server_hostname_when_set(monkeypatch):
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )
    from backend.core.ouroboros.governance.transport.bus_bridge_client import (
        BusBridgeClient,
    )

    captured: Dict[str, Any] = {}
    cfg = _mk_cfg(monkeypatch, "jarvis-brain")
    client = BusBridgeClient(
        StreamEventBroker(), cfg,
        url="wss://203.0.113.7:8443/ws/trinity-bus",
        session_factory=lambda: _CaptureSession(captured),
    )
    with pytest.raises(RuntimeError, match="capture-abort"):
        asyncio.get_event_loop().run_until_complete(client._connect_once())
    assert captured.get("server_hostname") == "jarvis-brain"


def test_client_omits_server_hostname_when_unset(monkeypatch):
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )
    from backend.core.ouroboros.governance.transport.bus_bridge_client import (
        BusBridgeClient,
    )

    captured: Dict[str, Any] = {}
    cfg = _mk_cfg(monkeypatch, None)
    client = BusBridgeClient(
        StreamEventBroker(), cfg,
        url="wss://203.0.113.7:8443/ws/trinity-bus",
        session_factory=lambda: _CaptureSession(captured),
    )
    with pytest.raises(RuntimeError, match="capture-abort"):
        asyncio.get_event_loop().run_until_complete(client._connect_once())
    assert "server_hostname" not in captured, "legacy call shape must be preserved"


# --------------------------------------------------------------------------- #
# (c) discovery probe passes server_hostname to open_connection when set.
# --------------------------------------------------------------------------- #
def test_probe_passes_server_hostname_when_set(monkeypatch):
    import backend.core.ouroboros.governance.brain_discovery as bd

    _clear_ws_env(monkeypatch)
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_SERVER_HOSTNAME", "jarvis-brain")
    captured: Dict[str, Any] = {}

    async def _fake_open_connection(**kwargs: Any):
        captured.update(kwargs)
        raise ConnectionRefusedError("capture-abort")

    monkeypatch.setattr(bd.asyncio, "open_connection", _fake_open_connection)
    ok = asyncio.get_event_loop().run_until_complete(
        bd._default_ws_health_probe("wss://203.0.113.7:8443/ws/trinity-bus")
    )
    assert ok is False  # fail-soft on refused connect
    assert captured.get("server_hostname") == "jarvis-brain"
    assert captured.get("host") == "203.0.113.7"


def test_probe_omits_server_hostname_when_unset(monkeypatch):
    import backend.core.ouroboros.governance.brain_discovery as bd

    _clear_ws_env(monkeypatch)
    captured: Dict[str, Any] = {}

    async def _fake_open_connection(**kwargs: Any):
        captured.update(kwargs)
        raise ConnectionRefusedError("capture-abort")

    monkeypatch.setattr(bd.asyncio, "open_connection", _fake_open_connection)
    ok = asyncio.get_event_loop().run_until_complete(
        bd._default_ws_health_probe("wss://203.0.113.7:8443/ws/trinity-bus")
    )
    assert ok is False
    assert "server_hostname" not in captured
