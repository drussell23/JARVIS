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
# Slice 242 — adaptive statistical recovery-duration prior gate (graduated-on,
# mirrors JARVIS_DW_DYNAMIC_RECOVERY_ENABLED). =0 → byte-identical static-5s path.
_ENV_PRIOR_ENABLED = "JARVIS_RECOVERY_PRIOR_ENABLED"

_DEFAULT_INITIAL_S = 5.0
_DEFAULT_MAX_S = 300.0
_DEFAULT_DURATION_S = 3600.0


def _recovery_prior_enabled() -> bool:
    """Master gate for the adaptive first-probe interval. NEVER raises."""
    try:
        return os.getenv(_ENV_PRIOR_ENABLED, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


# Slice 243 — Adaptive Grid Stability Matrix. A 200-OK ping is insufficient to
# prove a flapping grid can carry a heavy PLAN-EXPLOIT stream; the gate runs a
# micro-streaming load test before waking. =0 → byte-identical wake-on-ping.
_ENV_STABILITY_GATE = "JARVIS_GRID_STABILITY_GATE_ENABLED"
_ENV_STABILITY_CHECKS = "JARVIS_GRID_STABILITY_STREAM_CHECKS"
_DEFAULT_STABILITY_CHECKS = 1


def _stability_gate_enabled() -> bool:
    """Master gate for micro-streaming stability verification. NEVER raises."""
    try:
        return os.getenv(_ENV_STABILITY_GATE, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def _stability_stream_checks() -> int:
    """Consecutive clean micro-streams required to declare the grid stable.
    Dynamic confidence knob. NEVER raises (floors at 1)."""
    try:
        v = int(float(os.getenv(_ENV_STABILITY_CHECKS, "").strip() or _DEFAULT_STABILITY_CHECKS))
        return v if v >= 1 else _DEFAULT_STABILITY_CHECKS
    except (TypeError, ValueError):
        return _DEFAULT_STABILITY_CHECKS


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
        # Slice 243 — the provider the last ping found healthy, so the stability
        # gate can run its micro-streaming load test against the same endpoint.
        self._last_healthy_provider: Optional[Any] = None

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

    def _first_probe_delay(self) -> float:
        """The interval before the FIRST probe of a dark grid.

        Slice 242: when the recovery-prior is enabled and has enough outage
        history, derive it from a low quantile (p25) of past dark-window
        durations — don't ping before the grid plausibly recovers. Below
        ``min_samples`` (or gate off) → the static ``initial_delay_s``. Fully
        fail-soft: any error falls back to the static default."""
        if not _recovery_prior_enabled():
            return self._initial_delay_s
        try:
            from backend.core.ouroboros.governance.dw_transport_recovery import (
                get_recovery_prior,
            )

            derived = get_recovery_prior().first_probe_interval(
                default_s=self._initial_delay_s,
                max_s=self._max_delay_s,
            )
            if derived != self._initial_delay_s:
                logger.info(
                    "[HibernationProber] adaptive first-probe delay=%.1fs "
                    "(static default=%.1fs) — derived from outage history",
                    derived, self._initial_delay_s,
                )
            return derived
        except Exception:  # noqa: BLE001 — never starve probing on a prior error
            return self._initial_delay_s

    def _record_outage_duration(self, elapsed_s: float) -> None:
        """Record the just-ended dark-window duration so the NEXT outage's
        first probe is timed from history. NEVER raises."""
        if not _recovery_prior_enabled():
            return
        try:
            from backend.core.ouroboros.governance.dw_transport_recovery import (
                get_recovery_prior,
            )

            get_recovery_prior().record(elapsed_s)
        except Exception:  # noqa: BLE001
            pass

    async def _probe_loop(self) -> None:
        """Exponential-backoff probe loop running in its own Task."""
        delay = self._first_probe_delay()
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
                    # Slice 243 — a ping is not enough: prove the grid can carry
                    # a heavy stream before waking the DAG. A flapping grid fails
                    # here and we fall through to the backoff loop untouched.
                    stable = await self._verify_grid_stability(
                        self._last_healthy_provider, healthy_name,
                    )
                    if stable:
                        self._last_result = f"woken_by:{healthy_name}"
                        self._wake_count += 1
                        # Slice 242: the dark window just ended — feed its
                        # observed duration into the prior so the NEXT outage's
                        # first probe is timed from history, not a static guess.
                        # Only a STABLE resurrection banks the duration — a flap
                        # is not the end of the outage.
                        self._record_outage_duration(elapsed)
                        logger.info(
                            "[HibernationProber] %s healthy + stream-stable "
                            "after %d probes in %.1fs — waking controller",
                            healthy_name, self._probe_attempts, elapsed,
                        )
                        await self._wake(healthy_name)
                        return
                    self._last_result = "flapping_grid"

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
                self._last_healthy_provider = provider
                return name
        return None

    async def _verify_grid_stability(self, provider: Any, name: str) -> bool:
        """Slice 243 — the Stability Confidence Gate.

        A 200-OK ping only proves the grid is *reachable*, not that it can carry
        a heavy PLAN-EXPLOIT stream — DW's primary failure mode is a stateless
        socket that drops mid-stream (a *flapping* grid). Before waking, run a
        Micro-Streaming Load Test: request a lightweight multi-token stream and
        require ``_stability_stream_checks()`` consecutive clean completions.
        A mid-flight rupture (raise) or incomplete stream (falsy) →
        FLAPPING_GRID_DETECTED, abort the wake, return to the backoff loop —
        the hibernated WAL intent is never disturbed.

        Providers without a ``stream_health_probe`` (Prime/Claude) cannot be
        stream-tested → trust the ping (legacy wake-on-ping). NEVER raises."""
        if not _stability_gate_enabled():
            return True
        stream_probe = getattr(provider, "stream_health_probe", None)
        if stream_probe is None:
            return True  # legacy contract — no micro-stream available
        checks = _stability_stream_checks()
        logger.info(
            "[HibernationProber] %s VERIFYING_STABILITY — micro-streaming load "
            "test (%d clean stream(s) required)", name, checks,
        )
        for i in range(checks):
            try:
                ok = await stream_probe()
            except Exception as exc:  # noqa: BLE001 — rupture == flap
                logger.warning(
                    "[HibernationProber] FLAPPING_GRID_DETECTED — %s stream "
                    "ruptured mid-flight on check %d/%d (%s) — aborting wake, "
                    "returning to backoff", name, i + 1, checks, exc,
                )
                return False
            if not ok:
                logger.warning(
                    "[HibernationProber] FLAPPING_GRID_DETECTED — %s stream "
                    "incomplete on check %d/%d — aborting wake, returning to "
                    "backoff", name, i + 1, checks,
                )
                return False
        logger.info(
            "[HibernationProber] %s stream-stable (%d/%d clean) — grid ready "
            "for heavy work", name, checks, checks,
        )
        return True

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
