"""AttentionLedger — attended-time accounting behind every operator clock.
=======================================================================

Root problem
------------

``ReviewCoordinator`` gave the operator a **wall-clock** window to decide
on a Yellow-tier candidate (``JARVIS_REVIEW_TIMEOUT_S``, default 300s).
Wall-clock is blind to whether anyone was ever *looking*: a review raised
while the cockpit was detached burned its whole budget against an empty
socket and auto-EXPIRED → auto-REJECT. Verified work was discarded, and
the discard was indistinguishable from an operator who looked and said
no. That is the attention-path defect.

The fix is not "poll ``client_count`` in the wait loop". It is to make
*attended time* a first-class, monotone quantity that the transport
publishes at its own transitions, and to let deadlines take a **mark** on
it. This module owns that quantity.

Design invariants
-----------------

**One authority for time.** ``attended_elapsed()`` is the process-wide
monotone count of seconds during which at least one operator surface had
a live subscriber. A deadline is ``mark = attended_elapsed()`` plus a
budget — it keeps no private accumulator, so two concurrent reviews can
never disagree about how long the operator has been present. This is the
direct antidote to the dominant local defect class (a value re-derived
downstream acquires a second authority, and the downstream one is what
the operator sees).

**Counts, never deltas.** :meth:`set_count` takes the *current* size of
the caller's own subscriber set. It is idempotent and order-insensitive,
so a missed detach cannot leak a phantom attendee and a double-detach
cannot underflow. The transport's client set stays the single authority
for who is attached; this ledger is a projection of it.

**No phantom delta.** The ledger advances only at transitions, from
``time.monotonic()`` deltas — never by summing sleep durations, so it
cannot drift with the event loop. The charging interval opens exactly on
0→N and closes exactly on N→0. Time spent detached is not merely
uncounted; it is unreachable by the arithmetic.

**Flap hysteresis is not billable time.** A rapid detach/attach
(``ov attach`` restart, terminal resize storm, SIGKILL'd pane) must not
thrash the waiter between two awaits. Readers debounce their *pause
decision* with :attr:`AttentionSnapshot.unattended_for` against
``JARVIS_ATTENTION_FLAP_GRACE_S``. The debounce deliberately does NOT
reach the ledger: charging still stopped at the instant of disconnect,
so a flapping operator is credited with exactly the seconds they were
connected and the grace window grants nothing.

**Self-arming from evidence.** :attr:`AttentionSnapshot.armed` is False
until the first subscriber has ever attached in this process. A headless
soak, CI run, or daemon nobody has ever attached to therefore keeps
pure legacy wall-clock behavior — the gate cannot silently convert an
unattended session into an indefinitely-pinned one. It arms itself the
moment there is an attention path worth preserving.

**No background task.** The ledger is passive: it holds no timer, spawns
nothing, and its waiter futures self-evict via ``add_done_callback``.
A cancelled waiter leaves no residue.

Not to be confused with ``operator_presence.py``
------------------------------------------------

That sibling module answers a different question — *has the human typed
recently* (last-input timestamp vs. an idle threshold, publishing
``operator.active`` / ``operator.idle`` for the yield loop). This one
answers *is a surface attached at all*. A human can be attached and
silent for an hour: idle there, fully attended here. Conflating them
would let a quiet operator's review expire while they were watching it.
The two are composable — ``operator_present(liveness=...)`` accepts a
probe, and :meth:`AttentionLedger.snapshot` supplies a natural one — but
they are deliberately not the same authority.

Authority boundary
------------------

* §1 deterministic — pure bookkeeping; no LLM, no I/O
* §3 asynchronous tendrils — edge-triggered futures, never a poll loop
* §7 fail-closed — every method swallows; presence degrades to "unknown"
  (count 0, unarmed) rather than raising into a transport hot path
* §8 observable — :meth:`snapshot` is one atomic read of the whole state
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set

logger = logging.getLogger("Ouroboros.AttentionLedger")


ATTENTION_LEDGER_SCHEMA_VERSION: str = "attention_ledger.v1"


#: Source key published by the cockpit UDS bridge (``ov attach``).
SOURCE_COCKPIT: str = "cockpit_attach"


FLAP_GRACE_ENV_VAR: str = "JARVIS_ATTENTION_FLAP_GRACE_S"

#: Debounce on the attended→paused transition. Long enough to absorb an
#: ``ov attach`` reconnect; short enough that a genuinely departed
#: operator stops the clock promptly. Never billable (see module docs).
_DEFAULT_FLAP_GRACE_S: float = 2.0


def read_env_seconds(env_var: str, default: float) -> float:
    """Resolve a non-negative, finite seconds knob. Empty / garbage /
    negative / NaN / inf all fall back to ``default`` — an unparseable
    knob must never become an unbounded deadline. NEVER raises."""
    try:
        raw = os.environ.get(env_var, "").strip()
    except Exception:  # noqa: BLE001
        return default
    if not raw:
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < 0:
        return default
    return parsed


def read_flap_grace_s() -> float:
    """Resolve :data:`FLAP_GRACE_ENV_VAR`. ``0`` = no debounce (pause the
    instant the last subscriber leaves)."""
    return read_env_seconds(FLAP_GRACE_ENV_VAR, _DEFAULT_FLAP_GRACE_S)


# ===========================================================================
# Frozen record — one atomic read of the whole ledger
# ===========================================================================


@dataclass(frozen=True)
class AttentionSnapshot:
    """Everything a reader needs, taken under one lock acquisition.

    Reading ``count`` and ``epoch`` in two calls would admit a torn view
    (the count changes between them and the reader registers a waiter for
    an edge that already passed). One snapshot closes that race by
    construction — :meth:`AttentionLedger.change_future` resolves
    immediately when ``epoch`` has moved on.
    """

    epoch: int
    count: int
    armed: bool
    attended_elapsed: float
    unattended_for: float
    schema_version: str = ATTENTION_LEDGER_SCHEMA_VERSION

    @property
    def attended(self) -> bool:
        return self.count > 0


def _set_true(fut: "asyncio.Future") -> None:
    if not fut.done():
        fut.set_result(True)


# ===========================================================================
# AttentionLedger
# ===========================================================================


class AttentionLedger:
    """Process-wide attention ledger. Thread-safe; loop-agnostic."""

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}
        self._total: int = 0
        #: Closed attended spans, in seconds.
        self._accum: float = 0.0
        #: Monotonic instant the current attended span opened (None = paused).
        self._charging_since: Optional[float] = None
        #: Monotonic instant presence last fell to zero (None = attended).
        self._zero_at: Optional[float] = None
        self._epoch: int = 0
        self._armed: bool = False
        self._waiters: Set["asyncio.Future"] = set()
        self._lock = threading.RLock()

    # ---- write surface (called from transport seams) ------------------

    def set_count(self, source: object, count: object) -> None:
        """Republish ``source``'s CURRENT subscriber count.

        Idempotent and order-insensitive by construction: callers pass the
        length of their own live client set, never a delta. A no-op
        republish does not bump the epoch, so a chatty transport cannot
        wake every waiter for nothing. NEVER raises."""
        try:
            n = int(count)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if n < 0:
            n = 0
        key = str(source or "").strip() or "unknown"

        with self._lock:
            if self._counts.get(key, 0) == n:
                return  # no transition — no epoch bump, no wakeups
            self._counts[key] = n
            self._total = sum(self._counts.values())
            now = time.monotonic()
            if self._total > 0:
                self._armed = True
                if self._charging_since is None:
                    # 0→N: open a charging interval.
                    self._charging_since = now
                    self._zero_at = None
            elif self._charging_since is not None:
                # N→0: close it. The ledger advances HERE and only here.
                self._accum += now - self._charging_since
                self._charging_since = None
                self._zero_at = now
            self._epoch += 1
            waiters = tuple(self._waiters)
            self._waiters.clear()

        for fut in waiters:
            self._wake(fut)

    def forget_source(self, source: object) -> None:
        """Drop a source entirely (its surface went away). Equivalent to
        ``set_count(source, 0)`` plus removal of the key."""
        key = str(source or "").strip() or "unknown"
        self.set_count(key, 0)
        with self._lock:
            self._counts.pop(key, None)

    # ---- read surface -------------------------------------------------

    def snapshot(self) -> AttentionSnapshot:
        """One atomic view of the ledger. NEVER raises."""
        with self._lock:
            now = time.monotonic()
            attended = self._accum
            if self._charging_since is not None:
                attended += now - self._charging_since
            unattended = 0.0
            if self._total == 0 and self._zero_at is not None:
                unattended = now - self._zero_at
            return AttentionSnapshot(
                epoch=self._epoch,
                count=self._total,
                armed=self._armed,
                attended_elapsed=attended,
                unattended_for=unattended,
            )

    def attended_elapsed(self) -> float:
        """Monotone seconds-with-an-operator-present since process start.
        Deadlines take a mark on this rather than accumulating privately."""
        return self.snapshot().attended_elapsed

    # ---- edge-triggered rendezvous ------------------------------------

    def change_future(self, since_epoch: int) -> "asyncio.Future":
        """A future that resolves on the next presence transition.

        Pass the ``epoch`` from the same :meth:`snapshot` that decided you
        should wait — if the epoch has already moved, the returned future
        is *already resolved*, so an edge can never be missed between the
        decision and the registration.

        Self-evicting: the waiter removes itself from the registry on
        resolution OR cancellation, so an abandoned wait leaks nothing."""
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future" = loop.create_future()
        with self._lock:
            if self._epoch != since_epoch:
                fut.set_result(True)  # edge already passed
                return fut
            self._waiters.add(fut)
        fut.add_done_callback(self._forget_waiter)
        return fut

    def _forget_waiter(self, fut: "asyncio.Future") -> None:
        with self._lock:
            self._waiters.discard(fut)

    @staticmethod
    def _wake(fut: "asyncio.Future") -> None:
        """Resolve a waiter from whatever thread mutated presence.
        ``call_soon_threadsafe`` is used uniformly — it is legal from the
        owning loop's own thread and mandatory from any other."""
        try:
            loop = fut.get_loop()
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(_set_true, fut)
        except Exception:  # noqa: BLE001
            pass

    # ---- observability -------------------------------------------------

    def describe(self) -> Dict[str, object]:
        """Projection for ``/observability`` + REPL. NEVER raises."""
        snap = self.snapshot()
        with self._lock:
            per_source = dict(self._counts)
        return {
            "schema_version": ATTENTION_LEDGER_SCHEMA_VERSION,
            "count": snap.count,
            "armed": snap.armed,
            "attended_elapsed_s": round(snap.attended_elapsed, 3),
            "unattended_for_s": round(snap.unattended_for, 3),
            "epoch": snap.epoch,
            "sources": per_source,
        }


# ===========================================================================
# Module singleton
# ===========================================================================


_default_ledger: Optional[AttentionLedger] = None
_singleton_lock = threading.Lock()


def get_attention_ledger() -> AttentionLedger:
    """The process-wide attention ledger."""
    global _default_ledger
    with _singleton_lock:
        if _default_ledger is None:
            _default_ledger = AttentionLedger()
        return _default_ledger


def reset_attention_ledger_for_tests() -> None:
    global _default_ledger
    with _singleton_lock:
        _default_ledger = None


__all__ = [
    "FLAP_GRACE_ENV_VAR",
    "ATTENTION_LEDGER_SCHEMA_VERSION",
    "AttentionLedger",
    "AttentionSnapshot",
    "SOURCE_COCKPIT",
    "get_attention_ledger",
    "read_env_seconds",
    "read_flap_grace_s",
    "reset_attention_ledger_for_tests",
]
