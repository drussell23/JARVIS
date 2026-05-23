"""ImmutableBudgetStateMachine — the authoritative spend ledger.

§43.6.1's load-bearing primitive, relocated **out of process** per §43.7
spine principle. JARVIS cannot reach this object because Aegis runs in
a separate OS process. The in-process aspect of this state machine
(``immutable``, ``no public mutator``, ``env-loosen rejected``) is
defense; the cross-process aspect is the guarantee.

Closed taxonomy (AST-pinned in tests):

  * :class:`RejectReason` — 6 values, exactly matches §43.6.1 spec.

Caps composition (strictest-wins):

  An ``admit(...)`` call returns ``BudgetVerdict(admitted=True, ...)``
  iff **all three** caps allow it (route AND session AND hourly). Any
  one cap exceeded → ``BudgetVerdict(admitted=False, reason=...)`` and
  nothing is debited.

Reserve model:

  * On admit: debit ``reserve = estimated_cost_usd * overrun_multiplier``
    against all three caps.
  * On reconcile: debit ``actual_cost_usd`` instead, refund the
    difference. If actual exceeds reserve, the extra is debited and
    the caps may go negative — Slice 2 will add the streaming guillotine
    that prevents this at the wire level. Slice 1 just records faithfully.

Monotonic-tightening (composes ``adaptation/ledger.py`` discipline):

  ``tighten(...)`` accepts kwargs for any subset of caps. A new value
  is accepted only if it is strictly less than the current value
  (``<=`` for the per-route map; an entry not present is treated as
  unset/permissive). Any attempt to loosen raises
  :class:`MonotonicTighteningViolationError`.

No public mutator outside :meth:`tighten`. Caps are stored in a frozen
inner dataclass; setting them post-init requires invoking ``tighten``,
which validates direction.

NEVER reads ``os.environ`` after ``__init__``. The daemon reads env
once at boot, passes values in. Future cap changes come through
``tighten``, not env re-reads.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Deque, Dict, Mapping, Optional, Tuple

from backend.core.ouroboros.aegis.spend_wal import (
    SpendEntry,
    admit_entry,
    append_entry,
    boot_entry,
    reconcile_entry,
    replay_wal,
)

logger = logging.getLogger(__name__)


BUDGET_STATE_MACHINE_SCHEMA_VERSION: str = "aegis_budget.1"


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class RejectReason(str, enum.Enum):
    """Closed 6-value rejection taxonomy. AST-pinned: bytes-identical
    membership across slices. New rejection causes require explicit
    taxonomy extension + spine test update."""

    EMISSION_CAP_EXCEEDED = "emission_cap_exceeded"
    FANOUT_CAP_EXCEEDED = "fanout_cap_exceeded"
    COST_CEILING_EXCEEDED = "cost_ceiling_exceeded"
    CAUSAL_DEPTH_EXCEEDED = "causal_depth_exceeded"
    LINEAGE_FORGERY = "lineage_forgery"
    BUDGET_AUTHORITY_UNAVAILABLE = "budget_authority_unavailable"


# Routes the urgency router can stamp. Slice 1 doesn't validate route
# membership at admit (caller-trusted) but the per-route cap map keys
# off these names. Single-seam reference list.
KNOWN_ROUTES: Tuple[str, ...] = (
    "IMMEDIATE",
    "STANDARD",
    "COMPLEX",
    "BACKGROUND",
    "SPECULATIVE",
)


HOURLY_BURN_WINDOW_S: int = 3600


# ---------------------------------------------------------------------------
# Frozen verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetVerdict:
    """Frozen admit/reconcile result. §33.5 lossless to_dict/from_dict.

    On admit-rejection, ``reason`` is populated and ``debit_usd`` is 0.0.
    On admit-success, ``reason`` is None and ``debit_usd`` equals the
    reserve.
    """

    admitted: bool
    reason: Optional[RejectReason]
    debit_usd: float
    remaining_session_usd: float
    remaining_hourly_usd: float
    remaining_route_usd: float
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "admitted": self.admitted,
            "reason": self.reason.value if self.reason is not None else None,
            "debit_usd": self.debit_usd,
            "remaining_session_usd": self.remaining_session_usd,
            "remaining_hourly_usd": self.remaining_hourly_usd,
            "remaining_route_usd": self.remaining_route_usd,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "BudgetVerdict":
        reason_raw = d.get("reason")
        reason = RejectReason(reason_raw) if reason_raw is not None else None
        return cls(
            admitted=bool(d["admitted"]),
            reason=reason,
            debit_usd=float(d["debit_usd"]),  # type: ignore[arg-type]
            remaining_session_usd=float(d["remaining_session_usd"]),  # type: ignore[arg-type]
            remaining_hourly_usd=float(d["remaining_hourly_usd"]),  # type: ignore[arg-type]
            remaining_route_usd=float(d["remaining_route_usd"]),  # type: ignore[arg-type]
            detail=(None if d.get("detail") is None else str(d["detail"])),
        )


@dataclass(frozen=True)
class BudgetCaps:
    """Frozen cap-set. The state machine holds one of these and replaces
    it whole on each ``tighten`` (frozen → no in-place mutation)."""

    session_cap_usd: float
    hourly_burn_cap_usd: float
    # Per-route cap map. Missing route -> route cap not enforced
    # (only session + hourly apply). Slice 1 expects all 5 KNOWN_ROUTES
    # to be configured for "real" deployments; for tests, partial maps
    # are tolerated.
    route_caps_usd: Mapping[str, float] = field(default_factory=dict)
    overrun_multiplier: float = 1.5

    def route_cap(self, route: str) -> Optional[float]:
        """Return the configured cap for ``route`` or None if unconfigured."""
        return self.route_caps_usd.get(route)


class MonotonicTighteningViolationError(RuntimeError):
    """Raised by :meth:`ImmutableBudgetStateMachine.tighten` when an
    attempt is made to set a cap value greater than the current value
    (i.e., loosen the budget).

    Composes ``adaptation/ledger.py`` discipline: governance state
    moves in the tightening direction only; loosening requires the
    out-of-band operator amendment path.
    """


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class ImmutableBudgetStateMachine:
    """Per-Aegis-process authoritative ledger.

    Construction takes the initial caps + WAL path. After construction
    no environment is read — the daemon mints one instance at boot
    and passes references; subsequent runtime changes only via
    :meth:`tighten`.

    Concurrency: every mutating method (``admit``, ``reconcile``,
    ``tighten``) takes the same ``asyncio.Lock``. The daemon's event
    loop serializes them naturally, but the lock makes the boundary
    explicit and survives any future task-fanout.

    Persistence: every admit + reconcile + boot lifecycle event writes
    one row to ``wal_path`` via ``spend_wal.append_entry`` (which wraps
    ``cross_process_jsonl.flock_append_line``).
    """

    def __init__(
        self,
        *,
        caps: BudgetCaps,
        wal_path: Path,
    ) -> None:
        self._caps: BudgetCaps = caps
        self._wal_path: Path = Path(wal_path)

        # Cumulative running totals (frozen state moves only through
        # admit/reconcile/tighten — never set directly externally).
        self._session_debit_usd: float = 0.0
        # Per-route cumulative debit. Lazy-initialized so unknown
        # routes don't surface KeyError on first admit.
        self._route_debit_usd: Dict[str, float] = {}
        # Sliding 1h window of (ts, usd) for hourly burn cap.
        # Evict-on-scan; bounded only by the window length × admit rate.
        self._hourly_window: Deque[Tuple[float, float]] = deque()
        # Track reserves per lease nonce so reconcile can refund.
        self._open_reserves: Dict[str, float] = {}

        self._lock = asyncio.Lock()

    # -- caps read-only access ------------------------------------------------

    @property
    def caps(self) -> BudgetCaps:
        """Return the current (frozen) caps. Read-only by virtue of
        ``BudgetCaps`` being a frozen dataclass."""
        return self._caps

    @property
    def wal_path(self) -> Path:
        return self._wal_path

    # -- lifecycle ------------------------------------------------------------

    async def record_boot(self, *, detail: str) -> bool:
        """Write a BOOT row to the WAL. Best-effort; returns False on
        WAL failure (caller may surface a startup warning)."""
        entry = boot_entry(ts=time.time(), detail=detail)
        return await append_entry(self._wal_path, entry)

    def replay_for_recovery(self) -> None:
        """Replay the WAL synchronously and reconstruct in-memory
        cumulative state. Called by the daemon at boot, BEFORE the
        event loop starts handling requests.

        Idempotent. Tolerates corrupt rows (``replay_wal`` skips them).
        Hourly-window entries older than the window are dropped during
        replay (their cumulative effect is preserved in
        ``_session_debit_usd``).
        """
        entries = replay_wal(self._wal_path)
        now = time.time()
        for entry in entries:
            self._apply_entry_to_state(entry, now=now)

    def _apply_entry_to_state(self, entry: SpendEntry, *, now: float) -> None:
        """Apply one WAL entry to in-memory state. Used by replay."""
        if entry.kind.value == "admit":
            usd = entry.reserve_cost_usd or 0.0
            self._session_debit_usd += usd
            if entry.route is not None:
                self._route_debit_usd[entry.route] = (
                    self._route_debit_usd.get(entry.route, 0.0) + usd
                )
            # Only carry into the hourly window if still within range.
            if entry.ts >= (now - HOURLY_BURN_WINDOW_S):
                self._hourly_window.append((entry.ts, usd))
            if entry.lease_nonce is not None:
                self._open_reserves[entry.lease_nonce] = usd
        elif entry.kind.value == "reconcile":
            actual = entry.actual_cost_usd or 0.0
            reserve = entry.reserve_cost_usd or 0.0
            delta = actual - reserve  # positive = overrun, negative = refund
            self._session_debit_usd += delta
            if entry.route is not None:
                self._route_debit_usd[entry.route] = (
                    self._route_debit_usd.get(entry.route, 0.0) + delta
                )
            if entry.ts >= (now - HOURLY_BURN_WINDOW_S):
                self._hourly_window.append((entry.ts, delta))
            if entry.lease_nonce is not None:
                self._open_reserves.pop(entry.lease_nonce, None)
        # BOOT entries don't affect state.

    # -- admission ------------------------------------------------------------

    async def admit(
        self,
        *,
        route: str,
        estimated_cost_usd: float,
        lease_nonce: str,
        op_id: str,
    ) -> BudgetVerdict:
        """Reserve budget for one upcoming provider call.

        Strictest-wins: rejected if **any** cap (route, session, hourly)
        would be exceeded by the reserve. On success, all three caps
        are debited by ``reserve = estimated * overrun_multiplier`` and
        the WAL is appended.
        """
        if estimated_cost_usd < 0:
            return _denied(
                self._caps,
                reason=RejectReason.COST_CEILING_EXCEEDED,
                detail=f"estimated_cost_usd must be >= 0, got {estimated_cost_usd}",
            )

        reserve = max(0.0, float(estimated_cost_usd) * float(self._caps.overrun_multiplier))

        async with self._lock:
            now = time.time()
            self._evict_expired_hourly_locked(now=now)

            session_remaining = self._caps.session_cap_usd - self._session_debit_usd
            hourly_used = sum(usd for _, usd in self._hourly_window)
            hourly_remaining = self._caps.hourly_burn_cap_usd - hourly_used
            route_cap = self._caps.route_cap(route)
            route_debit = self._route_debit_usd.get(route, 0.0)
            route_remaining = (
                (route_cap - route_debit) if route_cap is not None else float("inf")
            )

            # Strictest-wins.
            if route_cap is not None and reserve > route_remaining:
                return BudgetVerdict(
                    admitted=False,
                    reason=RejectReason.COST_CEILING_EXCEEDED,
                    debit_usd=0.0,
                    remaining_session_usd=max(0.0, session_remaining),
                    remaining_hourly_usd=max(0.0, hourly_remaining),
                    remaining_route_usd=max(0.0, route_remaining),
                    detail=f"route {route} cap exceeded",
                )
            if reserve > session_remaining:
                return BudgetVerdict(
                    admitted=False,
                    reason=RejectReason.COST_CEILING_EXCEEDED,
                    debit_usd=0.0,
                    remaining_session_usd=max(0.0, session_remaining),
                    remaining_hourly_usd=max(0.0, hourly_remaining),
                    remaining_route_usd=max(0.0, route_remaining),
                    detail="session cap exceeded",
                )
            if reserve > hourly_remaining:
                return BudgetVerdict(
                    admitted=False,
                    reason=RejectReason.COST_CEILING_EXCEEDED,
                    debit_usd=0.0,
                    remaining_session_usd=max(0.0, session_remaining),
                    remaining_hourly_usd=max(0.0, hourly_remaining),
                    remaining_route_usd=max(0.0, route_remaining),
                    detail="hourly burn cap exceeded",
                )

            # Admit: debit + WAL.
            self._session_debit_usd += reserve
            self._route_debit_usd[route] = route_debit + reserve
            self._hourly_window.append((now, reserve))
            self._open_reserves[lease_nonce] = reserve

            verdict = BudgetVerdict(
                admitted=True,
                reason=None,
                debit_usd=reserve,
                remaining_session_usd=session_remaining - reserve,
                remaining_hourly_usd=hourly_remaining - reserve,
                remaining_route_usd=(
                    (route_remaining - reserve) if route_cap is not None
                    else float("inf")
                ),
            )

            entry = admit_entry(
                ts=now,
                lease_nonce=lease_nonce,
                op_id=op_id,
                route=route,
                estimated_cost_usd=float(estimated_cost_usd),
                reserve_cost_usd=reserve,
            )

        # WAL append OUTSIDE the lock — flock + thread offload would
        # otherwise hold our async lock through disk I/O.
        ok = await append_entry(self._wal_path, entry)
        if not ok:
            logger.warning(
                "[AegisBudget] WAL append failed for admit (nonce=%s); "
                "in-memory state still reflects the admit",
                lease_nonce,
            )

        return verdict

    # -- reconciliation -------------------------------------------------------

    async def reconcile(
        self,
        *,
        lease_nonce: str,
        op_id: str,
        route: str,
        actual_cost_usd: float,
    ) -> BudgetVerdict:
        """Replace the reserved debit with the actual one. Refund the
        difference (positive if actual < reserve) or take the overrun
        (negative refund → additional debit).

        Slice 1 contract: this is informational. Slice 2 will add the
        streaming guillotine that prevents actual > reserve at the wire
        level. Here we just record faithfully.

        Returns a fresh ``BudgetVerdict`` reflecting post-reconcile
        remaining caps. ``admitted`` is True iff the lease was open
        (was admitted previously); False with
        BUDGET_AUTHORITY_UNAVAILABLE if the lease nonce is unknown
        (replay or never admitted).
        """
        async with self._lock:
            now = time.time()
            self._evict_expired_hourly_locked(now=now)

            reserve = self._open_reserves.pop(lease_nonce, None)
            if reserve is None:
                # Unknown lease — caller asked us to reconcile something
                # we never admitted. Could be replay or a JARVIS-side
                # bug. Don't mutate state; just return.
                session_remaining = self._caps.session_cap_usd - self._session_debit_usd
                hourly_used = sum(usd for _, usd in self._hourly_window)
                hourly_remaining = self._caps.hourly_burn_cap_usd - hourly_used
                route_cap = self._caps.route_cap(route)
                route_debit = self._route_debit_usd.get(route, 0.0)
                route_remaining = (
                    (route_cap - route_debit) if route_cap is not None
                    else float("inf")
                )
                return BudgetVerdict(
                    admitted=False,
                    reason=RejectReason.BUDGET_AUTHORITY_UNAVAILABLE,
                    debit_usd=0.0,
                    remaining_session_usd=max(0.0, session_remaining),
                    remaining_hourly_usd=max(0.0, hourly_remaining),
                    remaining_route_usd=max(0.0, route_remaining),
                    detail="unknown lease nonce",
                )

            delta = float(actual_cost_usd) - reserve  # positive = overrun
            self._session_debit_usd += delta
            self._route_debit_usd[route] = (
                self._route_debit_usd.get(route, 0.0) + delta
            )
            self._hourly_window.append((now, delta))

            session_remaining = self._caps.session_cap_usd - self._session_debit_usd
            hourly_used = sum(usd for _, usd in self._hourly_window)
            hourly_remaining = self._caps.hourly_burn_cap_usd - hourly_used
            route_cap = self._caps.route_cap(route)
            route_debit = self._route_debit_usd.get(route, 0.0)
            route_remaining = (
                (route_cap - route_debit) if route_cap is not None
                else float("inf")
            )

            verdict = BudgetVerdict(
                admitted=True,
                reason=None,
                debit_usd=float(actual_cost_usd),
                remaining_session_usd=session_remaining,
                remaining_hourly_usd=hourly_remaining,
                remaining_route_usd=route_remaining,
            )

            entry = reconcile_entry(
                ts=now,
                lease_nonce=lease_nonce,
                op_id=op_id,
                route=route,
                actual_cost_usd=float(actual_cost_usd),
                reserve_cost_usd=reserve,
            )

        ok = await append_entry(self._wal_path, entry)
        if not ok:
            logger.warning(
                "[AegisBudget] WAL append failed for reconcile (nonce=%s)",
                lease_nonce,
            )

        return verdict

    # -- monotonic tightening -------------------------------------------------

    async def tighten(
        self,
        *,
        session_cap_usd: Optional[float] = None,
        hourly_burn_cap_usd: Optional[float] = None,
        route_caps_usd: Optional[Mapping[str, float]] = None,
        overrun_multiplier: Optional[float] = None,
    ) -> BudgetCaps:
        """Update caps in the tightening direction only.

        Each provided value must be strictly less than the current cap
        (``<=`` is treated as no-op for per-route map keys not present;
        any new value lower than current is accepted, any equal-or-higher
        raises).

        Returns the new (frozen) :class:`BudgetCaps` post-tighten.

        Raises:
            :class:`MonotonicTighteningViolationError` if any provided
            value would loosen.
        """
        async with self._lock:
            new_caps = self._caps

            if session_cap_usd is not None:
                if session_cap_usd >= new_caps.session_cap_usd:
                    raise MonotonicTighteningViolationError(
                        f"session_cap_usd: {session_cap_usd} would not tighten "
                        f"current {new_caps.session_cap_usd}"
                    )
                new_caps = replace(new_caps, session_cap_usd=float(session_cap_usd))

            if hourly_burn_cap_usd is not None:
                if hourly_burn_cap_usd >= new_caps.hourly_burn_cap_usd:
                    raise MonotonicTighteningViolationError(
                        f"hourly_burn_cap_usd: {hourly_burn_cap_usd} would not "
                        f"tighten current {new_caps.hourly_burn_cap_usd}"
                    )
                new_caps = replace(
                    new_caps, hourly_burn_cap_usd=float(hourly_burn_cap_usd),
                )

            if route_caps_usd is not None:
                merged: Dict[str, float] = dict(new_caps.route_caps_usd)
                for route, new_val in route_caps_usd.items():
                    current = merged.get(route)
                    if current is None:
                        # Adding a previously-unset cap is a tightening:
                        # unset = unlimited, set = bounded.
                        merged[route] = float(new_val)
                        continue
                    if new_val >= current:
                        raise MonotonicTighteningViolationError(
                            f"route_caps_usd[{route!r}]: {new_val} would not "
                            f"tighten current {current}"
                        )
                    merged[route] = float(new_val)
                new_caps = replace(new_caps, route_caps_usd=dict(merged))

            if overrun_multiplier is not None:
                if overrun_multiplier >= new_caps.overrun_multiplier:
                    raise MonotonicTighteningViolationError(
                        f"overrun_multiplier: {overrun_multiplier} would not "
                        f"tighten current {new_caps.overrun_multiplier}"
                    )
                new_caps = replace(
                    new_caps, overrun_multiplier=float(overrun_multiplier),
                )

            self._caps = new_caps
            return new_caps

    # -- introspection (read-only) -------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        """Return a read-only point-in-time view for /health /aegis/spend."""
        now = time.time()
        # Compute hourly used outside the lock — single-int reads are
        # safe and this method must not block. Worst case: a value
        # drifts by one in-flight admit; this is an observability
        # surface, not an authority decision.
        hourly_used = sum(
            usd for ts, usd in self._hourly_window
            if ts >= (now - HOURLY_BURN_WINDOW_S)
        )
        return {
            "session_cap_usd": self._caps.session_cap_usd,
            "session_debit_usd": self._session_debit_usd,
            "hourly_burn_cap_usd": self._caps.hourly_burn_cap_usd,
            "hourly_burn_used_usd": hourly_used,
            "route_caps_usd": dict(self._caps.route_caps_usd),
            "route_debit_usd": dict(self._route_debit_usd),
            "open_reserve_count": len(self._open_reserves),
            "overrun_multiplier": self._caps.overrun_multiplier,
            "schema_version": BUDGET_STATE_MACHINE_SCHEMA_VERSION,
        }

    # -- internal -------------------------------------------------------------

    def _evict_expired_hourly_locked(self, *, now: float) -> None:
        """Drop hourly-window entries older than HOURLY_BURN_WINDOW_S.

        Must be called with the lock held."""
        cutoff = now - HOURLY_BURN_WINDOW_S
        while self._hourly_window and self._hourly_window[0][0] < cutoff:
            self._hourly_window.popleft()


# ---------------------------------------------------------------------------
# Helper for synth verdicts (used by admit-validation pre-lock)
# ---------------------------------------------------------------------------


def _denied(caps: BudgetCaps, *, reason: RejectReason, detail: str) -> BudgetVerdict:
    return BudgetVerdict(
        admitted=False,
        reason=reason,
        debit_usd=0.0,
        remaining_session_usd=caps.session_cap_usd,
        remaining_hourly_usd=caps.hourly_burn_cap_usd,
        remaining_route_usd=0.0,
        detail=detail,
    )


__all__ = [
    "BUDGET_STATE_MACHINE_SCHEMA_VERSION",
    "BudgetCaps",
    "BudgetVerdict",
    "HOURLY_BURN_WINDOW_S",
    "ImmutableBudgetStateMachine",
    "KNOWN_ROUTES",
    "MonotonicTighteningViolationError",
    "RejectReason",
]
