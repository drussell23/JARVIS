"""Convergence Reaper — universal terminal-state guarantee.

**Why this exists** — the audit traced the P2 failure mode to a
structural gap: ops can hang past 1800s without ever reaching one
of the four ``TERMINAL_OPERATION_STATES`` (``applied``,
``rolled_back``, ``failed``, ``blocked``). Each prior fix was
local to a specific failure class — Fix A (autoscore visibility),
Fix B (Fail-Fast circuit breaker on exhaustion, commit
``b07bb03965``), Fix C (GENERATE_RETRY registry gap). The Fail-
Fast CB is the first *convergence primitive* — it forces an op to
``FAILED`` after N consecutive exhaustions. This reaper
*generalizes* that pattern: every op carries a deadline (explicit
or via the global ceiling), and a single reaper guarantees every
op reaches a terminal state + emits an ``operation_terminal`` SSE
event within bounded time, regardless of the failure mode that
caused the hang.

**Design** — pure composition of three existing primitives:

  1. :class:`InFlightRegistry` (Slice 1) — the typed view of
     "what is running right now". The reaper iterates its
     :meth:`snapshot` on each tick and calls
     :meth:`reap_past_deadline` (for ops with explicit deadlines)
     or :meth:`reap_older_than` (global ceiling fallback) to
     locate stuck ops.

  2. :func:`publish_operation_terminal` from
     :mod:`ide_observability_stream` — the canonical seam that
     fires the ``operation_terminal`` SSE event. The reaper
     composes this without mutating the orchestrator's
     ``OperationContext`` by wrapping it in a thin
     :class:`_ForcedTerminalCtxView` adapter that overrides only
     ``terminal_reason_code`` (and optionally ``phase``) via
     attribute access — Python's ``getattr`` semantics inside
     ``publish_operation_terminal`` mean the adapter is
     observationally identical to the real ctx for every other
     field.

  3. :class:`OperationState.FAILED` from :mod:`ledger` — the
     canonical terminal value the reaper forces. Composing the
     existing enum keeps the four-terminal-states taxonomy
     bytes-identical with the rest of the governance.

**What the reaper does NOT do**:

  * It does **not** rewind the orchestrator's state machine. If
    a hung op later genuinely terminates, both events land in
    the SSE stream — observers see the first (forced) signal and
    proceed; the second is a no-op against the dedup downstream.
    The orchestrator coroutine that's truly stuck remains stuck;
    the reaper's job is to signal observability, not to free
    leaked resources.

  * It does **not** scan ``GovernedLoopService._active_ops`` —
    that bare ``Set[str]`` has no metadata. The reaper composes
    the typed registry; Slice 3 wiring will hook the
    ``_active_ops.add()`` / ``.discard()`` lifecycle sites into
    :meth:`InFlightRegistry.register` / :meth:`unregister`.

  * It does **not** implement time-travel debugging — convergence
    guarantee first; replayable history is operator-deferred.

**Master flag** — ``JARVIS_CONVERGENCE_REAPER_ENABLED`` (default
**FALSE**, §33.1). When off, the reaper task body short-circuits
on every tick — operator graduates the master deliberately after
soaking the substrate.

**Configuration knobs (all env-driven, no hardcoded literals at
use sites)**:

  * ``JARVIS_CONVERGENCE_REAPER_TICK_S`` — seconds between
    reaper ticks. Default 30. Lower = faster convergence
    detection, higher CPU/lock-acquisition rate.
  * ``JARVIS_CONVERGENCE_REAPER_DEFAULT_CEILING_S`` — global
    ceiling for ops without explicit deadlines. Default 1800
    (matches the audit's observed worst-case hang).

The reaper is async — composes :func:`asyncio.create_task` for
its background loop and ``asyncio.sleep`` for inter-tick cadence.
Bounded by deadline, never tight-loops.
"""
from __future__ import annotations

import ast as _ast
import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


CONVERGENCE_REAPER_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Forced terminal reason taxonomy (closed)
# ---------------------------------------------------------------------------


class ForcedTerminalReason(str, enum.Enum):
    """Closed taxonomy of reaper-emitted terminal reason codes.

    Each value lands in the ``terminal_reason_code`` payload
    field of the ``operation_terminal`` SSE event. Observers
    (IDE webview, REPL, soak harness) can switch on this enum
    to render forced terminations distinctly from natural ones.

    AST-pinned single producer (this module) — no other module
    is allowed to emit these reason codes.
    """

    DEADLINE_EXCEEDED = "deadline_exceeded"
    """Op had an explicit deadline that has now passed without
    a natural terminal state being reached. Most common reason."""

    CEILING_EXCEEDED = "ceiling_exceeded"
    """Op had no explicit deadline; the global
    ``JARVIS_CONVERGENCE_REAPER_DEFAULT_CEILING_S`` ceiling has
    been crossed since registration. Catch-all for fire-and-
    forget ops (the Fix A class)."""

    REGISTRY_PURGED = "registry_purged"
    """Op was force-converged by an explicit external call
    (e.g. shutdown sweep). Reserved for Slice 3 wiring — Slice
    2 only emits the two above."""


# ---------------------------------------------------------------------------
# Master flag (§33.1)
# ---------------------------------------------------------------------------


_MASTER_FLAG = "JARVIS_CONVERGENCE_REAPER_ENABLED"


def reaper_enabled() -> bool:
    """Return ``True`` iff the reaper is master-ON. Default
    ``False`` per §33.1 — the substrate composes default-off and
    the operator graduates after soaking Slice 1 + Slice 2."""
    return os.environ.get(_MASTER_FLAG, "false").lower() == "true"


# ---------------------------------------------------------------------------
# Env-driven config (no hardcoded literals at use sites)
# ---------------------------------------------------------------------------


_TICK_S_ENV = "JARVIS_CONVERGENCE_REAPER_TICK_S"
_CEILING_S_ENV = "JARVIS_CONVERGENCE_REAPER_DEFAULT_CEILING_S"
_DEFAULT_TICK_S = 30.0
_DEFAULT_CEILING_S = 1800.0


def tick_interval_s() -> float:
    """Reaper tick cadence. Clamped to ``[1, 600]`` — values
    outside that range fall back to the default."""
    raw = os.environ.get(_TICK_S_ENV)
    if raw is None:
        return _DEFAULT_TICK_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TICK_S
    if value < 1.0 or value > 600.0:
        return _DEFAULT_TICK_S
    return value


def default_ceiling_s() -> float:
    """Global ceiling for ops without explicit deadlines.
    Clamped to ``[60, 86400]`` (1 minute to 1 day)."""
    raw = os.environ.get(_CEILING_S_ENV)
    if raw is None:
        return _DEFAULT_CEILING_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_CEILING_S
    if value < 60.0 or value > 86400.0:
        return _DEFAULT_CEILING_S
    return value


# ---------------------------------------------------------------------------
# Forced-terminal ctx view
# ---------------------------------------------------------------------------


class _ForcedTerminalCtxView:
    """Read-only adapter around an ``OperationContext`` that
    overrides only the fields ``publish_operation_terminal``
    reads to differentiate forced terminations from natural
    ones — specifically ``terminal_reason_code`` and (optionally)
    ``phase``.

    Why an adapter, not a mutation:
        :class:`OperationContext` is the orchestrator's source-
        of-truth state. If the reaper mutated it, an op that
        later genuinely terminates would see the reason_code
        overwritten, breaking downstream telemetry. The adapter
        is a one-shot view for the SSE publish call.

    Why ``__getattr__`` not ``__getattribute__``:
        ``__getattr__`` only fires for attributes Python can't
        find on the instance. Setting
        ``self.terminal_reason_code`` on the adapter shadows the
        real ctx for that one field; everything else
        (``op_id``, ``phase_entered_at``, etc.) falls through to
        the wrapped ctx via ``__getattr__``. Idempotent and
        cheap — no copy.
    """

    __slots__ = ("_real", "terminal_reason_code", "phase")

    def __init__(
        self,
        real_ctx: Any,
        *,
        reason_code: str,
        phase_override: Any = None,
    ) -> None:
        # Use object.__setattr__ to satisfy __slots__ semantics.
        object.__setattr__(self, "_real", real_ctx)
        object.__setattr__(
            self, "terminal_reason_code", reason_code,
        )
        object.__setattr__(
            self, "phase",
            phase_override
            if phase_override is not None
            else getattr(real_ctx, "phase", None),
        )

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only called for attributes NOT found
        # via normal lookup — i.e. NOT on __slots__. Delegates
        # to the wrapped ctx (op_id, phase_entered_at, etc.).
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReaperTickResult:
    """Outcome of one reaper tick — composes telemetry for
    observers (IDE GET endpoints, REPL ``/reaper`` verb, the
    soak harness's idle-watchdog).

    Frozen so consumers can't mutate a result; carries the full
    audit trail of one sweep.
    """

    inspected_count: int
    converged_count: int
    converged_op_ids: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    reasons: Tuple[ForcedTerminalReason, ...] = field(
        default_factory=tuple,
    )
    skipped_master_off: bool = False
    elapsed_s: float = 0.0
    schema_version: str = CONVERGENCE_REAPER_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Convergence reaper
# ---------------------------------------------------------------------------


class ConvergenceReaper:
    """Async background task that walks the in-flight registry
    on a configurable cadence and force-converges ops past their
    deadline or the global ceiling.

    Master-flag-gated and NEVER-raises. Composes the canonical
    :func:`publish_operation_terminal` publisher seam.

    Lifecycle:

      * :meth:`start` — schedules the background task on the
        current event loop. Idempotent — re-calling while already
        running is a silent no-op.

      * :meth:`stop` — cancels the background task. Awaitable;
        completes when the task has finished its current tick.

      * :meth:`tick_once` — single sweep, callable synchronously
        for tests or for forced sweeps at shutdown. Returns a
        :class:`ReaperTickResult` for telemetry.

    All public methods are NEVER-raise — substrate failures log
    at DEBUG and surface a safe :class:`ReaperTickResult`.
    """

    def __init__(
        self,
        *,
        registry: Optional[Any] = None,
        publish_fn: Optional[Any] = None,
        operation_state_failed: Optional[Any] = None,
    ) -> None:
        # All composition is dependency-injected (test seams)
        # and falls back to the canonical singletons at first
        # use. Lazy resolution avoids import cycles at module
        # load time.
        self._registry_override = registry
        self._publish_fn_override = publish_fn
        self._failed_state_override = operation_state_failed
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # ------------------------------------------------------------------
    # Canonical-substrate lazy resolution
    # ------------------------------------------------------------------

    def _resolve_registry(self) -> Any:
        if self._registry_override is not None:
            return self._registry_override
        from backend.core.ouroboros.governance.in_flight_registry import (  # noqa: E501
            get_default_registry,
        )
        return get_default_registry()

    def _resolve_publish_fn(self) -> Any:
        if self._publish_fn_override is not None:
            return self._publish_fn_override
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_operation_terminal,
        )
        return publish_operation_terminal

    def _resolve_failed_state(self) -> Any:
        if self._failed_state_override is not None:
            return self._failed_state_override
        from backend.core.ouroboros.governance.ledger import (
            OperationState,
        )
        return OperationState.FAILED

    # ------------------------------------------------------------------
    # Public surface — start / stop / tick
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Schedule the reaper's background loop. Idempotent."""
        if self.is_running():
            return
        if not reaper_enabled():
            logger.debug(
                "[convergence_reaper] master-OFF, "
                "skipping start",
            )
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError as err:
            logger.debug(
                "[convergence_reaper] no event loop: %r", err,
            )
            return
        self._stopping = False
        self._task = loop.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the background task and await its finalization.
        Idempotent — calling stop on a stopped reaper is a
        silent no-op."""
        self._stopping = True
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[convergence_reaper] stop swallowed: %r",
                err,
            )

    def tick_once(
        self,
        *,
        now_monotonic: Optional[float] = None,
    ) -> ReaperTickResult:
        """Run a single reaper sweep synchronously. The async
        loop body composes this — exposing it as a public method
        lets tests and shutdown-sweep callers run the same
        convergence path without scheduling the background task.

        NEVER raises. Returns a :class:`ReaperTickResult` even
        on substrate failure (with ``inspected_count=0``).
        """
        if not reaper_enabled():
            return ReaperTickResult(
                inspected_count=0,
                converged_count=0,
                skipped_master_off=True,
            )
        start = time.monotonic()
        try:
            return self._tick_inner(now_monotonic=now_monotonic)
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[convergence_reaper] tick_once swallowed: %r",
                err, exc_info=True,
            )
            return ReaperTickResult(
                inspected_count=0,
                converged_count=0,
                elapsed_s=max(0.0, time.monotonic() - start),
            )

    # ------------------------------------------------------------------
    # Private — the reaper's actual work
    # ------------------------------------------------------------------

    def _tick_inner(
        self,
        *,
        now_monotonic: Optional[float] = None,
    ) -> ReaperTickResult:
        start = time.monotonic()
        now = (
            now_monotonic if now_monotonic is not None
            else start
        )
        registry = self._resolve_registry()
        publish_fn = self._resolve_publish_fn()
        failed_state = self._resolve_failed_state()

        snap = registry.snapshot()
        inspected = len(snap)
        ceiling = default_ceiling_s()

        converged_ids = []
        reasons = []

        # Single-pass classification — assign a reason per
        # past-deadline op. Order: explicit deadline → ceiling
        # fallback. Each op is classified at most once per tick.
        for rec in snap:
            reason = self._classify_or_none(
                rec, now=now, ceiling=ceiling,
            )
            if reason is None:
                continue
            # Force-converge.
            if self._force_converge(
                rec=rec,
                reason=reason,
                publish_fn=publish_fn,
                failed_state=failed_state,
            ):
                converged_ids.append(rec.op_id)
                reasons.append(reason)
                # Unregister so we don't re-converge on the next
                # tick.
                registry.unregister(rec.op_id)

        return ReaperTickResult(
            inspected_count=inspected,
            converged_count=len(converged_ids),
            converged_op_ids=tuple(converged_ids),
            reasons=tuple(reasons),
            elapsed_s=max(0.0, time.monotonic() - start),
        )

    def _classify_or_none(
        self,
        rec: Any,
        *,
        now: float,
        ceiling: float,
    ) -> Optional[ForcedTerminalReason]:
        """Return the closed reason for forcing this record, or
        None if it's still within bounds. Composes
        :meth:`OpInFlight.is_past_deadline` (explicit deadline)
        + :meth:`OpInFlight.time_in_flight_s` (ceiling
        fallback). Pure-data — no side effects."""
        try:
            if rec.is_past_deadline(now_monotonic=now):
                return ForcedTerminalReason.DEADLINE_EXCEEDED
            elapsed = rec.time_in_flight_s(now_monotonic=now)
            if elapsed >= ceiling:
                return ForcedTerminalReason.CEILING_EXCEEDED
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[convergence_reaper] classify failed for "
                "%r: %r",
                getattr(rec, "op_id", "?"), err,
            )
        return None

    def _force_converge(
        self,
        *,
        rec: Any,
        reason: ForcedTerminalReason,
        publish_fn: Any,
        failed_state: Any,
    ) -> bool:
        """Compose the canonical
        :func:`publish_operation_terminal` via a
        :class:`_ForcedTerminalCtxView` adapter so the
        orchestrator's real ctx is untouched. Returns True
        on successful publish, False on any failure.

        NEVER raises — wrapping every step defensively because
        observability emission must not destabilize the reaper
        loop. A swallowed exception leaves the op in the
        registry, which means the next tick will re-attempt —
        acceptable degraded behavior.
        """
        try:
            ctx = getattr(rec, "ctx_ref", None)
            if ctx is None:
                # No ctx — can't drive the publisher (it reads
                # op_id, phase, etc. from the ctx). Build a
                # minimal-shim ctx so we can still emit the
                # SSE for visibility-only ops (e.g. Fix-A class:
                # fire-and-forget autoscore).
                ctx = _MinimalCtxShim(op_id=rec.op_id)
            view = _ForcedTerminalCtxView(
                ctx, reason_code=reason.value,
            )
            event_id = publish_fn(view, failed_state)
            logger.info(
                "[convergence_reaper] force-converged op=%s "
                "reason=%s event_id=%s",
                rec.op_id, reason.value, event_id,
            )
            return True
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[convergence_reaper] _force_converge "
                "swallowed for op=%s: %r",
                getattr(rec, "op_id", "?"), err,
            )
            return False

    async def _run_loop(self) -> None:
        """Background task body. Bounded cadence via
        :func:`asyncio.sleep`. NEVER raises out — every tick is
        wrapped, every exception swallowed."""
        try:
            while not self._stopping:
                if not reaper_enabled():
                    # Operator disabled mid-loop; back off then
                    # re-check.
                    await asyncio.sleep(tick_interval_s())
                    continue
                try:
                    self._tick_inner()
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        "[convergence_reaper] tick loop "
                        "swallowed: %r", err,
                    )
                await asyncio.sleep(tick_interval_s())
        except asyncio.CancelledError:
            # Expected shutdown path.
            return
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "[convergence_reaper] loop exited unexpectedly: "
                "%r", err,
            )


# ---------------------------------------------------------------------------
# Minimal ctx shim (for fire-and-forget ops with no real ctx)
# ---------------------------------------------------------------------------


class _MinimalCtxShim:
    """Bare-minimum duck-typed ctx for
    :func:`publish_operation_terminal`. Used when the registry
    entry was registered without a ``ctx_ref`` (fire-and-forget
    fire path) — we still want the SSE to fire so observers see
    the convergence, even if some payload fields are blank.
    """

    __slots__ = ("op_id", "phase", "phase_entered_at",
                 "terminal_reason_code")

    def __init__(self, op_id: str) -> None:
        self.op_id = op_id
        self.phase = None
        self.phase_entered_at = None
        self.terminal_reason_code = ""


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_DEFAULT_REAPER: Optional[ConvergenceReaper] = None


def get_default_reaper() -> ConvergenceReaper:
    global _DEFAULT_REAPER
    if _DEFAULT_REAPER is None:
        _DEFAULT_REAPER = ConvergenceReaper()
    return _DEFAULT_REAPER


def reset_default_reaper() -> None:
    """Test-only — drops the singleton."""
    global _DEFAULT_REAPER
    _DEFAULT_REAPER = None


# ---------------------------------------------------------------------------
# §33.3 register_shipped_invariants
# ---------------------------------------------------------------------------


_TARGET_FILE = (
    "backend/core/ouroboros/governance/convergence_reaper.py"
)


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _EXPECTED_REASONS = {
        "deadline_exceeded",
        "ceiling_exceeded",
        "registry_purged",
    }

    def _validate_master_default_false(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "reaper_enabled"
            ):
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.Call)
                        and len(sub.args) >= 2
                        and isinstance(sub.args[1], _ast.Constant)
                    ):
                        if sub.args[1].value != "false":
                            return (
                                "reaper_enabled() default arg "
                                f"drift: {sub.args[1].value!r}",
                            )
                        return ()
                return ("reaper_enabled() missing default-arg",)
        return ("reaper_enabled() not found",)

    def _validate_reason_taxonomy_closed(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "ForcedTerminalReason"
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
                missing = _EXPECTED_REASONS - found
                extra = found - _EXPECTED_REASONS
                if missing:
                    return (
                        f"ForcedTerminalReason missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ForcedTerminalReason drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("ForcedTerminalReason class not found",)

    def _validate_single_convergence_seam(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """The reaper is the SINGLE convergence seam — there is
        exactly one method that composes
        ``publish_operation_terminal`` with a
        :class:`ForcedTerminalReason` value (``_force_converge``).
        Adding a second such call site would create a parallel
        convergence path the AST pin must catch.

        AST-walks all functions and counts those that contain
        BOTH a call to the publisher (by name) AND a reference
        to ``ForcedTerminalReason``. Expected: exactly one
        (``_force_converge``)."""
        producers = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.FunctionDef):
                continue
            has_publish = False
            has_reason = False
            for sub in _ast.walk(node):
                if (
                    isinstance(sub, _ast.Call)
                    and isinstance(sub.func, _ast.Name)
                    and sub.func.id == "publish_fn"
                ):
                    has_publish = True
                if (
                    isinstance(sub, _ast.Name)
                    and sub.id == "ForcedTerminalReason"
                ):
                    has_reason = True
                if (
                    isinstance(sub, _ast.Attribute)
                    and isinstance(sub.value, _ast.Name)
                    and sub.value.id == "ForcedTerminalReason"
                ):
                    has_reason = True
            # Strict producer check — requires BOTH a publish_fn
            # call AND a ForcedTerminalReason reference within
            # the same function body. Just calling the publisher
            # is fine (resolver returns it); composing it WITH a
            # forced reason is the convergence-emission pattern.
            if has_publish and has_reason:
                producers.append(node.name)
        if producers != ["_force_converge"]:
            return (
                "convergence seam drift: expected exactly one "
                "function composing publish_fn with a "
                f"ForcedTerminalReason value — got {producers}",
            )
        return ()

    def _validate_composes_canonical_publisher(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """The reaper MUST resolve
        ``publish_operation_terminal`` from
        ``ide_observability_stream`` via the lazy import in
        :meth:`_resolve_publish_fn` — no parallel implementation
        of the SSE publish."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "_resolve_publish_fn"
            ):
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.ImportFrom)
                        and sub.module
                        == (
                            "backend.core.ouroboros."
                            "governance."
                            "ide_observability_stream"
                        )
                    ):
                        names = {a.name for a in sub.names}
                        if (
                            "publish_operation_terminal" in names
                        ):
                            return ()
                return (
                    "_resolve_publish_fn must compose canonical "
                    "publish_operation_terminal from "
                    "ide_observability_stream",
                )
        return ("_resolve_publish_fn not found",)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "convergence_reaper_master_default_false"
            ),
            target_file=_TARGET_FILE,
            description=(
                "§33.1 substrate canonical shape — master flag "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "convergence_reaper_reason_taxonomy_closed"
            ),
            target_file=_TARGET_FILE,
            description=(
                "ForcedTerminalReason 3-value taxonomy bytes-"
                "pinned. Adding / removing a reason requires "
                "updating downstream observers + the AST single-"
                "seam pin."
            ),
            validate=_validate_reason_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "convergence_reaper_single_convergence_seam"
            ),
            target_file=_TARGET_FILE,
            description=(
                "The reaper is the SINGLE module/function that "
                "composes publish_operation_terminal with a "
                "ForcedTerminalReason value. Adding a second "
                "such call site would create a parallel "
                "convergence path — drift caught here."
            ),
            validate=_validate_single_convergence_seam,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "convergence_reaper_composes_canonical_publisher"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Operator binding 'leverage existing files' — "
                "the reaper MUST compose "
                "publish_operation_terminal from "
                "ide_observability_stream via the lazy import "
                "in _resolve_publish_fn. No parallel "
                "publisher implementation."
            ),
            validate=_validate_composes_canonical_publisher,
        ),
    ]


# ---------------------------------------------------------------------------
# P2 Slice 3 — safe-wire helpers for GovernedLoopService lifecycle
# ---------------------------------------------------------------------------


def safe_start_default_reaper() -> bool:
    """Boot the default :class:`ConvergenceReaper` background
    task. Master-FALSE → silent no-op returning ``False``.
    Master-ON → composes :func:`get_default_reaper` + calls
    ``.start()``. NEVER raises. Returns ``True`` on successful
    start (or already-running).

    Drop-in for ``GovernedLoopService.start()`` boot path —
    keeps the loop's lifecycle one-liner-readable.
    """
    if not reaper_enabled():
        return False
    try:
        reaper = get_default_reaper()
        reaper.start()
        return reaper.is_running()
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[convergence_reaper] safe_start swallowed: %r",
            err,
        )
        return False


async def safe_stop_default_reaper() -> bool:
    """Cancel the default reaper's background task and await
    finalization. Master-FALSE → no-op. Master-ON → composes
    :meth:`ConvergenceReaper.stop`. NEVER raises. Returns
    ``True`` if a stop was attempted on a running reaper.

    Drop-in for ``GovernedLoopService.stop()`` shutdown path."""
    if not reaper_enabled():
        # Even when master off, attempt to stop any reaper
        # singleton that was started earlier — defensive
        # cleanup so a master-flag toggle mid-soak doesn't
        # leak a task.
        pass
    try:
        # Resolve from global singleton; do NOT instantiate a
        # new one — if no reaper was ever booted, this is a
        # no-op against an idle singleton.
        global _DEFAULT_REAPER
        if _DEFAULT_REAPER is None:
            return False
        if not _DEFAULT_REAPER.is_running():
            return False
        await _DEFAULT_REAPER.stop()
        return True
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[convergence_reaper] safe_stop swallowed: %r",
            err,
        )
        return False


__all__ = [
    "CONVERGENCE_REAPER_SCHEMA_VERSION",
    "ConvergenceReaper",
    "ForcedTerminalReason",
    "ReaperTickResult",
    "default_ceiling_s",
    "get_default_reaper",
    "reaper_enabled",
    "register_shipped_invariants",
    "reset_default_reaper",
    "safe_start_default_reaper",
    "safe_stop_default_reaper",
    "tick_interval_s",
]
