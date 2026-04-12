"""
Hibernation Prober — HIBERNATION_MODE step 6
============================================

Background loop that polls provider :meth:`health_probe` methods on an
exponential-backoff schedule while the
:class:`SupervisorOuroborosController` is in ``HIBERNATION``. As soon
as any provider reports healthy, the prober calls
:meth:`SupervisorOuroborosController.wake_from_hibernation` and exits.

The prober owns one long-running :class:`asyncio.Task` at a time and
is idempotent:

* :meth:`start` is a no-op if a task is already running.
* :meth:`stop` cancels the task without waking the controller (used
  when the surrounding service is torn down or when the operator
  clears an emergency-stop).
* :meth:`_probe_loop` self-exits after either (a) a successful probe,
  or (b) the configured ``max_duration_s`` budget is exhausted. The
  runtime can restart the prober later if the controller re-enters
  HIBERNATION.

Design notes
------------

- **Sequential probe, any-healthy wake**: probing all providers in
  parallel would burn DW credits for no benefit — one healthy
  provider is enough to resume. Iterating is also cheaper to reason
  about for backoff accounting.
- **Exponential backoff with cap**: ``initial_delay_s`` doubles each
  miss up to ``max_delay_s``. A 5 s initial, 300 s max, 3600 s budget
  means the prober can probe ~13 times over an hour without hammering
  a flaky endpoint.
- **No controller state reads**: the prober never inspects
  ``controller.mode``. It trusts its caller (the exhaustion watcher
  or whoever called ``start()``) to only invoke it while
  hibernating — the controller's own idempotence handles
  ``wake_from_hibernation`` when the state has already changed.

Environment
-----------

``JARVIS_HIBERNATION_PROBE_INITIAL_S`` (default ``5``): first probe
delay in seconds.

``JARVIS_HIBERNATION_PROBE_MAX_S`` (default ``300``): cap for the
exponential backoff between probes.

``JARVIS_HIBERNATION_MAX_DURATION_S`` (default ``3600``): hard budget
for the whole probe window. When exceeded the prober logs and exits
without waking; the next exhaustion → hibernation cycle will spawn
a fresh prober.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.HibernationProber")


_ENV_INITIAL_S = "JARVIS_HIBERNATION_PROBE_INITIAL_S"
_ENV_MAX_S = "JARVIS_HIBERNATION_PROBE_MAX_S"
_ENV_DURATION_S = "JARVIS_HIBERNATION_MAX_DURATION_S"

_DEFAULT_INITIAL_S = 5.0
_DEFAULT_MAX_S = 300.0
_DEFAULT_DURATION_S = 3600.0


def _resolve_float(env: str, default: float, explicit: Optional[float]) -> float:
    """Prefer explicit > env > default, falling back on parse errors."""
    if explicit is not None:
        if explicit <= 0:
            raise ValueError(f"{env} must be > 0 (got {explicit})")
        return float(explicit)
    raw = os.environ.get(env, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a float — falling back to default %s",
            env, raw, default,
        )
        return default
    if value <= 0:
        logger.warning(
            "%s=%s is non-positive — falling back to default %s",
            env, value, default,
        )
        return default
    return value


class HibernationProber:
    """Exponential-backoff health probe driving ``wake_from_hibernation``.

    Parameters
    ----------
    controller:
        Structurally typed — any object with an async
        ``wake_from_hibernation(reason: str)`` method.
    providers:
        Iterable of provider objects, each with an async
        ``health_probe() -> bool``. ``None`` entries are skipped so
        callers can pass ``[tier0, primary, fallback]`` directly
        without pre-filtering.
    initial_delay_s / max_delay_s / max_duration_s:
        Explicit overrides for the environment-default backoff and
        budget values. See module docstring.
    """

    def __init__(
        self,
        *,
        controller: Any,
        providers: Sequence[Any],
        initial_delay_s: Optional[float] = None,
        max_delay_s: Optional[float] = None,
        max_duration_s: Optional[float] = None,
    ) -> None:
        self._controller = controller
        self._providers: List[Any] = [p for p in providers if p is not None]
        self._initial_delay_s = _resolve_float(
            _ENV_INITIAL_S, _DEFAULT_INITIAL_S, initial_delay_s,
        )
        self._max_delay_s = _resolve_float(
            _ENV_MAX_S, _DEFAULT_MAX_S, max_delay_s,
        )
        self._max_duration_s = _resolve_float(
            _ENV_DURATION_S, _DEFAULT_DURATION_S, max_duration_s,
        )
        if self._max_delay_s < self._initial_delay_s:
            logger.warning(
                "[HibernationProber] max_delay_s (%.1f) < initial_delay_s "
                "(%.1f) — using initial for both",
                self._max_delay_s, self._initial_delay_s,
            )
            self._max_delay_s = self._initial_delay_s

        self._task: Optional[asyncio.Task[None]] = None
        self._started_at: Optional[float] = None
        self._probe_attempts: int = 0
        self._wake_count: int = 0
        self._last_result: Optional[str] = None

        logger.info(
            "HibernationProber initialised — providers=%d initial=%.1fs "
            "max=%.1fs budget=%.1fs",
            len(self._providers),
            self._initial_delay_s,
            self._max_delay_s,
            self._max_duration_s,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_probing(self) -> bool:
        """True while the internal asyncio task is running."""
        return self._task is not None and not self._task.done()

    @property
    def probe_attempts(self) -> int:
        """Number of probe iterations performed in the current / last run."""
        return self._probe_attempts

    @property
    def wake_count(self) -> int:
        """Total successful wakes the prober has issued across its lifetime."""
        return self._wake_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Spawn the probe loop. Idempotent — returns False if already running.

        Also returns False if no usable providers were supplied, since
        probing an empty set would spin forever without ever waking.
        """
        if self.is_probing:
            logger.debug("[HibernationProber] start() no-op — already probing")
            return False
        if not self._providers:
            logger.warning(
                "[HibernationProber] start() refused — no providers to probe"
            )
            return False
        self._started_at = time.monotonic()
        self._probe_attempts = 0
        self._last_result = None
        self._task = asyncio.create_task(
            self._probe_loop(), name="hibernation_prober",
        )
        logger.info(
            "[HibernationProber] probe loop started (providers=%d)",
            len(self._providers),
        )
        return True

    async def stop(self) -> bool:
        """Cancel the probe task without waking the controller.

        Idempotent — returns False when nothing was running. The task
        reference is cleared before awaiting its cancellation so a
        concurrent ``start()`` cannot observe the stopping task.
        """
        task = self._task
        self._task = None
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "[HibernationProber] stop() — task raised while cancelling",
                exc_info=True,
            )
        logger.info("[HibernationProber] probe loop stopped")
        return True

    def snapshot(self) -> Dict[str, Any]:
        """Lock-free observability snapshot for health()/TUI."""
        elapsed: Optional[float] = None
        if self._started_at is not None:
            elapsed = time.monotonic() - self._started_at
        return {
            "is_probing": self.is_probing,
            "probe_attempts": self._probe_attempts,
            "wake_count": self._wake_count,
            "initial_delay_s": self._initial_delay_s,
            "max_delay_s": self._max_delay_s,
            "max_duration_s": self._max_duration_s,
            "providers": [
                getattr(p, "provider_name", type(p).__name__)
                for p in self._providers
            ],
            "elapsed_s": elapsed,
            "last_result": self._last_result,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _probe_loop(self) -> None:
        """Exponential-backoff probe loop running in its own Task."""
        delay = self._initial_delay_s
        try:
            while True:
                await asyncio.sleep(delay)
                self._probe_attempts += 1

                elapsed = time.monotonic() - (self._started_at or time.monotonic())
                if elapsed > self._max_duration_s:
                    self._last_result = "budget_exhausted"
                    logger.error(
                        "[HibernationProber] budget exhausted after %d "
                        "probes in %.1fs — giving up",
                        self._probe_attempts, elapsed,
                    )
                    return

                healthy_name = await self._probe_any()
                if healthy_name is not None:
                    self._last_result = f"woken_by:{healthy_name}"
                    self._wake_count += 1
                    logger.info(
                        "[HibernationProber] %s healthy after %d probes "
                        "in %.1fs — waking controller",
                        healthy_name, self._probe_attempts, elapsed,
                    )
                    await self._wake(healthy_name)
                    return

                delay = min(delay * 2.0, self._max_delay_s)
                logger.debug(
                    "[HibernationProber] all providers down "
                    "(attempt=%d, next_delay=%.1fs)",
                    self._probe_attempts, delay,
                )
        except asyncio.CancelledError:
            self._last_result = "cancelled"
            logger.debug("[HibernationProber] probe loop cancelled")
            raise
        except Exception:  # noqa: BLE001
            self._last_result = "crash"
            logger.exception("[HibernationProber] probe loop crashed")

    async def _probe_any(self) -> Optional[str]:
        """Probe every provider in order; return the name of the first healthy one.

        Returns ``None`` if every provider is down / raised / lacks a
        ``health_probe`` method. Individual failures are swallowed so
        one broken provider cannot poison the whole probe cycle.
        """
        for provider in self._providers:
            name = getattr(provider, "provider_name", type(provider).__name__)
            probe = getattr(provider, "health_probe", None)
            if probe is None:
                logger.debug(
                    "[HibernationProber] %s has no health_probe, skipping",
                    name,
                )
                continue
            try:
                result = await probe()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[HibernationProber] %s health_probe raised: %s", name, exc,
                )
                continue
            logger.info(
                "[HibernationProber] %s probe → %s",
                name, "UP" if result else "DOWN",
            )
            if result:
                return name
        return None

    async def _wake(self, healthy_name: str) -> None:
        """Call ``controller.wake_from_hibernation`` and swallow errors.

        The controller's own idempotence handles the already-awake
        case; we only log and move on.
        """
        wake = getattr(self._controller, "wake_from_hibernation", None)
        if wake is None:
            logger.error(
                "[HibernationProber] controller has no wake_from_hibernation"
            )
            return
        try:
            await wake(
                reason=(
                    f"probe_success:{healthy_name}:"
                    f"attempts={self._probe_attempts}"
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[HibernationProber] wake_from_hibernation raised"
            )
