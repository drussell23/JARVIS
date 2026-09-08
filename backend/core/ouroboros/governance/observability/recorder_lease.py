"""How long the trajectory recorder waits for an op's verdict — the op decides.

## The defect this closes

``TrajectoryRecorder`` holds each generation in ``_pending`` until its op
reports a terminal outcome, and sweeps anything older than a **static 900 s**
into the corpus as ``outcome=unknown, should_train=false``.

Measured, session ``bt-2026-09-08-193049``::

    12:47:06  op=op-01a08280-f4f2-...-cau expired after 900s with 1 candidate(s)
              and NO verdict — writing outcome=unknown, should_train=false
    12:55:32  DECISION outcome=failed reason_code=l2_stopped duration_s=1447.75

The op was alive, healthy and eight minutes from a real verdict. The pool had
given it a **1530 s** ceiling. The recorder gave it 900. The verdict arrived,
and there was nothing left in ``_pending`` to attach it to.

This is not a tuning miss, it is an arithmetic guarantee. The pool's ceiling is
adaptive and the recorder's TTL is a constant, so the moment the ceiling exceeds
900 s **every** long op is destined to be recorded as unknown. With the local
lane clamped to one worker on one GPU — where an op costs ~1450 s — that is the
whole corpus. The DPO flywheel is starved by a clock, not by generation quality.

## What replaces it

A **lease**: the op's own runtime envelope, published where the recorder can
read it. The pool stamps a lease when a worker picks an op up, keyed by the
context op id the recorder already keys ``_pending`` by. The recorder asks for
the lease at sweep time and waits at least that long.

Extension is not a separate mechanism, and that is deliberate. The lease stores
a DEADLINE, and the pool re-stamps it whenever the FSM grants a timebox
extension — so an op that legitimately runs long simply has a later deadline
the next time the recorder looks. A recorder that had to be *told* to extend
would need to be told by the very op thread that is busy, which is how the
900 s constant got away with being wrong for so long.

## Fail-closed, and what that means here

Fail-closed for a recorder is **write the row**, not drop it. On any lease
fault the answer degrades to the caller's own static TTL — today's behaviour —
so a broken lease can only ever cost precision, never data, and never the op
thread that produced it. ``RecorderLeaseFault`` is logged, and counted, so a
degraded lease is visible instead of silently reverting to the constant this
module exists to remove.

## Invariants

1. **A lease never SHORTENS the wait.** The answer is always
   ``max(static_ttl, lease_remaining + buffer)``. A lease is a reason to wait
   longer, never a new way to expire early.
2. **Bounded.** The registry is capped and self-sweeping; a session that
   submits thousands of ops cannot grow it without bound, and a lease whose op
   vanished expires on its own without anyone calling release.
3. **Monotonic per op.** Re-stamping only ever moves a deadline OUT, mirroring
   ``restamp_pipeline_deadline_at_start``: an op is never punished for a
   re-registration that happened to observe a shorter remaining ceiling.
4. **Never raises.** Every entry point is total. A lease is bookkeeping about
   telemetry; it may not fail either.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.RecorderLease")

__all__ = [
    "Lease",
    "RecorderLeaseFault",
    "effective_ttl_s",
    "lease_buffer_fraction",
    "lease_remaining_s",
    "register_lease",
    "release_lease",
    "reset_for_tests",
    "stats",
]

#: Slack ADDED to an op's remaining ceiling, as a fraction of it. The verdict
#: is emitted after the op's last phase returns, so a lease that ends exactly
#: at the ceiling races the very event it is waiting for.
ENV_BUFFER_FRACTION = "JARVIS_RECORDER_LEASE_BUFFER_FRACTION"
#: Hard ceiling on any single lease. An op that somehow registers an enormous
#: ceiling must not be able to pin its generations in memory for a whole
#: session — the leak `_expire_pending` exists to prevent.
ENV_MAX_LEASE_S = "JARVIS_RECORDER_LEASE_MAX_S"
#: Registry capacity. Bounded like every other in-memory map in this package.
ENV_REGISTRY_MAX = "JARVIS_RECORDER_LEASE_REGISTRY_MAX"
#: Master kill switch — off falls back to the static TTL everywhere.
ENV_ENABLED = "JARVIS_RECORDER_LEASE_ENABLED"

_DEFAULT_BUFFER_FRACTION = 0.25
_DEFAULT_MAX_LEASE_S = 7200.0
_DEFAULT_REGISTRY_MAX = 512


class RecorderLeaseFault(RuntimeError):
    """A lease could not be honoured. Logged and counted, never propagated.

    Carried as an exception type rather than a log string so a test can assert
    on the failure mode, and so a future consumer can classify it, without
    anything in the recorder path ever having to catch it.
    """


@dataclass(frozen=True)
class Lease:
    """One op's declared runtime envelope."""

    op_id: str
    deadline_monotonic: float
    ceiling_s: float
    source: str

    def remaining_s(self, now: Optional[float] = None) -> float:
        """Seconds left on this lease, floored at zero. NEVER raises."""
        try:
            ref = time.monotonic() if now is None else float(now)
            return max(0.0, float(self.deadline_monotonic) - ref)
        except (TypeError, ValueError):
            return 0.0


_lock = threading.Lock()
_leases: "OrderedDict[str, Lease]" = OrderedDict()
_stats: Dict[str, int] = {
    "registered": 0, "renewed": 0, "released": 0,
    "evicted": 0, "swept": 0, "faults": 0, "extended_ttl": 0,
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        if not raw:
            return default
        val = float(raw)
        if not math.isfinite(val) or val < minimum:
            return default
        return val
    except (TypeError, ValueError):
        return default


def lease_enabled() -> bool:
    """Default ON. Off restores the static-TTL behaviour exactly."""
    raw = (os.environ.get(ENV_ENABLED, "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def lease_buffer_fraction() -> float:
    """Slack past the ceiling, as a fraction of it. Bounded to [0, 1]."""
    frac = _env_float(ENV_BUFFER_FRACTION, _DEFAULT_BUFFER_FRACTION, minimum=0.0)
    return frac if 0.0 <= frac <= 1.0 else _DEFAULT_BUFFER_FRACTION


def _max_lease_s() -> float:
    return _env_float(ENV_MAX_LEASE_S, _DEFAULT_MAX_LEASE_S, minimum=1.0)


def _registry_max() -> int:
    try:
        raw = (os.environ.get(ENV_REGISTRY_MAX, "") or "").strip()
        val = int(raw) if raw else _DEFAULT_REGISTRY_MAX
        return val if val >= 1 else _DEFAULT_REGISTRY_MAX
    except (TypeError, ValueError):
        return _DEFAULT_REGISTRY_MAX


def _fault(reason: str, op_id: str = "") -> None:
    """Record a lease fault. Logs the named exception WITHOUT raising it —
    the whole contract of this module is that its failures are visible and
    inert."""
    with _lock:
        _stats["faults"] += 1
    logger.warning(
        "[RecorderLease] %s op=%s reason=%s — degrading to the static TTL",
        RecorderLeaseFault.__name__, op_id or "-", reason,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _sweep_locked(now: float) -> None:
    """Drop leases whose deadline is long past. Caller holds ``_lock``.

    Self-sweeping is what makes ``release_lease`` an optimisation rather than a
    correctness requirement: a worker that dies mid-op, or a path that simply
    forgets to release, leaks nothing.
    """
    grace = _max_lease_s()
    stale = [
        oid for oid, lease in _leases.items()
        if (now - lease.deadline_monotonic) > grace
    ]
    for oid in stale:
        _leases.pop(oid, None)
        _stats["swept"] += 1


def register_lease(
    op_id: Any, ceiling_s: Any, *, source: str = "pool",
) -> Optional[Lease]:
    """Declare *op_id*'s runtime envelope. Idempotent; only ever EXTENDS.

    Called by the pool worker at pickup, with the same adaptive ceiling it
    enforces, and again whenever the FSM grants a timebox extension — which is
    the entire extension mechanism: a later deadline is simply read as later
    the next time the recorder sweeps.

    Returns the stored lease, or ``None`` when disabled or on any fault.
    NEVER raises.
    """
    try:
        if not lease_enabled():
            return None
        oid = str(op_id or "").strip()
        if not oid:
            _fault("empty op id")
            return None
        ceiling = float(ceiling_s)
        if not math.isfinite(ceiling) or ceiling <= 0.0:
            _fault(f"non-positive ceiling {ceiling_s!r}", oid)
            return None
        ceiling = min(ceiling, _max_lease_s())
        now = time.monotonic()
        deadline = now + ceiling

        with _lock:
            _sweep_locked(now)
            existing = _leases.get(oid)
            if existing is not None:
                if existing.deadline_monotonic >= deadline:
                    # Never pull a deadline IN. Mirrors
                    # restamp_pipeline_deadline_at_start: a re-stamp that
                    # observed less remaining ceiling must not punish an op
                    # that was already promised more.
                    _leases.move_to_end(oid)
                    return existing
                _stats["renewed"] += 1
            else:
                _stats["registered"] += 1
            lease = Lease(
                op_id=oid, deadline_monotonic=deadline,
                ceiling_s=ceiling, source=str(source or "pool"),
            )
            _leases[oid] = lease
            _leases.move_to_end(oid)
            while len(_leases) > _registry_max():
                # Evict the LEAST recently stamped. It is the one whose op has
                # been quiet longest, so it is the one whose generations are
                # most likely already resolved.
                _leases.popitem(last=False)
                _stats["evicted"] += 1
            return lease
    except Exception as exc:  # noqa: BLE001 — a lease may never fail an op
        _fault(f"register degraded: {exc}", str(op_id or ""))
        return None


def release_lease(op_id: Any) -> None:
    """Drop *op_id*'s lease — an optimisation, not a requirement.

    The registry sweeps itself, so a missed release costs a little memory for
    one grace period and nothing else. NEVER raises.
    """
    try:
        oid = str(op_id or "").strip()
        if not oid:
            return
        with _lock:
            if _leases.pop(oid, None) is not None:
                _stats["released"] += 1
    except Exception:  # noqa: BLE001
        logger.debug("[RecorderLease] release degraded", exc_info=True)


def lease_remaining_s(op_id: Any) -> float:
    """Seconds left on *op_id*'s lease; ``0.0`` when there is none.
    NEVER raises."""
    try:
        oid = str(op_id or "").strip()
        if not oid:
            return 0.0
        now = time.monotonic()
        with _lock:
            lease = _leases.get(oid)
        return lease.remaining_s(now) if lease is not None else 0.0
    except Exception:  # noqa: BLE001
        logger.debug("[RecorderLease] remaining degraded", exc_info=True)
        return 0.0


def effective_ttl_s(op_id: Any, static_ttl_s: float) -> float:
    """How long the recorder should hold *op_id*'s generations.

    ``max(static_ttl, lease_remaining + buffer)`` — a lease is a reason to wait
    LONGER and can never shorten the wait, so arming this cannot cause a single
    expiry that would not have happened anyway. An op with no lease (a fake
    provider in a unit test, a direct orchestrator call, a disabled registry)
    gets exactly today's constant.

    NEVER raises: on any fault it returns *static_ttl_s*, which is the
    behaviour this module replaces — failing closed means keeping the row, not
    dropping it.
    """
    try:
        base = float(static_ttl_s)
        if not math.isfinite(base) or base < 0.0:
            base = 0.0
    except (TypeError, ValueError):
        _fault("unusable static ttl", str(op_id or ""))
        return _DEFAULT_MAX_LEASE_S if static_ttl_s is None else 0.0

    try:
        remaining = lease_remaining_s(op_id)
        if remaining <= 0.0:
            return base
        want = remaining * (1.0 + lease_buffer_fraction())
        want = min(want, _max_lease_s())
        if want > base:
            with _lock:
                _stats["extended_ttl"] += 1
            return want
        return base
    except Exception as exc:  # noqa: BLE001
        _fault(f"ttl derivation degraded: {exc}", str(op_id or ""))
        return base


def stats() -> Dict[str, Any]:
    """Counters plus live registry size, for the health surface.
    NEVER raises."""
    try:
        with _lock:
            out: Dict[str, Any] = dict(_stats)
            out["open"] = len(_leases)
            return out
    except Exception:  # noqa: BLE001
        return {}


def reset_for_tests() -> None:
    """Forget every lease and counter. Test seam only."""
    with _lock:
        _leases.clear()
        for key in _stats:
            _stats[key] = 0
