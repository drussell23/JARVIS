from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Optional


def _env_str(key: str, default: Optional[str]) -> Optional[str]:
    raw = os.environ.get(key)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "")
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "")
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def distributed_bus_enabled() -> bool:
    """Master switch. Default False -- Stage 0 ships dark."""
    return _env_bool("JARVIS_DISTRIBUTED_BUS_ENABLED", False)


@dataclass(frozen=True)
class TransportConfig:
    """Fully env-resolved transport configuration. No baked thresholds."""

    host: Optional[str]
    port: int
    path: str
    heartbeat_s: float
    reconnect_base_s: float
    reconnect_max_s: float
    reconnect_jitter: float
    queue_maxsize: int
    history_maxlen: int
    degrade_after_missed_hb: int
    tls_enabled: bool
    tls_cert: Optional[str]
    tls_key: Optional[str]
    tls_ca: Optional[str]
    tls_ephemeral: bool
    source_id: str
    # The DNS identity to verify the server cert against (its SAN), for clients
    # that dial a raw IP (GCE discovery). None = verify against the dialed host
    # (legacy loopback behavior).
    tls_server_hostname: Optional[str] = None

    @classmethod
    def from_env(cls, *, role: str) -> "TransportConfig":
        default_source = _env_str(
            "JARVIS_BRAIN_WS_SOURCE_ID",
            f"{role}-{socket.gethostname()}",
        )
        return cls(
            host=_env_str("JARVIS_BRAIN_WS_HOST", None),
            port=_env_int("JARVIS_BRAIN_WS_PORT", 0),
            path=_env_str("JARVIS_BRAIN_WS_PATH", "/ws/trinity-bus") or "/ws/trinity-bus",
            heartbeat_s=_env_float("JARVIS_BRAIN_WS_HEARTBEAT_S", 15.0),
            reconnect_base_s=_env_float("JARVIS_BRAIN_WS_RECONNECT_BASE_S", 0.5),
            reconnect_max_s=_env_float("JARVIS_BRAIN_WS_RECONNECT_MAX_S", 30.0),
            reconnect_jitter=_env_float("JARVIS_BRAIN_WS_RECONNECT_JITTER", 0.3),
            queue_maxsize=_env_int("JARVIS_BRAIN_WS_QUEUE_MAXSIZE", 256),
            history_maxlen=_env_int("JARVIS_BRAIN_WS_HISTORY_MAXLEN", 1024),
            degrade_after_missed_hb=_env_int("JARVIS_BRAIN_WS_DEGRADE_AFTER_MISSED_HB", 2),
            tls_enabled=_env_bool("JARVIS_BRAIN_WS_TLS_ENABLED", True),
            tls_cert=_env_str("JARVIS_BRAIN_WS_TLS_CERT", None),
            tls_key=_env_str("JARVIS_BRAIN_WS_TLS_KEY", None),
            tls_ca=_env_str("JARVIS_BRAIN_WS_TLS_CA", None),
            tls_ephemeral=_env_bool("JARVIS_BRAIN_WS_TLS_EPHEMERAL", False),
            source_id=default_source or f"{role}-unknown",
            tls_server_hostname=_env_str("JARVIS_BRAIN_WS_TLS_SERVER_HOSTNAME", None),
        )
