"""In-Flight Operation Registry — typed parallel registry of
operations currently running through the governance pipeline.

**Why this exists** — the audit traced P2's "ops hang 1800s never
reaching a TERMINAL_OPERATION_STATE" failure mode to a structural
visibility gap: ``GovernedLoopService._active_ops`` is a
``Set[str]`` of dedupe-keys. It tells us *whether* an op is in
flight, but carries no metadata — no start time, no deadline, no
``ctx`` reference, no current phase. Code that needs to ask
"which ops are past their deadline?" or "which op has been stuck
in phase X for N seconds?" can't compose a bare set.

The Fail-Fast circuit-breaker (b07bb03965) is the first
convergence primitive — it forces ops to ``FAILED`` after N
consecutive exhaustions. But it's exhaustion-specific (Fix B):
ops can hang for other reasons (GENERATE_RETRY registry gap →
Fix C; fire-and-forget autoscore → Fix A; ``operation_terminal``
SSE never fired). To generalize convergence into a universal
invariant, the *reaper* needs an enriched in-flight view this
substrate provides.

**Scope** — this module is pure-data infrastructure. It does NOT
schedule the reaper, publish SSE, or call into the orchestrator.
It is the typed source of truth that
:mod:`convergence_reaper` (Slice 2) and any future observers
(IDE GET endpoints, REPL ``/inflight`` verb, etc.) compose.

The dependency direction is one-way: consumers compose this
substrate; this substrate composes nothing from governance. The
``Any`` ctx ref is deliberate — keeping the type loose lets us
stash ``OperationContext`` without importing ``op_context``,
which would create an observability → state-machine cycle.

**Master flag** — ``JARVIS_IN_FLIGHT_REGISTRY_ENABLED`` (default
**FALSE**, §33.1). When off, every entry is a no-op or an empty
snapshot; the registry data structure stays alive (descriptive,
not authoritative) so callers can register conservatively
without branching on the master flag. When on, the reaper's
view of in-flight ops becomes load-bearing.

**Concurrency** — the registry uses ``threading.RLock`` around
the dict mutation surface. The governance loop runs on a single
asyncio event loop, but background tasks (BGAgentPool workers,
the reaper itself) may interact concurrently. ``RLock``
(re-entrant) lets a single thread compose ``register`` from
within an observer callback without deadlocking. All mutating
methods acquire the lock; ``snapshot`` returns an immutable
tuple under the lock so iteration can proceed unlocked.
"""
from __future__ import annotations

import ast as _ast
import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


IN_FLIGHT_REGISTRY_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Master flag (§33.1)
# ---------------------------------------------------------------------------


_MASTER_FLAG = "JARVIS_IN_FLIGHT_REGISTRY_ENABLED"


def master_enabled() -> bool:
    """Return ``True`` iff the in-flight registry is master-ON.

    Default is ``False`` per §33.1. When off, the data structure
    still works — callers can register / unregister / snapshot
    without branching — but observers (the reaper) read it as
    advisory rather than authoritative.
    """
    return os.environ.get(_MASTER_FLAG, "false").lower() == "true"


# ---------------------------------------------------------------------------
# Phase enum (descriptive — not authoritative)
# ---------------------------------------------------------------------------


class InFlightPhase(str, enum.Enum):
    """Coarse pipeline-phase taxonomy for in-flight ops.

    Mirrors the canonical :class:`OperationPhase` (op_context.py)
    but stays decoupled — keeping this module's import surface
    minimal. Consumers that need the precise phase walk
    ``record.last_phase_name`` (the original phase string the
    caller passed at registration / update); this enum is just
    a stable closed taxonomy for snapshot consumers (UIs, the
    IDE GET observability endpoint) that don't want to depend on
    op_context.
    """

    ROUTE = "route"
    PLAN = "plan"
    GENERATE = "generate"
    VALIDATE = "validate"
    APPROVE = "approve"
    APPLY = "apply"
    VERIFY = "verify"
    POSTMORTEM = "postmortem"
    OTHER = "other"

    @classmethod
    def from_name(cls, name: Optional[str]) -> "InFlightPhase":
        """Coarse-grain coerce an arbitrary phase string into the
        closed taxonomy. Unknown phases fold to ``OTHER`` rather
        than raising — keeps the substrate resilient to upstream
        phase additions."""
        if not name:
            return cls.OTHER
        try:
            return cls(name.lower())
        except ValueError:
            return cls.OTHER


# ---------------------------------------------------------------------------
# OpInFlight record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpInFlight:
    """Immutable snapshot of an in-flight operation.

    Frozen so consumers (reaper, IDE observability, REPL) cannot
    mutate a record they pulled from a snapshot — any update goes
    through :meth:`InFlightRegistry.update_phase` which atomically
    swaps the entry under the registry lock.

    Field semantics:

    * ``op_id`` — load-bearing identity; matches the
      ``OperationContext.op_id`` the orchestrator uses for SSE
      payloads.

    * ``started_at_monotonic`` — captured at :meth:`register`.
      Used for "time in flight" telemetry and for the reaper's
      deadline check (``now - started >= timeout``). Monotonic
      so clock skew doesn't matter.

    * ``deadline_monotonic`` — absolute monotonic timestamp at
      which the op is *past deadline*. If ``None``, the op has
      no caller-supplied deadline; the reaper falls back to its
      own configured ceiling (Slice 2 concern).

    * ``ctx_ref`` — opaque reference to the canonical
      ``OperationContext``. ``Any`` type to avoid the op_context
      import cycle. The reaper composes a thin view around this
      to feed ``publish_operation_terminal`` without mutating
      the orchestrator's own ctx.

    * ``last_phase_name`` — string name of the last phase the op
      was observed entering. Free-form so it survives upstream
      phase enum additions.

    * ``last_phase_at_monotonic`` — when we last observed a phase
      change. Useful for "stuck in phase X for N seconds"
      diagnostics; bounded by ``started_at_monotonic`` at
      registration.

    * ``metadata`` — best-effort caller-supplied dict (provider,
      route, urgency, etc.) for diagnostic surfaces. Frozen
      defensively so consumers can't accidentally mutate the
      registry's view.
    """

    op_id: str
    started_at_monotonic: float
    deadline_monotonic: Optional[float] = None
    ctx_ref: Any = None
    last_phase_name: str = ""
    last_phase_at_monotonic: float = 0.0
    metadata: Tuple[Tuple[str, str], ...] = field(
        default_factory=tuple,
    )
    schema_version: str = IN_FLIGHT_REGISTRY_SCHEMA_VERSION

    def time_in_flight_s(
        self, *, now_monotonic: Optional[float] = None,
    ) -> float:
        """Return seconds elapsed since :meth:`register` was
        called for this op."""
        now = (
            now_monotonic if now_monotonic is not None
            else time.monotonic()
        )
        return max(0.0, now - self.started_at_monotonic)

    def is_past_deadline(
        self, *, now_monotonic: Optional[float] = None,
    ) -> bool:
        """Return ``True`` iff this op has a deadline and we've
        crossed it. Returns ``False`` for ops without an explicit
        deadline (those fall under the reaper's own ceiling
        policy in Slice 2)."""
        if self.deadline_monotonic is None:
            return False
        now = (
            now_monotonic if now_monotonic is not None
            else time.monotonic()
        )
        return now >= self.deadline_monotonic

    def coarse_phase(self) -> InFlightPhase:
        """Map ``last_phase_name`` to the closed
        :class:`InFlightPhase` taxonomy. Unknown phases fold to
        ``OTHER``."""
        return InFlightPhase.from_name(self.last_phase_name)

    def to_dict(self) -> Dict[str, Any]:
        """Lossless §33.5 dict view for SSE payloads / IDE GET
        endpoints. Excludes ``ctx_ref`` because it isn't JSON-
        serializable and isn't meaningful outside the process."""
        return {
            "op_id": self.op_id,
            "started_at_monotonic": self.started_at_monotonic,
            "deadline_monotonic": self.deadline_monotonic,
            "last_phase_name": self.last_phase_name,
            "last_phase_at_monotonic": (
                self.last_phase_at_monotonic
            ),
            "coarse_phase": self.coarse_phase().value,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# InFlightRegistry
# ---------------------------------------------------------------------------


class InFlightRegistry:
    """Thread-safe typed registry of in-flight operations.

    Single instance recommended per process; the module-level
    :func:`get_default_registry` returns one. Tests instantiate
    fresh registries via the constructor for isolation.

    Public surface:

    * :meth:`register` — add an op (idempotent on op_id)
    * :meth:`unregister` — remove an op (idempotent — missing
      op_id is silent)
    * :meth:`update_phase` — atomically swap a record with a new
      phase name + timestamp
    * :meth:`lookup` — fetch a single record by op_id
    * :meth:`snapshot` — atomic immutable tuple of all current
      records (the reaper's read seam)
    * :meth:`reap_past_deadline` — pure-data filter over the
      snapshot; returns records that are past their explicit
      deadline. Does NOT mutate. The reaper decides whether to
      converge.
    * :meth:`size` — current count (cheap lock-held read)
    * :meth:`clear` — purge all entries (test surface; production
      uses :meth:`unregister` per-op)

    All public methods are NEVER-raise — internal failure logs
    at DEBUG and returns a safe value (None / empty tuple /
    False as appropriate).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[str, OpInFlight] = {}

    def register(
        self,
        op_id: str,
        *,
        ctx_ref: Any = None,
        deadline_monotonic: Optional[float] = None,
        last_phase_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[OpInFlight]:
        """Register a new in-flight op. Idempotent on ``op_id`` —
        re-registering an existing op overwrites the record
        (callers should prefer :meth:`update_phase` for
        progression). Returns the stored record, or ``None`` on
        invalid input."""
        if not isinstance(op_id, str) or not op_id:
            return None
        try:
            now = time.monotonic()
            meta_tuple = (
                tuple(
                    sorted(
                        (str(k), str(v))
                        for k, v in metadata.items()
                    )
                )
                if metadata else tuple()
            )
            record = OpInFlight(
                op_id=op_id,
                started_at_monotonic=now,
                deadline_monotonic=deadline_monotonic,
                ctx_ref=ctx_ref,
                last_phase_name=str(last_phase_name or ""),
                last_phase_at_monotonic=now,
                metadata=meta_tuple,
            )
            with self._lock:
                self._records[op_id] = record
            return record
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[in_flight_registry] register failed for %s: %r",
                op_id, err,
            )
            return None

    def unregister(self, op_id: str) -> bool:
        """Remove an op. Returns ``True`` if an entry existed,
        ``False`` if op_id was already absent. Idempotent —
        re-unregistering is a silent no-op + ``False``."""
        if not isinstance(op_id, str) or not op_id:
            return False
        with self._lock:
            return self._records.pop(op_id, None) is not None

    def update_phase(
        self,
        op_id: str,
        *,
        phase_name: str,
    ) -> Optional[OpInFlight]:
        """Atomically swap an existing record with a new phase
        name + ``last_phase_at_monotonic`` timestamp. Returns the
        new record, or ``None`` if the op wasn't registered.
        Other fields are preserved."""
        if not isinstance(op_id, str) or not op_id:
            return None
        try:
            now = time.monotonic()
            with self._lock:
                existing = self._records.get(op_id)
                if existing is None:
                    return None
                updated = OpInFlight(
                    op_id=existing.op_id,
                    started_at_monotonic=(
                        existing.started_at_monotonic
                    ),
                    deadline_monotonic=(
                        existing.deadline_monotonic
                    ),
                    ctx_ref=existing.ctx_ref,
                    last_phase_name=str(phase_name or ""),
                    last_phase_at_monotonic=now,
                    metadata=existing.metadata,
                )
                self._records[op_id] = updated
                return updated
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[in_flight_registry] update_phase failed for "
                "%s: %r", op_id, err,
            )
            return None

    def lookup(self, op_id: str) -> Optional[OpInFlight]:
        """Return the record for ``op_id`` or ``None`` if absent."""
        if not isinstance(op_id, str) or not op_id:
            return None
        with self._lock:
            return self._records.get(op_id)

    def snapshot(self) -> Tuple[OpInFlight, ...]:
        """Atomic immutable snapshot of all current records.

        The reaper's read seam — composes the canonical iteration
        pattern. Held briefly under the registry lock; iteration
        proceeds unlocked over the immutable tuple."""
        with self._lock:
            return tuple(self._records.values())

    def reap_past_deadline(
        self, *, now_monotonic: Optional[float] = None,
    ) -> Tuple[OpInFlight, ...]:
        """Return records whose explicit ``deadline_monotonic``
        is reached. Pure-data — does NOT mutate the registry.
        The reaper inspects this result, decides convergence,
        and calls :meth:`unregister` itself.

        Ops without an explicit deadline are excluded — the
        reaper applies its own configured ceiling to them in
        Slice 2 (a separate read-path)."""
        now = (
            now_monotonic if now_monotonic is not None
            else time.monotonic()
        )
        snap = self.snapshot()
        return tuple(
            r for r in snap if r.is_past_deadline(
                now_monotonic=now,
            )
        )

    def reap_older_than(
        self,
        ceiling_s: float,
        *,
        now_monotonic: Optional[float] = None,
    ) -> Tuple[OpInFlight, ...]:
        """Return records whose ``time_in_flight_s`` exceeds
        ``ceiling_s``, regardless of whether an explicit deadline
        was set. The reaper's fallback policy: any op older than
        the global ceiling is past-deadline-by-default. Pure-data,
        non-mutating, NEVER-raise."""
        if not isinstance(ceiling_s, (int, float)) or ceiling_s <= 0:
            return tuple()
        now = (
            now_monotonic if now_monotonic is not None
            else time.monotonic()
        )
        snap = self.snapshot()
        return tuple(
            r for r in snap
            if r.time_in_flight_s(now_monotonic=now) >= ceiling_s
        )

    def size(self) -> int:
        """Current count of in-flight records."""
        with self._lock:
            return len(self._records)

    def clear(self) -> int:
        """Purge all entries. Returns count purged. Test surface
        — production paths use per-op :meth:`unregister`."""
        with self._lock:
            n = len(self._records)
            self._records.clear()
            return n

    def op_ids(self) -> Tuple[str, ...]:
        """Snapshot of all registered op_ids — composes
        :meth:`snapshot` for callers that only need identity."""
        return tuple(r.op_id for r in self.snapshot())


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[InFlightRegistry] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_registry() -> InFlightRegistry:
    """Return the process-wide default :class:`InFlightRegistry`
    singleton. Tests prefer fresh instances via the constructor."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_REGISTRY is None:
                _DEFAULT_REGISTRY = InFlightRegistry()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Reset the singleton. Test-only surface — production never
    needs this. Composes :class:`InFlightRegistry.clear` then
    drops the singleton reference."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is not None:
            _DEFAULT_REGISTRY.clear()
        _DEFAULT_REGISTRY = None


# ---------------------------------------------------------------------------
# §33.3 register_shipped_invariants
# ---------------------------------------------------------------------------


_TARGET_FILE = (
    "backend/core/ouroboros/governance/in_flight_registry.py"
)


def register_shipped_invariants() -> list:
    """AST pins — auto-discovered by the §33.3 meta runner."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _EXPECTED_PHASES = {
        "route", "plan", "generate", "validate", "approve",
        "apply", "verify", "postmortem", "other",
    }

    def _validate_master_default_false(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.Call)
                        and len(sub.args) >= 2
                        and isinstance(sub.args[1], _ast.Constant)
                    ):
                        if sub.args[1].value != "false":
                            return (
                                "master_enabled() default arg "
                                f"drift: {sub.args[1].value!r}",
                            )
                        return ()
                return ("master_enabled() missing default-arg",)
        return ("master_enabled() not found",)

    def _validate_phase_taxonomy_closed(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """InFlightPhase 9-value taxonomy is bytes-pinned —
        adding/removing a phase requires updating IDE
        observability consumers + ``from_name`` semantics."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "InFlightPhase"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, _ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(
                            sub.targets[0], _ast.Name,
                        )
                        and isinstance(sub.value, _ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_PHASES - found
                extra = found - _EXPECTED_PHASES
                if missing:
                    return (
                        f"InFlightPhase missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"InFlightPhase drift (unexpected "
                        f"values): {sorted(extra)}",
                    )
                return ()
        return ("InFlightPhase class not found",)

    def _validate_authority_asymmetry(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Substrate purity — registry MUST NOT import the
        orchestrator, op_context, ide_observability_stream, or
        any policy module. Consumers compose us; we don't pull
        on them. Importing ``op_context`` here would create the
        observability → state-machine cycle we deliberately
        avoid with the ``Any`` ctx_ref type."""
        forbidden = {
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.op_context",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.policy_engine",
            "backend.core.ouroboros.governance.change_engine",
            (
                "backend.core.ouroboros.governance."
                "candidate_generator"
            ),
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.semantic_guardian",
            (
                "backend.core.ouroboros.governance."
                "ide_observability_stream"
            ),
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.auto_committer",
        }
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in forbidden:
                    violations.append(f"forbidden import: {mod}")
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append(
                            f"forbidden import: {alias.name}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "in_flight_registry_master_default_false"
            ),
            target_file=_TARGET_FILE,
            description=(
                "§33.1 substrate canonical shape — master flag "
                "default-FALSE. Drift would silently flip the "
                "registry to authoritative before the reaper "
                "has been wired."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "in_flight_registry_phase_taxonomy_closed"
            ),
            target_file=_TARGET_FILE,
            description=(
                "InFlightPhase 9-value taxonomy bytes-pinned. "
                "Adding / removing phases requires updating "
                "IDE observability consumers + ``from_name`` "
                "semantics (unknown values fold to OTHER)."
            ),
            validate=_validate_phase_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "in_flight_registry_authority_asymmetry"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Substrate purity — registry MUST NOT import "
                "orchestrator / op_context / "
                "ide_observability_stream / policy / providers "
                "/ change_engine / auto_committer. Consumers "
                "compose us; importing op_context creates the "
                "observability → state-machine cycle."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


# ---------------------------------------------------------------------------
# P2 Slice 3 — safe-wire helpers for GovernedLoopService lifecycle
# ---------------------------------------------------------------------------
#
# These wrappers are the *only* surface the live loop's hot path
# touches. They compose ``master_enabled`` + ``get_default_registry``
# behind a NEVER-raise envelope so the four ``_active_ops.add`` /
# ``_active_ops.discard`` sites can drop in single-line calls
# without branching on the master flag at each site.


def register_op_safely(
    op_id: str,
    *,
    ctx_ref: Any = None,
    deadline_monotonic: Optional[float] = None,
    last_phase_name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Lifecycle-site wrapper for :meth:`InFlightRegistry.register`.

    Master-FALSE → silent no-op returning ``False``. Master-ON →
    composes :func:`get_default_registry` and registers. NEVER
    raises. Returns ``True`` on successful registration.

    Designed for drop-in placement next to existing
    ``self._active_ops.add(...)`` sites in
    ``GovernedLoopService`` — the loop's hot path stays
    one-liner-readable.
    """
    if not master_enabled():
        return False
    try:
        result = get_default_registry().register(
            op_id,
            ctx_ref=ctx_ref,
            deadline_monotonic=deadline_monotonic,
            last_phase_name=last_phase_name,
            metadata=metadata,
        )
        return result is not None
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[in_flight_registry] register_op_safely "
            "swallowed for %s: %r", op_id, err,
        )
        return False


def unregister_op_safely(op_id: str) -> bool:
    """Lifecycle-site wrapper for
    :meth:`InFlightRegistry.unregister`.

    Master-FALSE → silent no-op returning ``False``. Master-ON →
    composes :func:`get_default_registry` and unregisters.
    NEVER raises. Returns ``True`` if an entry existed.

    Drop-in for the four ``_active_ops.discard`` sites in
    ``GovernedLoopService``.
    """
    if not master_enabled():
        return False
    try:
        return get_default_registry().unregister(op_id)
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[in_flight_registry] unregister_op_safely "
            "swallowed for %s: %r", op_id, err,
        )
        return False


def update_phase_safely(op_id: str, *, phase_name: str) -> bool:
    """Lifecycle-site wrapper for
    :meth:`InFlightRegistry.update_phase`.

    Master-FALSE → silent no-op. Master-ON → composes default
    registry. NEVER raises. Returns ``True`` if the op was
    registered (and the phase swapped)."""
    if not master_enabled():
        return False
    try:
        return get_default_registry().update_phase(
            op_id, phase_name=phase_name,
        ) is not None
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[in_flight_registry] update_phase_safely "
            "swallowed for %s: %r", op_id, err,
        )
        return False


__all__ = [
    "IN_FLIGHT_REGISTRY_SCHEMA_VERSION",
    "InFlightPhase",
    "InFlightRegistry",
    "OpInFlight",
    "get_default_registry",
    "master_enabled",
    "register_op_safely",
    "register_shipped_invariants",
    "reset_default_registry",
    "unregister_op_safely",
    "update_phase_safely",
]
