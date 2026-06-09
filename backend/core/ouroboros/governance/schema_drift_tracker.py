"""Slice 20C — Schema drift tracker for fleet rotation on the next retry.

# What this closes

v15 soak ``bt-2026-05-26-184355`` exposed three distinct DW failure
shapes that all leave a candidate generation op stuck in a
deterministic loop with the same model:

1. **JSON malformation** (``json_parse_error``) — the model keeps
   producing the same syntactical defect on retry because the same
   model + same prompt = same output (modulo sampling noise).
2. **Schema id hallucination** — the model returns
   ``schema_version: "2b.1-diff"`` despite being told ``2c.1`` is
   canonical for the route; retrying with the same model is futile.
3. **Zero-candidate return** — the model completes 200s+ of Venom
   tool exploration then judges 0 candidates; retrying with the same
   model usually produces the same judgment.

The architectural fix per the operator's Slice 20C directive: when a
model produces a structurally-bad output on a given op_id, the next
retry within that same op MUST dispatch to a **distinct sibling model
in the trusted fleet** to break the deterministic pattern trap.

# Architectural discipline

* **Op-scoped, not global**: drift events are per-(op_id, model_id).
  They do NOT touch the global AsyncTopologySentinel breaker state.
  A model that drifts on op A is still eligible for op B — this is
  pattern-breaking, not provider banning.
* **Composes with existing dispatch walk**: the sentinel dispatch
  loop at ``candidate_generator.py:2540-2615`` already iterates
  ``ranked_models`` and skips OPEN states. Slice 20C adds one more
  skip predicate: "has this model drifted on this op?" If yes,
  treat indistinguishably from OPEN at the dispatch gate.
* **Closed taxonomy**: 3 ``DriftType`` values covering the 3 v15
  failure shapes. Adding a 4th drift kind is a deliberate slice, not
  an implicit overload — keeps the tracker's API contract tight.
* **Bounded memory**: per-op ring (default 10 events/op), global cap
  on tracked op_ids (default 256 ops). Tracker NEVER grows unbounded
  — a long-running session with hundreds of ops auto-evicts oldest.
* **Master flag default-FALSE**: ``JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED``
  opts in. Graduate after a v16+ soak proves at least one op was
  rescued via rotation (drifted model X → rotated to model Y → Y
  succeeded → op reached APPLY → VERIFY → RESOLVED).
* **Substrate-level tracker, observability-bystander**: the tracker
  is a thin in-memory dict + ring; it has no authority over dispatch
  (the dispatcher consults it). Compatible with the existing §8
  observability discipline.

# Lifecycle

* **Record**: when a parse fails / hallucinated schema id detected /
  zero-candidate verdict surfaces, the caller records the drift.
* **Consult**: the dispatcher's ranked-models walk consults
  ``has_drifted(op_id, model_id)`` per iteration; if true, skip.
* **Clear**: when an op generation succeeds (a model produces a
  validated candidate that reaches APPLY-ready), the caller clears
  the op's drift history. This makes follow-up retries (e.g. L2
  repair iterations on the same op_id) start with a clean slate.
* **Auto-evict**: when the tracked op cap is hit, the oldest op's
  entire drift history is evicted. Deterministic FIFO so test runs
  reproduce.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants + env knobs
# ──────────────────────────────────────────────────────────────────────

_ENV_MASTER = "JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED"
_ENV_MAX_EVENTS_PER_OP = "JARVIS_SCHEMA_DRIFT_MAX_EVENTS_PER_OP"
_ENV_MAX_TRACKED_OPS = "JARVIS_SCHEMA_DRIFT_MAX_TRACKED_OPS"

_DEFAULT_MAX_EVENTS_PER_OP = 10
_DEFAULT_MAX_TRACKED_OPS = 256

# Hard floor + ceiling on env-tunable bounds so misconfigured deploys
# can't blow up memory. Slice 20C is hot-path observed (consulted on
# every dispatch iteration) — we keep the bounds tight.
_HARD_FLOOR_EVENTS = 1
_HARD_CEILING_EVENTS = 100
_HARD_FLOOR_OPS = 16
_HARD_CEILING_OPS = 4096


# ──────────────────────────────────────────────────────────────────────
# Closed taxonomies
# ──────────────────────────────────────────────────────────────────────


class DriftType(str, Enum):
    """The closed set of structurally-bad outputs Slice 20C rotates on.

    Each value corresponds to one v15 failure shape. Extending this
    enum is a deliberate slice — adding a new drift kind requires
    updating the dispatcher's skip predicate and the audit ledger.
    """

    JSON_PARSE_ERROR_AFTER_HEAL = "json_parse_error_after_heal"
    """JSON failed deterministic regex repair (and LLM heal if enabled).

    Sourced from the parse path in ``providers._parse_generation_response``
    /``json_healer.heal_and_retry_parse``. The model's syntactic output
    is broken — retrying with the same model is unlikely to fix it.
    """

    SCHEMA_ID_HALLUCINATION = "schema_id_hallucination"
    """Model returned a schema_version it was explicitly told NOT to use.

    v15 example: route declared ``2c.1`` canonical, model returned
    ``2b.1-diff``. The model is ignoring schema discipline on this
    op — a sibling model with different prior distribution may comply.
    """

    ZERO_CANDIDATE_RETURN = "zero_candidate_return"
    """Parser succeeded but returned ``candidates=()`` for a non-no-op.

    Sourced from ``GenerationResult.candidates`` being empty when the
    op was not declared no-op. Distinct from no_op (which is a
    LEGITIMATE judgment) — this is the model judging "nothing to do"
    AFTER it explored the codebase via Venom and after the prompt
    explicitly prohibits a zero-candidate return (Phase 3 reinforcement).
    A sibling model may be less prone to this judgment.
    """

    DUAL_ARM_FAILURE = "dual_arm_failure"
    """Slice 194 — a proactive hedge race where BOTH transport arms failed
    on this model (e.g. an RT RuntimeError + a structural batch rejection).

    Sourced from ``race_triage.record_dual_arm_blacklist`` after the
    triage engine confirms a hard model/endpoint blockage (internal
    faults and cancelled arms are carved out — Slice 185 doctrine).
    Stored here so dual-arm events share the bounded per-op storage and
    the /drift audit surface; the dispatch skip predicate for THIS kind
    is ``race_triage.is_blacklisted_for_op`` (own master, default TRUE),
    NOT ``has_drifted`` (whose rotation master defaults FALSE).
    """


@dataclass(frozen=True)
class DriftEvent:
    """One recorded drift instance. Frozen — never mutated after record()."""

    op_id: str
    model_id: str
    drift_type: DriftType
    timestamp: float
    raw_excerpt: str
    """First 200 chars of the offending output, for forensic recall."""


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _envb(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _envi_bounded(name: str, default: int, floor: int, ceiling: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(floor, min(ceiling, v))


def is_rotation_enabled() -> bool:
    """Master gate — read at every consultation so toggles take effect immediately."""
    return _envb(_ENV_MASTER, default=False)


# ──────────────────────────────────────────────────────────────────────
# The tracker
# ──────────────────────────────────────────────────────────────────────


class SchemaDriftTracker:
    """Op-scoped in-memory tracker of model drift events.

    Thread-safe via a single lock — drift events can arrive from
    parse paths on different asyncio tasks for different ops, so
    concurrent record() / has_drifted() reads are expected.

    Memory shape:
      ``_events: OrderedDict[op_id, Deque[DriftEvent]]``
      ``_drifted_models: OrderedDict[op_id, Set[model_id]]``  (derived)

    OrderedDict preserves insertion order for deterministic FIFO
    eviction when ``_max_tracked_ops`` is exceeded. The
    ``_drifted_models`` denormalization makes ``has_drifted()`` O(1)
    on the hot path (dispatch consults per-model-per-iteration).
    """

    def __init__(
        self,
        *,
        max_events_per_op: Optional[int] = None,
        max_tracked_ops: Optional[int] = None,
    ) -> None:
        self._max_events_per_op = max_events_per_op or _envi_bounded(
            _ENV_MAX_EVENTS_PER_OP,
            _DEFAULT_MAX_EVENTS_PER_OP,
            _HARD_FLOOR_EVENTS,
            _HARD_CEILING_EVENTS,
        )
        self._max_tracked_ops = max_tracked_ops or _envi_bounded(
            _ENV_MAX_TRACKED_OPS,
            _DEFAULT_MAX_TRACKED_OPS,
            _HARD_FLOOR_OPS,
            _HARD_CEILING_OPS,
        )
        self._events: "OrderedDict[str, Deque[DriftEvent]]" = OrderedDict()
        self._drifted_models: "OrderedDict[str, set]" = OrderedDict()
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────

    def record(
        self,
        *,
        op_id: str,
        model_id: str,
        drift_type: DriftType,
        raw_excerpt: str = "",
    ) -> DriftEvent:
        """Record a drift event for (op_id, model_id, drift_type).

        Returns the frozen ``DriftEvent`` that was stored. Always
        succeeds — recording is forensic and pattern-breaking signal;
        it MUST NOT raise into the caller.
        """
        # Defensive: bound the raw_excerpt; it's just for recall in
        # /drift dumps, not for analysis.
        excerpt = (raw_excerpt or "")[:200]
        ev = DriftEvent(
            op_id=op_id,
            model_id=model_id,
            drift_type=drift_type,
            timestamp=time.time(),
            raw_excerpt=excerpt,
        )
        with self._lock:
            ring = self._events.get(op_id)
            if ring is None:
                ring = deque(maxlen=self._max_events_per_op)
                self._events[op_id] = ring
                self._drifted_models[op_id] = set()
                # Evict oldest if we're over the cap
                while len(self._events) > self._max_tracked_ops:
                    evicted_op, _ = self._events.popitem(last=False)
                    self._drifted_models.pop(evicted_op, None)
                    logger.debug(
                        "[schema_drift] evicted oldest op=%s (cap=%d)",
                        evicted_op, self._max_tracked_ops,
                    )
            ring.append(ev)
            self._drifted_models[op_id].add(model_id)

        logger.info(
            "[schema_drift] op=%s model=%s drift_type=%s recorded "
            "(op_drift_count=%d)",
            op_id, model_id, drift_type.value, len(ring),
        )
        return ev

    def has_drifted(self, op_id: str, model_id: str) -> bool:
        """O(1) check — has ``model_id`` drifted on ``op_id``?

        The hot path predicate: dispatcher consults this per
        ``ranked_models`` iteration. Returns False when:
          - master flag is off (rotation disabled at the gate)
          - op_id has no recorded drift
          - model_id has no drift on this op_id
        """
        if not is_rotation_enabled():
            return False
        with self._lock:
            drifted = self._drifted_models.get(op_id)
            return drifted is not None and model_id in drifted

    def drifted_models(self, op_id: str) -> FrozenSet[str]:
        """All models that have drifted on ``op_id``. Used by /drift REPL + tests."""
        with self._lock:
            drifted = self._drifted_models.get(op_id)
            return frozenset(drifted) if drifted else frozenset()

    def events_for(self, op_id: str) -> Tuple[DriftEvent, ...]:
        """Bounded snapshot of all drift events for ``op_id``. For audit / tests."""
        with self._lock:
            ring = self._events.get(op_id)
            return tuple(ring) if ring is not None else ()

    def clear(self, op_id: str) -> int:
        """Wipe drift history for ``op_id``. Returns # events cleared.

        Called by the caller when an op succeeds — so subsequent
        retries (e.g. L2 repair on the same op_id) start with a clean
        slate and the dispatcher doesn't unnecessarily exclude models
        that previously drifted but might now succeed.
        """
        with self._lock:
            ring = self._events.pop(op_id, None)
            self._drifted_models.pop(op_id, None)
            return len(ring) if ring is not None else 0

    def stats(self) -> Dict[str, int]:
        """Cheap snapshot for /drift REPL + observability."""
        with self._lock:
            total_events = sum(len(r) for r in self._events.values())
            return {
                "tracked_ops": len(self._events),
                "total_events": total_events,
                "max_events_per_op": self._max_events_per_op,
                "max_tracked_ops": self._max_tracked_ops,
            }

    def reset(self) -> None:
        """Wipe all state — used by tests + ``/drift reset`` REPL."""
        with self._lock:
            self._events.clear()
            self._drifted_models.clear()


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton (mirrors get_default_broker pattern)
# ──────────────────────────────────────────────────────────────────────

_default_tracker: Optional[SchemaDriftTracker] = None
_singleton_lock = threading.Lock()


def get_default_tracker() -> SchemaDriftTracker:
    """Return the process-wide tracker, constructing on first call.

    Bounded by env knobs ``JARVIS_SCHEMA_DRIFT_MAX_EVENTS_PER_OP`` +
    ``JARVIS_SCHEMA_DRIFT_MAX_TRACKED_OPS``. Read once at construction;
    a process restart picks up changes.
    """
    global _default_tracker
    if _default_tracker is None:
        with _singleton_lock:
            if _default_tracker is None:
                _default_tracker = SchemaDriftTracker()
    return _default_tracker


def reset_default_tracker() -> None:
    """Test hook — drop the singleton so a fresh one is constructed next call."""
    global _default_tracker
    with _singleton_lock:
        _default_tracker = None
