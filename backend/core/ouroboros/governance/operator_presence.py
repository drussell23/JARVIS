"""Deterministic operator-presence detector + edge-triggered event watcher (spec §5.3).

Design constraints
------------------
* DETERMINISTIC only — no CAI/LLM call in the presence decision.
* Edge-triggered publishing — emits operator.active / operator.idle only on state
  transition; never repeats the same topic twice in a row (no level-spam).
* Default-off — the long-running ``run()`` loop returns immediately unless
  ``JARVIS_OPERATOR_YIELD_ENABLED`` is true.
* Fail-soft throughout — a missing/unavailable bus never raises; a misbehaving
  liveness probe is treated as absent (False).
* Injected bus support — ``run_once`` and ``run`` accept an optional ``bus``
  argument so callers (and tests) can supply a fake without touching globals.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Topic constants (public API)
# ---------------------------------------------------------------------------

EVENT_OPERATOR_ACTIVE: str = "operator.active"
EVENT_OPERATOR_IDLE: str = "operator.idle"

# ---------------------------------------------------------------------------
# Module-level last-human-input timestamp (monotonic)
# ---------------------------------------------------------------------------

_last_input: float = 0.0  # never present by default until first note_human_input()


def note_human_input() -> None:
    """Stamp the current monotonic time as the most recent human-input moment.

    Call this from the intake layer / REPL whenever the human sends a message.
    Thread-safe via the GIL (float assignment is atomic in CPython).
    """
    global _last_input
    _last_input = time.monotonic()


# ---------------------------------------------------------------------------
# Pure detection helpers
# ---------------------------------------------------------------------------

def _idle_threshold_s() -> float:
    """Read JARVIS_OPERATOR_IDLE_S from env; default 45s."""
    try:
        return float(os.environ.get("JARVIS_OPERATOR_IDLE_S", "45"))
    except (ValueError, TypeError):
        return 45.0


def _is_present(
    last_input_monotonic: float,
    now: float,
    liveness: Optional[Callable[[], bool]] = None,
) -> bool:
    """Pure, deterministic presence test.

    Returns True iff the operator is considered present.

    Args:
        last_input_monotonic: ``time.monotonic()`` value of the last human input.
        now: Current ``time.monotonic()`` value.
        liveness: Optional callable; if it returns truthy the operator is
                  considered present regardless of the input timestamp.
                  Exceptions from the probe are swallowed (fail-soft → False).
    """
    # Check liveness probe first — it can override a stale timestamp.
    if liveness is not None:
        try:
            if liveness():
                return True
        except Exception:  # noqa: BLE001 — fail-soft
            pass

    elapsed = now - last_input_monotonic
    return elapsed < _idle_threshold_s()


def operator_present(liveness: Optional[Callable[[], bool]] = None) -> bool:
    """Convenience wrapper using the module-level ``_last_input`` timestamp."""
    return _is_present(
        last_input_monotonic=_last_input,
        now=time.monotonic(),
        liveness=liveness,
    )


# ---------------------------------------------------------------------------
# Watcher class
# ---------------------------------------------------------------------------

_UNSET = object()  # sentinel for "no prior state"


class OperatorPresenceWatcher:
    """Edge-triggered operator-presence watcher.

    Holds the last-emitted state and publishes ``operator.active`` /
    ``operator.idle`` events on the TrinityEventBus only when the state
    transitions.
    """

    def __init__(self) -> None:
        self._last_state: object = _UNSET  # True | False | _UNSET

    # ------------------------------------------------------------------
    # Edge-trigger logic (pure, testable without async)
    # ------------------------------------------------------------------

    def _transition(self, present: bool) -> Optional[str]:
        """Return the event topic string if the state changed, else None.

        Args:
            present: Current computed presence value.

        Returns:
            ``EVENT_OPERATOR_ACTIVE`` / ``EVENT_OPERATOR_IDLE`` on transition;
            ``None`` when state is unchanged (no level-spam).
        """
        if self._last_state is _UNSET or self._last_state != present:
            self._last_state = present
            return EVENT_OPERATOR_ACTIVE if present else EVENT_OPERATOR_IDLE
        return None

    # ------------------------------------------------------------------
    # Async publishing
    # ------------------------------------------------------------------

    async def run_once(
        self,
        bus: object = None,
        liveness: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Evaluate presence once; publish to *bus* if state changed.

        Args:
            bus: A TrinityEventBus instance (or a fake with an async
                 ``publish(event, persist=...)`` method).  When None the
                 real singleton is resolved via ``get_event_bus_if_exists()``.
            liveness: Optional callable; forwarded to ``_is_present``.
        """
        present = _is_present(
            last_input_monotonic=_last_input,
            now=time.monotonic(),
            liveness=liveness,
        )
        topic = self._transition(present)
        if topic is None:
            return  # no state change → nothing to publish

        effective_bus = bus if bus is not None else _get_bus()
        if effective_bus is None:
            logger.debug("[OperatorPresence] No bus available; skipping publish of %s", topic)
            return

        try:
            from backend.core.trinity_event_bus import (
                EventPriority,
                RepoType,
                TrinityEvent,
            )

            event = TrinityEvent(
                topic=topic,
                source=RepoType.JARVIS,
                priority=EventPriority.NORMAL,
                payload={
                    "present": present,
                    "last_input_monotonic": _last_input,
                    "source": "operator_presence_watcher",
                },
            )
            await effective_bus.publish(event, persist=False)
            logger.debug("[OperatorPresence] Published %s", topic)
        except Exception:  # noqa: BLE001 — fail-soft
            logger.debug("[OperatorPresence] publish failed (fail-soft)", exc_info=True)

    async def run(
        self,
        bus: object = None,
        liveness: Optional[Callable[[], bool]] = None,
        interval_s: float = 5.0,
    ) -> None:
        """Polling loop — calls ``run_once`` on *interval_s* cadence.

        Returns immediately (no-op) when ``JARVIS_OPERATOR_YIELD_ENABLED``
        is not set to a truthy value.

        Args:
            bus: Bus instance to publish to (injected for tests).
            liveness: Optional liveness probe forwarded to each ``run_once``.
            interval_s: Seconds between presence checks.
        """
        if not _enabled():
            logger.debug("[OperatorPresence] JARVIS_OPERATOR_YIELD_ENABLED=false; watcher inactive")
            return

        logger.info("[OperatorPresence] Starting presence watcher (interval=%.1fs)", interval_s)
        try:
            while True:
                await self.run_once(bus=bus, liveness=liveness)
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            logger.debug("[OperatorPresence] Watcher cancelled")
            raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    """Return True when JARVIS_OPERATOR_YIELD_ENABLED is truthy."""
    val = os.environ.get("JARVIS_OPERATOR_YIELD_ENABLED", "false").lower()
    return val in ("true", "1", "yes", "on")


def _get_bus() -> Optional[object]:
    """Return the real bus singleton without creating one.

    Uses ``get_event_bus_if_exists()`` which is non-async and side-effect-free.
    Falls back to None when the bus is not yet initialised.
    """
    try:
        from backend.core.trinity_event_bus import get_event_bus_if_exists
        return get_event_bus_if_exists()
    except Exception:  # noqa: BLE001
        return None
