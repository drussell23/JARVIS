"""PermissionDecisionArchive — Venom V2 observability ring.
=============================================================

Closes the Venom V2 observability gap surfaced by the §37 v10
brutal review: ``tool_permission.py`` ships the policy substrate
+ is composed by ``tool_executor._maybe_evaluate_tool_permission``,
but the operator has no query surface into recent decisions —
no REPL verb, no SSE event, no IDE GET endpoint. This module
adds the read-side ring without touching policy code.

Architecture
------------

Mirrors :class:`backend.core.ouroboros.battle_test.tool_render_store.
BoundedBodyStore` (the canonical Gap #2 ring pattern):

  * Thread-safe FIFO with drop-oldest eviction at capacity
  * Monotonic ``p-N`` refs allocated from a counter that NEVER
    rewinds — a printed ref always resolves to the same decision
    or to ``None`` (evicted), never a different decision
  * In-memory only — decisions vanish on process exit
  * Composes the existing :class:`AggregatePermissionDecision` +
    :class:`ToolPermissionDecision` taxonomy from
    ``tool_permission.py`` — ZERO new policy types
  * Master flag default-FALSE per §33.1 graduation contract

Reference scheme
----------------

``p-N`` joins the cross-substrate family — ``t-N`` (Gap #2 tool
bodies) / ``d-N`` (Gap #4 diff archive) / ``o-N`` (Gap #3 op
blocks) / ``n-N`` (Gap #6 narrative frames). The unified
``/expand <ref>`` REPL verb in serpent_flow dispatches by prefix.

Authority boundary
------------------

* §1 deterministic — pure container; no LLM, no I/O, no console
* §7 fail-closed — every public method has a fallback; invalid
  refs return ``None``, never raise
* §8 observable — :class:`ArchiveSnapshot` projection lets the
  observability layer report capacity / utilization

What this module does NOT do
----------------------------

* Decide policy — :func:`tool_permission.compute_permission_decision`
  is authoritative. This module is read-only telemetry.
* Persist to disk — capacity-only FIFO eviction.
* Re-export ``AggregatePermissionDecision`` — callers import that
  from ``tool_permission``; this module accepts pre-computed
  decisions and stores their ``to_dict()`` projection.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.PermissionDecisionArchive")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION: str = (
    "permission_decision_archive.v1"
)


MASTER_FLAG_ENV_VAR: str = "JARVIS_PERMISSION_ARCHIVE_ENABLED"
ARCHIVE_SIZE_ENV_VAR: str = "JARVIS_PERMISSION_ARCHIVE_SIZE"


_DEFAULT_ARCHIVE_SIZE: int = 50

_MIN_ARCHIVE_SIZE: int = 1
_MAX_ARCHIVE_SIZE: int = 10_000  # defensive RAM bound


# Reference prefix — exposed publicly so REPL parsers / tests can
# build refs without string-munging this module's literals.
REF_PREFIX: str = "p-"


# ===========================================================================
# Master flag
# ===========================================================================


def permission_archive_enabled() -> bool:
    """Master switch. Default-FALSE per §33.1 graduation contract.

    Recording is a no-op when this flag is off. The archive class
    itself is always importable — callers should always invoke
    :func:`get_default_archive` and let the recording code path
    short-circuit on flag-off; that keeps the import graph clean
    and makes the master flag a runtime knob rather than a
    structural toggle.
    """
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class DecisionRecord:
    """One archived permission decision.

    Composes the existing tool_permission.AggregatePermissionDecision
    projection (via its ``to_dict()``) — this module does NOT
    redefine the decision taxonomy.

    Fields
    ------
    * ``ref`` — opaque expansion handle (``"p-12"``); the only
      stable identifier callers should use.
    * ``op_id`` / ``tool_name`` — original key components, kept
      for filtering queries.
    * ``decision_value`` — ``"allow"`` / ``"deny"`` / ``"ask"`` /
      ``"defer"`` (the canonical 4-value taxonomy from
      :class:`tool_permission.ToolPermissionDecision`). Stored as
      string for projection-friendliness; the canonical enum lives
      in tool_permission.py.
    * ``decision_projection`` — the full ``AggregatePermissionDecision.
      to_dict()`` mapping. Read-only; consumers MUST NOT mutate.
    * ``inserted_at`` — ``time.monotonic()`` timestamp; for telemetry.
    """

    ref: str
    op_id: str
    tool_name: str
    decision_value: str
    decision_projection: Dict[str, Any]
    inserted_at: float
    schema_version: str = (
        PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "op_id": self.op_id,
            "tool_name": self.tool_name,
            "decision_value": self.decision_value,
            "decision": dict(self.decision_projection),
            "inserted_at": self.inserted_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ArchiveSnapshot:
    """Read-only projection of the archive's state for observability."""

    capacity: int
    size: int
    next_seq: int
    schema_version: str = (
        PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION
    )

    @property
    def utilization(self) -> float:
        """Fraction in [0.0, 1.0] of capacity currently used."""
        if self.capacity <= 0:
            return 0.0
        return min(1.0, self.size / self.capacity)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capacity": self.capacity,
            "size": self.size,
            "next_seq": self.next_seq,
            "utilization": self.utilization,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Helpers
# ===========================================================================


def _read_capacity_from_env() -> int:
    raw = os.environ.get(ARCHIVE_SIZE_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_ARCHIVE_SIZE
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.debug(
            "[PermissionDecisionArchive] %s=%r is not an int; "
            "using default %d",
            ARCHIVE_SIZE_ENV_VAR, raw, _DEFAULT_ARCHIVE_SIZE,
        )
        return _DEFAULT_ARCHIVE_SIZE
    if parsed < _MIN_ARCHIVE_SIZE:
        return _MIN_ARCHIVE_SIZE
    if parsed > _MAX_ARCHIVE_SIZE:
        return _MAX_ARCHIVE_SIZE
    return parsed


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


# ===========================================================================
# BoundedDecisionArchive — the ring
# ===========================================================================


class BoundedDecisionArchive:
    """Thread-safe bounded FIFO of permission decisions.

    Mirrors :class:`tool_render_store.BoundedBodyStore` semantics
    exactly — the goal is a single canonical ring shape across the
    cross-substrate ``/expand <ref>`` family (``t-N``/``d-N``/
    ``o-N``/``n-N``/``p-N``).

    Eviction policy
    ---------------
    Drop-oldest on overflow. The newest decision always wins
    capacity against the oldest; refs to evicted decisions become
    permanently invalid and resolve to ``None`` via :meth:`lookup`.

    Reference allocation
    --------------------
    Refs are issued from a monotonic counter (``p-1``, ``p-2``, …).
    The counter never resets within an archive instance — even
    when eviction shrinks ``size`` back to zero. A printed ref
    refers either to the same decision (still in the ring) or to
    nothing (evicted) — NEVER a different decision.

    Thread safety
    -------------
    A single :class:`threading.RLock` serializes mutating + reading
    operations. Reentrant so an observer reading via :meth:`snapshot`
    inside a listener doesn't self-deadlock.
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        if capacity is None:
            cap = _read_capacity_from_env()
        else:
            try:
                int_cap = int(capacity)
            except (TypeError, ValueError):
                int_cap = _DEFAULT_ARCHIVE_SIZE
            cap = max(
                _MIN_ARCHIVE_SIZE,
                min(_MAX_ARCHIVE_SIZE, int_cap),
            )
        self._capacity: int = cap
        self._items: "OrderedDict[str, DecisionRecord]" = (
            OrderedDict()
        )
        self._next_seq: int = 1
        self._lock = threading.RLock()

    # ---- introspection ----------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> ArchiveSnapshot:
        """Cheap read-only projection. NEVER raises."""
        with self._lock:
            return ArchiveSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
            )

    # ---- mutating API -----------------------------------------------

    def record(
        self,
        *,
        op_id: object,
        tool_name: object,
        decision: Any,
    ) -> Optional[DecisionRecord]:
        """Park one permission decision and return the
        :class:`DecisionRecord` (with stable ``ref``).

        Composes the canonical
        :class:`tool_permission.AggregatePermissionDecision`
        projection — ``decision`` MUST expose a ``to_dict()``
        method (the §33.5 frozen-artifact contract) AND a
        ``decision`` attribute carrying the
        :class:`ToolPermissionDecision` enum (or any object
        whose ``.value`` is a 4-value taxonomy string). When the
        decision shape is malformed, the record is still archived
        with ``decision_value="defer"`` (the safe DEFER default
        from the policy substrate's first-match-wins aggregation)
        rather than raising.

        Master-flag-gated: when
        :func:`permission_archive_enabled` is ``False`` this is
        a no-op returning ``None`` (callers should treat that as
        "telemetry skipped, not an error"). NEVER raises into
        the policy path.
        """
        if not permission_archive_enabled():
            return None

        op_id_safe = _safe_str(op_id)
        tool_safe = _safe_str(tool_name)

        # Extract decision value via duck-typing — composes
        # ToolPermissionDecision.value contract without importing
        # the enum (preserves authority asymmetry; this module
        # MUST NOT import tool_permission's policy code).
        decision_value = "defer"
        try:
            inner = getattr(decision, "decision", None)
            if inner is not None:
                v = getattr(inner, "value", None)
                if isinstance(v, str) and v in (
                    "allow", "deny", "ask", "defer",
                ):
                    decision_value = v
        except Exception:  # noqa: BLE001
            pass

        # Extract projection via the §33.5 to_dict contract;
        # fall back to {} when the shape is foreign so we still
        # archive the event for ref-stability.
        projection: Dict[str, Any] = {}
        try:
            to_dict = getattr(decision, "to_dict", None)
            if callable(to_dict):
                raw = to_dict()
                if isinstance(raw, dict):
                    projection = dict(raw)
        except Exception:  # noqa: BLE001
            projection = {}

        with self._lock:
            ref = f"{REF_PREFIX}{self._next_seq}"
            self._next_seq += 1
            record = DecisionRecord(
                ref=ref,
                op_id=op_id_safe,
                tool_name=tool_safe,
                decision_value=decision_value,
                decision_projection=projection,
                inserted_at=time.monotonic(),
            )
            self._items[ref] = record
            # Evict oldest until back within capacity.
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return record

    def clear(self) -> None:
        """Drop all parked decisions (e.g. on session end). The
        ref counter is NOT reset — see class docstring."""
        with self._lock:
            self._items.clear()

    # ---- lookup ------------------------------------------------------

    def lookup(self, ref: object) -> Optional[DecisionRecord]:
        """Resolve a ref to its :class:`DecisionRecord`, or ``None``
        if absent / evicted / malformed. NEVER raises."""
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def all_refs(self) -> Tuple[str, ...]:
        """All currently-resident refs, oldest → newest."""
        with self._lock:
            return tuple(self._items.keys())

    def recent(self, limit: int = 20) -> List[DecisionRecord]:
        """Newest-first list, capped at ``limit``. NEVER raises."""
        try:
            cap = max(0, int(limit))
        except (TypeError, ValueError):
            cap = 20
        with self._lock:
            items = list(self._items.values())
        items.reverse()  # newest first
        return items[:cap]

    def by_tool(
        self, tool_name: object, *, limit: int = 20,
    ) -> List[DecisionRecord]:
        """Filter to one tool name (case-sensitive exact match),
        newest-first. NEVER raises."""
        target = _safe_str(tool_name)
        if not target:
            return []
        try:
            cap = max(0, int(limit))
        except (TypeError, ValueError):
            cap = 20
        with self._lock:
            items = [
                r for r in self._items.values()
                if r.tool_name == target
            ]
        items.reverse()
        return items[:cap]

    def by_op(
        self, op_id: object, *, limit: int = 100,
    ) -> List[DecisionRecord]:
        """Filter to one op (case-sensitive exact match),
        newest-first. NEVER raises."""
        target = _safe_str(op_id)
        if not target:
            return []
        try:
            cap = max(0, int(limit))
        except (TypeError, ValueError):
            cap = 100
        with self._lock:
            items = [
                r for r in self._items.values()
                if r.op_id == target
            ]
        items.reverse()
        return items[:cap]


# ===========================================================================
# Module singleton
# ===========================================================================


_default_archive: Optional[BoundedDecisionArchive] = None
_singleton_lock = threading.Lock()


def get_default_archive() -> BoundedDecisionArchive:
    """Return the process-wide default archive (constructed lazily).

    Capacity is read from :data:`ARCHIVE_SIZE_ENV_VAR` at first
    construction. Use :func:`reset_default_archive_for_tests` to
    drop the singleton between tests.
    """
    global _default_archive
    with _singleton_lock:
        if _default_archive is None:
            _default_archive = BoundedDecisionArchive()
        return _default_archive


def reset_default_archive_for_tests() -> None:
    """Test isolation hook — drops the singleton; next call to
    :func:`get_default_archive` re-reads the env."""
    global _default_archive
    with _singleton_lock:
        _default_archive = None


# ===========================================================================
# Producer-bridge §33.2 — single-line composition for tool_permission
# ===========================================================================


def maybe_record_decision(
    *,
    op_id: object,
    tool_name: object,
    decision: Any,
) -> None:
    """Single-call producer-bridge §33.2 for the policy substrate.

    Composes the canonical archive without leaking it into the
    policy module's import graph (callers import THIS function,
    not the archive class). Master-flag-gated: when
    :func:`permission_archive_enabled` is False this is a no-op.

    **SSE producer-bridge** (Slice 3): when the record is
    archived successfully, also fires
    ``EVENT_TYPE_PERMISSION_DECISION_RECORDED`` on the canonical
    :class:`StreamEventBroker` so IDE consumers see the decision
    live. Composes the existing :func:`publish_task_event`
    bridge (best-effort, never raises; stream-side master flag
    ``JARVIS_IDE_STREAM_ENABLED`` still gates the publish).

    NEVER raises into the policy path.
    """
    try:
        if not permission_archive_enabled():
            return
        archive = get_default_archive()
        record = archive.record(
            op_id=op_id,
            tool_name=tool_name,
            decision=decision,
        )
        # SSE producer-bridge §33.2 — fire the canonical
        # permission_decision_recorded event so IDE consumers
        # (Slice 4 GET endpoint, VS Code extension) can correlate
        # the decision to a /expand p-N retrieval. Best-effort:
        # stream-disabled / broker-unavailable / publish-raises
        # all fall through silently. The archive's authoritative
        # record() write happened above; SSE is a parallel
        # observability surface.
        if record is not None:
            try:
                from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                    EVENT_TYPE_PERMISSION_DECISION_RECORDED,
                    publish_task_event,
                )
                publish_task_event(
                    EVENT_TYPE_PERMISSION_DECISION_RECORDED,
                    record.op_id,
                    record.to_dict(),
                )
            except Exception:  # noqa: BLE001 — best-effort
                pass
    except Exception as exc:  # noqa: BLE001 — never raise into policy path
        logger.debug(
            "[PermissionDecisionArchive] maybe_record_decision "
            "swallowed: %r", exc,
        )


__all__ = [
    "ARCHIVE_SIZE_ENV_VAR",
    "ArchiveSnapshot",
    "BoundedDecisionArchive",
    "DecisionRecord",
    "MASTER_FLAG_ENV_VAR",
    "PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION",
    "REF_PREFIX",
    "get_default_archive",
    "maybe_record_decision",
    "permission_archive_enabled",
    "register_flags",
    "reset_default_archive_for_tests",
]


# ===========================================================================
# Venom V2 Slice 5 — FlagRegistry self-registration
# ===========================================================================
#
# Auto-discovered by ``flag_registry_seed._discover_module_provided_flags``
# via the ``backend.core.ouroboros.governance`` provider package walk. The
# §33.3 naming-cage discipline applied to flags: co-located with the
# consuming substrate (this module), zero edits to the seed file. Adding
# a new flag here lands in the registry next boot without touching
# ``flag_registry_seed.SEED_SPECS``.


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration for the Venom V2
    permission-decision archive.

    Returns count of FlagSpecs added. NEVER raises — graduation soak
    path is fail-open (the canonical module-discovery primitive
    swallows our return value as a best-effort hint).

    Composes the canonical :class:`FlagSpec` shape from
    :mod:`flag_registry`. Mirrors the seed pattern from
    ``tool_render_view.register_flags`` (Gap #2 Slice 5, 2026-05-04):

      * master kill switch first (``SAFETY`` category, BOOL,
        default-FALSE per §33.1 graduation contract — operator
        flip-points)
      * capacity tuning second (``CAPACITY`` category, INT,
        default 50 — RAM-bound)

    Both flags are operator-visible via ``/help flags`` + ``GET
    /observability/flags`` + Levenshtein typo detection.
    """
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name=MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for the Venom V2 permission "
                "decision archive. When false, "
                "``maybe_record_decision`` short-circuits before "
                "the ring write AND before the SSE producer-bridge "
                "(``permission_decision_recorded`` event). The "
                "IDE GET routes ``/observability/tool-permissions"
                "[/by-tool/{tool_name}|/{op_id}]`` 403 with "
                "reason_code=``ide_observability.tool_permissions_"
                "disabled``. The REPL verb ``/tool_permissions`` "
                "returns a disabled-notice (help still works). "
                "Default FALSE per §33.1 graduation contract — "
                "operator flips via 3-clean-soak ladder when "
                "Venom V2 callback registrations land in production."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "permission_decision_archive.py"
            ),
            example="true",
            since="Venom V2 Slice 1 (v2.89, 2026-05-10)",
        ),
        FlagSpec(
            name=ARCHIVE_SIZE_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_ARCHIVE_SIZE,
            description=(
                "Capacity of the BoundedDecisionArchive ring (Slice "
                "1) — the session-scoped FIFO of permission "
                "decisions behind ``/expand p-N`` recovery hints + "
                "``/tool_permissions`` REPL queries + "
                "``/observability/tool-permissions`` IDE GET. "
                "Drop-oldest eviction; clamped to [1, 10_000]. "
                "Increase for high-decision-throughput sessions "
                "(rare today since Venom V2 callback registrations "
                "are operator-opt-in); decrease for memory-tight "
                "environments. Monotonic ``p-N`` ref counter "
                "NEVER rewinds even after eviction — refs always "
                "resolve to the same decision or to None."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "permission_decision_archive.py"
            ),
            example="50",
            since="Venom V2 Slice 1 (v2.89, 2026-05-10)",
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — boot-time fail-open
            logger.debug(
                "[PermissionDecisionArchive] flag registration "
                "failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count
