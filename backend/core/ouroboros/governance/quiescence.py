"""
Autonomous Quiescence Protocol — Core Isolation (Task #104, 2026-05-14).

The B1 falsification campaign (Task #103) produced definitive proof:
disabling Oracle's boot ``full_index`` flipped the Claude SDK
``stream.first_raw_event`` count from **0 → 24** — Oracle's 29k-file
index was the dominant event-loop suffocator.  But the residual
timing (`first_raw_event` still arrived 94–333s late with 97 other
not-done tasks) proved the remaining consciousness/sensor loops are
also guilty.  Per-subsystem ``asyncio.sleep(0)`` whack-a-mole (Task
#102) helps but does not mathematically guarantee the core stream
gets the loop.

This module deploys **deterministic containment**: a single global
``asyncio.Event`` (the *quiescence gate*).  Default **set** —
background work allowed.  When the GENERATE phase engages the Claude
SDK stream, the core **clears** the gate; every heavy background
loop that awaits the gate at the top of its critical iteration
instantly drops to 0% CPU (it is parked in ``Event.wait()``, which
consumes nothing).  When the stream terminates, the core **sets**
the gate and the background matrix resumes.

Design invariants:

  * **Refcounted core entry** — the BG pool runs up to 3 concurrent
    workers; concurrent GENERATE streams must not race the gate.
    First concurrent core entrant clears; last exit sets.  Guarded
    by an ``asyncio.Lock``.

  * **Bounded max-pause (anti-starvation)** — a hung core must not
    freeze the organism forever.  ``await_quiescence_clearance``
    wraps ``Event.wait()`` in ``asyncio.wait_for`` with
    ``JARVIS_QUIESCENCE_MAX_PAUSE_S`` (default 420s — longer than
    any single GENERATE budget).  On timeout the background loop
    proceeds anyway and logs a WARN: degrade, never starve.

  * **Lazy loop binding** — ``asyncio.Event`` / ``asyncio.Lock``
    must be created inside the running loop.  Module-level getters
    create on first use (always from within the live loop during a
    stream / a background iteration).

  * **Master switch** — ``JARVIS_QUIESCENCE_PROTOCOL_ENABLED``
    (default true, SAFETY).  When false, both surfaces are no-ops:
    ``await_quiescence_clearance`` returns immediately,
    ``quiescence_core_active`` is a pass-through.  Byte-identical
    legacy behavior.

  * **No new primitive** — composes ``asyncio.Event`` +
    ``asyncio.Lock`` + ``asyncio.wait_for`` only.  No subprocess,
    no thread, no fracture of the async context.

  * **Composition over duplication** — Task #102's
    ``cooperative_yield_every_n_async`` is extended to call
    ``await_quiescence_clearance`` at each yield point, so every
    existing consumer (Oracle ``_scan_for_changes`` + any future
    one) gets containment for free.  Heavy loops that don't use
    that primitive (Oracle boot ``_index_repository``, sensor
    poll loops) adopt the gate with a single
    ``await await_quiescence_clearance()`` line.

NEVER raises into the core path or a background loop — all failure
modes degrade to "proceed".
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

logger = logging.getLogger("Ouroboros.Quiescence")

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Longer than any single GENERATE budget (max ~360s thinking + grace)
# so a healthy core never trips the safety valve, but a wedged core
# can't freeze the organism beyond this ceiling.
_QUIESCENCE_MAX_PAUSE_S_DEFAULT = 420.0

# Lazy singletons — must bind to the running loop, created on first
# use (always from within the live event loop).
_gate: Optional[asyncio.Event] = None
_refcount_lock: Optional[asyncio.Lock] = None
_core_refcount: int = 0


def quiescence_protocol_enabled() -> bool:
    """Master switch — ``JARVIS_QUIESCENCE_PROTOCOL_ENABLED`` (default
    true).  False → both surfaces are no-ops (byte-identical legacy)."""
    return os.environ.get(
        "JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def resolve_max_pause_s() -> float:
    """Resolve ``JARVIS_QUIESCENCE_MAX_PAUSE_S`` to a positive float.
    Invalid / non-positive → default (420s).  This is the
    anti-starvation ceiling: background loops proceed (degraded) if
    the core holds the gate longer than this."""
    try:
        _raw = float(
            os.environ.get(
                "JARVIS_QUIESCENCE_MAX_PAUSE_S",
                str(_QUIESCENCE_MAX_PAUSE_S_DEFAULT),
            )
        )
    except (TypeError, ValueError):
        return _QUIESCENCE_MAX_PAUSE_S_DEFAULT
    return _raw if _raw > 0.0 else _QUIESCENCE_MAX_PAUSE_S_DEFAULT


def _get_gate() -> asyncio.Event:
    """Lazy gate accessor — created set (background allowed) on first
    use within the running loop."""
    global _gate
    if _gate is None:
        _gate = asyncio.Event()
        _gate.set()  # DEFAULT: background work ALLOWED
    return _gate


def _get_refcount_lock() -> asyncio.Lock:
    global _refcount_lock
    if _refcount_lock is None:
        _refcount_lock = asyncio.Lock()
    return _refcount_lock


def is_core_active() -> bool:
    """Read-only probe — True when the core currently holds the gate
    (background should be paused).  Safe to call outside a loop
    (returns False if the gate was never created)."""
    if _gate is None:
        return False
    return not _gate.is_set()


async def await_quiescence_clearance(*, label: str = "") -> bool:
    """Background-loop checkpoint.  Call at the TOP of a heavy loop's
    critical iteration.

    * Master off → returns ``True`` immediately (legacy no-op).
    * Gate set (no core activity) → returns ``True`` immediately.
    * Gate cleared (core stream in flight) → parks in
      ``Event.wait()`` (0% CPU) until the core releases, bounded by
      ``JARVIS_QUIESCENCE_MAX_PAUSE_S``.

    Returns ``True`` if it proceeded normally (gate was/became set),
    ``False`` if it proceeded via the anti-starvation safety valve
    (core held too long — background degrades, never starves).

    NEVER raises.
    """
    if not quiescence_protocol_enabled():
        return True
    try:
        gate = _get_gate()
        if gate.is_set():
            return True
        await asyncio.wait_for(gate.wait(), timeout=resolve_max_pause_s())
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "[Quiescence] max-pause %.0fs exceeded — core held the "
            "gate too long (label=%s); background loop proceeding "
            "(degrade, NOT starve).  Investigate a wedged GENERATE "
            "if this recurs.",
            resolve_max_pause_s(), label or "?",
        )
        return False
    except Exception:  # noqa: BLE001 — never raise into a bg loop
        logger.debug(
            "[Quiescence] await_quiescence_clearance degraded "
            "(label=%s)", label or "?", exc_info=True,
        )
        return True


@asynccontextmanager
async def quiescence_core_active(*, label: str = "") -> AsyncIterator[None]:
    """Core-side context manager.  Wrap the Claude SDK stream's
    critical network section in this.

    First concurrent core entrant CLEARS the gate (background matrix
    instantly parks in ``Event.wait``).  Last exit SETS it (matrix
    resumes).  Refcounted under an ``asyncio.Lock`` so the BG pool's
    concurrent workers compose correctly.

    Master off → pure pass-through.  NEVER raises from the
    enter/exit bookkeeping (the wrapped body's exceptions propagate
    normally — we only guard the gate mutation).
    """
    global _core_refcount
    if not quiescence_protocol_enabled():
        yield
        return

    _entered = False
    try:
        gate = _get_gate()
        lock = _get_refcount_lock()
        async with lock:
            _core_refcount += 1
            _entered = True
            if _core_refcount == 1:
                gate.clear()
                logger.info(
                    "[Quiescence] core ACTIVE (label=%s) — background "
                    "matrix PAUSED (refcount=1)", label or "?",
                )
    except Exception:  # noqa: BLE001 — gate bookkeeping must not break core
        logger.debug(
            "[Quiescence] core-enter bookkeeping degraded (label=%s)",
            label or "?", exc_info=True,
        )
        _entered = False

    try:
        yield
    finally:
        if _entered:
            try:
                lock = _get_refcount_lock()
                async with lock:
                    _core_refcount -= 1
                    if _core_refcount <= 0:
                        _core_refcount = 0
                        _get_gate().set()
                        logger.info(
                            "[Quiescence] core RELEASED (label=%s) — "
                            "background matrix RESUMED (refcount=0)",
                            label or "?",
                        )
            except Exception:  # noqa: BLE001
                # Last-ditch: ensure the gate is not left cleared
                # forever if bookkeeping failed.
                try:
                    if _gate is not None:
                        _gate.set()
                except Exception:
                    pass
                logger.debug(
                    "[Quiescence] core-exit bookkeeping degraded; "
                    "gate force-set as failsafe (label=%s)",
                    label or "?", exc_info=True,
                )


def reset_for_tests() -> None:
    """Test-only — drop the lazy singletons so each test starts from
    a clean gate state."""
    global _gate, _refcount_lock, _core_refcount
    _gate = None
    _refcount_lock = None
    _core_refcount = 0


__all__ = [
    "await_quiescence_clearance",
    "is_core_active",
    "quiescence_core_active",
    "quiescence_protocol_enabled",
    "reset_for_tests",
    "resolve_max_pause_s",
]
