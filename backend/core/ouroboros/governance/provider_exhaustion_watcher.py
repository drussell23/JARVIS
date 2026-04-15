"""
Provider Exhaustion Watcher — HIBERNATION_MODE step 5
=====================================================

Counts consecutive ``all_providers_exhausted`` events from
:class:`CandidateGenerator` and, at a configurable threshold, calls
:meth:`SupervisorOuroborosController.enter_hibernation` so the organism
survives a provider outage instead of dying on it.

The watcher is deliberately small:

- It holds an ``asyncio.Lock`` so the counter can't race between workers.
- It resets on any successful generation — a single success means the
  substrate is back, even if a few failures slipped through first.
- It is idempotent at the threshold: once hibernation is entered the
  controller's own idempotence kicks in and further exhaustion reports
  are tracked for observability but don't re-fire the transition.
- A successful ``wake_from_hibernation()`` does NOT need to reset the
  counter here — the first successful generation after wake will.

Integration points
------------------

The watcher is owned by the governance stack (constructed alongside
the controller) and passed into :class:`CandidateGenerator` at build
time. The generator notifies the watcher from a single catch/return
point wrapping :meth:`CandidateGenerator._generate_dispatch`:

  * successful generation → ``record_success()``
  * ``RuntimeError("all_providers_exhausted")`` → ``record_exhaustion()``

Environment
-----------

``JARVIS_HIBERNATION_TRIGGER_THRESHOLD`` (default ``3``): the number of
*consecutive* exhaustion events required before the watcher asks the
controller to hibernate. Setting this to ``1`` makes hibernation fire
on the first exhaustion (useful for tests); setting it to ``0`` is
rejected at construction time.

Per-op dedup (Session P fix, 2026-04-15)
----------------------------------------

Historically ``record_exhaustion`` was purely process-global: every
``all_providers_exhausted`` event from :class:`CandidateGenerator`
incremented the consecutive counter regardless of which op caused it.
Session ``bt-2026-04-15-192504`` (Session P) diagnosed the failure
mode: a single transient Claude API flake produced 3 exhaustion events
across 2 ops (one complex-route probe's retry + one runtime_health
reflex op's two attempts = 3 hits), tripping the threshold and
hibernating the organism even though only 2 distinct ops actually
failed.

The fix: ``record_exhaustion(op_id=...)`` now dedupes by op_id within
the current consecutive run. An op that exhausts both its attempts
contributes **one** event to the counter, not two. The set of
already-counted op_ids is cleared on every ``record_success()``,
matching the reset-on-success semantics of the consecutive counter
itself. Callers that don't pass ``op_id`` preserve the pre-patch
behavior (every call increments) — the default is ``None`` so
every existing test + call site is unchanged at the wire.

See memory ``project_exhaustion_watcher_retry_counting.md`` for the
full diagnosis history.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("Ouroboros.ExhaustionWatcher")


_DEFAULT_THRESHOLD = 3
_ENV_THRESHOLD = "JARVIS_HIBERNATION_TRIGGER_THRESHOLD"

# Cap on the size of ``_counted_op_ids`` to prevent unbounded growth
# in pathological long-running sessions with no successful generations.
# 256 ≈ 13 KB at 50-byte op_ids — trivially affordable, and well above
# any realistic hibernation-threshold * op-density product. When the
# cap is exceeded the oldest half is evicted FIFO-style; at that
# point the consecutive counter itself has long since crossed the
# threshold and hibernation has fired, so the eviction is cosmetic.
_MAX_COUNTED_OPS: int = 256


def _resolve_threshold(explicit: Optional[int]) -> int:
    """Pick the effective threshold: explicit arg > env var > default."""
    if explicit is not None:
        if explicit <= 0:
            raise ValueError(
                f"threshold must be >= 1 (got {explicit})"
            )
        return explicit
    raw = os.environ.get(_ENV_THRESHOLD, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            logger.warning(
                "%s=%r is not an int — falling back to default %d",
                _ENV_THRESHOLD, raw, _DEFAULT_THRESHOLD,
            )
            return _DEFAULT_THRESHOLD
        if parsed <= 0:
            logger.warning(
                "%s=%d is non-positive — falling back to default %d",
                _ENV_THRESHOLD, parsed, _DEFAULT_THRESHOLD,
            )
            return _DEFAULT_THRESHOLD
        return parsed
    return _DEFAULT_THRESHOLD


class ProviderExhaustionWatcher:
    """Threshold-triggered bridge from CandidateGenerator to the controller.

    Parameters
    ----------
    controller:
        Anything with an async ``enter_hibernation(reason: str)`` method —
        concretely a :class:`SupervisorOuroborosController` in production
        and a fake in tests. Kept structurally typed to avoid an import
        cycle.
    threshold:
        Consecutive-exhaustion count that triggers hibernation. ``None``
        reads ``JARVIS_HIBERNATION_TRIGGER_THRESHOLD`` with a default of 3.
    prober:
        Optional :class:`HibernationProber` (or any object with an async
        ``start()`` method). When supplied, ``prober.start()`` is awaited
        immediately after the controller accepts ``enter_hibernation()``
        so the health-probe loop can schedule the wake. The prober is
        idempotent, so a burst of exhaustion events will not spawn
        duplicate tasks.
    """

    def __init__(
        self,
        controller: Any,
        *,
        threshold: Optional[int] = None,
        prober: Optional[Any] = None,
    ) -> None:
        self._controller = controller
        self._threshold: int = _resolve_threshold(threshold)
        self._consecutive: int = 0
        self._total_exhaustions: int = 0
        self._total_successes: int = 0
        self._hibernations_triggered: int = 0
        self._deduped_events: int = 0
        self._last_reason: Optional[str] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._prober = prober
        # Op-ids already credited toward the current consecutive run.
        # Cleared on any successful generation. See Session P fix notes.
        self._counted_op_ids: Set[str] = set()
        logger.info(
            "ProviderExhaustionWatcher initialised — threshold=%d prober=%s",
            self._threshold,
            "yes" if prober is not None else "no",
        )

    def attach_prober(self, prober: Any) -> None:
        """Install a :class:`HibernationProber` after construction.

        Used by the governance stack, which has to build the
        CandidateGenerator (and therefore the provider list) before it
        can build the prober. The watcher is constructed first and the
        prober is stitched in once the providers are known.
        """
        self._prober = prober
        logger.info(
            "[ExhaustionWatcher] prober attached (%s)",
            type(prober).__name__ if prober is not None else "None",
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def threshold(self) -> int:
        """Configured exhaustion threshold (consecutive events)."""
        return self._threshold

    @property
    def consecutive(self) -> int:
        """Current consecutive-exhaustion run length (0 means healthy)."""
        return self._consecutive

    @property
    def total_exhaustions(self) -> int:
        """Cumulative count of exhaustion events observed this process."""
        return self._total_exhaustions

    @property
    def hibernations_triggered(self) -> int:
        """Number of times this watcher actually flipped the controller."""
        return self._hibernations_triggered

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record_exhaustion(
        self,
        *,
        reason: str = "",
        op_id: Optional[str] = None,
    ) -> bool:
        """Record an ``all_providers_exhausted`` event.

        Increments the consecutive counter. If the threshold is reached
        the watcher asks the controller to enter hibernation. The
        counter is NOT reset at the threshold — a successful generation
        (via :meth:`record_success`) is the only thing that clears it,
        so a flapping provider that oscillates below the threshold
        does not keep thrashing the controller.

        When ``op_id`` is supplied, this call dedupes within the current
        consecutive run: repeat events for the same op (e.g. both
        retries of a CandidateGenerator dispatch that both exhaust)
        contribute **one** increment, not two. The set of counted
        op_ids is cleared on every :meth:`record_success` and
        :meth:`reset`. Callers that pass ``op_id=None`` retain the
        pre-patch behavior and every call increments.

        Returns ``True`` iff this call actually transitioned the
        controller into HIBERNATION (controller may refuse if already
        hibernating / DISABLED / EMERGENCY_STOP).
        """
        async with self._lock:
            if op_id and op_id in self._counted_op_ids:
                self._deduped_events += 1
                self._total_exhaustions += 1
                logger.info(
                    "[ExhaustionWatcher] record_exhaustion(op_id=%s) "
                    "DEDUPED — consecutive stays at %d/%d total=%d "
                    "deduped=%d",
                    op_id,
                    self._consecutive,
                    self._threshold,
                    self._total_exhaustions,
                    self._deduped_events,
                )
                return False
            self._consecutive += 1
            self._total_exhaustions += 1
            self._last_reason = reason or "unspecified"
            if op_id:
                self._counted_op_ids.add(op_id)
                if len(self._counted_op_ids) > _MAX_COUNTED_OPS:
                    # FIFO-ish eviction: keep the most recent half.
                    # Sets don't preserve insertion order strictly, but
                    # this is cosmetic — by the time we're evicting the
                    # consecutive counter has long since tripped the
                    # threshold and hibernation has fired.
                    keep = list(self._counted_op_ids)[-(_MAX_COUNTED_OPS // 2):]
                    self._counted_op_ids = set(keep)
                    logger.warning(
                        "[ExhaustionWatcher] _counted_op_ids exceeded %d "
                        "— evicted oldest half (now %d entries)",
                        _MAX_COUNTED_OPS,
                        len(self._counted_op_ids),
                    )
            logger.warning(
                "[ExhaustionWatcher] record_exhaustion(reason=%r op_id=%s) "
                "— consecutive=%d/%d total=%d",
                self._last_reason,
                op_id or "-",
                self._consecutive,
                self._threshold,
                self._total_exhaustions,
            )
            if self._consecutive < self._threshold:
                return False
            transitioned = await self._maybe_hibernate()
            return transitioned

    async def record_success(self) -> None:
        """Reset the consecutive counter on any successful generation.

        Cheap path for the hot case: if the counter is already zero and
        there are no counted op_ids we skip the lock. Otherwise we grab
        the lock, clear the counter, and clear the dedup set.
        """
        self._total_successes += 1
        if self._consecutive == 0 and not self._counted_op_ids:
            return
        async with self._lock:
            if self._consecutive == 0 and not self._counted_op_ids:
                return
            previous = self._consecutive
            previous_ops = len(self._counted_op_ids)
            self._consecutive = 0
            self._last_reason = None
            self._counted_op_ids.clear()
            logger.info(
                "[ExhaustionWatcher] record_success() — consecutive reset "
                "(was %d, counted_ops=%d)",
                previous,
                previous_ops,
            )

    async def reset(self) -> None:
        """Hard reset — used by tests and emergency-stop cleanup."""
        async with self._lock:
            self._consecutive = 0
            self._last_reason = None
            self._counted_op_ids.clear()

    def snapshot(self) -> Dict[str, Any]:
        """Lock-free observability snapshot for health()/TUI."""
        return {
            "threshold": self._threshold,
            "consecutive": self._consecutive,
            "total_exhaustions": self._total_exhaustions,
            "total_successes": self._total_successes,
            "hibernations_triggered": self._hibernations_triggered,
            "deduped_events": self._deduped_events,
            "unique_ops_counted": len(self._counted_op_ids),
            "last_reason": self._last_reason,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _maybe_hibernate(self) -> bool:
        """Invoke ``controller.enter_hibernation()`` and swallow controller failures.

        Called under ``self._lock``. The controller's own idempotence
        handles the "already hibernating" case; we only translate its
        return value into ours and count the transition.
        """
        enter = getattr(self._controller, "enter_hibernation", None)
        if enter is None:
            logger.error(
                "[ExhaustionWatcher] controller has no enter_hibernation() "
                "— watcher cannot trigger HIBERNATION"
            )
            return False
        reason = (
            f"consecutive_exhaustion={self._consecutive} "
            f"last={self._last_reason!r}"
        )
        try:
            result = await enter(reason=reason)
        except RuntimeError as exc:
            # EMERGENCY_STOP refuses hibernation — log and move on.
            logger.error(
                "[ExhaustionWatcher] enter_hibernation refused: %s", exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[ExhaustionWatcher] enter_hibernation raised unexpectedly: %s",
                exc,
            )
            return False
        if result:
            self._hibernations_triggered += 1
            logger.critical(
                "[ExhaustionWatcher] HIBERNATION triggered "
                "(cycle #%d, reason=%r)",
                self._hibernations_triggered,
                reason,
            )
            if self._prober is not None:
                prober_start = getattr(self._prober, "start", None)
                if prober_start is None:
                    logger.warning(
                        "[ExhaustionWatcher] prober has no start() method "
                        "— wake will not be scheduled"
                    )
                else:
                    try:
                        await prober_start()
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[ExhaustionWatcher] prober.start() raised — "
                            "hibernation entered but wake loop not running"
                        )
        return bool(result)
