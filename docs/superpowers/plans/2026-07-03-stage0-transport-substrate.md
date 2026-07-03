# Stage 0 — Transport Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bidirectional WebSocket bridge that extends the in-process `TrinityEventBus`/`StreamEventBroker` across a socket, so `publish()` on one host reaches subscribers on the other — with exact Last-Event-ID replay, heartbeat, drop-oldest backpressure, mTLS with dynamically-resolved material, and a static AST invariant that structurally forbids real-time actuation layers from importing the remote transport.

**Architecture:** A new focused subpackage `backend/core/ouroboros/governance/transport/` wraps the *existing* `StreamEventBroker` (never reimplements it). A server component mounts an aiohttp `WebSocketResponse` route (mountable on the existing `EventChannelServer` app); a client component connects with exp-backoff+jitter reconnect and resumes via Last-Event-ID. A frame codec serializes `StreamEvent` to/from JSON with source-qualified IDs for cross-host dedup. All thresholds and all cryptographic material resolve dynamically from environment config at boot — zero baked constants. Stage 0 is loopback-only (real TCP over `127.0.0.1`, plus one two-OS-process smoke test); NO VM provisioning and NO sensor migration.

**Tech Stack:** Python 3.9+, `asyncio` (no `asyncio.timeout`; use `asyncio.wait_for`), `aiohttp` (already a dependency — WS server `web.WebSocketResponse` + client `ClientSession.ws_connect`), stdlib `ssl` for mTLS, stdlib `ast` for the invariant, `pytest`/`pytest-asyncio` for tests. Reuses `StreamEventBroker`/`IDEStreamRouter` (`ide_observability_stream.py`), `EventChannelServer` (`event_channel.py`), `TrinityEventBus`/`LifecycleEventPublisher` (`lifecycle_event_orchestrator.py`), and the `shipped_code_invariants` AST-check registry (`meta/shipped_code_invariants.py`).

## Global Constraints

- **Python 3.9+ only** — never `asyncio.timeout` (3.11+); use `asyncio.wait_for` everywhere. Every new module starts with `from __future__ import annotations`.
- **No hardcoded endpoints, IPs, ports, or cryptographic paths** — every network address and every TLS material path resolves from `JARVIS_*` environment variables at call time. A grep/AST invariant enforces this (Task 8). Loopback tests pass `127.0.0.1`/ephemeral port **via env or fixture**, never as a module constant.
- **Every threshold is env-resolved with a sensible default** — heartbeat cadence, reconnect base/max/jitter, queue maxsize, history maxlen, degraded-mode miss count, TLS on/off. Resolve at call time (read `os.environ` inside a function), never at import time, so tests can monkeypatch.
- **Reuse `StreamEventBroker`, never reimplement it** — the bridge owns a broker instance and calls its public `publish` / `subscribe` / `stream_iter` / `_seed_replay`-backed replay. Bounded history, drop-oldest `stream_lag`, and heartbeat come from the broker, not new code.
- **Master switch `JARVIS_DISTRIBUTED_BUS_ENABLED` (default `false`)** — Stage 0 ships dark. No live loop wires the bridge in until Stage 2. Nothing in this plan mutates the running organism's boot path.
- **Defensive contract on hot hooks** — publish-side and frame-ingest hooks NEVER raise and NEVER block the event loop (mirror `StreamEventBroker.publish`'s "never raises, never blocks" contract).
- **ASCII-only source** (Iron Gate ASCII-strictness applies to shipped code).
- **mTLS material is dynamically resolved** — cert/key/CA come from env-resolved paths; loopback tests use an in-memory ephemeral self-signed pair generated at runtime, never a checked-in cert. The ephemeral `/32` cloud firewall rule is **Stage 1** (cross-host) and explicitly out of Stage 0 scope; the transport is mTLS-*capable* from Stage 0 so Stage 1 only adds the firewall, not the crypto.
- **Real-time actuation isolation invariant** — `backend/ghost_hands/**` and any real-time actuation module MUST NOT import the transport subpackage. Enforced by an AST invariant (Task 8), not by convention.

---

## File Structure

**New subpackage** `backend/core/ouroboros/governance/transport/`:
- `__init__.py` — exports the public surface (`DistributedEventBus`, `BusBridgeServer`, `BusBridgeClient`, `TransportConfig`, `build_client_ssl_context`, `build_server_ssl_context`).
- `transport_config.py` — env-resolved config dataclass; zero baked thresholds.
- `transport_security.py` — mTLS SSL-context builders + ephemeral self-signed material for loopback.
- `bus_frame.py` — wire frame codec (`BusFrame`: `hello`/`event`/`heartbeat`/`ack`), source-qualified IDs, `StreamEvent` <-> JSON.
- `bus_bridge_server.py` — `BusBridgeServer`: aiohttp WS route over a `StreamEventBroker`; Last-Event-ID replay on connect; inbound-frame dedup + republish.
- `bus_bridge_client.py` — `BusBridgeClient`: `ws_connect` with exp-backoff+jitter reconnect, heartbeat-miss degraded flip, contiguous-id tracking + Last-Event-ID resume.
- `distributed_event_bus.py` — `DistributedEventBus`: the `publish()`-here-subscribers-there seam tying a broker to either a server or a client transport.

**Modified:**
- `backend/core/ouroboros/governance/meta/shipped_code_invariants.py` — add `_validate_realtime_actuation_no_remote_transport` + `_validate_transport_no_hardcoded_endpoints`, register both in `_register_seed_invariants`.

**Tests** under `tests/governance/transport/`:
- `test_transport_config.py`, `test_transport_security.py`, `test_bus_frame.py`, `test_bus_bridge_server.py`, `test_bus_bridge_client.py`, `test_distributed_event_bus.py`, `test_hostile_network_replay.py`, `test_two_process_loopback.py`
- `hostile_network.py` — reusable fault-injection proxy fixture (latency/jitter/reorder/partial-drop).
- `tests/governance/test_transport_invariants.py` — the two AST invariants.

---

## Task 1: Transport Config (env-resolved, zero baked thresholds)

**Files:**
- Create: `backend/core/ouroboros/governance/transport/__init__.py`
- Create: `backend/core/ouroboros/governance/transport/transport_config.py`
- Test: `tests/governance/transport/test_transport_config.py`

**Interfaces:**
- Produces:
  - `distributed_bus_enabled() -> bool` — reads `JARVIS_DISTRIBUTED_BUS_ENABLED` (default `False`).
  - `@dataclass(frozen=True) class TransportConfig` with fields: `host: Optional[str]`, `port: int`, `path: str`, `heartbeat_s: float`, `reconnect_base_s: float`, `reconnect_max_s: float`, `reconnect_jitter: float`, `queue_maxsize: int`, `history_maxlen: int`, `degrade_after_missed_hb: int`, `tls_enabled: bool`, `tls_cert: Optional[str]`, `tls_key: Optional[str]`, `tls_ca: Optional[str]`, `tls_ephemeral: bool`, `source_id: str`.
  - `TransportConfig.from_env(*, role: str) -> TransportConfig` — classmethod resolving every field from `os.environ` at call time. `role` is `"server"` or `"client"`; `source_id` defaults to `f"{role}-{socket.gethostname()}"` when `JARVIS_BRAIN_WS_SOURCE_ID` is unset.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/transport/test_transport_config.py
from __future__ import annotations

import os

import pytest

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
    assert cfg.host is None  # no baked endpoint — must be discovered
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/transport/test_transport_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named '...transport.transport_config'`

- [ ] **Step 3: Create the package init**

```python
# backend/core/ouroboros/governance/transport/__init__.py
"""Distributed TrinityEventBus transport substrate (Stage 0).

Extends the in-process StreamEventBroker across a WebSocket so
publish() on one host reaches subscribers on the other. Ships dark
(JARVIS_DISTRIBUTED_BUS_ENABLED default false) until Stage 2 wires it
into the live loop.
"""
from __future__ import annotations

__all__ = [
    "TransportConfig",
    "distributed_bus_enabled",
]

from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: E402
    TransportConfig,
    distributed_bus_enabled,
)
```

- [ ] **Step 4: Implement the config resolver**

```python
# backend/core/ouroboros/governance/transport/transport_config.py
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
    """Master switch. Default False — Stage 0 ships dark."""
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
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/transport/test_transport_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/transport/__init__.py backend/core/ouroboros/governance/transport/transport_config.py tests/governance/transport/test_transport_config.py
git commit -m "feat(transport): env-resolved TransportConfig for distributed bus (Stage 0, dark)"
```

---

## Task 2: mTLS material resolver (dynamic material, ephemeral for loopback)

**Files:**
- Create: `backend/core/ouroboros/governance/transport/transport_security.py`
- Test: `tests/governance/transport/test_transport_security.py`

**Interfaces:**
- Consumes: `TransportConfig` (Task 1).
- Produces:
  - `generate_ephemeral_material() -> Tuple[str, str]` — returns `(cert_pem_path, key_pem_path)` for a freshly generated in-memory self-signed pair written to a `tempfile` dir (loopback tests only). Uses stdlib only where possible; if `cryptography` is unavailable, raises `EphemeralMaterialUnavailable`.
  - `build_server_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]` — returns `None` when `cfg.tls_enabled is False`; otherwise a `PROTOCOL_TLS_SERVER` context loading cert/key from `cfg` (or ephemeral when `cfg.tls_ephemeral`), with `verify_mode = CERT_REQUIRED` and the CA loaded when `cfg.tls_ca` is set (mutual auth).
  - `build_client_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]` — mirror for the client (`PROTOCOL_TLS_CLIENT`), loading the client cert/key and trusting the CA.
  - `class EphemeralMaterialUnavailable(RuntimeError)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/transport/test_transport_security.py
from __future__ import annotations

import ssl

import pytest

from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport import transport_security as ts


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=15.0,
        reconnect_base_s=0.5, reconnect_max_s=30.0, reconnect_jitter=0.3,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=True, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=True, source_id="test",
    )
    base.update(over)
    return TransportConfig(**base)


def test_tls_disabled_returns_none():
    cfg = _cfg(tls_enabled=False, tls_ephemeral=False)
    assert ts.build_server_ssl_context(cfg) is None
    assert ts.build_client_ssl_context(cfg) is None


def test_ephemeral_material_builds_a_real_server_context():
    cfg = _cfg()
    ctx = ts.build_server_ssl_context(cfg)
    assert isinstance(ctx, ssl.SSLContext)
    # mTLS: server demands a client cert.
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_no_hardcoded_material_paths_in_module_source():
    import inspect
    src = inspect.getsource(ts)
    # No absolute cert/key paths baked in.
    assert "/etc/ssl" not in src
    assert "-----BEGIN" not in src  # no inlined PEM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/transport/test_transport_security.py -v`
Expected: FAIL — `ModuleNotFoundError: ...transport_security`

- [ ] **Step 3: Implement the security module**

```python
# backend/core/ouroboros/governance/transport/transport_security.py
from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import tempfile
from typing import Optional, Tuple

from backend.core.ouroboros.governance.transport.transport_config import TransportConfig


class EphemeralMaterialUnavailable(RuntimeError):
    """Raised when ephemeral self-signed material is requested but the
    `cryptography` package is not installed. Loopback-test-only path."""


def generate_ephemeral_material() -> Tuple[str, str]:
    """Generate an in-memory self-signed cert/key pair for loopback
    tests. Returns (cert_path, key_path) under a fresh temp dir.

    NOT for production — production resolves cert/key/CA from
    TransportConfig paths (which come from env / the golden-image
    metadata pattern in Stage 1).
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise EphemeralMaterialUnavailable(str(exc)) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    tmpdir = tempfile.mkdtemp(prefix="jarvis-ws-tls-")
    cert_path = os.path.join(tmpdir, "cert.pem")
    key_path = os.path.join(tmpdir, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


def _resolve_material(cfg: TransportConfig) -> Tuple[str, str, Optional[str]]:
    """Return (cert_path, key_path, ca_path). Ephemeral when requested;
    otherwise the env-resolved paths from cfg."""
    if cfg.tls_ephemeral:
        cert, key = generate_ephemeral_material()
        return cert, key, cfg.tls_ca
    if not cfg.tls_cert or not cfg.tls_key:
        raise EphemeralMaterialUnavailable(
            "tls_enabled but no cert/key resolved from env "
            "(JARVIS_BRAIN_WS_TLS_CERT / _KEY) and tls_ephemeral is false"
        )
    return cfg.tls_cert, cfg.tls_key, cfg.tls_ca


def build_server_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]:
    if not cfg.tls_enabled:
        return None
    cert, key, ca = _resolve_material(cfg)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS
    if ca:
        ctx.load_verify_locations(cafile=ca)
    elif cfg.tls_ephemeral:
        # Loopback: trust our own ephemeral cert as the peer CA so the
        # two ends of a self-signed handshake verify each other.
        ctx.load_verify_locations(cafile=cert)
    return ctx


def build_client_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]:
    if not cfg.tls_enabled:
        return None
    cert, key, ca = _resolve_material(cfg)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    if ca:
        ctx.load_verify_locations(cafile=ca)
        ctx.check_hostname = True
    elif cfg.tls_ephemeral:
        ctx.load_verify_locations(cafile=cert)
        ctx.check_hostname = False  # self-signed loopback
    return ctx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/transport/test_transport_security.py -v`
Expected: PASS (3 passed). If `cryptography` is not installed, `test_ephemeral_material_builds_a_real_server_context` errors with `EphemeralMaterialUnavailable` — install with `python3 -m pip install cryptography` (it is already a transitive dependency of the repo's auth stack) and re-run.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transport/transport_security.py tests/governance/transport/test_transport_security.py
git commit -m "feat(transport): mTLS context builders with dynamic + ephemeral material (Stage 0)"
```

---

## Task 3: Wire frame codec (source-qualified IDs, StreamEvent round-trip)

**Files:**
- Create: `backend/core/ouroboros/governance/transport/bus_frame.py`
- Test: `tests/governance/transport/test_bus_frame.py`

**Interfaces:**
- Consumes: `StreamEvent` from `ide_observability_stream.py` (fields: `event_id`, `event_type`, `op_id`, `timestamp`, `payload`, `schema_version`; has `.to_dict()`).
- Produces:
  - `FRAME_HELLO = "hello"`, `FRAME_EVENT = "event"`, `FRAME_HEARTBEAT = "heartbeat"`, `FRAME_ACK = "ack"` (str constants).
  - `@dataclass(frozen=True) class BusFrame` with `kind: str`, `source_id: str`, `seq: int` (source-native monotonic sequence; for events it is `int(event_id, 16)`), `last_event_id: Optional[str]`, `event: Optional[Dict[str, Any]]` (a `StreamEvent.to_dict()` payload), `ts: str`.
  - `BusFrame.encode() -> bytes` — UTF-8 JSON.
  - `BusFrame.decode(raw: Union[str, bytes]) -> Optional["BusFrame"]` — returns `None` on malformed input (never raises).
  - `event_frame(ev: StreamEvent, source_id: str) -> BusFrame`.
  - `hello_frame(source_id: str, last_event_id: Optional[str]) -> BusFrame`.
  - `heartbeat_frame(source_id: str) -> BusFrame`.
  - `ack_frame(source_id: str, last_event_id: str) -> BusFrame`.
  - `qualified_id(source_id: str, event_id: str) -> str` — returns `f"{source_id}:{event_id}"` (cross-host dedup key).

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/transport/test_bus_frame.py
from __future__ import annotations

from backend.core.ouroboros.governance.ide_observability_stream import StreamEvent
from backend.core.ouroboros.governance.transport import bus_frame as bf


def _ev(seq: int) -> StreamEvent:
    return StreamEvent(
        event_id=format(seq, "012x"),
        event_type="task_started",
        op_id="op-1",
        timestamp="2026-07-03T00:00:00.000Z",
        payload={"k": "v"},
    )


def test_event_frame_roundtrip_preserves_all_fields():
    frame = bf.event_frame(_ev(42), source_id="brain-01")
    raw = frame.encode()
    back = bf.BusFrame.decode(raw)
    assert back is not None
    assert back.kind == bf.FRAME_EVENT
    assert back.source_id == "brain-01"
    assert back.seq == 42  # int(event_id, 16)
    assert back.event["event_id"] == format(42, "012x")
    assert back.event["payload"] == {"k": "v"}


def test_hello_frame_carries_last_event_id():
    frame = bf.hello_frame("mac-01", last_event_id="0000000000ff")
    back = bf.BusFrame.decode(frame.encode())
    assert back.kind == bf.FRAME_HELLO
    assert back.last_event_id == "0000000000ff"


def test_decode_malformed_returns_none_never_raises():
    assert bf.BusFrame.decode(b"not json") is None
    assert bf.BusFrame.decode("{}") is None  # missing required kind
    assert bf.BusFrame.decode(b'{"kind": 123}') is None  # wrong type


def test_qualified_id_is_source_scoped():
    assert bf.qualified_id("mac-01", "0000000000ff") == "mac-01:0000000000ff"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/transport/test_bus_frame.py -v`
Expected: FAIL — `ModuleNotFoundError: ...bus_frame`

- [ ] **Step 3: Implement the codec**

```python
# backend/core/ouroboros/governance/transport/bus_frame.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from backend.core.ouroboros.governance.ide_observability_stream import StreamEvent

FRAME_HELLO = "hello"
FRAME_EVENT = "event"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ACK = "ack"

_VALID_KINDS = (FRAME_HELLO, FRAME_EVENT, FRAME_HEARTBEAT, FRAME_ACK)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def qualified_id(source_id: str, event_id: str) -> str:
    """Cross-host dedup key: source-scoped so two brokers' native
    monotonic ids never collide."""
    return f"{source_id}:{event_id}"


@dataclass(frozen=True)
class BusFrame:
    kind: str
    source_id: str
    seq: int = -1
    last_event_id: Optional[str] = None
    event: Optional[Dict[str, Any]] = None
    ts: str = ""

    def encode(self) -> bytes:
        return json.dumps({
            "kind": self.kind,
            "source_id": self.source_id,
            "seq": self.seq,
            "last_event_id": self.last_event_id,
            "event": self.event,
            "ts": self.ts or _iso_now(),
        }, ensure_ascii=True).encode("utf-8")

    @classmethod
    def decode(cls, raw: Union[str, bytes]) -> Optional["BusFrame"]:
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        kind = obj.get("kind")
        if not isinstance(kind, str) or kind not in _VALID_KINDS:
            return None
        source_id = obj.get("source_id")
        if not isinstance(source_id, str):
            return None
        event = obj.get("event")
        if event is not None and not isinstance(event, dict):
            return None
        seq = obj.get("seq", -1)
        if not isinstance(seq, int):
            seq = -1
        return cls(
            kind=kind,
            source_id=source_id,
            seq=seq,
            last_event_id=obj.get("last_event_id"),
            event=event,
            ts=obj.get("ts", "") if isinstance(obj.get("ts"), str) else "",
        )


def event_frame(ev: StreamEvent, source_id: str) -> BusFrame:
    try:
        seq = int(ev.event_id, 16)
    except (ValueError, TypeError):
        seq = -1
    return BusFrame(
        kind=FRAME_EVENT,
        source_id=source_id,
        seq=seq,
        event=ev.to_dict(),
        ts=_iso_now(),
    )


def hello_frame(source_id: str, last_event_id: Optional[str]) -> BusFrame:
    return BusFrame(kind=FRAME_HELLO, source_id=source_id, last_event_id=last_event_id, ts=_iso_now())


def heartbeat_frame(source_id: str) -> BusFrame:
    return BusFrame(kind=FRAME_HEARTBEAT, source_id=source_id, ts=_iso_now())


def ack_frame(source_id: str, last_event_id: str) -> BusFrame:
    return BusFrame(kind=FRAME_ACK, source_id=source_id, last_event_id=last_event_id, ts=_iso_now())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/transport/test_bus_frame.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transport/bus_frame.py tests/governance/transport/test_bus_frame.py
git commit -m "feat(transport): BusFrame codec with source-qualified IDs (Stage 0)"
```

---

## Task 4: Bridge server (WS route over a StreamEventBroker; Last-Event-ID replay + inbound dedup)

**Files:**
- Create: `backend/core/ouroboros/governance/transport/bus_bridge_server.py`
- Test: `tests/governance/transport/test_bus_bridge_server.py`

**Interfaces:**
- Consumes: `TransportConfig` (Task 1), `build_server_ssl_context` (Task 2), `BusFrame`/`event_frame`/`hello_frame`/`heartbeat_frame`/`qualified_id`/frame constants (Task 3), `StreamEventBroker` + `StreamEvent` (`ide_observability_stream.py`).
- Produces:
  - `class BusBridgeServer` constructed as `BusBridgeServer(broker: StreamEventBroker, cfg: TransportConfig, *, on_inbound: Optional[Callable[[StreamEvent], None]] = None)`.
    - `def register_routes(self, app: web.Application) -> None` — mounts the WS handler at `cfg.path` (so `EventChannelServer`'s app can host it).
    - `async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse` — the handler: on `hello`, seed replay from the broker's history since `last_event_id` and stream subsequent events; ingest inbound `event` frames, dedup by `qualified_id`, and republish to the broker via `on_inbound` (or the broker directly).
    - `def seen_count(self) -> int` — number of distinct inbound qualified-ids applied (test hook).
  - Server dedups inbound: a bounded `set`/`OrderedDict` of `qualified_id` values; a replayed frame already seen is dropped (idempotent ingest).

- [ ] **Step 1: Write the failing test** (uses aiohttp's test server; TLS off for this unit — TLS handshake is covered in Task 9)

```python
# tests/governance/transport/test_bus_bridge_server.py
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
    StreamEvent,
)
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport import bus_frame as bf
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer

pytestmark = pytest.mark.asyncio


def _cfg():
    return TransportConfig(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.5, reconnect_max_s=30.0, reconnect_jitter=0.3,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain-test",
    )


async def _mk_client(broker, cfg, inbound_sink):
    server = BusBridgeServer(broker, cfg, on_inbound=lambda ev: inbound_sink.append(ev))
    app = web.Application()
    server.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return server, client


async def test_server_replays_history_since_last_event_id():
    broker = StreamEventBroker(history_maxlen=100)
    # Pre-load three events into history.
    id1 = broker.publish("task_started", "op-1", {"n": 1})
    id2 = broker.publish("task_progress", "op-1", {"n": 2})
    id3 = broker.publish("task_completed", "op-1", {"n": 3})
    sink = []
    server, client = await _mk_client(broker, _cfg(), sink)
    try:
        ws = await client.ws_connect("/ws/trinity-bus")
        await ws.send_bytes(bf.hello_frame("mac-test", last_event_id=id1).encode())
        got = []
        # Expect replay of id2, id3 (everything after id1).
        for _ in range(2):
            msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            frame = bf.BusFrame.decode(msg.data)
            if frame and frame.kind == bf.FRAME_EVENT:
                got.append(frame.event["event_id"])
        assert got == [id2, id3]
        await ws.close()
    finally:
        await client.close()


async def test_server_dedups_replayed_inbound_events():
    broker = StreamEventBroker(history_maxlen=100)
    sink = []
    server, client = await _mk_client(broker, _cfg(), sink)
    try:
        ws = await client.ws_connect("/ws/trinity-bus")
        await ws.send_bytes(bf.hello_frame("mac-test", last_event_id=None).encode())
        ev = StreamEvent(event_id=format(7, "012x"), event_type="task_started",
                         op_id="op-9", timestamp="2026-07-03T00:00:00.000Z", payload={})
        frame = bf.event_frame(ev, source_id="mac-test").encode()
        await ws.send_bytes(frame)
        await ws.send_bytes(frame)  # duplicate resend
        await asyncio.sleep(0.2)
        assert len(sink) == 1  # deduped by qualified_id
        assert server.seen_count() == 1
        await ws.close()
    finally:
        await client.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/transport/test_bus_bridge_server.py -v`
Expected: FAIL — `ModuleNotFoundError: ...bus_bridge_server`

- [ ] **Step 3: Implement the server**

```python
# backend/core/ouroboros/governance/transport/bus_bridge_server.py
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Callable, Optional

from aiohttp import WSMsgType, web

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEvent,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport import bus_frame as bf

logger = logging.getLogger(__name__)

_DEDUP_MAX = 8192  # bounded seen-id memory


class BusBridgeServer:
    """Hosts the WS endpoint that bridges a local StreamEventBroker to a
    remote peer. Server-authoritative history + Last-Event-ID replay come
    from the broker; inbound peer events are deduped by qualified id and
    republished locally."""

    def __init__(
        self,
        broker: StreamEventBroker,
        cfg: TransportConfig,
        *,
        on_inbound: Optional[Callable[[StreamEvent], None]] = None,
    ) -> None:
        self._broker = broker
        self._cfg = cfg
        self._on_inbound = on_inbound
        self._seen: "OrderedDict[str, None]" = OrderedDict()

    def seen_count(self) -> int:
        return len(self._seen)

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get(self._cfg.path, self._handle_ws)

    def _mark_seen(self, qid: str) -> bool:
        """Return True if NEW (not seen before). Bounded FIFO eviction."""
        if qid in self._seen:
            return False
        self._seen[qid] = None
        if len(self._seen) > _DEDUP_MAX:
            self._seen.popitem(last=False)
        return True

    def _ingest(self, frame: bf.BusFrame) -> None:
        ev_dict = frame.event or {}
        event_id = ev_dict.get("event_id", "")
        qid = bf.qualified_id(frame.source_id, event_id)
        if not self._mark_seen(qid):
            return  # idempotent — already applied
        ev = StreamEvent(
            event_id=event_id,
            event_type=ev_dict.get("event_type", ""),
            op_id=ev_dict.get("op_id", ""),
            timestamp=ev_dict.get("timestamp", ""),
            payload=ev_dict.get("payload", {}) or {},
        )
        try:
            if self._on_inbound is not None:
                self._on_inbound(ev)
        except Exception:  # noqa: BLE001 — never crash the WS loop
            logger.debug("[BusBridgeServer] on_inbound raised", exc_info=True)

    async def _pump_outbound(self, ws: web.WebSocketResponse, last_event_id: Optional[str]) -> None:
        """Subscribe to the broker and stream events (with replay) to the peer."""
        sub = self._broker.subscribe(op_id_filter=None, last_event_id=last_event_id)
        if sub is None:
            return
        try:
            hb = self._cfg.heartbeat_s
            async for event in self._broker.stream_iter(sub, heartbeat_s=hb):
                if ws.closed:
                    break
                frame = bf.event_frame(event, source_id=self._cfg.source_id)
                await ws.send_bytes(frame.encode())
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[BusBridgeServer] outbound pump ended", exc_info=True)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        pump_task: Optional[asyncio.Task] = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.BINARY and msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                        break
                    continue
                frame = bf.BusFrame.decode(msg.data)
                if frame is None:
                    continue
                if frame.kind == bf.FRAME_HELLO and pump_task is None:
                    pump_task = asyncio.ensure_future(
                        self._pump_outbound(ws, frame.last_event_id)
                    )
                elif frame.kind == bf.FRAME_EVENT:
                    self._ingest(frame)
                elif frame.kind == bf.FRAME_HEARTBEAT:
                    await ws.send_bytes(bf.heartbeat_frame(self._cfg.source_id).encode())
                # FRAME_ACK is plumbed for Stage 3 WAL trim; no-op here.
        finally:
            if pump_task is not None:
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        return ws
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/transport/test_bus_bridge_server.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transport/bus_bridge_server.py tests/governance/transport/test_bus_bridge_server.py
git commit -m "feat(transport): BusBridgeServer WS route with Last-Event-ID replay + inbound dedup (Stage 0)"
```

---

## Task 5: Bridge client (reconnect backoff+jitter, heartbeat-miss degraded flip, contiguous-id resume)

**Files:**
- Create: `backend/core/ouroboros/governance/transport/bus_bridge_client.py`
- Test: `tests/governance/transport/test_bus_bridge_client.py`

**Interfaces:**
- Consumes: `TransportConfig` (Task 1), `build_client_ssl_context` (Task 2), `BusFrame`/frame builders/constants (Task 3), `StreamEventBroker`/`StreamEvent` (`ide_observability_stream.py`).
- Produces:
  - `class BusBridgeClient(broker: StreamEventBroker, cfg: TransportConfig, *, url: Optional[str] = None, session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None)`.
    - `async def run(self) -> None` — the reconnect loop: connect, send `hello` with `self.last_event_id`, pump broker events out + apply inbound events, retry on drop with exp-backoff+jitter.
    - `async def stop(self) -> None` — cancel the loop cleanly.
    - `property last_event_id: Optional[str]` — highest **contiguous** server event_id applied (drives replay). Starts `None`.
    - `property degraded: bool` — True after `cfg.degrade_after_missed_hb` consecutive heartbeat-window misses; flips back False on a live frame.
    - `def _next_backoff(self, attempt: int) -> float` — `min(base * 2**attempt, max) * (1 +/- jitter)`; pure, unit-testable.
    - `def _advance_contiguous(self, event_id: str) -> None` — update `last_event_id` only when `int(event_id,16)` is exactly one past the current high-water; a gap is left unfilled so the next reconnect replays it.

- [ ] **Step 1: Write the failing test** (backoff math + contiguity are pure; the live socket path is exercised end-to-end in Tasks 6-7-9)

```python
# tests/governance/transport/test_bus_bridge_client.py
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.5, reconnect_max_s=8.0, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="mac-test",
    )
    base.update(over)
    return TransportConfig(**base)


def test_backoff_grows_geometrically_and_caps():
    c = BusBridgeClient(StreamEventBroker(), _cfg())
    assert c._next_backoff(0) == 0.5
    assert c._next_backoff(1) == 1.0
    assert c._next_backoff(2) == 2.0
    assert c._next_backoff(20) == 8.0  # capped at reconnect_max_s


def test_backoff_jitter_stays_in_band():
    c = BusBridgeClient(StreamEventBroker(), _cfg(reconnect_jitter=0.3))
    for attempt in range(6):
        base = min(0.5 * 2 ** attempt, 8.0)
        val = c._next_backoff(attempt)
        assert base * 0.7 <= val <= base * 1.3


def test_contiguous_advance_leaves_gaps_for_replay():
    c = BusBridgeClient(StreamEventBroker(), _cfg())
    assert c.last_event_id is None
    c._advance_contiguous(format(1, "012x"))
    assert c.last_event_id == format(1, "012x")
    c._advance_contiguous(format(2, "012x"))
    assert c.last_event_id == format(2, "012x")
    # A gap (skip 3, receive 4) must NOT advance the high-water past 2.
    c._advance_contiguous(format(4, "012x"))
    assert c.last_event_id == format(2, "012x")  # replay will refetch 3,4
    # Filling the gap advances again.
    c._advance_contiguous(format(3, "012x"))
    assert c.last_event_id == format(3, "012x")


def test_degraded_defaults_false():
    c = BusBridgeClient(StreamEventBroker(), _cfg())
    assert c.degraded is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/transport/test_bus_bridge_client.py -v`
Expected: FAIL — `ModuleNotFoundError: ...bus_bridge_client`

- [ ] **Step 3: Implement the client**

```python
# backend/core/ouroboros/governance/transport/bus_bridge_client.py
from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, Optional, Set

import aiohttp

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEvent,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.transport_security import (
    build_client_ssl_context,
)
from backend.core.ouroboros.governance.transport import bus_frame as bf

logger = logging.getLogger(__name__)


class BusBridgeClient:
    """Connects to a BusBridgeServer, resumes via Last-Event-ID, and
    mirrors the two brokers. Reconnect is exp-backoff + jitter; a
    heartbeat-window miss flips degraded mode deterministically."""

    def __init__(
        self,
        broker: StreamEventBroker,
        cfg: TransportConfig,
        *,
        url: Optional[str] = None,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
    ) -> None:
        self._broker = broker
        self._cfg = cfg
        self._url = url
        self._session_factory = session_factory
        self._stopped = False
        self._last_event_id: Optional[str] = None
        self._high_water: int = 0
        self._degraded = False
        self._missed_hb = 0
        self._seen: Set[str] = set()

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last_event_id

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _next_backoff(self, attempt: int) -> float:
        base = min(self._cfg.reconnect_base_s * (2 ** attempt), self._cfg.reconnect_max_s)
        if self._cfg.reconnect_jitter <= 0:
            return base
        span = base * self._cfg.reconnect_jitter
        return base + random.uniform(-span, span)

    def _advance_contiguous(self, event_id: str) -> None:
        try:
            seq = int(event_id, 16)
        except (ValueError, TypeError):
            return
        if seq == self._high_water + 1:
            self._high_water = seq
            self._last_event_id = event_id
            # Absorb any already-received events that now become contiguous.
            # (Gap-fill: caller applies events as they arrive; the high-water
            # only tracks the contiguous prefix.)

    def _resolve_url(self) -> str:
        if self._url:
            return self._url
        scheme = "wss" if self._cfg.tls_enabled else "ws"
        host = self._cfg.host or "127.0.0.1"
        return f"{scheme}://{host}:{self._cfg.port}{self._cfg.path}"

    async def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        attempt = 0
        while not self._stopped:
            try:
                await self._connect_once()
                attempt = 0  # clean disconnect resets backoff
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — any drop -> reconnect
                logger.debug("[BusBridgeClient] connection ended", exc_info=True)
            if self._stopped:
                break
            delay = self._next_backoff(attempt)
            attempt += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        ssl_ctx = build_client_ssl_context(self._cfg)
        session = (self._session_factory() if self._session_factory
                   else aiohttp.ClientSession())
        try:
            async with session.ws_connect(
                self._resolve_url(), ssl=ssl_ctx, heartbeat=None,
            ) as ws:
                await ws.send_bytes(
                    bf.hello_frame(self._cfg.source_id, self._last_event_id).encode()
                )
                out_task = asyncio.ensure_future(self._pump_outbound(ws))
                try:
                    await self._pump_inbound(ws)
                finally:
                    out_task.cancel()
                    try:
                        await out_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
        finally:
            await session.close()

    async def _pump_outbound(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        sub = self._broker.subscribe(op_id_filter=None, last_event_id=None)
        if sub is None:
            return
        async for event in self._broker.stream_iter(sub, heartbeat_s=self._cfg.heartbeat_s):
            if ws.closed:
                break
            await ws.send_bytes(bf.event_frame(event, source_id=self._cfg.source_id).encode())

    async def _pump_inbound(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hb = self._cfg.heartbeat_s
        while not ws.closed and not self._stopped:
            try:
                if hb > 0:
                    msg = await asyncio.wait_for(ws.receive(), timeout=hb * 1.5)
                else:
                    msg = await ws.receive()
            except asyncio.TimeoutError:
                self._missed_hb += 1
                if self._missed_hb >= self._cfg.degrade_after_missed_hb:
                    self._degraded = True
                continue
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            self._missed_hb = 0
            self._degraded = False
            frame = bf.BusFrame.decode(msg.data)
            if frame is None or frame.kind != bf.FRAME_EVENT:
                continue
            self._apply_inbound(frame)

    def _apply_inbound(self, frame: bf.BusFrame) -> None:
        ev_dict = frame.event or {}
        event_id = ev_dict.get("event_id", "")
        qid = bf.qualified_id(frame.source_id, event_id)
        if qid in self._seen:
            return
        self._seen.add(qid)
        try:
            self._broker.publish(
                ev_dict.get("event_type", ""),
                ev_dict.get("op_id", ""),
                ev_dict.get("payload", {}) or {},
            )
        except Exception:  # noqa: BLE001
            logger.debug("[BusBridgeClient] local republish failed", exc_info=True)
        self._advance_contiguous(event_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/transport/test_bus_bridge_client.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transport/bus_bridge_client.py tests/governance/transport/test_bus_bridge_client.py
git commit -m "feat(transport): BusBridgeClient with backoff+jitter reconnect, degraded flip, contiguous resume (Stage 0)"
```

---

## Task 6: Distributed bus adapter + real-socket integration

**Files:**
- Create: `backend/core/ouroboros/governance/transport/distributed_event_bus.py`
- Modify: `backend/core/ouroboros/governance/transport/__init__.py` (export the new surface)
- Test: `tests/governance/transport/test_distributed_event_bus.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces:
  - `class DistributedEventBus(broker: StreamEventBroker, cfg: TransportConfig, *, role: str)`.
    - `def publish(self, event_type: str, op_id: str, payload: dict) -> Optional[str]` — delegates to the local broker (unchanged TrinityEventBus semantics); the bridge propagates it across the socket.
    - `def register_server_routes(self, app: web.Application) -> None` — server role only; mounts `BusBridgeServer`.
    - `async def start_client(self, url: Optional[str] = None) -> None` / `async def stop(self) -> None` — client role; drives a `BusBridgeClient`.
  - Exports added to `__init__.py`: `DistributedEventBus`, `BusBridgeServer`, `BusBridgeClient`, `build_client_ssl_context`, `build_server_ssl_context`.

- [ ] **Step 1: Write the failing test — publish here, observed there, over a real loopback socket**

```python
# tests/governance/transport/test_distributed_event_bus.py
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.distributed_event_bus import DistributedEventBus

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.1, reconnect_max_s=1.0, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain-test",
    )
    base.update(over)
    return TransportConfig(**base)


async def test_publish_on_server_reaches_client_broker():
    server_broker = StreamEventBroker(history_maxlen=100)
    client_broker = StreamEventBroker(history_maxlen=100)
    server_bus = DistributedEventBus(server_broker, _cfg(source_id="brain"), role="server")
    app = web.Application()
    server_bus.register_server_routes(app)
    tclient = TestClient(TestServer(app))
    await tclient.start_server()
    url = str(tclient.make_url("/ws/trinity-bus")).replace("http", "ws")

    client_bus = DistributedEventBus(client_broker, _cfg(source_id="mac"), role="client")
    run_task = asyncio.ensure_future(client_bus.start_client(url=url))
    try:
        await asyncio.sleep(0.3)  # allow connect + hello
        # Publish on the SERVER; assert it lands in the CLIENT's broker.
        server_broker.publish("task_started", "op-42", {"hello": "world"})
        deadline = asyncio.get_event_loop().time() + 3.0
        found = False
        while asyncio.get_event_loop().time() < deadline:
            hist = client_broker.recent_history()
            if any(e.op_id == "op-42" and e.event_type == "task_started" for e in hist):
                found = True
                break
            await asyncio.sleep(0.05)
        assert found, "event published on server never reached client broker"
    finally:
        await client_bus.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
        await tclient.close()
```

Note: confirm `StreamEventBroker.recent_history()` returns the events (signature seen at `ide_observability_stream.py:1861`); if its parameter differs, pass the documented arguments — it returns a fresh list of `StreamEvent`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/transport/test_distributed_event_bus.py -v`
Expected: FAIL — `ModuleNotFoundError: ...distributed_event_bus`

- [ ] **Step 3: Implement the adapter**

```python
# backend/core/ouroboros/governance/transport/distributed_event_bus.py
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient

logger = logging.getLogger(__name__)


class DistributedEventBus:
    """The publish()-here-subscribers-there seam. Wraps a local
    StreamEventBroker; a server- or client-role bridge propagates events
    across the socket. TrinityEventBus callers are unchanged — they still
    publish/subscribe on the local broker."""

    def __init__(self, broker: StreamEventBroker, cfg: TransportConfig, *, role: str) -> None:
        if role not in ("server", "client"):
            raise ValueError(f"role must be server|client, got {role!r}")
        self._broker = broker
        self._cfg = cfg
        self._role = role
        self._server: Optional[BusBridgeServer] = None
        self._client: Optional[BusBridgeClient] = None

    def publish(self, event_type: str, op_id: str, payload: dict) -> Optional[str]:
        return self._broker.publish(event_type, op_id, payload)

    def register_server_routes(self, app: web.Application) -> None:
        if self._role != "server":
            raise RuntimeError("register_server_routes requires role=server")
        # Inbound peer events republish into the local broker so local
        # subscribers see them (idempotent — server dedups by qualified id).
        self._server = BusBridgeServer(
            self._broker, self._cfg,
            on_inbound=lambda ev: self._broker.publish(
                ev.event_type, ev.op_id, dict(ev.payload)
            ),
        )
        self._server.register_routes(app)

    async def start_client(self, url: Optional[str] = None) -> None:
        if self._role != "client":
            raise RuntimeError("start_client requires role=client")
        self._client = BusBridgeClient(self._broker, self._cfg, url=url)
        await self._client.run()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
```

- [ ] **Step 4: Update the package exports**

```python
# backend/core/ouroboros/governance/transport/__init__.py  (append to __all__ + imports)
__all__ = [
    "TransportConfig",
    "distributed_bus_enabled",
    "DistributedEventBus",
    "BusBridgeServer",
    "BusBridgeClient",
    "build_client_ssl_context",
    "build_server_ssl_context",
]

from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: E402,F401
    TransportConfig,
    distributed_bus_enabled,
)
from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: E402,F401
    DistributedEventBus,
)
from backend.core.ouroboros.governance.transport.bus_bridge_server import (  # noqa: E402,F401
    BusBridgeServer,
)
from backend.core.ouroboros.governance.transport.bus_bridge_client import (  # noqa: E402,F401
    BusBridgeClient,
)
from backend.core.ouroboros.governance.transport.transport_security import (  # noqa: E402,F401
    build_client_ssl_context,
    build_server_ssl_context,
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/transport/test_distributed_event_bus.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/transport/distributed_event_bus.py backend/core/ouroboros/governance/transport/__init__.py tests/governance/transport/test_distributed_event_bus.py
git commit -m "feat(transport): DistributedEventBus seam + real-socket loopback integration (Stage 0)"
```

---

## Task 7: Hostile-network simulation + exact Last-Event-ID replay (LOAD-BEARING)

**Files:**
- Create: `tests/governance/transport/hostile_network.py` (reusable fault-injection proxy)
- Test: `tests/governance/transport/test_hostile_network_replay.py`

**Interfaces:**
- Consumes: `BusBridgeServer` (Task 4), `BusBridgeClient` (Task 5), `StreamEventBroker`.
- Produces:
  - `class HostileProxy` — an aiohttp app that sits between client and the real server WS, applying: `latency_s` (fixed delay per frame), `jitter_s` (uniform extra delay), `reorder_window` (buffer N frames and flush shuffled), `drop_then_close_after` (deliver K frames, drop the connection to force a client reconnect). Deterministic via a seeded `random.Random`.
  - `async def run_hostile_case(*, n_events, faults) -> Tuple[List[str], List[str]]` — returns `(published_ids, client_final_ids)` for assertions.

**The load-bearing assertion:** after the hostile transport churns (drops mid-stream, forces a reconnect, replays via Last-Event-ID), the set of event ids the client ultimately holds equals the set the server published — **no gap, no duplicate** — and the contiguous prefix matches exactly.

- [ ] **Step 1: Write the fault-injection proxy**

```python
# tests/governance/transport/hostile_network.py
from __future__ import annotations

import asyncio
import random
from typing import List, Optional

from aiohttp import ClientSession, WSMsgType, web


class HostileProxy:
    """WS proxy that degrades the link between a client and the real
    upstream server. Deterministic (seeded)."""

    def __init__(
        self,
        upstream_url: str,
        *,
        latency_s: float = 0.0,
        jitter_s: float = 0.0,
        reorder_window: int = 1,
        drop_after: Optional[int] = None,
        seed: int = 1234,
    ) -> None:
        self._upstream = upstream_url
        self._latency = latency_s
        self._jitter = jitter_s
        self._reorder_window = max(1, reorder_window)
        self._drop_after = drop_after
        self._rng = random.Random(seed)

    def register(self, app: web.Application, path: str) -> None:
        app.router.add_get(path, self._handle)

    async def _delay(self) -> None:
        d = self._latency + (self._rng.uniform(0, self._jitter) if self._jitter else 0.0)
        if d > 0:
            await asyncio.sleep(d)

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        downstream = web.WebSocketResponse(heartbeat=None)
        await downstream.prepare(request)
        session = ClientSession()
        delivered = 0
        buf: List[bytes] = []
        try:
            async with session.ws_connect(self._upstream, heartbeat=None) as upstream:
                async def c2u() -> None:
                    async for msg in downstream:
                        if msg.type in (WSMsgType.BINARY, WSMsgType.TEXT):
                            await upstream.send_bytes(
                                msg.data if isinstance(msg.data, bytes) else msg.data.encode()
                            )
                        else:
                            break

                async def u2c() -> None:
                    nonlocal delivered
                    async for msg in upstream:
                        if msg.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
                            break
                        data = msg.data if isinstance(msg.data, bytes) else msg.data.encode()
                        await self._delay()
                        buf.append(data)
                        if len(buf) >= self._reorder_window:
                            self._rng.shuffle(buf)
                            for frame in buf:
                                if self._drop_after is not None and delivered >= self._drop_after:
                                    await downstream.close()
                                    return
                                await downstream.send_bytes(frame)
                                delivered += 1
                            buf.clear()

                await asyncio.gather(c2u(), u2c(), return_exceptions=True)
                # Flush any remaining buffered frames unless we already dropped.
                for frame in buf:
                    if self._drop_after is not None and delivered >= self._drop_after:
                        break
                    await downstream.send_bytes(frame)
                    delivered += 1
        finally:
            await session.close()
            if not downstream.closed:
                await downstream.close()
        return downstream
```

- [ ] **Step 2: Write the failing load-bearing test**

```python
# tests/governance/transport/test_hostile_network_replay.py
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient
from tests.governance.transport.hostile_network import HostileProxy

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=4096, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain",
    )
    base.update(over)
    return TransportConfig(**base)


async def test_replay_is_exact_across_drop_and_reorder():
    server_broker = StreamEventBroker(history_maxlen=4096)
    # Real upstream server hosting the bridge.
    up_app = web.Application()
    BusBridgeServer(server_broker, _cfg(source_id="brain")).register_routes(up_app)
    up = TestClient(TestServer(up_app))
    await up.start_server()
    upstream_url = str(up.make_url("/ws/trinity-bus")).replace("http", "ws")

    # Hostile proxy in front of it: reorder in windows of 3, drop the
    # connection after 5 frames to force a reconnect + Last-Event-ID replay.
    proxy = HostileProxy(upstream_url, reorder_window=3, drop_after=5,
                         latency_s=0.001, jitter_s=0.003, seed=7)
    px_app = web.Application()
    proxy.register(px_app, "/ws/trinity-bus")
    px = TestClient(TestServer(px_app))
    await px.start_server()
    proxy_url = str(px.make_url("/ws/trinity-bus")).replace("http", "ws")

    client_broker = StreamEventBroker(history_maxlen=4096)
    client = BusBridgeClient(client_broker, _cfg(source_id="mac"), url=proxy_url)
    run_task = asyncio.ensure_future(client.run())

    published = []
    try:
        await asyncio.sleep(0.2)
        # Publish 12 events on the server. The proxy drops mid-stream;
        # the client must reconnect and replay the gap from Last-Event-ID.
        for i in range(12):
            eid = server_broker.publish("task_progress", "op-hostile", {"i": i})
            published.append(eid)
            await asyncio.sleep(0.02)
        # Give reconnect + replay time to converge.
        deadline = asyncio.get_event_loop().time() + 6.0
        while asyncio.get_event_loop().time() < deadline:
            got = {e.event_id for e in client_broker.recent_history()
                   if e.op_id == "op-hostile"}
            if set(published).issubset(got):
                break
            await asyncio.sleep(0.1)
        got_ids = [e.event_id for e in client_broker.recent_history()
                   if e.op_id == "op-hostile"]
        # EXACT: every published id present, no duplicates.
        assert set(published).issubset(set(got_ids)), (
            f"missing after replay: {set(published) - set(got_ids)}"
        )
        assert len(got_ids) == len(set(got_ids)), "duplicate events after replay"
    finally:
        await client.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
        await px.close()
        await up.close()
```

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `python3 -m pytest tests/governance/transport/test_hostile_network_replay.py -v`
Expected FIRST: FAIL — either `ModuleNotFoundError` on `hostile_network` (if Step 1 not saved) or an assertion gap if replay is imperfect. If it fails on a replay gap, the bug is in `BusBridgeClient._advance_contiguous`/hello resume — fix there (this is the whole point of the task), do not weaken the assertion.
Expected AFTER fixes: PASS (1 passed).

- [ ] **Step 4: Commit**

```bash
git add tests/governance/transport/hostile_network.py tests/governance/transport/test_hostile_network_replay.py
git commit -m "test(transport): hostile-network sim proving exact Last-Event-ID replay across drop+reorder (Stage 0)"
```

---

## Task 8: AST invariants — real-time actuation isolation + no hardcoded endpoints

**Files:**
- Modify: `backend/core/ouroboros/governance/meta/shipped_code_invariants.py` (add two validators + register both in `_register_seed_invariants`, near line 3481)
- Test: `tests/governance/test_transport_invariants.py`

**Interfaces:**
- Consumes: the `ShippedCodeInvariant` schema (`invariant_name`, `target_file`, `description`, `validate`), `register_shipped_code_invariant`, and the validator contract `Callable[[ast.Module, str], Tuple[str, ...]]` (all in `shipped_code_invariants.py`; validators never raise, empty tuple = holds).
- Produces:
  - `_validate_realtime_actuation_no_remote_transport(tree: ast.Module, source: str) -> Tuple[str, ...]` — target file `backend/ghost_hands/ghost_hands_controller.py` (the real-time actuation entry; adjust to the actual controller module name found in `backend/ghost_hands/`): flags any `import`/`from ... import` whose module path contains `governance.transport`.
  - `_validate_transport_no_hardcoded_endpoints(tree: ast.Module, source: str) -> Tuple[str, ...]` — target file `backend/core/ouroboros/governance/transport/transport_config.py`: flags any string literal matching an IPv4 dotted-quad or a `ws://`/`wss://` URL (endpoints must be env-resolved, not baked). The loopback default `127.0.0.1` lives in tests/fixtures, never in this module — so the module must contain zero such literals.

- [ ] **Step 1: Confirm the real-time actuation entry module name**

Run: `ls backend/ghost_hands/ && grep -rlE "class .*(Controller|Actuator|GhostHands)" backend/ghost_hands/*.py | head`
Use the identified controller module path as `target_file` in Step 3. If multiple real-time modules exist, register one invariant per module (repeat the `ShippedCodeInvariant(...)` block with each `target_file`).

- [ ] **Step 2: Write the failing test**

```python
# tests/governance/test_transport_invariants.py
from __future__ import annotations

import ast

from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    _validate_realtime_actuation_no_remote_transport,
    _validate_transport_no_hardcoded_endpoints,
)


def test_actuation_importing_transport_is_flagged():
    bad = (
        "from backend.core.ouroboros.governance.transport import DistributedEventBus\n"
        "def click(x, y):\n    pass\n"
    )
    violations = _validate_realtime_actuation_no_remote_transport(ast.parse(bad), bad)
    assert violations
    assert any("transport" in v for v in violations)


def test_actuation_without_transport_import_holds():
    good = "import time\ndef click(x, y):\n    time.sleep(0)\n"
    violations = _validate_realtime_actuation_no_remote_transport(ast.parse(good), good)
    assert violations == ()


def test_hardcoded_ipv4_in_config_is_flagged():
    bad = 'HOST = "10.1.2.3"\nURL = "wss://10.1.2.3:8443/ws"\n'
    violations = _validate_transport_no_hardcoded_endpoints(ast.parse(bad), bad)
    assert violations


def test_env_resolved_config_holds():
    good = 'import os\nhost = os.environ.get("JARVIS_BRAIN_WS_HOST")\n'
    violations = _validate_transport_no_hardcoded_endpoints(ast.parse(good), good)
    assert violations == ()
```

- [ ] **Step 3: Implement the two validators** (add near the other `_validate_*` functions)

```python
# backend/core/ouroboros/governance/meta/shipped_code_invariants.py  (add these functions)
import re as _re  # if `re` is not already imported at module top; otherwise reuse it

_IPV4_RE = _re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WSURL_RE = _re.compile(r"wss?://")


def _validate_realtime_actuation_no_remote_transport(
    tree: "ast.Module", source: str,
) -> "Tuple[str, ...]":
    """Real-time actuation layers MUST NOT import the network transport
    subpackage. A remote dependency on a hard-real-time path would couple
    cursor/keyboard latency to network RTT. Structural block."""
    out = []
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if "governance.transport" in mod:
                    out.append(
                        f"real-time actuation imports remote transport: "
                        f"from {mod} (line {node.lineno})"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "governance.transport" in (alias.name or ""):
                        out.append(
                            f"real-time actuation imports remote transport: "
                            f"import {alias.name} (line {node.lineno})"
                        )
    except Exception:  # noqa: BLE001 — validators never raise
        return ()
    return tuple(out)


def _validate_transport_no_hardcoded_endpoints(
    tree: "ast.Module", source: str,
) -> "Tuple[str, ...]":
    """The transport config module must resolve every endpoint from env —
    no baked IPv4 literals, no baked ws:// / wss:// URLs."""
    out = []
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                if _IPV4_RE.search(val) or _WSURL_RE.search(val):
                    out.append(
                        f"hardcoded endpoint literal {val!r} "
                        f"(line {getattr(node, 'lineno', -1)}) — resolve from env"
                    )
    except Exception:  # noqa: BLE001
        return ()
    return tuple(out)
```

- [ ] **Step 4: Register both invariants** (inside `_register_seed_invariants`, after the last existing `register_shipped_code_invariant(...)` block)

```python
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="realtime_actuation_no_remote_transport",
            target_file="backend/ghost_hands/ghost_hands_controller.py",  # <- Step 1 result
            description=(
                "Real-time actuation (Ghost Hands) MUST NOT import "
                "backend.core.ouroboros.governance.transport — a hard-real-time "
                "path cannot take a network dependency (Stage 0 latency-scope "
                "invariant)."
            ),
            validate=_validate_realtime_actuation_no_remote_transport,
        ),
    )
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="transport_config_no_hardcoded_endpoints",
            target_file=(
                "backend/core/ouroboros/governance/transport/transport_config.py"
            ),
            description=(
                "TransportConfig must resolve every endpoint from env — no baked "
                "IPv4 or ws:// / wss:// literals (Stage 0 no-hardcoding invariant)."
            ),
            validate=_validate_transport_no_hardcoded_endpoints,
        ),
    )
```

- [ ] **Step 5: Run the invariant unit test + the whole-registry validation**

Run: `python3 -m pytest tests/governance/test_transport_invariants.py -v`
Expected: PASS (4 passed)

Run: `python3 -c "from backend.core.ouroboros.governance.meta.shipped_code_invariants import validate_all; v=[x for x in validate_all() if 'transport' in x.invariant_name or 'actuation' in x.invariant_name]; print('violations:', [x.to_dict() for x in v]); assert not v, v"`
Expected: `violations: []` — the real Ghost Hands controller does NOT import the transport, and `transport_config.py` has no baked endpoint. If either fires, that is a real violation to fix in the source (do not weaken the validator).

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/meta/shipped_code_invariants.py tests/governance/test_transport_invariants.py
git commit -m "feat(invariant): structural block on real-time actuation importing remote transport + no-hardcoded-endpoint pin (Stage 0)"
```

---

## Task 9: Two-process loopback smoke + mTLS handshake end-to-end

**Files:**
- Create: `tests/governance/transport/test_two_process_loopback.py`
- Create: `backend/core/ouroboros/governance/transport/_smoke_server.py` (a tiny runnable brain-side server for the subprocess smoke — env-driven, no hardcoded host/port)
- Test: same file above

**Interfaces:**
- Consumes: `DistributedEventBus`, `TransportConfig`, `build_server_ssl_context`/`build_client_ssl_context`.
- Produces: proof that (a) a real second OS process hosting the server exchanges events with an in-test client, and (b) the mTLS handshake succeeds with ephemeral material and is REQUIRED (a plaintext client is rejected).

- [ ] **Step 1: Write the env-driven smoke server**

```python
# backend/core/ouroboros/governance/transport/_smoke_server.py
"""Runnable brain-side WS server for the two-process loopback smoke test.
Host/port/TLS come entirely from env — no hardcoded endpoint. Writes the
bound port to the file named by JARVIS_BRAIN_WS_PORTFILE so the parent can
discover it (loopback stand-in for Stage 1's Reachability discovery)."""
from __future__ import annotations

import asyncio
import os

from aiohttp import web

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.distributed_event_bus import DistributedEventBus
from backend.core.ouroboros.governance.transport.transport_security import (
    build_server_ssl_context,
)


async def _main() -> None:
    cfg = TransportConfig.from_env(role="server")
    broker = StreamEventBroker(history_maxlen=cfg.history_maxlen)
    bus = DistributedEventBus(broker, cfg, role="server")
    app = web.Application()
    bus.register_server_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx = build_server_ssl_context(cfg)
    site = web.TCPSite(runner, cfg.host or "127.0.0.1", cfg.port, ssl_context=ssl_ctx)
    await site.start()
    # Publish one heartbeat event on a timer so the client observes traffic.
    portfile = os.environ.get("JARVIS_BRAIN_WS_PORTFILE")
    bound = site._server.sockets[0].getsockname()[1]  # actual bound port
    if portfile:
        with open(portfile, "w") as fh:
            fh.write(str(bound))
    for i in range(1000):
        broker.publish("task_progress", "op-smoke", {"i": i})
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 2: Write the failing test**

```python
# tests/governance/transport/test_two_process_loopback.py
from __future__ import annotations

import asyncio
import os
import ssl
import subprocess
import sys
import tempfile
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.transport_security import (
    build_server_ssl_context,
    build_client_ssl_context,
)

pytestmark = pytest.mark.asyncio


def _tls_cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=True, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=True, source_id="brain",
    )
    base.update(over)
    return TransportConfig(**base)


async def test_mtls_handshake_succeeds_and_is_required():
    # Shared ephemeral material for both ends (loopback stands in for a
    # shared CA). Resolve once, feed both contexts the same cert/key.
    from backend.core.ouroboros.governance.transport import transport_security as ts
    cert, key = ts.generate_ephemeral_material()
    cfg = _tls_cfg(tls_ephemeral=False, tls_cert=cert, tls_key=key, tls_ca=cert)

    broker = StreamEventBroker(history_maxlen=100)
    app = web.Application()
    BusBridgeServer(broker, cfg).register_routes(app)
    server_ssl = build_server_ssl_context(cfg)
    tserver = TestServer(app)
    await tserver.start_server(ssl=server_ssl)
    host, port = tserver.host, tserver.port

    import aiohttp
    client_ssl = build_client_ssl_context(cfg)
    # (a) mTLS client connects.
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"wss://{host}:{port}/ws/trinity-bus", ssl=client_ssl) as ws:
            assert not ws.closed
    # (b) a plaintext / no-cert client is rejected (mTLS required).
    with pytest.raises((aiohttp.ClientConnectorError, ssl.SSLError, aiohttp.ClientError,
                        ConnectionResetError, aiohttp.WSServerHandshakeError)):
        no_verify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        no_verify.check_hostname = False
        no_verify.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession() as s:
            # No client cert loaded -> server's CERT_REQUIRED rejects.
            async with s.ws_connect(f"wss://{host}:{port}/ws/trinity-bus", ssl=no_verify) as ws:
                await ws.receive()
    await tserver.close()


async def test_two_os_process_server_exchanges_events():
    portfile = os.path.join(tempfile.mkdtemp(prefix="jarvis-smoke-"), "port")
    env = dict(os.environ)
    env.update({
        "JARVIS_BRAIN_WS_HOST": "127.0.0.1",
        "JARVIS_BRAIN_WS_PORT": "0",
        "JARVIS_BRAIN_WS_TLS_ENABLED": "false",
        "JARVIS_BRAIN_WS_PORTFILE": portfile,
        "JARVIS_BRAIN_WS_SOURCE_ID": "brain-proc",
    })
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    ).stdout.strip()
    proc = subprocess.Popen(
        [sys.executable, "-m",
         "backend.core.ouroboros.governance.transport._smoke_server"],
        env=env, cwd=repo_root,
    )
    try:
        # Wait for the child to write its bound port.
        deadline = time.time() + 10.0
        while time.time() < deadline and not os.path.exists(portfile):
            await asyncio.sleep(0.1)
        assert os.path.exists(portfile), "smoke server never bound"
        with open(portfile) as fh:
            port = int(fh.read().strip())

        client_broker = StreamEventBroker(history_maxlen=1024)
        from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient
        cfg = _tls_cfg(tls_enabled=False, tls_ephemeral=False)
        url = f"ws://127.0.0.1:{port}/ws/trinity-bus"
        client = BusBridgeClient(client_broker, cfg, url=url)
        run_task = asyncio.ensure_future(client.run())
        try:
            deadline = asyncio.get_event_loop().time() + 8.0
            got = False
            while asyncio.get_event_loop().time() < deadline:
                if any(e.op_id == "op-smoke" for e in client_broker.recent_history()):
                    got = True
                    break
                await asyncio.sleep(0.1)
            assert got, "no events crossed the two-process boundary"
        finally:
            await client.stop()
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

- [ ] **Step 3: Run test to verify it fails, then implement fixes, then passes**

Run: `python3 -m pytest tests/governance/transport/test_two_process_loopback.py -v`
Expected FIRST: FAIL — `ModuleNotFoundError` on `_smoke_server` until Step 1 saved; then possibly TLS-context wiring. Fix in `_smoke_server.py`/`transport_security.py` (not by relaxing the mTLS-required assertion).
Expected AFTER: PASS (2 passed).

- [ ] **Step 4: Commit**

```bash
git add backend/core/ouroboros/governance/transport/_smoke_server.py tests/governance/transport/test_two_process_loopback.py
git commit -m "test(transport): two-process loopback smoke + mTLS-required handshake (Stage 0)"
```

---

## Task 10: Full-suite gate + progress ledger

**Files:**
- Modify: `.superpowers/sdd/progress.md` (append Stage 0 completion)

- [ ] **Step 1: Run the whole transport suite + the invariant suite together**

Run: `python3 -m pytest tests/governance/transport/ tests/governance/test_transport_invariants.py -v`
Expected: all pass. Record the count.

- [ ] **Step 2: Confirm the master switch keeps Stage 0 dark**

Run: `python3 -c "import os; os.environ.pop('JARVIS_DISTRIBUTED_BUS_ENABLED', None); from backend.core.ouroboros.governance.transport import distributed_bus_enabled; assert distributed_bus_enabled() is False; print('Stage 0 ships dark: OK')"`
Expected: `Stage 0 ships dark: OK`

- [ ] **Step 3: Confirm no live boot path imports the transport** (nothing wired in yet)

Run: `grep -rnE "governance\.transport|DistributedEventBus" backend --include=*.py | grep -v "governance/transport/" | grep -v "/tests/"`
Expected: no matches (the subpackage is self-contained; nothing in the running organism references it until Stage 2).

- [ ] **Step 4: Append the ledger entry and commit**

```bash
# Append a Stage 0 completion block to .superpowers/sdd/progress.md, then:
git add .superpowers/sdd/progress.md
git commit -m "docs(ledger): Stage 0 transport substrate complete (dark; N tests green)"
```

---

## Self-Review

**1. Spec coverage** (against `2026-07-03-gcp-orchestrator-relocation-design.md` §Staging item 0 + §Testing Strategy + the three user invariants):
- Bidirectional WS bridge over `StreamEventBroker` → Tasks 4, 5, 6. ✓
- `TrinityEventBus` network-transport adapter (publish here → subscribers there) → Task 6 (`DistributedEventBus`). ✓
- Exact `Last-Event-ID` replay → Task 4 (server replay via broker) + Task 5 (contiguous resume) + Task 7 (hostile-net proof). ✓
- Heartbeat → Task 5 (`_pump_inbound` timeout + degraded flip) + broker `stream_iter` heartbeat. ✓
- Drop-oldest backpressure → inherited from `StreamEventBroker` (Task 4/5 use its `subscribe`/`stream_iter`; the `stream_lag` behavior is the broker's, unchanged). ✓
- mTLS with dynamically-resolved material → Task 2 + Task 9 (handshake required). ✓ User invariant #3 (crypto material dynamic). The ephemeral `/32` firewall is explicitly deferred to Stage 1 and noted in Global Constraints. ✓
- Static AST invariant blocking real-time actuation from remote transport → Task 8. ✓ User invariant #1.
- All thresholds dynamic / no hardcoding → Task 1 (env-resolved config) + Task 8 (no-hardcoded-endpoint invariant). ✓ User invariant #2.
- Hostile-network simulation (latency, jitter, out-of-order, partial drop) → Task 7 `HostileProxy`. ✓
- Loopback two-process scope, no VM, no sensor migration → Task 9 (two-process), and Task 10 Step 3 proves nothing is wired into the live loop. ✓

**2. Placeholder scan:** No "TBD"/"implement later". Task 8 Step 1 requires confirming the actual Ghost Hands controller filename before writing the `target_file` — that is a lookup step with an exact command, not a placeholder. All code blocks are complete.

**3. Type consistency:** `TransportConfig` field names are identical across Tasks 1-9. `BusFrame` kind constants (`FRAME_HELLO`/`FRAME_EVENT`/`FRAME_HEARTBEAT`/`FRAME_ACK`) used consistently. `StreamEventBroker.publish(event_type, op_id, payload)` and `.subscribe(op_id_filter, last_event_id)` and `.stream_iter(sub, heartbeat_s)` match the real signatures read from `ide_observability_stream.py`. Validator signature `(ast.Module, str) -> Tuple[str, ...]` matches the `ShippedCodeValidator` contract. `recent_history()` is flagged in Task 6 Step 1 for signature confirmation (it exists at `ide_observability_stream.py:1861`).

**Known verification points for the implementer** (surfaced, not hidden):
- `StreamEventBroker.recent_history()` exact parameters — confirm at first use (Task 6).
- The real-time actuation controller module path under `backend/ghost_hands/` — confirm in Task 8 Step 1 before registering the invariant.
- `TestServer.start_server(ssl=...)` / `TCPSite(..., ssl_context=...)` argument names are aiohttp-version-dependent — if the installed aiohttp rejects the kwarg, use the version's documented spelling (`ssl` vs `ssl_context`); this does not change the design.
