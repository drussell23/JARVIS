from __future__ import annotations

import os

from backend.core.ouroboros.governance.transport.transport_config import (
    TransportConfig,
    distributed_bus_enabled,
)


def test_defaults_are_dark_and_sane(monkeypatch):
    # Clear every knob so we observe pure defaults.
    for key in list(os.environ):
        if key.startswith("JARVIS_BRAIN_WS_") or key == "JARVIS_DISTRIBUTED_BUS_ENABLED":
            monkeypatch.delenv(key, raising=False)
    assert distributed_bus_enabled() is False  # ships dark
    cfg = TransportConfig.from_env(role="client")
    assert cfg.host is None  # no baked endpoint -- must be discovered
    assert cfg.port == 0  # ephemeral by default
    assert cfg.path == "/ws/trinity-bus"
    assert cfg.heartbeat_s == 15.0
    assert cfg.reconnect_base_s == 0.5
    assert cfg.reconnect_max_s == 30.0
    assert 0.0 <= cfg.reconnect_jitter <= 1.0
    assert cfg.queue_maxsize == 256
    assert cfg.history_maxlen == 1024
    assert cfg.degrade_after_missed_hb == 2
    assert cfg.tls_enabled is True
    assert cfg.tls_ephemeral is False
    assert cfg.source_id.startswith("client-")


def test_every_threshold_is_env_overridable(monkeypatch):
    monkeypatch.setenv("JARVIS_DISTRIBUTED_BUS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HOST", "10.1.2.3")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PATH", "/ws/x")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HEARTBEAT_S", "3.5")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_BASE_S", "0.1")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_MAX_S", "9.0")
    monkeypatch.setenv("JARVIS_BRAIN_WS_RECONNECT_JITTER", "0.5")
    monkeypatch.setenv("JARVIS_BRAIN_WS_QUEUE_MAXSIZE", "512")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HISTORY_MAXLEN", "2048")
    monkeypatch.setenv("JARVIS_BRAIN_WS_DEGRADE_AFTER_MISSED_HB", "4")
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_EPHEMERAL", "true")
    monkeypatch.setenv("JARVIS_BRAIN_WS_SOURCE_ID", "brain-01")
    assert distributed_bus_enabled() is True
    cfg = TransportConfig.from_env(role="server")
    assert (cfg.host, cfg.port, cfg.path) == ("10.1.2.3", 8443, "/ws/x")
    assert cfg.heartbeat_s == 3.5
    assert (cfg.reconnect_base_s, cfg.reconnect_max_s, cfg.reconnect_jitter) == (0.1, 9.0, 0.5)
    assert cfg.queue_maxsize == 512
    assert cfg.history_maxlen == 2048
    assert cfg.degrade_after_missed_hb == 4
    assert cfg.tls_enabled is False
    assert cfg.tls_ephemeral is True
    assert cfg.source_id == "brain-01"


def test_malformed_numeric_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_HEARTBEAT_S", "not-a-number")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "")
    cfg = TransportConfig.from_env(role="client")
    assert cfg.heartbeat_s == 15.0  # bad value -> default, never crash
    assert cfg.port == 0
