"""WaitTimeEstimator — Slice 2 rolling EWMA per route.

Companion to :mod:`admission_gate` (Slice 1's pure-decision
substrate). The gate consumes a ``projected_wait_s`` value;
this module computes that value from observed semaphore
wait times.

Architectural rationale for the split: the admission gate stays
**stateless + bit-deterministic** so its decision function can
be tested with exact equality on records (Slice 1 §K). Rolling
EWMA state lives in a separate module so adding/removing
estimator state never affects the gate's purity invariant.

## Strict design constraints

* **NEVER raises into callers.** Every method has an outermost
  ``try/except`` that swallows + logs at DEBUG. The estimator
  cannot be the cause of an op failure.

* **Thread-safe.** A single ``threading.Lock`` guards all dict
  reads + writes. Concurrent producers (the post-sem-acquire
  ``update_observed`` calls) and concurrent readers (the
  pre-sem-acquire ``project_wait`` calls) compose correctly
  even when 8+ workers fire concurrently.

* **Memory-bounded.** Per-route observations dict; routes are
  a closed enum vocabulary (immediate / standard / complex /
  background / speculative — 5 values), so the dict has at
  most 5 entries ever. No unbounded growth.

* **Pure-stdlib.** Same import discipline as :mod:`admission_gate` —
  no ``backend.*`` deps, no ``asyncio`` (sync-only contract;
  Slice 2's call site is in sync code paths inside the async
  ``_call_fallback`` method).

* **Sane cold-start.** First observation per route initializes
  the EWMA at the observed value (no decay from a fictitious
  zero-prior). Without observations, ``project_wait`` returns
  ``0.0`` — matches the "first op pays nothing" expectation
  the admission gate's math handles correctly (zero projected
  wait → required budget collapses to ``min_viable_call_s``).

## Authority invariants (AST-pinned in Slice 3)

* MUST NOT import: ``candidate_generator`` / ``providers`` /
  ``orchestrator`` / ``urgency_router`` / ``policy`` /
  ``iron_gate`` / ``risk_tier`` / ``change_engine`` /
  ``yaml_writer`` / ``asyncio``.
* No ``exec`` / ``eval`` / ``compile``.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


ADMISSION_ESTIMATOR_SCHEMA_VERSION: str = "admission_estimator.v1"


# ---------------------------------------------------------------------------
# Env knobs — clamped, hot-revertable. Slice 3 graduation registers
# in FlagRegistry.
# ---------------------------------------------------------------------------


def estimator_alpha() -> float:
    """``JARVIS_ADMISSION_ESTIMATOR_ALPHA`` — EWMA weight on the
    new observation. Default 0.3, clamped [0.05, 0.95].

    Higher alpha → more responsive to recent waits (good when
    queue depth is volatile; can over-react to outliers).
    Lower alpha → smoother, more stable projection (good when
    queue depth is steady; slow to react to genuine load
    changes). 0.3 balances both — recent observations matter
    but a single outlier doesn't dominate."""
    raw = os.environ.get("JARVIS_ADMISSION_ESTIMATOR_ALPHA")
    if raw is None:
        return 0.3
    try:
        return max(0.05, min(0.95, float(raw)))
    except (TypeError, ValueError):
        return 0.3


# ---------------------------------------------------------------------------
# WaitTimeEstimator
# ---------------------------------------------------------------------------


class WaitTimeEstimator:
    """Per-route rolling EWMA of observed semaphore wait times.

    Thread-safe via a single ``threading.Lock``. NEVER raises.
    Memory-bounded by the size of the route enum vocabulary.

    Lifecycle:
      * Construct once per ``CandidateGenerator`` instance.
      * Call :meth:`update_observed` after EVERY successful
        sem-acquire with the observed ``sem_wait_total_s``.
      * Call :meth:`project_wait` BEFORE every sem-acquire to
        get the gate's ``projected_wait_s`` input.

    Cold-start contract: with no observations, every route
    projects ``0.0`` seconds. The gate's math (Slice 1) treats
    a zero projection as "the first op pays nothing" — required
    budget collapses to just ``min_viable_call_s``, so initial
    ops are admitted freely. Once observations accumulate, the
    EWMA reflects actual queue pressure.
    """

    def __init__(self, *, alpha: Optional[float] = None) -> None:
        try:
            if alpha is not None:
                alpha = float(alpha)
                self._alpha = max(0.05, min(0.95, alpha))
            else:
                self._alpha = estimator_alpha()
        except (TypeError, ValueError):
            self._alpha = 0.3
        self._observations: Dict[str, float] = {}
        self._sample_counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def alpha(self) -> float:
        return self._alpha

    def project_wait(self, route: str) -> float:
        """Return the EWMA wait projection for ``route``, in
        seconds. Returns ``0.0`` for unknown routes or empty
        observations (cold start). NEVER raises."""
        try:
            route_key = self._normalize_route(route)
            if not route_key:
                return 0.0
            with self._lock:
                return float(
                    self._observations.get(route_key, 0.0),
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[WaitTimeEstimator] project_wait %r degraded: "
                "%s", route, exc,
            )
            return 0.0

    def update_observed(
        self, route: str, observed_s: float,
    ) -> None:
        """Update the route's EWMA with a new observation.
        NEVER raises. Silently no-ops on garbage input (NaN,
        negative, non-numeric, empty route)."""
        try:
            route_key = self._normalize_route(route)
            if not route_key:
                return
            try:
                obs = float(observed_s)
            except (TypeError, ValueError):
                return
            # NaN check (NaN != NaN) + non-negative
            if obs != obs or obs < 0.0:
                return
            with self._lock:
                prev = self._observations.get(route_key)
                if prev is None:
                    # Cold-start: initialize at the observed
                    # value (no decay from a fictitious zero-
                    # prior).
                    new = obs
                else:
                    new = (
                        self._alpha * obs
                        + (1.0 - self._alpha) * float(prev)
                    )
                self._observations[route_key] = new
                self._sample_counts[route_key] = (
                    self._sample_counts.get(route_key, 0) + 1
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[WaitTimeEstimator] update_observed %r,%r "
                "degraded: %s", route, observed_s, exc,
            )

    def stats(self) -> Dict[str, Any]:
        """Read-only snapshot for observability. NEVER raises."""
        try:
            with self._lock:
                return {
                    "alpha": self._alpha,
                    "ewma_per_route_s": dict(self._observations),
                    "sample_counts": dict(self._sample_counts),
                    "schema_version": (
                        ADMISSION_ESTIMATOR_SCHEMA_VERSION
                    ),
                }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "alpha": self._alpha,
                "ewma_per_route_s": {},
                "sample_counts": {},
                "schema_version": (
                    ADMISSION_ESTIMATOR_SCHEMA_VERSION
                ),
            }

    def reset(self) -> None:
        """Test helper — drops all observations + sample counts.
        Production code MUST NOT call this. NEVER raises."""
        try:
            with self._lock:
                self._observations.clear()
                self._sample_counts.clear()
        except Exception:  # noqa: BLE001 — defensive
            pass

    @staticmethod
    def _normalize_route(route: Any) -> str:
        """Lower-case + strip the route string. Returns empty
        string for None / non-string inputs."""
        try:
            if route is None:
                return ""
            return str(route).strip().lower()
        except Exception:  # noqa: BLE001 — defensive
            return ""


# ---------------------------------------------------------------------------
# Slice 3 — RecentDecisionsRing for the GET-route observability surface.
# Bounded in-memory ring of recent AdmissionRecord projections.
# Module-level singleton so the candidate_generator integration
# point + ide_observability handler agree on the same instance
# without threading an explicit reference through.
# ---------------------------------------------------------------------------


from collections import deque  # noqa: E402 — keep with class
from typing import Deque  # noqa: E402


def history_ring_size() -> int:
    """``JARVIS_ADMISSION_HISTORY_RING_SIZE`` — bounded ring
    capacity. Default 64, clamped [4, 4096]. The GET route
    returns at most this many entries; older entries are
    evicted FIFO."""
    raw = os.environ.get(
        "JARVIS_ADMISSION_HISTORY_RING_SIZE",
    )
    if raw is None:
        return 64
    try:
        return max(4, min(4096, int(raw)))
    except (TypeError, ValueError):
        return 64


class RecentDecisionsRing:
    """Bounded thread-safe ring of recent admission-record
    projections. Read-only-once-recorded — appended in order,
    evicted FIFO at capacity. NEVER raises.

    Records are stored as plain dicts (the
    :meth:`AdmissionRecord.to_dict` projection) so the GET
    handler doesn't need to import the AdmissionRecord
    dataclass — the JSON-shape contract decouples reader from
    writer.
    """

    def __init__(
        self, *, capacity: Optional[int] = None,
    ) -> None:
        try:
            cap = (
                int(capacity) if capacity is not None
                else history_ring_size()
            )
            cap = max(4, min(4096, cap))
        except (TypeError, ValueError):
            cap = 64
        self._capacity = cap
        self._ring: Deque[Dict[str, Any]] = deque(maxlen=cap)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, record_dict: Dict[str, Any]) -> None:
        """Append a record's dict projection. NEVER raises.
        Garbage input (None / non-dict) silently no-ops."""
        try:
            if not isinstance(record_dict, dict):
                return
            with self._lock:
                # deque.append handles the eviction
                # automatically when at maxlen.
                self._ring.append(dict(record_dict))
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[RecentDecisionsRing] record degraded: %s", exc,
            )

    def snapshot(
        self, limit: Optional[int] = None,
    ) -> tuple:
        """Return up to ``limit`` most-recent projections, newest
        last. NEVER raises. ``limit=None`` returns all."""
        try:
            with self._lock:
                items = list(self._ring)
            if limit is not None and limit >= 0:
                items = items[-int(limit):]
            return tuple(items)
        except Exception:  # noqa: BLE001 — defensive
            return tuple()

    def stats(self) -> Dict[str, Any]:
        """Read-only telemetry. NEVER raises."""
        try:
            with self._lock:
                return {
                    "capacity": self._capacity,
                    "size": len(self._ring),
                    "schema_version": (
                        ADMISSION_ESTIMATOR_SCHEMA_VERSION
                    ),
                }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "capacity": self._capacity, "size": 0,
                "schema_version": (
                    ADMISSION_ESTIMATOR_SCHEMA_VERSION
                ),
            }

    def reset(self) -> None:
        """Test helper — drops all records. NEVER raises."""
        try:
            with self._lock:
                self._ring.clear()
        except Exception:  # noqa: BLE001 — defensive
            pass


# ---------------------------------------------------------------------------
# Process-wide singletons — module-level so the
# candidate_generator integration + ide_observability handler
# share the same instances without a runtime reference.
# ---------------------------------------------------------------------------


_DEFAULT_ESTIMATOR: Optional[WaitTimeEstimator] = None
_DEFAULT_HISTORY: Optional[RecentDecisionsRing] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_estimator() -> WaitTimeEstimator:
    """Process-wide :class:`WaitTimeEstimator` singleton.
    Constructed lazily on first call. Used by Slice 3's GET
    route + by callers that don't want to construct their own."""
    global _DEFAULT_ESTIMATOR
    with _DEFAULT_LOCK:
        if _DEFAULT_ESTIMATOR is None:
            _DEFAULT_ESTIMATOR = WaitTimeEstimator()
        return _DEFAULT_ESTIMATOR


def get_default_history() -> RecentDecisionsRing:
    """Process-wide :class:`RecentDecisionsRing` singleton.
    Constructed lazily on first call."""
    global _DEFAULT_HISTORY
    with _DEFAULT_LOCK:
        if _DEFAULT_HISTORY is None:
            _DEFAULT_HISTORY = RecentDecisionsRing()
        return _DEFAULT_HISTORY


def reset_singletons_for_tests() -> None:
    """Test helper — drops both singletons so the next get_*
    call constructs a fresh instance. NEVER called from
    production. NEVER raises."""
    global _DEFAULT_ESTIMATOR, _DEFAULT_HISTORY
    with _DEFAULT_LOCK:
        _DEFAULT_ESTIMATOR = None
        _DEFAULT_HISTORY = None


__all__ = [
    "ADMISSION_ESTIMATOR_SCHEMA_VERSION",
    "RecentDecisionsRing",
    "WaitTimeEstimator",
    "estimator_alpha",
    "get_default_estimator",
    "get_default_history",
    "history_ring_size",
    "reset_singletons_for_tests",
]
