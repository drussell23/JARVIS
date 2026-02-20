"""Unit tests for Reactor<->Prime adaptive transport negotiation."""

from __future__ import annotations

import asyncio
import sys
import types

from backend.autonomy.reactor_core_integration import (
    PrimeNeuralMeshBridge,
    ReactorCoreConfig,
    ReactorCoreIntegration,
)


def _make_config(**overrides) -> ReactorCoreConfig:
    config = ReactorCoreConfig(
        prime_host="localhost",
        prime_port=8002,
        prime_port_candidates=[8002, 8001],
        prime_websocket_paths=["/ws/events", "/ws/alt"],
        prime_health_paths=["/health", "/healthz"],
        prime_event_poll_interval=0.01,
        prime_event_probe_timeout=0.1,
        prime_transport_reprobe_interval=0.1,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_prime_port_candidates_deduplicate_and_preserve_priority():
    integration = ReactorCoreIntegration(
        _make_config(
            prime_port=8002,
            prime_port_candidates=[8001, 8002, 8000, 8001],
        )
    )
    assert integration._prime_port_candidates() == [8002, 8001, 8000]


def test_prime_health_url_candidates_expand_ports_and_paths():
    integration = ReactorCoreIntegration(
        _make_config(
            prime_port=8002,
            prime_port_candidates=[8001],
            prime_health_paths=["/health", "/healthz"],
        )
    )
    candidates = integration._prime_health_url_candidates()
    assert candidates[0] == (8002, "/health", "http://localhost:8002/health")
    assert (8002, "/healthz", "http://localhost:8002/healthz") in candidates
    assert (8001, "/health", "http://localhost:8001/health") in candidates


def test_contract_error_detection():
    assert PrimeNeuralMeshBridge._is_endpoint_contract_error(RuntimeError("HTTP 404 Not Found"))
    assert PrimeNeuralMeshBridge._is_endpoint_contract_error(RuntimeError("server replied 403"))
    assert not PrimeNeuralMeshBridge._is_endpoint_contract_error(RuntimeError("connection reset by peer"))


def test_probe_timeout_adapts_to_observed_latency():
    bridge = PrimeNeuralMeshBridge(_make_config(prime_event_probe_timeout=1.0))
    assert bridge._probe_timeout_seconds() == 1.0
    bridge._observe_probe_latency(2.0)
    # Adaptive timeout should increase but stay bounded by clamp.
    assert 4.0 <= bridge._probe_timeout_seconds() <= 6.0


async def test_connect_prime_websocket_rotates_candidates_on_contract_error(monkeypatch):
    bridge = PrimeNeuralMeshBridge(
        _make_config(
            prime_port=8002,
            prime_port_candidates=[8002],
            prime_websocket_paths=["/ws/events", "/ws/alt"],
        )
    )

    attempts = []

    async def fake_connect(url: str, **_kwargs):
        attempts.append(url)
        if url.endswith("/ws/events"):
            raise RuntimeError("server rejected WebSocket connection: HTTP 404")
        return object()

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect))

    ws, selected_url = await bridge._connect_prime_websocket(timeout_seconds=0.2)
    assert ws is not None
    assert attempts[0].endswith("/ws/events")
    assert selected_url.endswith("/ws/alt")


class _BrokenPrimeConnector:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return None

    async def stream_events(self):
        if False:  # pragma: no cover
            yield None
        raise RuntimeError("websocket unavailable")


async def test_stream_prime_events_falls_back_to_health_poll(monkeypatch):
    integration = ReactorCoreIntegration(_make_config())
    integration._prime_connector = _BrokenPrimeConnector()

    call_count = 0

    async def fake_poll():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "event_type": "status_poll",
                "data": {"status": "starting"},
                "timestamp": "2026-02-20T00:00:00",
            }
        return None

    monkeypatch.setattr(integration, "_poll_prime_health_event", fake_poll)

    stream = integration.stream_prime_events()
    first_event = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert first_event["event_type"] == "status_poll"
    assert first_event["data"]["status"] == "starting"
    await stream.aclose()
