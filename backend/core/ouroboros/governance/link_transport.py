"""link_transport — the wire under the Body/Engine link.

WHAT THIS ADDS TO ``link_protocol``
-----------------------------------
``link_protocol`` is the half provable at a desk: clocks, sequence
arithmetic, admission, resume planning. This is the half that touches a
socket — and it deliberately owns nothing but the socket. Every rule it
executes comes from that module, so the transport can be replaced without
renegotiating the protocol and the protocol can be tested without a network.

ON "ADAPT THE FRAME SIZE TO MTU"
--------------------------------
It is worth saying plainly why this module does not do that, because the
request is reasonable and the answer is not obvious.

MTU is a property of a path, and TCP already owns it: the kernel does path
MTU discovery, segments the stream, and retransmits per segment. An
application writing 8 KiB or 64 KiB into a TCP socket produces the same
packets on the wire — the framing above the stream is invisible to it. Code
that read an MTU and sized application frames to it would be performing a
calculation whose result changes nothing, which is a fabricated measurement
in the same family as a blast radius nobody scanned.

What DOES matter above TCP, and is implemented here:

* **A negotiated maximum frame** (§29.6), so neither side can be made to
  buffer an allocation the other chose. Minimum of the two advertisements
  wins — a ceiling one side cannot honour is not a limit, it is a fault
  waiting for a large frame.
* **Backpressure that propagates**, via ``StreamWriter.drain()``. When the
  kernel send buffer fills, ``drain()`` suspends the producer instead of
  letting an in-process queue grow — buffer bloat moved from the socket into
  Python is still buffer bloat, and now it is invisible to ``ss``.
* **A batch size that adapts to observed drain latency**, which is the real
  congestion signal available at this layer. A link that is draining slowly
  gets smaller batches, so a stall is detected in one batch time rather than
  after a large write has already committed.

HALF-OPEN SOCKETS
-----------------
If the peer loses power, no FIN is sent. The local socket stays ESTABLISHED
and a ``read`` blocks — by default until the OS TCP keepalive fires, which
on Linux is **two hours**. That is indistinguishable from an idle link, and
it is the single most important failure this module has to catch quickly.

``SO_KEEPALIVE`` is enabled as defence in depth but is not relied upon: its
timers are OS-global policy, not this link's to set portably. The detection
that matters is an application heartbeat whose deadline is derived from
*measured* round-trip time — Jacobson's smoothed RTT and variance, the same
estimator TCP uses for its own retransmit timer — rather than from a
constant. A constant is wrong in both directions: too tight on a congested
café link (false deaths, reconnect storms) and too loose on a fast one
(minutes of silence before anyone notices).

Nothing here sleeps to wait. Every wait is an ``asyncio.wait_for`` on real
I/O with a computed deadline, so a stalled peer produces a timeout at the
read, not a task parked on a timer.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.LinkTransport")

LINK_TRANSPORT_SCHEMA_VERSION: str = "link_transport.1"

#: Frame kinds. A closed vocabulary: an unrecognised kind is dropped with a
#: counter rather than dispatched, so a newer peer cannot drive an older one
#: down a path it does not have.
KIND_HELLO = "hello"
KIND_WELCOME = "welcome"
KIND_HEARTBEAT = "heartbeat"
KIND_HEARTBEAT_ACK = "heartbeat_ack"
KIND_TELEMETRY = "telemetry"
KIND_COMMAND = "command"
KIND_VERDICT = "verdict"
KIND_RESUME = "resume"
KIND_RESUME_PLAN = "resume_plan"

_KNOWN_KINDS = frozenset({
    KIND_HELLO, KIND_WELCOME, KIND_HEARTBEAT, KIND_HEARTBEAT_ACK,
    KIND_TELEMETRY, KIND_COMMAND, KIND_VERDICT, KIND_RESUME,
    KIND_RESUME_PLAN,
})


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------------------
# Configuration — every value negotiated or tunable, none baked in
# ---------------------------------------------------------------------------


def max_frame_bytes() -> int:
    """This side's advertised frame ceiling. The peer's may differ."""
    return _env_int("JARVIS_LINK_MAX_FRAME_BYTES", 1 << 20, minimum=4096)


def heartbeat_interval_s() -> float:
    """Floor for the heartbeat cadence; the adaptive estimator may slow it."""
    return _env_float("JARVIS_LINK_HEARTBEAT_S", 5.0, minimum=0.5)


def heartbeat_miss_limit() -> int:
    """Consecutive missed heartbeats before the peer is declared dead."""
    return _env_int("JARVIS_LINK_HEARTBEAT_MISSES", 3, minimum=1)


def rtt_window() -> int:
    """Samples in the sliding RTT window."""
    return _env_int("JARVIS_LINK_RTT_WINDOW", 16, minimum=4)


def reassembly_capacity() -> int:
    """Out-of-order frames held before the gap is declared unrecoverable."""
    return _env_int("JARVIS_LINK_REASSEMBLY_CAPACITY", 256, minimum=8)


def tls_dir() -> Path:
    """Where the mTLS material lives. Never a hardcoded path in logic."""
    return Path(_env_str(
        "JARVIS_LINK_TLS_DIR",
        str(Path(_env_str("JARVIS_PROJECT_ROOT", ".")) / ".jarvis" / "brain_mtls"),
    ))


# ---------------------------------------------------------------------------
# Adaptive liveness — RTT-derived, not constant
# ---------------------------------------------------------------------------


class RttEstimator:
    """Jacobson/Karels smoothed RTT and variance over a sliding window.

    The same estimator TCP uses for its retransmit timer, for the same
    reason: a deadline must track the path, because a path that changes by
    an order of magnitude (Ethernet → café Wi-Fi → 5G tether) makes any
    constant wrong somewhere. ``deadline`` is ``srtt + 4*rttvar`` clamped to
    a floor — the classic RTO form, whose 4-sigma margin is what keeps a
    normally-jittery link from being declared dead.

    Pure and lock-guarded; no I/O, no clock of its own beyond the samples
    it is handed.
    """

    def __init__(self) -> None:
        self._srtt: Optional[float] = None
        self._rttvar: float = 0.0
        self._samples: List[float] = []

    def observe(self, rtt_s: float) -> None:
        r = max(0.0, float(rtt_s))
        self._samples.append(r)
        if len(self._samples) > rtt_window():
            del self._samples[0]
        if self._srtt is None:
            self._srtt = r
            self._rttvar = r / 2.0
            return
        # RFC 6298 constants: alpha=1/8, beta=1/4.
        self._rttvar = 0.75 * self._rttvar + 0.25 * abs(self._srtt - r)
        self._srtt = 0.875 * self._srtt + 0.125 * r

    @property
    def srtt(self) -> Optional[float]:
        return self._srtt

    def deadline_s(self) -> float:
        """How long to wait for a heartbeat ack before counting a miss."""
        floor = heartbeat_interval_s()
        if self._srtt is None:
            return floor
        return max(floor, self._srtt + 4.0 * self._rttvar)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "srtt_ms": round((self._srtt or 0.0) * 1000.0, 2),
            "rttvar_ms": round(self._rttvar * 1000.0, 2),
            "deadline_ms": round(self.deadline_s() * 1000.0, 2),
            "samples": len(self._samples),
        }


class LivenessMonitor:
    """Sliding-window dead-peer detection above a half-open socket.

    A miss is not a death. Wi-Fi loses single packets constantly, and a
    detector that killed the session on one would produce exactly the
    reconnect storm ``FlapBreaker`` exists to survive. ``miss_limit``
    consecutive misses — each measured against an RTT-derived deadline
    rather than a constant — is the signal.

    Any inbound traffic counts as liveness, not only an ack: a peer sending
    telemetry is demonstrably alive, and demanding a specific frame type as
    proof would declare a busy peer dead.
    """

    def __init__(self) -> None:
        self.rtt = RttEstimator()
        self._consecutive_misses = 0
        self._last_inbound_mono = time.monotonic()
        self._deaths = 0

    def note_inbound(self) -> None:
        self._consecutive_misses = 0
        self._last_inbound_mono = time.monotonic()

    def note_ack(self, sent_mono: float) -> None:
        self.rtt.observe(time.monotonic() - sent_mono)
        self.note_inbound()

    def note_miss(self) -> bool:
        """Record a missed deadline. True when the peer is now dead."""
        self._consecutive_misses += 1
        if self._consecutive_misses >= heartbeat_miss_limit():
            self._deaths += 1
            return True
        return False

    @property
    def silent_for_s(self) -> float:
        return max(0.0, time.monotonic() - self._last_inbound_mono)

    def snapshot(self) -> Dict[str, Any]:
        out = self.rtt.snapshot()
        out.update({
            "consecutive_misses": self._consecutive_misses,
            "silent_for_s": round(self.silent_for_s, 2),
            "miss_limit": heartbeat_miss_limit(),
            "deaths": self._deaths,
        })
        return out


# ---------------------------------------------------------------------------
# Reassembly — contiguity restored before anything is applied
# ---------------------------------------------------------------------------


@dataclass
class ReassemblyResult:
    ready: List[Any] = field(default_factory=list)
    buffered: int = 0
    gap_unrecoverable: bool = False
    reason: str = ""


class ReassemblyBuffer:
    """Holds out-of-order frames until contiguity is restored.

    **When frames actually arrive out of order.** Not on one TCP connection
    — TCP delivers a stream in order by definition, and any design premised
    on reordering *within* a connection is solving a problem that does not
    exist. The real case is a RECONNECT: replayed frames from the durable
    generation interleave with live ones produced while the replay was in
    flight, and the live ones can land first.

    So this buffer is small and its job is narrow: hold the ahead-of-time
    frames, release them the moment the hole fills, and release them
    **atomically** as a contiguous run so a consumer never sees a partial
    sequence it would have to reason about.

    Bounded. A gap that cannot be closed within capacity is not a buffering
    problem to be solved with more memory — it is a lost range, and the
    honest response is to say so and let ``plan_resume`` ask for a resync.
    """

    def __init__(self, next_expected: int = 1) -> None:
        self._next = max(1, int(next_expected))
        self._held: Dict[int, Any] = {}

    @property
    def next_expected(self) -> int:
        return self._next

    def offer(self, seq: int, frame: Any) -> ReassemblyResult:
        s = int(seq)
        if s < self._next:
            # Already applied — a duplicate from a replay. Not an error;
            # the sink's DeliveryLedger suppresses it idempotently.
            return ReassemblyResult(buffered=len(self._held),
                                    reason="duplicate")
        if s > self._next:
            if len(self._held) >= reassembly_capacity():
                return ReassemblyResult(
                    buffered=len(self._held), gap_unrecoverable=True,
                    reason=(f"gap at seq={self._next} still open with "
                            f"{len(self._held)} frames held — resync"),
                )
            self._held[s] = frame
            return ReassemblyResult(buffered=len(self._held),
                                    reason="held out of order")
        ready = [frame]
        self._next = s + 1
        while self._next in self._held:
            ready.append(self._held.pop(self._next))
            self._next += 1
        return ReassemblyResult(ready=ready, buffered=len(self._held),
                                reason="contiguous")

    def reset(self, next_expected: int) -> None:
        self._next = max(1, int(next_expected))
        self._held.clear()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "next_expected": self._next, "held": len(self._held),
            "capacity": reassembly_capacity(),
        }


# ---------------------------------------------------------------------------
# Handshake — limits are agreed, never assumed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NegotiatedLimits:
    max_frame_bytes: int
    heartbeat_s: float
    protocol_version: str
    peer_node_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_frame_bytes": self.max_frame_bytes,
            "heartbeat_s": self.heartbeat_s,
            "protocol_version": self.protocol_version,
            "peer_node_id": self.peer_node_id,
        }


def negotiate(local: Dict[str, Any], remote: Dict[str, Any]) -> NegotiatedLimits:
    """Reconcile two advertisements. Pure, so it is testable without a socket.

    **Minimum wins on every dimension.** A ceiling one side cannot honour is
    not a limit — it is a fault waiting for a large frame. The heartbeat
    takes the FASTER of the two (the shorter interval), because liveness is
    the one axis where the more demanding party should win: a peer that
    wants proof of life every second is not harmed by getting it, and a peer
    that needs it cannot be overruled by a lazier one.
    """
    def _int(d: Dict[str, Any], k: str, fallback: int) -> int:
        try:
            v = int(d.get(k, fallback))
            return v if v > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    def _float(d: Dict[str, Any], k: str, fallback: float) -> float:
        try:
            v = float(d.get(k, fallback))
            return v if v > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    return NegotiatedLimits(
        max_frame_bytes=min(_int(local, "max_frame_bytes", max_frame_bytes()),
                            _int(remote, "max_frame_bytes", max_frame_bytes())),
        heartbeat_s=min(_float(local, "heartbeat_s", heartbeat_interval_s()),
                        _float(remote, "heartbeat_s", heartbeat_interval_s())),
        protocol_version=str(remote.get("protocol_version")
                             or LINK_TRANSPORT_SCHEMA_VERSION),
        peer_node_id=str(remote.get("node_id") or "?"),
    )


def local_advertisement(node_id: str) -> Dict[str, Any]:
    return {
        "node_id": str(node_id),
        "protocol_version": LINK_TRANSPORT_SCHEMA_VERSION,
        "max_frame_bytes": max_frame_bytes(),
        "heartbeat_s": heartbeat_interval_s(),
    }


# ---------------------------------------------------------------------------
# TLS — identity, not merely encryption
# ---------------------------------------------------------------------------


def build_ssl_context(*, server_side: bool) -> Optional[ssl.SSLContext]:
    """Mutual-TLS context from ``.jarvis/brain_mtls/``. None when absent.

    The overlay network (Tailscale) supplies the tunnel; this supplies the
    IDENTITY. Only the second one answers "which Body is this?" after a
    session id is replayed — which is exactly the ``REJECT`` case
    ``plan_resume`` exists to catch. Encryption without authentication would
    let anything on the tailnet resume another Body's session.

    ``CERT_REQUIRED`` on both sides: the client verifies the Engine and the
    Engine verifies the client. A missing certificate returns None so the
    caller can refuse to start rather than silently downgrading — a link
    that quietly ran unauthenticated would be worse than one that did not
    run.
    """
    base = tls_dir()
    ca = base / "ca.pem"
    cert = base / ("server-cert.pem" if server_side else "client-cert.pem")
    key = base / ("server-key.pem" if server_side else "client-key.pem")
    missing = [str(p.name) for p in (ca, cert, key) if not p.exists()]
    if missing:
        logger.error(
            "[LinkTransport] mTLS material missing from %s: %s — refusing to "
            "start unauthenticated", base, ", ".join(missing))
        return None
    try:
        purpose = (ssl.Purpose.CLIENT_AUTH if server_side
                   else ssl.Purpose.SERVER_AUTH)
        ctx = ssl.create_default_context(purpose, cafile=str(ca))
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        if not server_side:
            # The peer is reached by tailnet NAME, and the certificate is
            # issued to that name. Hostname checking stays ON: turning it
            # off is how an mTLS deployment silently becomes encryption
            # without identity.
            ctx.check_hostname = True
        return ctx
    except (ssl.SSLError, OSError) as exc:
        logger.error("[LinkTransport] mTLS context failed: %s", exc)
        return None


def bind_host() -> str:
    """Address to bind. Default LOOPBACK, never ``0.0.0.0``.

    A default of ``0.0.0.0`` would listen on every interface — the home LAN,
    any café Wi-Fi the host joins, anything a router later forwards. One end
    of this link runs ``bash`` and mutates code, so the correct default is
    the narrowest thing that works, and the operator widens it deliberately
    to the tailnet address.
    """
    return _env_str("JARVIS_LINK_BIND_HOST", "127.0.0.1")


def bind_port() -> int:
    """Port to bind. 0 asks the OS to choose, which is what tests use."""
    return _env_int("JARVIS_LINK_PORT", 0, minimum=0)


def _tune_socket(sock: Optional[socket.socket]) -> None:
    """TCP options that make a stall visible sooner. Never raises.

    ``TCP_NODELAY`` because heartbeats are tiny and Nagle would coalesce
    them into exactly the delay the estimator is trying to measure.
    ``SO_KEEPALIVE`` as defence in depth — its timers are OS policy and not
    portably ours, which is why the application heartbeat is the detector
    that matters rather than a fallback.
    """
    if sock is None:
        return
    for level, opt in ((socket.IPPROTO_TCP, "TCP_NODELAY"),
                       (socket.SOL_SOCKET, "SO_KEEPALIVE")):
        try:
            flag = getattr(socket, opt, None)
            if flag is not None:
                sock.setsockopt(level, flag, 1)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Framed stream I/O — the codec is the transcript's, not a second one
# ---------------------------------------------------------------------------


class FrameTooLarge(ValueError):
    """A frame exceeded the NEGOTIATED ceiling."""


async def write_frame(
    writer: Any, record: Dict[str, Any], *, limits: NegotiatedLimits,
) -> int:
    """Encode, bound-check, write and DRAIN. Returns bytes written.

    ``drain()`` is the backpressure seam and is not optional: without it a
    producer faster than the link fills an in-process buffer instead of the
    socket's, which is buffer bloat relocated somewhere ``ss`` cannot see
    it. Awaiting drain suspends the producer at the exact rate the path
    allows.
    """
    from backend.core.ouroboros.battle_test.transcript_log import encode_record
    from backend.core.ouroboros.governance.link_protocol import (
        ensure_frame_envelope,
    )
    # Same validator the outbox uses. A frame that skips the envelope
    # encodes cleanly and is rejected only at the PEER's reader, where this
    # side can no longer do anything about it.
    frame = encode_record(ensure_frame_envelope(record))
    if len(frame) > limits.max_frame_bytes:
        raise FrameTooLarge(
            f"frame is {len(frame)}B, negotiated ceiling is "
            f"{limits.max_frame_bytes}B")
    writer.write(frame)
    await writer.drain()
    return len(frame)


async def read_frame(
    reader: Any, *, limits: NegotiatedLimits, timeout_s: float,
) -> Optional[Dict[str, Any]]:
    """One frame, or None on EOF. Raises ``asyncio.TimeoutError`` on stall.

    The timeout is the half-open detector: a peer that lost power sends no
    FIN, so the only evidence is that nothing arrived within a deadline the
    RTT estimator says is generous. ``readuntil`` is bounded by the
    negotiated ceiling, so a peer cannot make this side buffer without limit
    by omitting a terminator.
    """
    from backend.core.ouroboros.battle_test.transcript_log import decode_frame
    try:
        raw = await asyncio.wait_for(
            reader.readuntil(b"\n"), timeout=timeout_s)
    except asyncio.IncompleteReadError:
        return None                      # clean EOF
    except asyncio.LimitOverrunError as exc:
        raise FrameTooLarge(f"frame exceeds stream limit: {exc}") from exc
    if not raw:
        return None
    record, reason = decode_frame(raw.rstrip(b"\n"))
    if record is None:
        logger.warning("[LinkTransport] rejected frame: %s",
                       getattr(reason, "value", reason))
        return {"kind": "__rejected__", "reason": str(
            getattr(reason, "value", reason))}
    return record


def is_known_kind(kind: Any) -> bool:
    """Closed vocabulary: a newer peer cannot drive an older one off-path."""
    return str(kind) in _KNOWN_KINDS


# ---------------------------------------------------------------------------
# Adaptive batching — the congestion signal that exists at this layer
# ---------------------------------------------------------------------------


class AdaptiveBatcher:
    """Batch size follows observed drain latency.

    Not MTU (see the module docstring) but the one congestion signal
    genuinely visible above TCP: how long ``drain()`` took. A link whose
    drains are slow is congested, and shrinking the batch means a stall is
    noticed within one batch rather than after a large write has already
    been committed to a socket that will not accept it.

    Multiplicative decrease, additive increase — the shape that converges
    rather than oscillating, for the same reason TCP uses it.
    """

    def __init__(self) -> None:
        self._batch = float(_env_int("JARVIS_LINK_BATCH_START", 16, minimum=1))
        self._min = float(_env_int("JARVIS_LINK_BATCH_MIN", 1, minimum=1))
        self._max = float(_env_int("JARVIS_LINK_BATCH_MAX", 256, minimum=1))
        self._target_s = _env_float("JARVIS_LINK_DRAIN_TARGET_S", 0.05,
                                    minimum=0.001)

    def observe_drain(self, seconds: float) -> None:
        if seconds > self._target_s:
            self._batch = max(self._min, self._batch / 2.0)
        else:
            self._batch = min(self._max, self._batch + 1.0)

    @property
    def size(self) -> int:
        return max(1, int(self._batch))

    def snapshot(self) -> Dict[str, Any]:
        return {"batch": self.size, "target_drain_s": self._target_s,
                "min": int(self._min), "max": int(self._max)}
