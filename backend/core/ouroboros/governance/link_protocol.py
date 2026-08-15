"""link_protocol — the Body/Engine link, as logic with no socket in it.

THE TOPOLOGY THIS SERVES
------------------------
The Body (macOS) keeps the sensor edge: CoreAudio, AppKit, Quartz capture,
the cockpit. The Engine (Windows/WSL2) holds the accelerator and runs the
11-phase FSM. They are separate machines on separate premises, reached over
an overlay network (Tailscale/mTLS) whose availability is nobody's to
assume.

Everything here is pure: clocks, sequence arithmetic, admission decisions,
resume planning. No sockets, no asyncio, no transport. That is deliberate —
a distributed protocol whose correctness can only be observed by running two
machines is a protocol that will be debugged in production. This module is
the part that can be proven at a desk, and both ends import the SAME
implementation, so the two sides cannot disagree about the rules by drifting
apart in two codebases.

WHY THREE PRIMITIVES, AND NOT A FRAMEWORK
-----------------------------------------
The link has exactly three hard problems. Each gets one primitive, and
nothing here is general-purpose beyond them.

**1. The two clocks are not comparable.** The Mac's wall clock and a WSL2
VM's wall clock drift — the VM's especially, since it is suspended and
resumed with the host and re-syncs on its own schedule. Any rule of the form
"reject frames older than N seconds" computed across that boundary is a
random number generator. So:

    A timestamp that crosses a machine boundary is DATA, never a clock.

Ordering uses a Lamport clock (:class:`LogicalClock`); durations use each
side's own ``time.monotonic()`` and are never differenced against the peer's.
A remote wall-clock stamp may be displayed to an operator and may never
reach a decision.

**2. A reconnect must not create state.** Roaming access points produce
connection flapping — fifty attempts in ten seconds is ordinary, not
pathological. That only exhausts an engine if each *connection* allocates a
session, a queue and a task. So session identity is separate from connection
identity (:class:`SessionRegistry`): the socket is disposable, the session
survives it, and a reconnect RESUMES. Admission is then a bounded question
about attempt rate (:class:`FlapBreaker`) rather than an unbounded one about
handler count.

**3. A verdict can be lost mid-flight.** The Engine finishes a heavy op,
emits the verdict, and the socket ruptures between ``send`` and ``recv``.
Exactly-once *delivery* is unachievable over an unreliable link; exactly-once
*effect* is achievable, and is the difference between a protocol that works
and one that hopes. It needs three things, all present here: a durable
monotonic sequence at the source, replay of a range on request
(:class:`ResumePlan`), and idempotent application at the sink
(:class:`DeliveryLedger`).

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not transport bytes, hold a socket, or own a task. The telemetry
lane belongs to ``ide_observability_stream`` — a bounded broker with
``Last-Event-ID`` replay, drop-oldest backpressure and a heartbeat, which is
already the correct shape for lossy high-frequency frames. Durability of the
command lane belongs to ``durable_io`` — ``atomic_replace`` plus the fsync
discipline that makes "we wrote it" mean "it survives". Neither is
reimplemented here; this module supplies the ORDERING and ADMISSION rules
those two lanes execute.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.LinkProtocol")

LINK_PROTOCOL_SCHEMA_VERSION: str = "link_protocol.1"


# ---------------------------------------------------------------------------
# Env helpers — same shape as every other governance module here.
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Master switch. Default **false** pending graduation."""
    return _env_bool("JARVIS_LINK_BRIDGE_ENABLED", False)


# ---------------------------------------------------------------------------
# 1. Logical time — immune to NTP desync by construction
# ---------------------------------------------------------------------------


class LogicalClock:
    """A Lamport clock: ordering without a shared notion of "now".

    Two rules, and they are the whole algorithm:

    * every local event increments the counter (:meth:`tick`);
    * every received frame sets the counter to ``max(local, remote) + 1``
      (:meth:`observe`).

    What that buys is the only ordering guarantee that survives clock drift:
    **if event A caused event B, then ``A.lamport < B.lamport``.** The
    converse does not hold — two concurrent events may carry any relative
    values — and that is not a defect. Concurrency is a real property of a
    distributed system, and a protocol that pretends to totally order
    genuinely-concurrent events has invented information.

    Ties break on ``node_id`` when a total order is required for display, so
    the two ends render history in the same sequence without either one's
    wall clock being consulted.

    Thread-safe. Monotone by construction: the counter never moves backwards,
    which is precisely what a re-synced NTP daemon does to a wall clock.
    """

    def __init__(self, node_id: str) -> None:
        self._node_id = str(node_id)
        self._lock = threading.Lock()
        self._counter = 0

    @property
    def node_id(self) -> str:
        return self._node_id

    def tick(self) -> int:
        """Stamp a locally-originated event."""
        with self._lock:
            self._counter += 1
            return self._counter

    def observe(self, remote: Optional[int]) -> int:
        """Fold a peer's stamp in on receipt. Returns the new local value.

        A malformed or absent remote stamp advances the clock anyway rather
        than raising: a peer that cannot count must not be able to stop this
        side from ordering its own events.
        """
        try:
            remote_val = int(remote) if remote is not None else 0
        except (TypeError, ValueError):
            remote_val = 0
        with self._lock:
            self._counter = max(self._counter, max(0, remote_val)) + 1
            return self._counter

    def peek(self) -> int:
        with self._lock:
            return self._counter

    def stamp(self) -> Tuple[int, str]:
        """``(lamport, node_id)`` — the tuple that totally orders for display."""
        return self.tick(), self._node_id


def happens_before(a: Tuple[int, str], b: Tuple[int, str]) -> bool:
    """Total order over ``(lamport, node_id)`` stamps.

    Causal where causality exists, deterministic where it does not — which
    is what a UI needs to render two machines' events in one list without
    either side's wall clock being involved.
    """
    return (int(a[0]), str(a[1])) < (int(b[0]), str(b[1]))


@dataclass(frozen=True)
class LinkFrame:
    """One unit crossing the link.

    ``sender_wall_ns`` is carried for HUMAN display and forensics only. It is
    named to make misuse obvious at the call site: any code that differences
    it against a local clock has reintroduced the drift bug this module
    exists to remove.
    """

    seq: int
    lamport: int
    node_id: str
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    #: DISPLAY ONLY. Never an input to a decision. See the class docstring.
    sender_wall_ns: int = 0
    schema_version: str = LINK_PROTOCOL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "seq": self.seq,
            "lamport": self.lamport, "node_id": self.node_id,
            "kind": self.kind, "payload": self.payload,
            "sender_wall_ns": self.sender_wall_ns,
        }

    @staticmethod
    def from_dict(raw: Any) -> Optional["LinkFrame"]:
        """Parse a frame off the wire. Returns None on anything malformed.

        Never raises and never partially trusts: a frame missing its ordering
        fields is not a frame with defaults, it is garbage, and admitting it
        with ``seq=0`` would corrupt the resume arithmetic of a link that was
        otherwise healthy.
        """
        try:
            if not isinstance(raw, dict):
                return None
            seq = int(raw["seq"])
            lamport = int(raw["lamport"])
            node_id = str(raw["node_id"])
            kind = str(raw["kind"])
            if seq < 0 or lamport < 0 or not node_id or not kind:
                return None
            payload = raw.get("payload")
            return LinkFrame(
                seq=seq, lamport=lamport, node_id=node_id, kind=kind,
                payload=payload if isinstance(payload, dict) else {},
                sender_wall_ns=int(raw.get("sender_wall_ns") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None


def ensure_frame_envelope(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``record`` conforming to the shared frame contract, or raise.

    **One validator, both directions, both media.** The wire codec and the
    spill file are the same encoder (``transcript_log``), and that encoder
    enforces an envelope — ``v``/``seq``/``kind``/``ref``, with ``seq``
    a positive integer. A record that skips it encodes cleanly and is
    rejected only later: on the peer's reader, or worse, by ``recover_log``
    after a restart, which loses exactly the verdicts durability existed to
    protect.

    So the check lives HERE, in the module both ends import, and is called
    by the outbox before it spills and by the transport before it writes.
    Validating in two places with two opinions is how the two sides of a
    link start disagreeing about what a frame is.

    ``seq`` is never invented. It is load-bearing for resume arithmetic, and
    a fabricated one would put a hole in a range the peer will later ask to
    replay. ``seq=0`` is refused because the codec reserves it, and because
    :func:`plan_resume` reads 0 as "this peer has applied nothing" — one
    value cannot mean both.

    Raises ``ValueError``; callers catch at their boundary so the fault
    lands on the side that can still do something about it.
    """
    if not isinstance(record, dict):
        raise ValueError(f"frame must be a mapping, got {type(record).__name__}")
    out = dict(record)
    kind = out.get("kind")
    if not kind or not isinstance(kind, str):
        raise ValueError("frame requires a non-empty string 'kind'")
    seq = out.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError(
            f"frame requires an integer seq >= 1 (got {seq!r}); "
            "seq=0 is reserved for 'nothing yet'")
    out.setdefault("ref", f"l-{seq}")
    return out


# ---------------------------------------------------------------------------
# 2. Flap admission — the socket is disposable, the session is not
# ---------------------------------------------------------------------------


def flap_window_s() -> float:
    """Rolling window over which connection attempts are counted."""
    return _env_float("JARVIS_LINK_FLAP_WINDOW_S", 10.0, minimum=0.5)


def flap_max_attempts() -> int:
    """Attempts per identity per window before the breaker opens."""
    return _env_int("JARVIS_LINK_FLAP_MAX_ATTEMPTS", 8, minimum=1)


def flap_open_s() -> float:
    """How long the breaker stays open once tripped."""
    return _env_float("JARVIS_LINK_FLAP_OPEN_S", 15.0, minimum=0.5)


def flap_max_identities() -> int:
    """Hard cap on tracked identities — a memory bound, not a policy.

    This topology has one Body, so the steady state is a handful. The cap
    exists for the pathological case: a peer cycling session ids faster than
    the window retires them.
    """
    return _env_int("JARVIS_LINK_FLAP_MAX_IDENTITIES", 512, minimum=8)


def backoff_base_s() -> float:
    return _env_float("JARVIS_LINK_BACKOFF_BASE_S", 0.5, minimum=0.05)


def backoff_max_s() -> float:
    return _env_float("JARVIS_LINK_BACKOFF_MAX_S", 30.0, minimum=1.0)


class AdmitVerdict(str, enum.Enum):
    ADMIT = "admit"
    THROTTLE = "throttle"


@dataclass(frozen=True)
class AdmitDecision:
    verdict: AdmitVerdict
    retry_after_s: float
    attempts_in_window: int
    reason: str

    @property
    def admitted(self) -> bool:
        return self.verdict is AdmitVerdict.ADMIT


class FlapBreaker:
    """Bounded admission for reconnect storms.

    **The failure.** A roaming access point drops and restores the Mac's link
    fifty times in ten seconds. Each attempt reaches the Engine. If every
    connection allocates a handler task, a queue and a session, the Engine
    exhausts itself defending against a client that is not attacking it.

    **Why this is admission control and not a retry policy.** Client-side
    backoff is necessary and insufficient: it is advice the client may fail to
    follow, and a client already misbehaving is exactly the one that will not.
    The Engine therefore decides, per identity, in a rolling window, before
    any resource is committed. :meth:`retry_after` gives the client a number
    to obey, so a cooperative peer converges rather than guessing.

    Deterministic and lock-guarded; no timers, no background task, and never
    a sleep — the caller schedules, this only decides. Nothing here touches
    the event loop.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._attempts: Dict[str, List[float]] = {}
        self._open_until: Dict[str, float] = {}
        self._tripped = 0

    def admit(self, identity: str) -> AdmitDecision:
        """May this identity open a connection right now?"""
        ident = str(identity or "?")
        now = self._clock()
        window = flap_window_s()
        limit = flap_max_attempts()
        with self._lock:
            open_until = self._open_until.get(ident, 0.0)
            if now < open_until:
                return AdmitDecision(
                    AdmitVerdict.THROTTLE, max(0.0, open_until - now),
                    len(self._attempts.get(ident, ())),
                    "breaker open",
                )
            bucket = [t for t in self._attempts.get(ident, ()) if t >= now - window]
            bucket.append(now)
            self._attempts[ident] = bucket
            self._bound_locked(now, window)
            if len(bucket) > limit:
                until = now + flap_open_s()
                self._open_until[ident] = until
                self._tripped += 1
                logger.warning(
                    "[LinkProtocol] flap breaker OPEN for %s — %d attempts "
                    "in %.0fs; holding off %.0fs",
                    ident, len(bucket), window, flap_open_s(),
                )
                return AdmitDecision(
                    AdmitVerdict.THROTTLE, flap_open_s(), len(bucket),
                    "attempt rate exceeded",
                )
            return AdmitDecision(
                AdmitVerdict.ADMIT, 0.0, len(bucket), "within rate",
            )

    def _bound_locked(self, now: float, window: float) -> None:
        """Hold the identity map under a HARD cap. Caller holds the lock.

        Two-stage, and the second stage is the one that matters. Pruning
        entries older than the window is the cheap case: an identity that has
        not tried in a full window cannot be flapping, so its bucket is not
        evidence and dropping it costs nothing.

        But staleness alone does not bound anything. A peer cycling session
        identities — a buggy client regenerating an id per attempt, or a
        hostile one doing it deliberately — produces thousands of ENTIRELY
        FRESH identities inside a single window, none of them stale, and the
        map grows without limit. On a link reachable across an overlay
        network that is a memory-exhaustion vector, and it is reached by the
        very traffic pattern this class exists to survive.

        So the second stage is a hard capacity bound: evict by least-recent
        activity until the map fits. Evicting an active flapper only costs it
        a forgotten history — it re-accumulates on its next attempt and trips
        again — whereas an unbounded map costs the process.
        """
        cap = flap_max_identities()
        if len(self._attempts) <= cap:
            return
        horizon = now - window
        for key, times in list(self._attempts.items()):
            if not times or max(times) < horizon:
                self._attempts.pop(key, None)
                self._open_until.pop(key, None)
        if len(self._attempts) <= cap:
            return
        by_recency = sorted(
            self._attempts.items(), key=lambda kv: max(kv[1], default=0.0))
        for key, _ in by_recency[: len(self._attempts) - cap]:
            self._attempts.pop(key, None)
            self._open_until.pop(key, None)
        logger.debug(
            "[LinkProtocol] flap map bounded to %d identities", cap)

    def on_established(self, identity: str) -> None:
        """A connection succeeded and stayed up — retire its flap history.

        Without this a client that legitimately reconnects once an hour
        accumulates attempts forever and is eventually throttled for being
        long-lived, which is the opposite of the intent.
        """
        with self._lock:
            self._attempts.pop(str(identity or "?"), None)
            self._open_until.pop(str(identity or "?"), None)

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            return {
                "tracked_identities": len(self._attempts),
                "open_breakers": sum(1 for t in self._open_until.values()
                                     if t > now),
                "tripped_total": self._tripped,
                "window_s": flap_window_s(),
                "max_attempts": flap_max_attempts(),
            }


def backoff_delay_s(attempt: int, *, jitter: float = 0.0) -> float:
    """Exponential backoff with caller-supplied jitter. Pure.

    Jitter is a PARAMETER rather than an internal ``random()`` call so the
    schedule is reproducible under test. A caller that omits it gets a
    deterministic ramp — correct, and thundering-herd prone only if many
    clients share one Engine, which this topology does not.
    """
    n = max(0, int(attempt))
    delay = backoff_base_s() * (2 ** min(n, 16))
    delay = min(delay, backoff_max_s())
    return max(0.0, delay * (1.0 + max(-0.5, min(0.5, jitter))))


# ---------------------------------------------------------------------------
# 3. Resume — exactly-once EFFECT across a rupture
# ---------------------------------------------------------------------------


class ResumeAction(str, enum.Enum):
    CONTINUE = "continue"        # nothing missed
    REPLAY = "replay"            # send [from_seq, to_seq]
    RESYNC = "resync"            # gap exceeds retention — snapshot instead
    REJECT = "reject"            # the peer's claim is not coherent


@dataclass(frozen=True)
class ResumePlan:
    action: ResumeAction
    from_seq: int = 0
    to_seq: int = 0
    reason: str = ""
    schema_version: str = LINK_PROTOCOL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "action": self.action.value,
            "from_seq": self.from_seq, "to_seq": self.to_seq,
            "reason": self.reason,
        }


def plan_resume(
    *,
    peer_last_applied: int,
    source_latest: int,
    source_oldest_retained: int,
) -> ResumePlan:
    """What the Engine owes a reconnecting Body.

    **The split-brain case this exists for.** The Engine completes a heavy
    op, emits the verdict, and the socket ruptures mid-transmission. The Body
    never applied it. On reconnect the Body states the last sequence it
    applied CONTIGUOUSLY — not the highest it has seen, which after an
    out-of-order delivery would silently skip the hole — and the Engine
    replays the range from its durable spine.

    Four outcomes, and the third is the one that keeps the link honest:

    ``CONTINUE`` the Body is current.
    ``REPLAY``   the gap is inside retention; send it.
    ``RESYNC``   the gap is older than anything retained. The Engine CANNOT
                 close it, and pretending otherwise by resuming from the
                 oldest retained record would silently drop everything before
                 it. The Body must rebuild from a snapshot and be told so —
                 the same honesty ``ide_observability_stream`` already
                 practises when it marks a replay ``known: false``.
    ``REJECT``   the Body claims to have applied more than the Engine ever
                 emitted. That is not a gap, it is an incoherent peer —
                 a stale session id reused after an Engine restart, or two
                 Bodies sharing one identity. Serving it would corrupt both.
    """
    peer = max(-1, int(peer_last_applied))
    latest = max(0, int(source_latest))
    oldest = max(0, int(source_oldest_retained))

    if peer > latest:
        return ResumePlan(
            ResumeAction.REJECT, reason=(
                f"peer claims seq={peer} but source has only emitted "
                f"{latest} — incoherent session"),
        )
    if peer == latest:
        return ResumePlan(ResumeAction.CONTINUE, reason="peer is current")
    first_needed = peer + 1
    if first_needed < oldest:
        return ResumePlan(
            ResumeAction.RESYNC, from_seq=first_needed, to_seq=latest,
            reason=(f"needs seq>={first_needed} but retention starts at "
                    f"{oldest} — {oldest - first_needed} record(s) evicted"),
        )
    return ResumePlan(
        ResumeAction.REPLAY, from_seq=first_needed, to_seq=latest,
        reason=f"replaying {latest - peer} record(s)",
    )


class DeliveryLedger:
    """Idempotent application — exactly-once EFFECT at the sink.

    At-least-once delivery plus idempotent application is the only
    combination achievable over an unreliable link. Replay makes delivery
    at-least-once; this makes application exactly-once.

    Also tracks the **contiguous** applied watermark, which is the number a
    reconnect must report. Tracking the highest-seen value instead would
    paper over a hole: a frame arriving out of order would advance the mark
    past records that were never applied, and the resume would never ask for
    them. The hole would then be permanent and invisible — the worst shape a
    data-loss bug can take.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._lock = threading.Lock()
        self._seen: Dict[str, int] = {}
        self._order: List[str] = []
        self._capacity = max(64, int(capacity))
        self._contiguous = 0
        self._pending: set = set()
        self._duplicates = 0

    def apply_once(self, frame: LinkFrame, fn: Callable[[LinkFrame], Any]) -> bool:
        """Run ``fn`` for a frame not yet applied. True if it ran.

        ``fn`` raising means NOT applied: the frame stays unrecorded so a
        later redelivery retries it. Recording first and running second would
        convert a transient handler fault into permanent silent loss.
        """
        key = self._key(frame)
        with self._lock:
            if key in self._seen:
                self._duplicates += 1
                return False
        fn(frame)
        with self._lock:
            self._seen[key] = frame.seq
            self._order.append(key)
            while len(self._order) > self._capacity:
                self._seen.pop(self._order.pop(0), None)
            self._advance(frame.seq)
        return True

    def _advance(self, seq: int) -> None:
        """Move the contiguous watermark, closing holes as they fill."""
        if seq == self._contiguous + 1:
            self._contiguous = seq
            while self._contiguous + 1 in self._pending:
                self._contiguous += 1
                self._pending.discard(self._contiguous)
        elif seq > self._contiguous + 1:
            self._pending.add(seq)

    @staticmethod
    def _key(frame: LinkFrame) -> str:
        cid = frame.payload.get("command_id") if frame.payload else None
        return str(cid) if cid else f"{frame.node_id}:{frame.seq}"

    @property
    def contiguous_applied(self) -> int:
        """The watermark to report on reconnect."""
        with self._lock:
            return self._contiguous

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "contiguous_applied": self._contiguous,
                "pending_out_of_order": len(self._pending),
                "tracked_ids": len(self._seen),
                "duplicates_suppressed": self._duplicates,
            }


# ---------------------------------------------------------------------------
# Session identity — survives the socket, so a reconnect costs nothing
# ---------------------------------------------------------------------------


def session_idle_expiry_s() -> float:
    """How long a detached session is held before it is reaped."""
    return _env_float("JARVIS_LINK_SESSION_EXPIRY_S", 900.0, minimum=10.0)


@dataclass
class LinkSession:
    session_id: str
    node_id: str
    ledger: DeliveryLedger
    clock: LogicalClock
    #: Engine-local monotonic. Never compared with the peer's.
    created_mono: float = 0.0
    last_seen_mono: float = 0.0
    attached: bool = False
    reconnects: int = 0


class SessionRegistry:
    """Sessions outlive connections.

    **Why this is the root-cause fix for flapping**, rather than the breaker.
    The breaker bounds the damage; this removes the reason there is damage.
    If a reconnect allocates nothing — same session, same ledger, same
    logical clock, resumed by sequence — then fifty reconnects cost fifty
    handshakes and no state. Flapping stops being a resource question and
    becomes a latency one.

    Detached sessions are held for :func:`session_idle_expiry_s` so a Wi-Fi
    drop is a PAUSE, not a loss: the Engine keeps the op parked and the
    verdict queued, and the Body picks up exactly where it stopped. Expiry is
    lazy — checked on access, no reaper task — because a background sweeper
    is one more thing to keep alive on an event loop that has real work.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._sessions: Dict[str, LinkSession] = {}

    def attach(self, session_id: str, node_id: str) -> Tuple[LinkSession, bool]:
        """Resume or create. Returns ``(session, resumed)``."""
        sid = str(session_id or "")
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            existing = self._sessions.get(sid)
            if existing is not None:
                existing.attached = True
                existing.last_seen_mono = now
                existing.reconnects += 1
                return existing, True
            session = LinkSession(
                session_id=sid, node_id=str(node_id or "?"),
                ledger=DeliveryLedger(), clock=LogicalClock(str(node_id or "?")),
                created_mono=now, last_seen_mono=now, attached=True,
            )
            self._sessions[sid] = session
            return session, False

    def detach(self, session_id: str) -> None:
        """The socket went away. The session does not."""
        with self._lock:
            s = self._sessions.get(str(session_id or ""))
            if s is not None:
                s.attached = False
                s.last_seen_mono = self._clock()

    def touch(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.get(str(session_id or ""))
            if s is not None:
                s.last_seen_mono = self._clock()

    def _expire_locked(self, now: float) -> None:
        horizon = session_idle_expiry_s()
        for sid, s in list(self._sessions.items()):
            if not s.attached and (now - s.last_seen_mono) > horizon:
                self._sessions.pop(sid, None)
                logger.info(
                    "[LinkProtocol] session %s expired after %.0fs detached",
                    sid, now - s.last_seen_mono)

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "attached": sum(1 for s in self._sessions.values() if s.attached),
                "detached": sum(1 for s in self._sessions.values()
                                if not s.attached),
                "reconnects_total": sum(s.reconnects
                                        for s in self._sessions.values()),
                "expiry_s": session_idle_expiry_s(),
                "now_mono": now,
            }
