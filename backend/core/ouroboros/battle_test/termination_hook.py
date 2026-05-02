"""TerminationHookRegistry — Slice 1 pure-stdlib primitive.

Architectural fix for the asymmetric-insurance bug between the
signal-driven shutdown path and the wall-clock-driven shutdown
path. Mirrors the established :mod:`lifecycle_hook` substrate
shape (closed-taxonomy enums + frozen dataclasses + total
decision function + module-owned `register_*` discovery
contract).

Background:
  The signal-handler path
  (``harness._handle_shutdown_signal``) runs a synchronous
  partial-summary writer BEFORE setting any shutdown event, so
  the session dir is guaranteed to carry a parseable
  ``summary.json`` even when the asyncio cleanup wedges and the
  ``BoundedShutdownWatchdog`` fires ``os._exit(75)``.

  The wall-clock path (``harness._wall_clock_watchdog``) does
  NOT run the same pre-event sync writer. It arms the
  ``BoundedShutdownWatchdog`` and sets the wall-clock event,
  then returns. If the async cleanup doesn't finish in 30s,
  ``os._exit`` fires WITHOUT going through ``atexit`` — by
  Python contract, ``os._exit`` bypasses atexit entirely.

  Result: empirically reproduced on session
  ``bt-2026-05-02-203805`` — clean wall-cap termination but
  NO summary.json on disk.

This module ships the substrate Slice 2 wraps with the
registry + auto-discovery, Slice 3 wires into the harness
paths uniformly (replacing the ad-hoc
``_atexit_fallback_write`` direct calls), and Slice 4
graduates with AST pins + FlagRegistry seeds + SSE event +
GET route.

## Strict design constraints (per operator directives)

* **Sync-first, no asyncio entanglement.** Hooks for the
  ``PRE_SHUTDOWN_EVENT_SET`` phase MUST run synchronously and
  MUST execute even when the asyncio loop is wedged. We use
  ``threading.Thread`` for per-hook isolation — Python's GIL
  makes daemon-thread join-with-timeout safe and portable.

* **Deterministic budgets.** Per-hook timeout AND a hard
  per-phase wall-clock cap. The dispatcher refuses to start
  a new hook once the phase budget is exhausted. A single
  misbehaving hook cannot consume the entire grace window
  the ``BoundedShutdownWatchdog`` allots before
  ``os._exit(75)``.

* **NEVER raises into callers.** Every failure mode collapses
  to a closed-enum outcome with a sanitized detail string.
  This module is invoked from emergency-shutdown contexts
  where any unhandled exception risks exiting the process
  WITHOUT writing the partial summary the caller is asking
  us to write.

* **Pure-stdlib.** No ``backend.*`` dependencies. Slice 1 ships
  the substrate; Slice 2's registry imports this module.

## Authority invariant (AST-pinned in Slice 4)

This module imports nothing from:
  ``asyncio`` (sync-only contract — see directives)
  ``yaml_writer`` / ``orchestrator`` / ``iron_gate`` /
  ``risk_tier`` / ``change_engine`` / ``candidate_generator`` /
  ``gate`` / ``policy``

The dispatcher is read-only over its inputs. Hooks themselves
may write (that's the entire point — e.g. partial-summary
write); the dispatcher just gives them a bounded execution
slot.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, Optional,
    Sequence, Tuple,
)

logger = logging.getLogger(__name__)


TERMINATION_HOOK_SCHEMA_VERSION: str = "termination_hook.v1"


# ---------------------------------------------------------------------------
# Closed 8-value taxonomy of TerminationCause (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class TerminationCause(str, enum.Enum):
    """Why the session is terminating. Every dispatch of the
    termination-hook registry MUST stamp exactly one cause —
    never None, never implicit fall-through. Slice 4 AST-pins the
    literal vocabulary so silent additions break graduation.

    ``WALL_CLOCK_CAP``     — the ``--max-wall-seconds`` watchdog
                              fired (the bug this whole arc
                              fixes — wall-cap path historically
                              didn't go through any sync-write
                              insurance).
    ``SIGTERM`` / ``SIGINT`` / ``SIGHUP`` — the corresponding
                              POSIX signal arrived. Maps 1:1 to
                              the existing
                              ``_handle_shutdown_signal``
                              ``signal_name`` argument vocabulary
                              for migration parity in Slice 3.
    ``IDLE_TIMEOUT``       — ``--idle-timeout`` elapsed without
                              op activity.
    ``BUDGET_EXCEEDED``    — ``--cost-cap`` consumed.
    ``NORMAL_EXIT``        — clean async ``_generate_report``
                              completed; included so non-emergency
                              shutdowns ALSO fan through the
                              registry uniformly. Slice 3 routes
                              the clean path through the same
                              dispatcher.
    ``UNKNOWN``            — defensive sentinel for callers that
                              can't classify (e.g. unhandled
                              exception during boot before any
                              cause is identifiable).
    """

    WALL_CLOCK_CAP = "wall_clock_cap"
    SIGTERM = "sigterm"
    SIGINT = "sigint"
    SIGHUP = "sighup"
    IDLE_TIMEOUT = "idle_timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    NORMAL_EXIT = "normal_exit"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Closed 3-value taxonomy of TerminationPhase
# ---------------------------------------------------------------------------


class TerminationPhase(str, enum.Enum):
    """When in the shutdown sequence the hook fires. Each phase
    has its own execution discipline + budget envelope.

    ``PRE_SHUTDOWN_EVENT_SET`` — STRICT SYNCHRONOUS phase. Runs
        BEFORE the asyncio shutdown event is set + BEFORE the
        ``BoundedShutdownWatchdog`` is armed. Hooks here MUST
        survive a wedged asyncio loop — they execute on
        threading.Thread workers + are isolated from the event
        loop entirely. This is the phase the partial-summary
        writer registers at.

    ``POST_ASYNC_CLEANUP``     — runs AFTER the async
        ``_generate_report`` completes (clean path). Hooks here
        can rely on the asyncio loop being healthy. Use case:
        flushing SSE final events, closing IDE-stream subscribers
        gracefully.

    ``PRE_HARD_EXIT``          — last-chance synchronous phase
        invoked by the ``BoundedShutdownWatchdog`` itself
        IMMEDIATELY before ``os._exit``. Tighter budget than
        PRE_SHUTDOWN_EVENT_SET (default 2s vs 10s) because the
        watchdog deadline is measured in seconds. Use case:
        forensic stderr emission, last-mile state checkpoint.
    """

    PRE_SHUTDOWN_EVENT_SET = "pre_shutdown_event_set"
    POST_ASYNC_CLEANUP = "post_async_cleanup"
    PRE_HARD_EXIT = "pre_hard_exit"


# ---------------------------------------------------------------------------
# Closed 4-value taxonomy of HookOutcome
# ---------------------------------------------------------------------------


class HookOutcome(str, enum.Enum):
    """Per-hook execution outcome. Closed taxonomy.

    ``OK``         — hook returned without raising within budget.
    ``FAILED``     — hook raised. Exception type + truncated
                     message captured in the
                     :class:`HookExecutionRecord` ``detail``.
    ``TIMED_OUT``  — hook didn't return within its per-hook
                     timeout (and the worker thread is still
                     running, abandoned as a daemon — by design,
                     since the process is shutting down anyway).
    ``SKIPPED``    — hook never started because the phase budget
                     was already exhausted by earlier hooks. The
                     dispatcher refuses to start hooks that
                     would push past the phase wall-clock cap.
    """

    OK = "ok"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminationHookContext:
    """Read-only snapshot the dispatcher passes to every hook.

    Frozen so a misbehaving hook can't mutate state visible to
    the next hook. ``session_dir`` is a string (not ``Path``)
    so this module stays free of path-shape coupling — hooks
    construct their own ``Path`` objects from the string at
    use time.
    """

    cause: TerminationCause
    phase: TerminationPhase
    session_dir: str
    started_at: float
    stop_reason: str = ""
    schema_version: str = TERMINATION_HOOK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cause": self.cause.value,
            "phase": self.phase.value,
            "session_dir": self.session_dir,
            "started_at": self.started_at,
            "stop_reason": self.stop_reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class HookExecutionRecord:
    """One hook's execution outcome. Frozen for safe propagation
    across the registry's audit trail."""

    hook_name: str
    outcome: HookOutcome
    duration_ms: float
    detail: str = ""
    schema_version: str = TERMINATION_HOOK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hook_name": self.hook_name,
            "outcome": self.outcome.value,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["HookExecutionRecord"]:
        try:
            if not isinstance(raw, Mapping):
                return None
            if (
                raw.get("schema_version")
                != TERMINATION_HOOK_SCHEMA_VERSION
            ):
                return None
            return cls(
                hook_name=str(raw.get("hook_name", "")),
                outcome=HookOutcome(str(raw["outcome"])),
                duration_ms=float(raw.get("duration_ms", 0.0)),
                detail=str(raw.get("detail", ""))[:200],
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class TerminationDispatchResult:
    """Aggregate dispatch outcome for one phase. Frozen."""

    phase: TerminationPhase
    cause: TerminationCause
    records: Tuple[HookExecutionRecord, ...]
    total_duration_ms: float
    budget_exhausted: bool
    schema_version: str = TERMINATION_HOOK_SCHEMA_VERSION

    def all_ok(self) -> bool:
        """True iff every executed hook returned OK. SKIPPED hooks
        DO count as a failure of the phase as a whole — if hooks
        were skipped, downstream callers should know the phase
        didn't fully execute. The :attr:`budget_exhausted` field
        is the canonical signal for that case; ``all_ok`` is the
        per-hook conjunction."""
        return all(
            r.outcome is HookOutcome.OK for r in self.records
        )

    def hooks_by_outcome(self) -> Dict[HookOutcome, int]:
        out: Dict[HookOutcome, int] = {
            o: 0 for o in HookOutcome
        }
        for r in self.records:
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase.value,
            "cause": self.cause.value,
            "records": [r.to_dict() for r in self.records],
            "total_duration_ms": self.total_duration_ms,
            "budget_exhausted": self.budget_exhausted,
            "outcome_histogram": {
                k.value: v
                for k, v in self.hooks_by_outcome().items()
            },
            "schema_version": self.schema_version,
        }


# Hook is a callable that takes a TerminationHookContext and returns
# nothing. Errors propagate as exceptions — the dispatcher catches.
TerminationHook = Callable[[TerminationHookContext], None]


# ---------------------------------------------------------------------------
# Internal: bounded per-hook executor (threading-only, no asyncio)
# ---------------------------------------------------------------------------


def _execute_one_hook(
    *,
    hook_name: str,
    hook: TerminationHook,
    context: TerminationHookContext,
    timeout_s: float,
    clock: Callable[[], float],
) -> HookExecutionRecord:
    """Run one hook on a daemon worker thread with bounded join.

    NEVER raises. Every failure mode collapses to a HookOutcome.

    The worker thread is spawned as a daemon. If the join times
    out, the thread keeps running but the dispatcher returns —
    by the time the next caller (the BoundedShutdownWatchdog)
    fires ``os._exit``, the orphaned thread is irrelevant. This
    is the documented contract: bounded execution, NOT
    cooperative cancellation. Hooks that need to cancel
    in-flight work must do so themselves on a deadline they
    track.
    """
    start = clock()
    if timeout_s <= 0:
        # No budget — record SKIPPED with zero duration. Caller
        # shouldn't get here (the dispatcher checks budget
        # before calling) but defense in depth.
        return HookExecutionRecord(
            hook_name=hook_name,
            outcome=HookOutcome.SKIPPED,
            duration_ms=0.0,
            detail="timeout_s_non_positive",
        )

    # Slot in shared list (NOT a dict — list assignment is
    # GIL-atomic for the simple case we need; we never mutate
    # from two threads concurrently because the worker writes
    # exactly once and the dispatcher reads once after join).
    result_slot: List[Any] = [None, None]  # [outcome, detail]

    def _worker() -> None:
        try:
            hook(context)
            result_slot[0] = HookOutcome.OK
            result_slot[1] = ""
        except BaseException as exc:  # noqa: BLE001 — defensive
            # BaseException net so KeyboardInterrupt etc. land
            # as FAILED rather than escaping into the daemon's
            # uncatchable death.
            try:
                result_slot[0] = HookOutcome.FAILED
                result_slot[1] = (
                    f"{type(exc).__name__}: {exc!s}"[:200]
                )
            except Exception:  # noqa: BLE001 — last-resort
                result_slot[0] = HookOutcome.FAILED
                result_slot[1] = "exc_unrenderable"

    thread = threading.Thread(
        target=_worker,
        name=f"termhook:{hook_name[:48]}",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=timeout_s)
    duration_ms = (clock() - start) * 1000.0

    if thread.is_alive():
        return HookExecutionRecord(
            hook_name=hook_name,
            outcome=HookOutcome.TIMED_OUT,
            duration_ms=duration_ms,
            detail=(
                f"timed_out_after_{timeout_s:.2f}s_"
                f"thread_orphaned"
            ),
        )

    outcome = result_slot[0]
    detail = result_slot[1] or ""
    if outcome is None:
        # Thread completed without setting result — should be
        # impossible (the BaseException net catches everything),
        # but defense in depth.
        return HookExecutionRecord(
            hook_name=hook_name,
            outcome=HookOutcome.FAILED,
            duration_ms=duration_ms,
            detail="worker_finished_without_result",
        )
    return HookExecutionRecord(
        hook_name=hook_name,
        outcome=outcome,
        duration_ms=duration_ms,
        detail=str(detail)[:200],
    )


# ---------------------------------------------------------------------------
# Total dispatcher
# ---------------------------------------------------------------------------


def dispatch_phase_sync(
    *,
    phase: TerminationPhase,
    cause: TerminationCause,
    hooks: Sequence[Tuple[str, TerminationHook]],
    context: TerminationHookContext,
    per_hook_timeout_s: float = 5.0,
    phase_budget_s: float = 10.0,
    clock: Optional[Callable[[], float]] = None,
) -> TerminationDispatchResult:
    """Synchronously execute every hook for a phase, bounded by
    BOTH a per-hook timeout AND a phase wall-clock budget.

    NEVER raises. Every failure mode collapses to a
    :class:`HookExecutionRecord` outcome.

    Strict sync-first: this function does NOT touch asyncio.
    Each hook runs on its own daemon thread with bounded
    join — survives asyncio-loop-wedge by construction.

    Strict deterministic budget enforcement:

      * ``phase_budget_s`` is the MAX wall-clock time the
        entire phase may consume across ALL hooks. Once
        exhausted, remaining hooks are recorded as ``SKIPPED``.
      * ``per_hook_timeout_s`` is the MAX wall-clock time a
        single hook may consume. The effective per-hook
        timeout is ``min(per_hook_timeout_s, remaining_budget)``
        so a single hook cannot push past the phase budget.

    Pure inputs → deterministic shape (modulo wall-clock-derived
    fields). Slice 2 wraps this with the registry — the registry
    just supplies the hooks list from its discovery contract.

    Caller-supplied ``hooks`` is iterated in order. Hook ordering
    is the caller's responsibility (Slice 2's registry exposes
    a documented insertion-order preservation contract).
    """
    _clock = clock or time.monotonic
    phase_start = _clock()

    # Defensive: caller might pass garbage. Coerce + filter
    # without raising.
    safe_hooks: List[Tuple[str, TerminationHook]] = []
    if isinstance(hooks, Iterable):
        for entry in hooks:
            try:
                name, fn = entry
            except (TypeError, ValueError):
                continue
            if not callable(fn):
                continue
            safe_name = str(name)[:128] if name else "unnamed"
            safe_hooks.append((safe_name, fn))

    records: List[HookExecutionRecord] = []
    budget_exhausted = False

    # Validate inputs to the bounds — non-positive caller values
    # collapse to safe defaults rather than letting them disable
    # budget enforcement.
    try:
        phase_budget_s = float(phase_budget_s)
        if phase_budget_s <= 0:
            phase_budget_s = 10.0
    except (TypeError, ValueError):
        phase_budget_s = 10.0
    try:
        per_hook_timeout_s = float(per_hook_timeout_s)
        if per_hook_timeout_s <= 0:
            per_hook_timeout_s = 5.0
    except (TypeError, ValueError):
        per_hook_timeout_s = 5.0

    for hook_name, hook in safe_hooks:
        elapsed = _clock() - phase_start
        remaining = phase_budget_s - elapsed
        if remaining <= 0:
            # Phase exhausted — record remaining hooks as SKIPPED
            # and stop dispatching.
            budget_exhausted = True
            records.append(HookExecutionRecord(
                hook_name=hook_name,
                outcome=HookOutcome.SKIPPED,
                duration_ms=0.0,
                detail=(
                    f"phase_budget_exhausted_after_"
                    f"{elapsed:.3f}s"
                ),
            ))
            continue
        # Effective per-hook timeout: min of the per-hook cap
        # and what remains in the phase budget. Guarantees one
        # slow hook can't push past the phase budget.
        effective_timeout = min(per_hook_timeout_s, remaining)
        try:
            rec = _execute_one_hook(
                hook_name=hook_name,
                hook=hook,
                context=context,
                timeout_s=effective_timeout,
                clock=_clock,
            )
        except BaseException as exc:  # noqa: BLE001 — last resort
            # _execute_one_hook is documented to never raise;
            # this is paranoia for the case where the threading
            # primitive itself failed (e.g. resource exhaustion).
            rec = HookExecutionRecord(
                hook_name=hook_name,
                outcome=HookOutcome.FAILED,
                duration_ms=0.0,
                detail=(
                    f"executor_internal:"
                    f"{type(exc).__name__}"
                )[:200],
            )
        records.append(rec)
        # If the hook timed out, the phase is at risk of going
        # over budget; we don't ABORT remaining hooks (they may
        # be cheap and important — the partial summary writer
        # is one such case), but the next iteration's elapsed
        # check will catch it.

    total_duration_ms = (_clock() - phase_start) * 1000.0
    return TerminationDispatchResult(
        phase=phase,
        cause=cause,
        records=tuple(records),
        total_duration_ms=total_duration_ms,
        budget_exhausted=budget_exhausted,
    )


# ---------------------------------------------------------------------------
# Default budgets (env-tunable in Slice 4)
# ---------------------------------------------------------------------------


#: Default per-hook timeout (seconds). Slice 4 will read this
#: from ``JARVIS_TERMINATION_HOOK_TIMEOUT_S``.
DEFAULT_PER_HOOK_TIMEOUT_S: float = 5.0

#: Default per-phase budget (seconds). Slice 4 will read this
#: from ``JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S``.
DEFAULT_PHASE_BUDGET_S: float = 10.0

#: Default per-phase budget for PRE_HARD_EXIT (seconds). Tighter
#: than the regular phase budget because the BoundedShutdownWatchdog
#: deadline is in single-digit seconds at this point. Hooks at this
#: phase are last-mile forensic emission, not full state flush.
DEFAULT_HARD_EXIT_PHASE_BUDGET_S: float = 2.0


__all__ = [
    "DEFAULT_HARD_EXIT_PHASE_BUDGET_S",
    "DEFAULT_PER_HOOK_TIMEOUT_S",
    "DEFAULT_PHASE_BUDGET_S",
    "HookExecutionRecord",
    "HookOutcome",
    "TERMINATION_HOOK_SCHEMA_VERSION",
    "TerminationCause",
    "TerminationDispatchResult",
    "TerminationHook",
    "TerminationHookContext",
    "TerminationPhase",
    "dispatch_phase_sync",
]
