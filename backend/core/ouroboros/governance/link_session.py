"""link_session — the loop that makes the Body/Engine link a running system.

WHAT THIS ASSEMBLES
-------------------
``link_protocol`` supplies the rules (clocks, resume planning, admission,
delivery). ``link_transport`` supplies the wire (framing, negotiation,
liveness, reassembly). ``link_outbox`` supplies durability. None of them
runs. This is the state machine that drives them, and it deliberately adds
no new rule of its own — every decision it makes is delegated to one of the
three, so a change in policy happens in one place and this loop does not
have to be re-reasoned about.

THE STATE MACHINE
-----------------
::

    DISCONNECTED ─connect─▶ HANDSHAKING ─welcome─▶ RESUMING ─plan─▶ ACTIVE
         ▲                       │                     │              │
         │                       ▼                     ▼              │
         └──────────────────── PARKED ◀────────── link fault ─────────┘

``PARKED`` is the state that makes this link honest. §26.6 established that
a detached operator's absence must not read as refusal; across a network the
link itself produces absence, so a fault parks the session rather than
failing the work. Ops stay queued, the outbox keeps its contents, the
logical clock keeps counting, and reconnection RESUMES rather than restarts.
Nothing infers a verdict from silence.

THREE FAILURE MODES THIS STRUCTURALLY PREVENTS
----------------------------------------------

**Handshake collision.** On a flapping link both ends can dial at the same
instant, each accepting the other's connection while its own is in flight.
Two live handshakes for one session id is split-brain: two ledgers, two
clocks, two opinions about what was applied. Resolved by a deterministic
tie-break both sides compute independently and identically — Lamport stamp
first, node id lexically second (see :func:`resolve_collision`). The loser
abandons its OWN handshake and keeps serving the peer's; the socket is not
severed, because severing it would drop the very connection that won.

**Re-keying mid-stream.** Certificate rotation must not cost the queue. It
does not, and not by special-casing: session state lives in the registry,
never in the connection, so a re-key is *a reconnect with a different SSL
context*. The outbox is untouched, unacknowledged frames are re-sent by the
resume path, and the state machine cannot tell the difference between a
re-key and a Wi-Fi drop. A dedicated "hot-swap" code path would be a second
way to do the thing the resume path already does correctly.

**Writer contention under backpressure.** Two producers — the outbox pump
(verdicts) and the SSE bridge (telemetry) — want the same socket. Locking
around a shared writer is how an event loop deadlocks under pressure. So
there is no shared writer: **one task owns the socket**, and both producers
enqueue into :class:`PriorityWriteQueue`. Contention is not managed, it is
removed. Verdicts outrank telemetry, telemetry is dropped rather than
blocking, and a bounded anti-starvation quantum guarantees the low lane
still moves under a sustained flood of the high one.

INVARIANT
---------
No lock is ever held across an ``await``. Every lock in this module guards a
handful of field assignments and is released before any I/O — which is the
single discipline that makes "deadlock under backpressure" unreachable
rather than merely unlikely.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance import link_protocol as proto
from backend.core.ouroboros.governance import proactive_mode as proto_mode
from backend.core.ouroboros.governance import link_transport as tx
from backend.core.ouroboros.governance.link_outbox import LinkOutbox

logger = logging.getLogger("Ouroboros.LinkSession")

LINK_SESSION_SCHEMA_VERSION: str = "link_session.1"


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


def telemetry_queue_depth() -> int:
    """Telemetry frames buffered before the oldest are dropped."""
    return _env_int("JARVIS_LINK_TELEMETRY_DEPTH", 256, minimum=8)


def starvation_quantum() -> int:
    """High-priority frames sent before the low lane is given a turn.

    Strict priority starves. A sustained verdict flood would silence
    telemetry entirely, and an operator watching a blank deck cannot tell a
    busy Engine from a dead one — the failure ``_ignition_line`` exists to
    prevent, reappearing across a network.
    """
    return _env_int("JARVIS_LINK_STARVATION_QUANTUM", 16, minimum=1)


def handshake_timeout_s() -> float:
    """Ceiling on one handshake. Bounded so a silent peer cannot pin a task."""
    return _env_float("JARVIS_LINK_HANDSHAKE_TIMEOUT_S", 10.0, minimum=0.5)


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


#: Kinds that carry the ORDERED, applied stream. Everything else is control
#: and bypasses reassembly — a heartbeat is not part of the history a resume
#: replays, and treating it as one puts holes in that history.
ORDERED_KINDS = frozenset({
    tx.KIND_VERDICT, tx.KIND_COMMAND, tx.KIND_TELEMETRY,
})


class SessionState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    HANDSHAKING = "handshaking"
    RESUMING = "resuming"
    ACTIVE = "active"
    PARKED = "parked"
    CLOSED = "closed"


class HandshakeError(RuntimeError):
    """A handshake could not complete. Always carries why."""


class CollisionLost(HandshakeError):
    """This side lost a deterministic handshake tie-break. Not a fault."""


# ---------------------------------------------------------------------------
# Handshake collision — decided identically on both ends
# ---------------------------------------------------------------------------


def resolve_collision(
    local: Tuple[int, str], remote: Tuple[int, str],
) -> bool:
    """True when the LOCAL handshake wins. Pure, total, and symmetric.

    Both ends run this with the arguments swapped and must reach opposite
    answers — that is the entire requirement, and it is why the comparison
    is a total order over ``(lamport, node_id)`` rather than a heuristic.
    A rule that could return "win" to both (a timestamp compared with
    tolerance, say) recreates the split-brain it was meant to prevent.

    Lamport first because it carries causality: a node that has observed
    more of the shared history is further along, and its view should
    survive. Node id second, lexically, because two concurrent stamps are
    genuinely tied and a tie needs an arbitrary but AGREED answer.

    Identical ``(lamport, node_id)`` means the peer is us — a loopback
    misconfiguration or a duplicated identity — and is refused rather than
    resolved, because there is no correct winner between a node and itself.
    """
    l_stamp, l_node = int(local[0]), str(local[1])
    r_stamp, r_node = int(remote[0]), str(remote[1])
    if (l_stamp, l_node) == (r_stamp, r_node):
        raise HandshakeError(
            f"peer advertises this node's own identity ({l_node!r}) — "
            "duplicate node id or a loopback misconfiguration")
    return (l_stamp, l_node) > (r_stamp, r_node)


# ---------------------------------------------------------------------------
# One writer, two lanes
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    record: Dict[str, Any]
    high: bool


class PriorityWriteQueue:
    """The socket's single ingress. Verdicts outrank telemetry; nothing blocks.

    **Why a queue and not a lock.** Two producers sharing a writer must
    serialise somehow. A lock around the writer serialises by BLOCKING, and
    a producer blocked while holding anything else is how an event loop
    deadlocks under backpressure. A queue serialises by HANDING OFF: the
    producers never touch the socket, one consumer does, and there is no
    lock to contend for.

    The two lanes have different contracts, matching §29.4:

    * **High (verdicts, commands, control).** Never dropped. If the link
      cannot carry them they belong in the durable outbox, not in RAM — so
      this lane is bounded only by the outbox's own policy upstream.
    * **Low (telemetry).** Lossy by design. A full lane drops its OLDEST
      and counts it, because stale telemetry is worthless and blocking a
      producer to preserve it converts a display problem into a stall.

    Anti-starvation is a bounded quantum rather than a fair-share weight:
    after ``starvation_quantum()`` high frames, one low frame goes out even
    if high work remains. Simple, provable, and enough — the goal is that
    the deck keeps moving, not that telemetry gets a guaranteed share.
    """

    def __init__(self) -> None:
        self._high: "asyncio.Queue[_Pending]" = asyncio.Queue()
        self._low: List[_Pending] = []
        self._wake = asyncio.Event()
        self._high_streak = 0
        self._dropped_low = 0
        self._sent_high = 0
        self._sent_low = 0

    def put_high(self, record: Dict[str, Any]) -> None:
        """Enqueue a verdict/command. Never blocks, never drops."""
        self._high.put_nowait(_Pending(record, True))
        self._wake.set()

    def put_low(self, record: Dict[str, Any]) -> bool:
        """Enqueue telemetry. False when it displaced an older frame."""
        dropped = False
        while len(self._low) >= telemetry_queue_depth():
            self._low.pop(0)
            self._dropped_low += 1
            dropped = True
        self._low.append(_Pending(record, False))
        self._wake.set()
        return not dropped

    async def get(self) -> _Pending:
        """Next frame to write, honouring priority and the quantum."""
        while True:
            if self._low and self._high_streak >= starvation_quantum():
                self._high_streak = 0
                self._sent_low += 1
                return self._low.pop(0)
            if not self._high.empty():
                self._high_streak += 1
                self._sent_high += 1
                return self._high.get_nowait()
            if self._low:
                self._high_streak = 0
                self._sent_low += 1
                return self._low.pop(0)
            self._wake.clear()
            await self._wake.wait()

    @property
    def depth(self) -> int:
        return self._high.qsize() + len(self._low)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "high_queued": self._high.qsize(), "low_queued": len(self._low),
            "sent_high": self._sent_high, "sent_low": self._sent_low,
            "telemetry_dropped": self._dropped_low,
            "quantum": starvation_quantum(),
        }


# ---------------------------------------------------------------------------
# The session loop
# ---------------------------------------------------------------------------


@dataclass
class SessionConfig:
    node_id: str
    session_id: str
    peer_host: str = ""
    peer_port: int = 0
    server_side: bool = False
    spill_path: Optional[Path] = None
    #: Injected so tests drive the loop without a socket, and so a re-key
    #: supplies a fresh context without this module knowing about certs.
    ssl_factory: Optional[Callable[[], Any]] = None


class LinkSessionLoop:
    """Drives one logical session across however many sockets it takes.

    The session outlives every connection it uses. Reconnection, re-keying
    and a Wi-Fi drop are the same event to this class, which is why none of
    them has a special path.
    """

    def __init__(
        self,
        config: SessionConfig,
        *,
        registry: Optional[proto.SessionRegistry] = None,
        breaker: Optional[proto.FlapBreaker] = None,
        outbox: Optional[LinkOutbox] = None,
        on_frame: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.config = config
        self.registry = registry or proto.SessionRegistry()
        self.breaker = breaker or proto.FlapBreaker()
        self.outbox = outbox or LinkOutbox(spill_path=config.spill_path)
        self.queue = PriorityWriteQueue()
        self.liveness = tx.LivenessMonitor()
        self.batcher = tx.AdaptiveBatcher()
        self.reassembly = tx.ReassemblyBuffer()
        self._on_frame = on_frame
        self._state = SessionState.DISCONNECTED
        self._limits: Optional[tx.NegotiatedLimits] = None
        self._session: Optional[proto.LinkSession] = None
        self._ctrl_seq = 0
        self._data_seq = 0
        self._attempt = 0
        self._handshake_in_flight: Optional[Tuple[int, str]] = None
        self._resyncs = 0
        self._parks = 0
        #: §30 slice 5 — what the PEER's dial says. Confirmation-gated, so a
        #: reconnect invalidates it until the Engine speaks again. Owned here
        #: rather than globally because it is a property of THIS session.
        self.remote_mode = proto_mode.RemoteModeView()
        self._last_mode_sent: Optional[str] = None

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    def _transition(self, to: SessionState, reason: str = "") -> None:
        if to is self._state:
            return
        logger.info("[LinkSession] %s → %s%s", self._state.value, to.value,
                    f" ({reason})" if reason else "")
        self._state = to
        if to is SessionState.PARKED:
            self._parks += 1

    def next_seq(self) -> int:
        """Next CONTROL sequence. 1-indexed; 0 is reserved for 'nothing yet'."""
        self._ctrl_seq += 1
        return self._ctrl_seq

    def next_data_seq(self) -> int:
        """Next ORDERED-DATA sequence.

        **Two spaces, deliberately.** Control frames — hello, welcome,
        heartbeat — are not part of the ordered stream and are never
        "applied", so they must not consume data sequence numbers. Sharing
        one counter looks tidy and is a correctness bug in two directions:

        * the handshake consumes seq 1, so the peer's reassembly waits
          forever for a data frame that will never exist;
        * a heartbeat that is dropped or reordered punches a permanent hole
          in the data sequence, and ``plan_resume`` — which is defined over
          APPLIED frames — would be asked to replay a range containing
          numbers no data frame ever carried.

        The receiver bypasses reassembly for control kinds, so the two
        spaces never interact and a collision between them is meaningless.
        """
        self._data_seq += 1
        return self._data_seq

    # -- handshake -------------------------------------------------------

    def build_hello(self) -> Dict[str, Any]:
        """The advertisement, stamped so a collision can be resolved."""
        sess = self._session
        clock = sess.clock if sess else proto.LogicalClock(self.config.node_id)
        lamport = clock.tick()
        self._handshake_in_flight = (lamport, self.config.node_id)
        frame = dict(tx.local_advertisement(self.config.node_id))
        frame.update({
            "kind": tx.KIND_HELLO, "seq": self.next_seq(),
            "lamport": lamport, "session_id": self.config.session_id,
            "last_applied": self._last_applied(),
        })
        return frame

    def _last_applied(self) -> int:
        sess = self._session
        return sess.ledger.contiguous_applied if sess else 0

    def on_hello(self, hello: Dict[str, Any]) -> Dict[str, Any]:
        """Handle an inbound HELLO, resolving a collision if one exists.

        Raises :class:`CollisionLost` when this side's own in-flight
        handshake loses — which the caller treats as "abandon mine, keep
        serving theirs", NOT as a transport fault. Severing the socket here
        would drop the connection that just won.
        """
        remote_stamp = (int(hello.get("lamport") or 0),
                        str(hello.get("node_id") or "?"))
        if self._handshake_in_flight is not None:
            mine = self._handshake_in_flight
            if not resolve_collision(mine, remote_stamp):
                logger.info(
                    "[LinkSession] handshake collision: local %s loses to "
                    "peer %s — abandoning our attempt, keeping theirs",
                    mine, remote_stamp)
                self._handshake_in_flight = None
                raise CollisionLost(f"local {mine} < peer {remote_stamp}")
            logger.info(
                "[LinkSession] handshake collision: local %s wins over peer "
                "%s", self._handshake_in_flight, remote_stamp)

        session, resumed = self.registry.attach(
            str(hello.get("session_id") or self.config.session_id),
            str(hello.get("node_id") or "?"))
        self._session = session
        session.clock.observe(remote_stamp[0])
        self._limits = tx.negotiate(
            tx.local_advertisement(self.config.node_id), hello)
        welcome = dict(tx.local_advertisement(self.config.node_id))
        welcome.update({
            "kind": tx.KIND_WELCOME, "seq": self.next_seq(),
            "lamport": session.clock.tick(),
            "session_id": session.session_id, "resumed": resumed,
            "last_applied": self._last_applied(),
        })
        self._transition(SessionState.RESUMING,
                         "resumed" if resumed else "new session")
        return welcome

    def on_welcome(self, welcome: Dict[str, Any]) -> proto.ResumePlan:
        """Accept the peer's WELCOME and compute what we are owed."""
        self._handshake_in_flight = None
        self._limits = tx.negotiate(
            tx.local_advertisement(self.config.node_id), welcome)
        session, _ = self.registry.attach(
            str(welcome.get("session_id") or self.config.session_id),
            str(welcome.get("node_id") or "?"))
        self._session = session
        session.clock.observe(int(welcome.get("lamport") or 0))
        plan = proto.plan_resume(
            peer_last_applied=self._last_applied(),
            source_latest=int(welcome.get("source_latest")
                              or welcome.get("last_applied") or 0),
            source_oldest_retained=int(welcome.get("oldest_retained") or 1),
        )
        if plan.action is proto.ResumeAction.RESYNC:
            self._resyncs += 1
            self.reassembly.reset(next_expected=plan.to_seq + 1)
            logger.warning("[LinkSession] %s", plan.reason)
        elif plan.action is proto.ResumeAction.REJECT:
            self._transition(SessionState.DISCONNECTED, "incoherent peer")
            raise HandshakeError(plan.reason)
        self._transition(SessionState.ACTIVE, plan.action.value)
        return plan

    # -- inbound ---------------------------------------------------------

    def dispatch(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fold one inbound frame in. Returns frames ready to APPLY.

        Reassembly then idempotent application, in that order: contiguity is
        restored before anything is applied, so a consumer never sees a
        partial sequence it would have to reason about.
        """
        self.liveness.note_inbound()
        kind = record.get("kind")
        if not tx.is_known_kind(kind):
            logger.debug("[LinkSession] dropped unknown kind %r", kind)
            return []
        sess = self._session
        if sess is not None:
            sess.clock.observe(record.get("lamport"))
        seq = record.get("seq")
        if not isinstance(seq, int):
            return []
        if kind == tx.KIND_MODE:
            # Confirmed BEFORE the control return below, so a mode frame can
            # never be counted as seen without also being applied — the two
            # would drift and the Body would show a rung it had received and
            # discarded.
            self.remote_mode.confirm(record)
            return [record]
        if kind not in ORDERED_KINDS:
            # Control: acknowledged by having been seen, never ordered and
            # never applied. Returning it lets a caller react (a heartbeat
            # ack, a resume plan) without it entering the replayed history.
            return [record]
        result = self.reassembly.offer(seq, record)
        if result.gap_unrecoverable:
            self._resyncs += 1
            logger.warning("[LinkSession] %s", result.reason)
            return []
        ready: List[Dict[str, Any]] = []
        for frame in result.ready:
            lf = proto.LinkFrame(
                seq=int(frame.get("seq") or 0),
                lamport=int(frame.get("lamport") or 0),
                node_id=str(frame.get("node_id") or "?"),
                kind=str(frame.get("kind") or ""), payload=frame,
            )
            applied = True
            if sess is not None:
                applied = sess.ledger.apply_once(
                    lf, lambda _f, fr=frame: self._apply(fr))
            elif self._on_frame is not None:
                self._apply(frame)
            if applied:
                ready.append(frame)
        return ready

    def _apply(self, frame: Dict[str, Any]) -> None:
        if self._on_frame is not None:
            self._on_frame(frame)

    # -- outbound --------------------------------------------------------

    def send_verdict(self, payload: Dict[str, Any]) -> None:
        """Queue a governance verdict. Durable, prioritised, never dropped."""
        record = dict(payload)
        record.setdefault("kind", tx.KIND_VERDICT)
        record["seq"] = self.next_data_seq()
        record["lamport"] = self._session.clock.tick() if self._session else 0
        record["node_id"] = self.config.node_id
        self.outbox.put(record)
        self.queue.put_high(record)

    def publish_mode(self, *, force: bool = False) -> bool:
        """Announce the effective rung to the peer. True when a frame was sent.

        EDGE-TRIGGERED, not periodic. A rung is a state, not a measurement,
        so re-sending an unchanged one would spend the link's budget
        restating a fact the peer already holds. ``force`` exists for the
        one case where the peer's knowledge is genuinely void: a fresh
        connection, where the Body has invalidated everything and is
        rendering *unknown* until told.

        Sent on the HIGH lane: a mode frame is governance, not telemetry.
        The lossy lane would drop it under exactly the pressure that makes
        an accurate autonomy display matter most.
        """
        try:
            frame = proto_mode.build_mode_frame(
                seq=self.next_seq(), node_id=self.config.node_id,
                lamport=(self._session.clock.tick() if self._session else 0),
            )
            if not force and frame["position"] == self._last_mode_sent:
                return False
            self.queue.put_high(frame)
            self._last_mode_sent = str(frame["position"])
            return True
        except Exception:  # noqa: BLE001 — the dial never breaks the link
            logger.debug("[LinkSession] mode publish degraded", exc_info=True)
            return False

    def send_telemetry(self, payload: Dict[str, Any]) -> bool:
        """Queue a telemetry frame. Lossy by contract, never durable."""
        record = dict(payload)
        record.setdefault("kind", tx.KIND_TELEMETRY)
        record["seq"] = self.next_data_seq()
        record["lamport"] = self._session.clock.tick() if self._session else 0
        record["node_id"] = self.config.node_id
        return self.queue.put_low(record)

    # -- the pump --------------------------------------------------------

    async def pump_writes(self, writer: Any) -> None:
        """Sole owner of the socket's write side. Runs until cancelled.

        Backpressure arrives through ``write_frame``'s ``drain()``: when the
        kernel buffer fills this coroutine suspends, the queue grows, and
        telemetry begins dropping at its own bound. No lock is held across
        that await — there is no lock at all — so a slow link throttles the
        producers without any possibility of deadlock.
        """
        limits = self._limits or tx.negotiate({}, {})
        while True:
            pending = await self.queue.get()
            started = time.monotonic()
            try:
                await tx.write_frame(writer, pending.record, limits=limits)
            except tx.FrameTooLarge as exc:
                # Refused before the wire. A verdict too large is a caller
                # bug and must be loud; dropping it silently would be
                # indistinguishable from an op that never ran.
                logger.error("[LinkSession] frame refused: %s", exc)
                continue
            self.batcher.observe_drain(time.monotonic() - started)

    async def pump_heartbeat(self, writer: Any) -> None:
        """Prove liveness on a cadence the RTT estimator sets.

        The wait is on the estimator's deadline, not a constant, and it is
        the ONLY timer in this module. It exists because a silent peer
        produces no event to react to — the absence IS the signal — which is
        the one case where a timer is the correct mechanism rather than a
        substitute for one.
        """
        while True:
            interval = max(tx.heartbeat_interval_s(),
                           self.liveness.rtt.deadline_s() / 2.0)
            await asyncio.sleep(interval)
            sent = time.monotonic()
            self.queue.put_high({
                "kind": tx.KIND_HEARTBEAT, "seq": self.next_seq(),
                "lamport": self._session.clock.tick() if self._session else 0,
                "node_id": self.config.node_id, "sent_mono": sent,
            })
            if self.liveness.silent_for_s > self.liveness.rtt.deadline_s():
                if self.liveness.note_miss():
                    raise ConnectionError(
                        f"peer silent for {self.liveness.silent_for_s:.1f}s "
                        f"across {tx.heartbeat_miss_limit()} deadlines — "
                        "declaring the link dead")

    async def pump_outbox(self, writer: Any) -> None:
        """Re-offer durable frames the peer has not acknowledged.

        Drains a batch, hands it to the priority queue, and acks only what
        the queue accepted. Batch size follows observed drain latency, so a
        congested link re-offers less and notices a stall sooner.
        """
        while True:
            batch = self.outbox.drain(self.batcher.size)
            if not batch:
                await asyncio.sleep(tx.heartbeat_interval_s() / 2.0)
                continue
            from backend.core.ouroboros.battle_test.transcript_log import (
                decode_frame,
            )
            accepted = 0
            for raw in batch:
                record, _reason = decode_frame(raw.rstrip(b"\n"))
                if record is None:
                    accepted += 1        # unusable; acking clears it
                    continue
                self.queue.put_high(record)
                accepted += 1
            self.outbox.ack(accepted)

    # -- lifecycle -------------------------------------------------------

    def park(self, reason: str) -> None:
        """The link failed. Hold everything; infer nothing.

        Explicitly NOT a failure of the work: §26.6's rule crossing the
        network. The outbox keeps its contents, the ledger keeps its
        watermark, the session stays in the registry, and reconnection
        resumes from exactly here.
        """
        self.registry.detach(self.config.session_id)
        self._handshake_in_flight = None
        # The peer's rung is no longer confirmable. NOT void — a park is a
        # pause, and advancing the epoch here would let an in-flight frame
        # from this connection confirm the next one.
        self.remote_mode.on_disconnected()
        self._transition(SessionState.PARKED, reason)

    def reconnect_delay_s(self) -> float:
        """Backoff for the next attempt. Also the re-key path.

        A re-key is a reconnect with a different SSL context — same state,
        same queue, same ledger — so it needs no separate machinery.
        """
        self._attempt += 1
        return proto.backoff_delay_s(self._attempt)

    def on_established(self) -> None:
        # A new connection voids every prior confirmation: the Body renders
        # *unknown* until the Engine speaks, rather than carrying a rung
        # across a gap it cannot vouch for.
        self.remote_mode.on_connected()
        self._last_mode_sent = None
        self._attempt = 0
        self.breaker.on_established(self.config.node_id)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": LINK_SESSION_SCHEMA_VERSION,
            "state": self._state.value,
            "session_id": self.config.session_id,
            "node_id": self.config.node_id,
            "ctrl_seq": self._ctrl_seq,
            "data_seq": self._data_seq,
            "last_applied": self._last_applied(),
            "resyncs": self._resyncs,
            "parks": self._parks,
            "limits": self._limits.to_dict() if self._limits else None,
            "queue": self.queue.snapshot(),
            "outbox": self.outbox.stats().to_dict(),
            "liveness": self.liveness.snapshot(),
            "reassembly": self.reassembly.snapshot(),
            "remote_mode": self.remote_mode.snapshot(),
            "registry": self.registry.snapshot(),
            "breaker": self.breaker.snapshot(),
        }


# ---------------------------------------------------------------------------
# SSE bridge — the telemetry lane, fed from the existing broker
# ---------------------------------------------------------------------------


class SseTelemetryBridge:
    """Forwards the observability stream onto the link's low lane.

    Injected rather than importing the broker directly, for two reasons: the
    loop stays testable without one, and a re-key or reconnect swaps the
    sink without the broker knowing a network exists. The broker's own
    contract already matches the lane's — bounded, drop-oldest, lossy — so
    this adapter adds a hop and no policy.
    """

    def __init__(self, session: LinkSessionLoop) -> None:
        self._session = session
        self._forwarded = 0
        self._suppressed = 0

    def on_event(self, event: Any) -> None:
        """Sink for one broker event. NEVER raises into the broker."""
        try:
            payload = (event.to_dict() if hasattr(event, "to_dict")
                       else dict(event))
            payload["kind"] = tx.KIND_TELEMETRY
            if self._session.send_telemetry(payload):
                self._forwarded += 1
            else:
                self._suppressed += 1
        except Exception:  # noqa: BLE001 — telemetry never breaks its producer
            self._suppressed += 1

    def snapshot(self) -> Dict[str, Any]:
        return {"forwarded": self._forwarded, "suppressed": self._suppressed}
