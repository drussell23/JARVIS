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
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.ExhaustionWatcher")


_DEFAULT_THRESHOLD = 3
_ENV_THRESHOLD = "JARVIS_HIBERNATION_TRIGGER_THRESHOLD"


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
    """

    def __init__(
        self,
        controller: Any,
        *,
        threshold: Optional[int] = None,
    ) -> None:
        self._controller = controller
        self._threshold: int = _resolve_threshold(threshold)
        self._consecutive: int = 0
        self._total_exhaustions: int = 0
        self._total_successes: int = 0
        self._hibernations_triggered: int = 0
        self._last_reason: Optional[str] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        logger.info(
            "ProviderExhaustionWatcher initialised — threshold=%d",
            self._threshold,
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

    async def record_exhaustion(self, *, reason: str = "") -> bool:
        """Record an ``all_providers_exhausted`` event.

        Increments the consecutive counter. If the threshold is reached
        the watcher asks the controller to enter hibernation. The
        counter is NOT reset at the threshold — a successful generation
        (via :meth:`record_success`) is the only thing that clears it,
        so a flapping provider that oscillates below the threshold
        does not keep thrashing the controller.

        Returns ``True`` iff this call actually transitioned the
        controller into HIBERNATION (controller may refuse if already
        hibernating / DISABLED / EMERGENCY_STOP).
        """
        async with self._lock:
            self._consecutive += 1
            self._total_exhaustions += 1
            self._last_reason = reason or "unspecified"
            logger.warning(
                "[ExhaustionWatcher] record_exhaustion(reason=%r) "
                "— consecutive=%d/%d total=%d",
                self._last_reason,
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

        Cheap path for the hot case: if the counter is already zero we
        skip the lock. Otherwise we grab the lock and clear.
        """
        self._total_successes += 1
        if self._consecutive == 0:
            return
        async with self._lock:
            if self._consecutive == 0:
                return
            previous = self._consecutive
            self._consecutive = 0
            self._last_reason = None
            logger.info(
                "[ExhaustionWatcher] record_success() — consecutive reset "
                "(was %d)",
                previous,
            )

    async def reset(self) -> None:
        """Hard reset — used by tests and emergency-stop cleanup."""
        async with self._lock:
            self._consecutive = 0
            self._last_reason = None

    def snapshot(self) -> Dict[str, Any]:
        """Lock-free observability snapshot for health()/TUI."""
        return {
            "threshold": self._threshold,
            "consecutive": self._consecutive,
            "total_exhaustions": self._total_exhaustions,
            "total_successes": self._total_successes,
            "hibernations_triggered": self._hibernations_triggered,
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
        return bool(result)
