"""TerminationHookRegistry Slice 3 — default adapters + harness wire-up.

Bridges the existing ``OuroborosBattleTest._atexit_fallback_write``
sync partial-summary writer into the Slice 2 registry as a
:class:`TerminationPhase.PRE_SHUTDOWN_EVENT_SET` hook.

## Pristine equivalency contract

The hook this module registers calls ``_atexit_fallback_write`` with
EXACTLY the arguments the existing signal-handler path passes —
``session_outcome="incomplete_kill"`` for any signal-derived OR
cap-derived termination (the dichotomy the
``LastSessionSummary``-side parser cares about is
"complete vs interrupted", not the specific cap variety). The
``stop_reason`` stamping mirrors the signal handler's discipline at
``harness.py:3286-3287``: stamp ONLY if not already set, so an
earlier path that already classified (e.g. wall-cap setting
"wall_clock_cap" before the dispatch fires) takes precedence over
the cause→string fallback.

This means:
  * Signal path post-migration writes the SAME ``summary.json`` it
    wrote pre-migration. Pinned by the byte-equivalency test in
    ``test_termination_hook_slice3_wiring.py``.
  * Wall-cap path post-migration writes a ``summary.json`` it
    NEVER wrote pre-migration (THE bug fix). Same shape + fields
    as the signal path's partial summary so downstream tooling
    needs no changes.

## Harness-singleton accessor

Termination hooks are dispatched from contexts where the harness
instance is not in scope (signal-handler callbacks, wall-clock
watchdog tasks). Rather than threading a harness reference through
every context object, this module exposes a per-process singleton
accessor (``set_active_harness`` / ``get_active_harness`` /
``clear_active_harness``).

Production: ``OuroborosBattleTest.__init__`` calls
``set_active_harness(self)`` exactly once. There is exactly one
harness per process (the harness wraps the entire async lifecycle
of a battle-test session — concurrent harnesses are not a
supported configuration).

Tests: each test that constructs a harness should call
``clear_active_harness()`` between cases. The Slice 3 test fixture
provides this automatically.

## Discovery contract

This module exposes ``register_termination_hooks(registry)`` so the
auto-discovery loop in
:mod:`termination_hook_registry.discover_module_provided_hooks`
picks it up. Boot wire-up calls
``discover_and_register_default()`` once after the harness is
constructed.

## Authority invariants (AST-pinned in Slice 4)

* MAY import: :mod:`termination_hook` (substrate) +
  :mod:`termination_hook_registry` (registry).
* MUST NOT import: ``asyncio`` (sync-first contract — the hook
  must survive a wedged event loop) / ``yaml_writer`` /
  ``orchestrator`` / ``iron_gate`` / ``risk_tier`` /
  ``change_engine`` / ``candidate_generator`` / ``gate`` /
  ``policy``.
* MAY NOT import :mod:`harness` directly (avoid import cycle —
  the harness imports US for the singleton setter; we look up
  the harness via the singleton at fire time).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from backend.core.ouroboros.battle_test.termination_hook import (
    TerminationCause,
    TerminationHookContext,
    TerminationPhase,
)
from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
    TerminationHookRegistry,
)

logger = logging.getLogger(__name__)


TERMINATION_HOOK_DEFAULT_ADAPTERS_SCHEMA_VERSION: str = (
    "termination_hook_default_adapters.1"
)


# ---------------------------------------------------------------------------
# Per-process harness singleton — set by harness __init__, looked up
# at hook fire time.
# ---------------------------------------------------------------------------


_ACTIVE_HARNESS: Optional[Any] = None
_ACTIVE_HARNESS_LOCK = threading.Lock()


def set_active_harness(harness: Any) -> None:
    """Install the per-process active harness. Called by
    ``OuroborosBattleTest.__init__`` exactly once. Idempotent on
    re-set with the same instance; logs at INFO if a different
    instance replaces an existing one (test-harness scenario or
    operator misconfig)."""
    global _ACTIVE_HARNESS
    with _ACTIVE_HARNESS_LOCK:
        if _ACTIVE_HARNESS is harness:
            return
        if _ACTIVE_HARNESS is not None and harness is not None:
            logger.info(
                "[TerminationHookAdapters] active harness "
                "replaced (was %r → now %r) — expected only in "
                "test contexts",
                type(_ACTIVE_HARNESS).__name__,
                type(harness).__name__,
            )
        _ACTIVE_HARNESS = harness


def get_active_harness() -> Optional[Any]:
    """Return the per-process active harness, or None if none is
    installed (cold start before __init__ completes, or post
    teardown). NEVER raises."""
    with _ACTIVE_HARNESS_LOCK:
        return _ACTIVE_HARNESS


def clear_active_harness() -> None:
    """Test helper — drop the singleton. NEVER raises."""
    global _ACTIVE_HARNESS
    with _ACTIVE_HARNESS_LOCK:
        _ACTIVE_HARNESS = None


# ---------------------------------------------------------------------------
# Cause → session_outcome mapping
# ---------------------------------------------------------------------------


# Closed mapping. Pristine-equivalency invariant: every cause that
# was previously classified as "incomplete_kill" by the signal
# handler at harness.py:3290 MUST still map to "incomplete_kill"
# after migration. New causes (wall-cap, idle, budget) ALSO map to
# "incomplete_kill" because the LastSessionSummary parser's
# clean-vs-interrupted dichotomy treats them all the same — the
# specific cause is in ``stop_reason``, which the writer also
# stamps.
_CAUSE_TO_SESSION_OUTCOME: dict = {
    TerminationCause.SIGTERM: "incomplete_kill",
    TerminationCause.SIGINT: "incomplete_kill",
    TerminationCause.SIGHUP: "incomplete_kill",
    TerminationCause.WALL_CLOCK_CAP: "incomplete_kill",
    TerminationCause.IDLE_TIMEOUT: "incomplete_kill",
    TerminationCause.BUDGET_EXCEEDED: "incomplete_kill",
    TerminationCause.NORMAL_EXIT: None,  # clean path — writer
                                          # uses default ("complete")
    TerminationCause.UNKNOWN: "incomplete_kill",
}


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def partial_summary_writer_hook(ctx: TerminationHookContext) -> None:
    """Adapter from :class:`TerminationHookContext` to the existing
    ``OuroborosBattleTest._atexit_fallback_write`` sync writer.

    Pristine equivalency:
      1. Resolves the active harness via
         :func:`get_active_harness` — None means no harness
         installed (cold start / test artifact); the hook is a
         silent no-op in that case (avoids crashing the dispatch
         + matches today's behavior where no harness == no
         summary write).
      2. Stamps ``harness._stop_reason = ctx.stop_reason`` ONLY if
         the harness's existing stop_reason is one of
         ``{"unknown", "", None}`` — same predicate the signal
         handler at harness.py:3286 uses, so a path that already
         classified (e.g. wall-cap stamping
         ``wall_clock_cap`` before the dispatch fires) is not
         clobbered. If ``ctx.stop_reason`` is also empty, falls
         back to ``ctx.cause.value``.
      3. Maps ``ctx.cause`` to the documented
         ``session_outcome`` per :data:`_CAUSE_TO_SESSION_OUTCOME`.
      4. Calls ``harness._atexit_fallback_write(session_outcome=...)``
         — IDENTICAL to the call the signal handler makes today
         (lines 3289-3290). The writer's ``_summary_written`` gate
         keeps it idempotent: if the clean path already wrote a
         full summary, the fallback is a no-op.

    NEVER raises. The Slice 1 dispatcher catches exceptions, but
    we belt-and-suspender it here so a missing/broken harness
    surface degrades to a logged debug line rather than a
    HookOutcome.FAILED that confuses the operator.
    """
    try:
        harness = get_active_harness()
        if harness is None:
            logger.debug(
                "[TerminationHookAdapters] no active harness — "
                "summary write skipped (cold-start or post-"
                "teardown)",
            )
            return
        # Stamp stop_reason if not already classified.
        try:
            existing = getattr(harness, "_stop_reason", None)
            if existing in ("unknown", "", None):
                # Prefer ctx.stop_reason (caller's classification);
                # fall back to ctx.cause.value if empty.
                stamp = ctx.stop_reason or ctx.cause.value
                harness._stop_reason = stamp
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[TerminationHookAdapters] stop_reason stamp "
                "failed: %s", exc,
            )
        # Map cause → session_outcome.
        session_outcome = _CAUSE_TO_SESSION_OUTCOME.get(
            ctx.cause,
            "incomplete_kill",  # defensive default
        )
        # Invoke the existing writer. _summary_written gate keeps
        # the call idempotent (won't double-write if the clean
        # async path already completed).
        try:
            writer = getattr(
                harness, "_atexit_fallback_write", None,
            )
            if not callable(writer):
                logger.debug(
                    "[TerminationHookAdapters] harness lacks "
                    "_atexit_fallback_write — write skipped",
                )
                return
            if session_outcome is None:
                # NORMAL_EXIT path — writer's default
                # ("partial_shutdown:atexit_fallback" with no
                # session_outcome stamp). This matches what
                # atexit.register would invoke today.
                writer()
            else:
                writer(session_outcome=session_outcome)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[TerminationHookAdapters] writer raised: %s",
                exc,
            )
    except Exception as exc:  # noqa: BLE001 — last-resort
        logger.debug(
            "[TerminationHookAdapters] hook internal: %s", exc,
        )


# ---------------------------------------------------------------------------
# Discovery contract — auto-discovery picks this up
# ---------------------------------------------------------------------------


#: The canonical name the registry stores this hook under. Pinned
#: as a module-level constant so Slice 4's AST validator can
#: assert the hook is still registered after a refactor.
PARTIAL_SUMMARY_WRITER_HOOK_NAME: str = "partial_summary_writer"


def register_termination_hooks(
    registry: TerminationHookRegistry,
) -> int:
    """Module-owned registration. Returns count of hooks installed.
    NEVER raises — the discovery loop in
    :func:`termination_hook_registry.discover_module_provided_hooks`
    swallows exceptions, but defensive contract keeps boot clean.

    Idempotent on re-import: if the hook is already registered
    (DuplicateHookNameError raised by the registry), returns 0
    so the count reflects only NEW registrations on this call."""
    installed = 0
    try:
        # Import locally so the duplicate-name exception is in
        # scope. Slice 2's registry exposes the exception class.
        from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
            DuplicateHookNameError,
        )
        try:
            registry.register(
                TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
                partial_summary_writer_hook,
                name=PARTIAL_SUMMARY_WRITER_HOOK_NAME,
                # Priority 10 → runs early in PRE_SHUTDOWN_EVENT_SET.
                # The partial-summary writer is the most important
                # hook in this phase (it's the entire reason this
                # arc exists); operator-defined hooks should run
                # AFTER it so a buggy operator hook can't preempt
                # the safety write.
                priority=10,
            )
            installed += 1
        except DuplicateHookNameError:
            # Already registered — idempotent; not an error.
            pass
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[TerminationHookAdapters] register_termination_hooks "
            "internal: %s", exc,
        )
    return installed


__all__ = [
    "PARTIAL_SUMMARY_WRITER_HOOK_NAME",
    "TERMINATION_HOOK_DEFAULT_ADAPTERS_SCHEMA_VERSION",
    "clear_active_harness",
    "get_active_harness",
    "partial_summary_writer_hook",
    "register_termination_hooks",
    "set_active_harness",
]
