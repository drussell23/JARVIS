"""What holds a capability open, and what closes it when nobody is left.

`capability_registry.Session` gave the vocabulary: a START outlives its own
call, an END releases it, and START is never SAFE_AUTO while END always is.
What it could not give is the BOOK — the record of which sessions are currently
open, who wanted them, and what happens when that someone goes away.

Without one, `video.start_streaming` is a call that returns True and then holds
the display capture open forever. The HUD that asked for it can quit, crash, or
be rebuilt; the green recording dot stays lit. That is the defect this exists to
delete, and it is a leak of a PHYSICAL resource — a camera indicator, a capture
session, a spawned pid — not of a dict entry.

REFCOUNTS, NOT OWNERSHIP
--------------------------
The obvious design gives each lease its own release: A opens the stream, A goes
away, the stream stops. It is wrong the moment there are two of anything. If the
HUD and a voice session both hold `video.start_streaming` and the HUD quits, a
per-lease release stops a stream the voice session is still using — and the
provider is a singleton, so there is no second stream to fall back to.

So leases REFCOUNT by capability. N leases on one capability release it exactly
once, when the last of them closes. This is the same reason a file descriptor is
not closed by whichever holder happens to exit first.

A DISCONNECT IS NOT IMMEDIATELY A DEPARTURE
---------------------------------------------
Reaping the instant a socket drops makes every HUD rebuild kill a stream the
operator wanted, and makes a one-second network hiccup indistinguishable from a
quit. So a departed principal enters a GRACE window; if the same principal comes
back before it expires, the reap is cancelled and nothing was ever interrupted.
The window is the only place absence is interpreted, and it is time-based, which
is the one thing a reaper is allowed to read.

WHAT THIS DELIBERATELY DOES NOT KNOW
--------------------------------------
The reaper reads `time.monotonic()` and this book. NOTHING else — not the
router's pending table, not a phase, not a liveness signal from the subsystem
whose session it is about to close. That is the Watchdog Isolation Invariant
(CLAUDE.md, Slice 47) applied one layer down: a reaper that waits for the thing
it guards to look healthy deadlocks WITH it when that thing wedges, which is
precisely the state a leaked session is usually in. A wedged `stop_streaming`
must not stop the reaper from noticing the next leak.

A FAILED RELEASE IS NOT A CLOSED LEASE
----------------------------------------
If `stop_streaming` raises, deleting the record would make the book report zero
open sessions while the camera light stays on — the leak plus a lie. Such a
lease goes to ORPHANED and STAYS in the book, counted and named, because the
number an operator can act on is the whole value of keeping a book at all.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import contextvars
import enum
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("JARVIS.CapabilityLeases")

#: WHO a session belongs to, carried ambiently rather than threaded through
#: every signature between the socket and the router.
#:
#: A contextvar because the alternative is an `owner=` parameter on `route`, on
#: `execute_tool`, on `_exec_derived_capability` and on the tool loop — five
#: signatures changed so that one string can travel, and five places for a
#: caller to forget it. Forgetting here yields "" (unowned), which is honest:
#: an unowned lease is still reaped by TTL, just not by disconnect.
#:
#: Set at the IPC boundary, which is the only place that actually KNOWS which
#: principal is speaking. Reads correctly across `await` because contextvars are
#: per-task, and the HUD dispatcher gives each event its own thread and its own
#: fresh context — so two clients' sessions can never be attributed to each
#: other, which a module-global would not guarantee.
_PRINCIPAL: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "jarvis_capability_principal", default="")


def set_principal(principal: str) -> Any:
    """Declare who the current task is acting for. Returns a reset token."""
    try:
        return _PRINCIPAL.set(str(principal or ""))
    except Exception:  # noqa: BLE001
        return None


def current_principal() -> str:
    """Who the current task is acting for, or "" if nobody said. NEVER raises."""
    try:
        return _PRINCIPAL.get()
    except Exception:  # noqa: BLE001
        return ""


#: The owner a reaper-initiated release runs under. Named rather than blank so
#: an audit can tell "the machine cleaned this up" from "nobody said".
REAPER_PRINCIPAL: str = "system:reaper"

CAPABILITY_LEASES_SCHEMA_VERSION: str = "capability_leases.v1"

#: Signature of the thing that actually releases a session. Injected rather
#: than imported: the book must not depend on the router, because the router
#: depends on the book. The router hands its own routing function in, so
#: releases still pass through the ONE gate rather than around it.
Releaser = Callable[[str, Dict[str, Any]], Awaitable[bool]]


def leases_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off means sessions are still TAGGED and still gated — only the bookkeeping
    and the reaper stop. That is a real position to hold while debugging a
    provider, and it is honest about what it costs: leaks become invisible
    again rather than merely unreaped.
    """
    return (os.environ.get("JARVIS_CAPABILITY_LEASES_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def default_ttl_s() -> float:
    """How long a session may live unrenewed. Clamped. NEVER raises.

    A ceiling, not a schedule. Most sessions are closed by their END capability
    long before this; the TTL is what catches the ones whose owner vanished in a
    way no disconnect signal described — a killed thread, a dropped task, a
    coroutine that was cancelled between opening and recording.
    """
    try:
        v = float(os.environ.get("JARVIS_CAPABILITY_LEASE_TTL_S", "1800"))
    except (TypeError, ValueError):
        v = 1800.0
    return max(30.0, min(v, 24 * 3600.0))


def disconnect_grace_s() -> float:
    """How long a departed principal may be gone before its sessions die.

    Long enough that a HUD rebuild or a socket blip does not interrupt a stream;
    short enough that a real quit is not a lease held for the rest of the day.
    """
    try:
        v = float(os.environ.get("JARVIS_CAPABILITY_LEASE_GRACE_S", "20"))
    except (TypeError, ValueError):
        v = 20.0
    return max(0.0, min(v, 3600.0))


def reaper_interval_s() -> float:
    """Sweep cadence. NEVER raises."""
    try:
        v = float(os.environ.get("JARVIS_CAPABILITY_LEASE_SWEEP_S", "5"))
    except (TypeError, ValueError):
        v = 5.0
    return max(1.0, min(v, 300.0))


def release_timeout_s() -> float:
    """Budget for one release call. NEVER raises.

    Bounded because a release is exactly the call most likely to hang: the
    subsystem is already in the state that produced the leak. An unbounded await
    here would wedge the sweep and turn one orphan into all of them.
    """
    try:
        v = float(os.environ.get("JARVIS_CAPABILITY_RELEASE_TIMEOUT_S", "20"))
    except (TypeError, ValueError):
        v = 20.0
    return max(1.0, min(v, 300.0))


def max_release_attempts() -> int:
    """How many times to retry a failing release before calling it orphaned."""
    try:
        return max(1, min(int(os.environ.get(
            "JARVIS_CAPABILITY_RELEASE_ATTEMPTS", "3")), 20))
    except (TypeError, ValueError):
        return 3


class LeaseState(str, enum.Enum):
    """Where a lease is. ORPHANED is the one that earns its keep.

    CLOSED and ORPHANED are both "no longer holding a refcount", and a book that
    rendered them the same would let a failed release read as a success.
    """

    OPEN = "open"
    RELEASING = "releasing"
    CLOSED = "closed"
    ORPHANED = "orphaned"          # release was attempted and did not succeed


class ReapReason(str, enum.Enum):
    """Why a lease was closed. Recorded because 'it stopped' is not a story."""

    EXPLICIT = "explicit"          # the END capability was called
    EXPIRED = "ttl_expired"
    OWNER_GONE = "owner_disconnected"
    SHUTDOWN = "shutdown"


@dataclass
class Lease:
    """One holder's claim on one open session."""

    lease_id: str
    capability: str                # the START name, e.g. "video.start_streaming"
    release: str                   # the END name, e.g. "video.stop_streaming"
    owner: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    #: Monotonic, because a lease must survive the operator changing the clock
    #: and NTP stepping it backwards mid-stream.
    opened_at: float = field(default_factory=time.monotonic)
    #: Wall-clock, for humans reading a dashboard. NEVER used for a decision.
    opened_wall: float = field(default_factory=time.time)
    ttl_s: float = field(default_factory=default_ttl_s)
    state: str = LeaseState.OPEN.value
    attempts: int = 0
    detail: str = ""

    @property
    def age_s(self) -> float:
        return max(0.0, time.monotonic() - self.opened_at)

    @property
    def holding(self) -> bool:
        """Whether this lease still contributes to its capability's refcount."""
        return self.state in (LeaseState.OPEN.value, LeaseState.RELEASING.value)

    def expired(self, now: Optional[float] = None) -> bool:
        if self.ttl_s <= 0:
            return False
        return ((now if now is not None else time.monotonic())
                - self.opened_at) > self.ttl_s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lease_id": self.lease_id, "capability": self.capability,
            "release": self.release, "owner": self.owner, "state": self.state,
            "age_s": round(self.age_s, 1), "ttl_s": round(self.ttl_s, 1),
            "opened_wall": self.opened_wall, "attempts": self.attempts,
            "detail": self.detail[:200],
        }


class LeaseBook:
    """Open sessions, refcounted by capability, reaped on time. NEVER raises."""

    def __init__(self, releaser: Optional[Releaser] = None) -> None:
        self._leases: Dict[str, Lease] = {}
        self._releaser = releaser
        #: principal -> monotonic deadline after which its leases are reaped.
        #: Presence in this dict IS the "currently absent" fact; a reconnect
        #: removes the entry and the pending reap simply stops being pending.
        self._departing: Dict[str, float] = {}
        #: One lock per capability, so releasing a video stream never serialises
        #: behind releasing an unrelated ghost-hands session.
        self._locks: Dict[str, asyncio.Lock] = {}
        self._counters: Dict[str, int] = {
            "opened": 0, "closed": 0, "orphaned": 0,
            "reaped_expired": 0, "reaped_owner": 0, "reaped_shutdown": 0,
            "release_failed": 0, "coalesced": 0,
        }

    # -- wiring ------------------------------------------------------------

    def set_releaser(self, releaser: Optional[Releaser]) -> None:
        """Install what actually stops a session. NEVER raises.

        Idempotent and late-bindable on purpose: the book is constructed before
        the router exists, and a book with no releaser still records leases
        correctly — it simply cannot close them, which it says out loud rather
        than pretending.
        """
        self._releaser = releaser

    # -- the book ----------------------------------------------------------

    def open(self, capability: str, release: str, *, owner: str = "",
             args: Optional[Dict[str, Any]] = None,
             ttl_s: Optional[float] = None) -> Optional[Lease]:
        """Record a session that just started. NEVER raises.

        Returns None when leasing is disabled or the START named no release —
        and returning None is deliberate rather than raising, because the
        session itself SUCCEEDED. Refusing to acknowledge a started stream would
        not un-start it; it would only remove the last record that it exists.
        """
        try:
            if not leases_enabled():
                return None
            if not capability:
                return None
            if not release:
                # A start with no named release cannot be reaped, so the book
                # would be recording an obligation it can never discharge.
                # `federation.unreleasable()` surfaces this at hydrate time; by
                # here it is a defect that already shipped, so it is LOUD.
                logger.error(
                    "[Leases] '%s' started a session with no declared release — "
                    "it cannot be reaped. Declare `release=` on its tag.",
                    capability)
                return None
            # A principal that opens a lease is, by definition, present.
            if owner:
                self._departing.pop(owner, None)
            lease = Lease(
                lease_id=uuid.uuid4().hex, capability=capability,
                release=release, owner=owner or "", args=dict(args or {}),
                ttl_s=(default_ttl_s() if ttl_s is None else float(ttl_s)))
            self._leases[lease.lease_id] = lease
            self._counters["opened"] += 1
            holders = self.holders(capability)
            if holders > 1:
                self._counters["coalesced"] += 1
            logger.info("[Leases] OPEN %s owner=%s holders=%d lease=%s",
                        capability, owner or "-", holders, lease.lease_id[:8])
            return lease
        except Exception:  # noqa: BLE001 — bookkeeping never breaks a capability
            logger.debug("[Leases] open(%s) degraded", capability, exc_info=True)
            return None

    def holders(self, capability: str) -> int:
        """How many live claims exist on one capability. NEVER raises."""
        try:
            return sum(1 for l in self._leases.values()
                       if l.capability == capability and l.holding)
        except Exception:  # noqa: BLE001
            return 0

    def active(self) -> List[Lease]:
        """Leases still holding something open. NEVER raises."""
        try:
            return [self._leases[k] for k in sorted(self._leases)
                    if self._leases[k].holding]
        except Exception:  # noqa: BLE001
            return []

    def orphans(self) -> List[Lease]:
        """Sessions whose release failed. Kept, counted, named. NEVER raises."""
        try:
            return [self._leases[k] for k in sorted(self._leases)
                    if self._leases[k].state == LeaseState.ORPHANED.value]
        except Exception:  # noqa: BLE001
            return []

    def by_owner(self, owner: str) -> List[Lease]:
        return [l for l in self.active() if l.owner == owner]

    # -- closing -----------------------------------------------------------

    async def close(self, lease_id: str, *,
                    reason: str = ReapReason.EXPLICIT.value) -> bool:
        """Drop ONE claim, releasing only if it was the last. NEVER raises.

        Idempotent: closing an already-closed lease is a no-op that reports
        success, because a caller that retries a close must not be told its
        session is somehow still open.
        """
        try:
            lease = self._leases.get(lease_id)
            if lease is None or not lease.holding:
                return True
            return await self._close_lease(lease, reason)
        except Exception:  # noqa: BLE001
            logger.debug("[Leases] close(%s) degraded", lease_id, exc_info=True)
            return False

    async def close_capability(self, capability: str, *, owner: str = "",
                               reason: str = ReapReason.EXPLICIT.value) -> int:
        """Drop the claims on one capability. NEVER raises.

        This is what an operator calling the END capability directly means: they
        said "stop streaming", not "drop my particular lease". Scoped to *owner*
        when one is given, so one principal ending its own session does not end
        somebody else's — and the refcount still decides whether the provider is
        actually stopped.
        """
        n = 0
        try:
            for lease in list(self.active()):
                if lease.capability != capability:
                    continue
                if owner and lease.owner != owner:
                    continue
                if await self._close_lease(lease, reason):
                    n += 1
        except Exception:  # noqa: BLE001
            logger.debug("[Leases] close_capability(%s) degraded", capability,
                         exc_info=True)
        return n

    def discharge(self, release: str, *, owner: str = "") -> int:
        """Record that a release ALREADY ran. Calls nothing. NEVER raises.

        What happens when the operator (or the model) invokes the END capability
        directly: by the time this is reached, the stream is genuinely stopped.
        Routing that through `close` would call `stop_streaming` a second time —
        and worse, it would RE-ENTER, because a reaper-driven release goes
        router → execute → here, and `_close_lease` holds a non-reentrant lock
        on the way in. Leases already in RELEASING are therefore skipped: the
        reaper is mid-flight on those and will record their own outcome.

        Deliberately NOT owner-scoped for the closing itself. The provider is a
        singleton, so one principal calling `stop_streaming` stops it for
        everyone whether the book likes it or not, and a book that kept the
        other holders' leases OPEN would be reporting a stream that is not
        running. It says so in the log instead of quietly disagreeing with
        reality.
        """
        n = 0
        try:
            if not release:
                return 0
            victims = [l for l in self.active()
                       if l.release == release
                       and l.state != LeaseState.RELEASING.value]
            others = [l for l in victims if owner and l.owner and l.owner != owner]
            for lease in victims:
                lease.state = LeaseState.CLOSED.value
                lease.detail = f"discharged by explicit {release}"
                self._counters["closed"] += 1
                n += 1
            if others:
                logger.warning(
                    "[Leases] '%s' by %s also ended %d session(s) held by %s — "
                    "the provider is shared, so it stopped for everyone",
                    release, owner or "-", len(others),
                    ", ".join(sorted({l.owner for l in others})))
            elif n:
                logger.info("[Leases] discharged %d lease(s) via explicit %s",
                            n, release)
        except Exception:  # noqa: BLE001
            logger.debug("[Leases] discharge(%s) degraded", release, exc_info=True)
        return n

    async def _close_lease(self, lease: Lease, reason: str) -> bool:
        """Refcount down, and release when the count reaches zero. NEVER raises."""
        try:
            async with self._lock(lease.capability):
                if not lease.holding:
                    return True
                # Count the OTHER holders explicitly rather than by marking this
                # one first and re-counting. `holding` is true for RELEASING —
                # correctly, since a session mid-release is still open — so a
                # state-based count included this very lease and every release
                # concluded that somebody else still needed the provider. The
                # stream was never stopped, by anyone, ever.
                remaining = sum(
                    1 for other in self._leases.values()
                    if other is not lease
                    and other.capability == lease.capability
                    and other.holding)
                if remaining > 0:
                    # Somebody else is still using it. This claim is done; the
                    # session is not. No release call is made at all.
                    lease.state = LeaseState.CLOSED.value
                    lease.detail = f"{reason}; {remaining} holder(s) remain"
                    self._counters["closed"] += 1
                    logger.info("[Leases] CLOSE %s lease=%s (%d remain — "
                                "provider left running)", lease.capability,
                                lease.lease_id[:8], remaining)
                    return True
                # Last holder. Mark RELEASING so a concurrent `discharge`
                # arriving from the router on the way back out of this very
                # call leaves it alone.
                lease.state = LeaseState.RELEASING.value
                ok, detail = await self._invoke_release(lease)
                if ok:
                    lease.state = LeaseState.CLOSED.value
                    lease.detail = reason
                    self._counters["closed"] += 1
                    logger.info("[Leases] RELEASED %s via %s (%s)",
                                lease.capability, lease.release, reason)
                    return True
                lease.attempts += 1
                lease.detail = detail
                if lease.attempts >= max_release_attempts():
                    lease.state = LeaseState.ORPHANED.value
                    self._counters["orphaned"] += 1
                    # LOUD. The session is still running and nothing left in
                    # this process is going to stop it.
                    logger.error(
                        "[Leases] ORPHANED %s — %s failed %d time(s): %s. The "
                        "session is STILL OPEN and will not be reaped.",
                        lease.capability, lease.release, lease.attempts,
                        detail[:200])
                else:
                    # Back to OPEN so the next sweep tries again. Staying in
                    # RELEASING would hold a refcount nobody would ever clear.
                    lease.state = LeaseState.OPEN.value
                    logger.warning("[Leases] release of %s failed (attempt "
                                   "%d/%d): %s", lease.capability,
                                   lease.attempts, max_release_attempts(),
                                   detail[:200])
                self._counters["release_failed"] += 1
                return False
        except Exception as exc:  # noqa: BLE001
            lease.state = LeaseState.OPEN.value
            lease.detail = f"{type(exc).__name__}: {exc}"
            logger.debug("[Leases] _close_lease degraded", exc_info=True)
            return False

    async def _invoke_release(self, lease: Lease) -> "tuple":
        """Call the releaser, bounded. Returns (ok, detail). NEVER raises."""
        if self._releaser is None:
            return (False, "no releaser installed")
        try:
            ok = await asyncio.wait_for(
                self._releaser(lease.release, dict(lease.args or {})),
                timeout=release_timeout_s())
            return (bool(ok), "" if ok else "releaser reported failure")
        except asyncio.TimeoutError:
            return (False, f"release exceeded {release_timeout_s():.0f}s")
        except Exception as exc:  # noqa: BLE001
            return (False, f"{type(exc).__name__}: {exc}")

    # -- reaping -----------------------------------------------------------

    def note_departure(self, owner: str) -> None:
        """A principal's connection dropped. Start its grace window. NEVER raises.

        Records an INTENT to reap rather than reaping: the caller here is a
        socket handler, which is the wrong place to be awaiting a provider's
        shutdown, and the whole point of the grace window is that this may yet
        turn out not to have been a departure at all.
        """
        try:
            if not owner:
                return
            grace = disconnect_grace_s()
            self._departing[owner] = time.monotonic() + grace
            n = len(self.by_owner(owner))
            if n:
                logger.info("[Leases] '%s' departed holding %d session(s) — "
                            "reaping in %.0fs unless it returns", owner, n, grace)
        except Exception:  # noqa: BLE001
            pass

    def note_arrival(self, owner: str) -> None:
        """A principal is present. Cancels any pending reap. NEVER raises."""
        try:
            if owner and self._departing.pop(owner, None) is not None:
                logger.info("[Leases] '%s' returned within grace — its "
                            "session(s) were never interrupted", owner)
        except Exception:  # noqa: BLE001
            pass

    async def sweep(self) -> int:
        """One pass: expired TTLs and elapsed grace windows. NEVER raises.

        Reads `time.monotonic()` and this book. Nothing else — see the module
        docstring on why a reaper that consults the health of what it guards is
        not a reaper.
        """
        closed = 0
        try:
            now = time.monotonic()
            gone = [o for o, deadline in list(self._departing.items())
                    if now >= deadline]
            for owner in gone:
                self._departing.pop(owner, None)
                for lease in self.by_owner(owner):
                    if await self._close_lease(lease, ReapReason.OWNER_GONE.value):
                        self._counters["reaped_owner"] += 1
                        closed += 1
            for lease in list(self.active()):
                if lease.expired(now):
                    logger.warning(
                        "[Leases] '%s' exceeded its %.0fs TTL (owner=%s) — "
                        "reaping", lease.capability, lease.ttl_s,
                        lease.owner or "-")
                    if await self._close_lease(lease, ReapReason.EXPIRED.value):
                        self._counters["reaped_expired"] += 1
                        closed += 1
        except Exception:  # noqa: BLE001
            logger.debug("[Leases] sweep degraded", exc_info=True)
        return closed

    async def run_reaper(self, stop: Optional[asyncio.Event] = None) -> None:
        """Sweep forever. Intended as a background task. NEVER raises.

        Survives its own failures by construction: one bad sweep logs and the
        loop continues, because a reaper that dies on the first surprise is
        indistinguishable from no reaper at all — and only discoverable later,
        by the leak it did not catch.
        """
        logger.info("[Leases] reaper started (sweep=%.0fs ttl=%.0fs grace=%.0fs)",
                    reaper_interval_s(), default_ttl_s(), disconnect_grace_s())
        while True:
            try:
                if stop is not None and stop.is_set():
                    return
                await self.sweep()
            except asyncio.CancelledError:
                logger.info("[Leases] reaper cancelled")
                raise
            except Exception:  # noqa: BLE001
                logger.debug("[Leases] reaper iteration degraded", exc_info=True)
            try:
                if stop is not None:
                    try:
                        await asyncio.wait_for(stop.wait(),
                                               timeout=reaper_interval_s())
                        return
                    except asyncio.TimeoutError:
                        continue
                await asyncio.sleep(reaper_interval_s())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(reaper_interval_s())

    async def release_all(self, *,
                          reason: str = ReapReason.SHUTDOWN.value) -> int:
        """Close every open session. For process shutdown. NEVER raises.

        The last line of defence, and the only one that runs while the operator
        is watching the app quit. What it cannot close it leaves ORPHANED and
        logs by name — a shutdown that silently abandoned a live screen capture
        would be the exact failure this module exists to make impossible.
        """
        n = 0
        try:
            for lease in list(self.active()):
                if await self._close_lease(lease, reason):
                    self._counters["reaped_shutdown"] += 1
                    n += 1
            remaining = self.active() + self.orphans()
            if remaining:
                logger.error("[Leases] shutdown left %d session(s) unclosed: %s",
                             len(remaining),
                             ", ".join(sorted({l.capability for l in remaining})))
        except Exception:  # noqa: BLE001
            logger.debug("[Leases] release_all degraded", exc_info=True)
        return n

    # -- observability -----------------------------------------------------

    def _lock(self, capability: str) -> asyncio.Lock:
        lk = self._locks.get(capability)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[capability] = lk
        return lk

    def stats(self) -> Dict[str, Any]:
        """NEVER raises."""
        try:
            active = self.active()
            orphans = self.orphans()
            return {
                "schema_version": CAPABILITY_LEASES_SCHEMA_VERSION,
                "enabled": leases_enabled(),
                "active": len(active),
                "orphaned": len(orphans),
                "departing": sorted(self._departing),
                "capabilities": sorted({l.capability for l in active}),
                # Named, not just counted: an orphan is something an operator
                # has to go and stop by hand, and they need to know what.
                "orphaned_capabilities": sorted({l.capability for l in orphans}),
                "ttl_s": default_ttl_s(),
                "grace_s": disconnect_grace_s(),
                "leases": [l.to_dict() for l in active],
                **self._counters,
            }
        except Exception:  # noqa: BLE001
            return {"schema_version": CAPABILITY_LEASES_SCHEMA_VERSION,
                    "enabled": False, "active": 0, "orphaned": 0}


_BOOK: Optional[LeaseBook] = None


def get_lease_book() -> LeaseBook:
    """Process-wide book. NEVER raises."""
    global _BOOK
    if _BOOK is None:
        _BOOK = LeaseBook()
    return _BOOK


def reset_lease_book() -> None:
    """Testing seam. NEVER raises."""
    global _BOOK
    _BOOK = None
