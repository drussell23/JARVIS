"""
Intent Engine — Central Orchestrator
======================================

Routes :class:`IntentSignal` instances through deduplication, rate limiting,
and governance submission.  The engine ties together every Layer 1 component:

* :class:`DedupTracker` — suppress duplicate signals within a cooldown window.
* :class:`RateLimiter` — enforce per-file, per-signal, hourly, and daily caps.
* :class:`TestWatcher` — poll pytest for stable failures (lazily imported).

Stable test-failure signals are auto-submitted to the
:class:`GovernedLoopService` pipeline.  All other signals are observed and
optionally narrated via a caller-supplied ``narrate_fn``.

Configuration
-------------

All knobs are driven by environment variables via
:meth:`IntentEngineConfig.from_env`, ensuring zero hard-coding:

.. envvar:: JARVIS_REPO_PATH
.. envvar:: JARVIS_PRIME_REPO_PATH
.. envvar:: JARVIS_REACTOR_REPO_PATH
.. envvar:: JARVIS_INTENT_TEST_INTERVAL_S
.. envvar:: JARVIS_INTENT_DEDUP_COOLDOWN_S
.. envvar:: JARVIS_INTENT_MAX_OPS_HOUR
.. envvar:: JARVIS_INTENT_MAX_OPS_DAY
.. envvar:: JARVIS_INTENT_FILE_COOLDOWN_S
.. envvar:: JARVIS_INTENT_SIGNAL_COOLDOWN_S
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .rate_limiter import RateLimiter, RateLimiterConfig
from .signals import DedupTracker, IntentSignal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentEngineConfig:
    """Immutable configuration for :class:`IntentEngine`.

    Parameters
    ----------
    repos:
        Mapping of ``repo_name -> repo_path`` for all watched repositories.
    test_dirs:
        Mapping of ``repo_name -> test_directory`` (relative to repo root).
    poll_interval_s:
        Seconds between TestWatcher polling cycles.
    dedup_cooldown_s:
        Minimum seconds between accepting two signals with the same dedup key.
    max_ops_per_hour:
        Maximum autonomous operations allowed within a rolling 1-hour window.
    max_ops_per_day:
        Maximum autonomous operations allowed within a rolling 24-hour window.
    file_cooldown_s:
        Minimum seconds between operations targeting the same file.
    signal_cooldown_s:
        Minimum seconds between operations triggered by the same signal key.
    """

    repos: Dict[str, str] = field(default_factory=dict)
    test_dirs: Dict[str, str] = field(default_factory=dict)
    poll_interval_s: float = 300.0
    dedup_cooldown_s: float = 300.0
    max_ops_per_hour: int = 5
    max_ops_per_day: int = 20
    file_cooldown_s: float = 600.0
    signal_cooldown_s: float = 300.0

    @classmethod
    def from_env(cls) -> IntentEngineConfig:
        """Build a config from environment variables, falling back to defaults.

        Environment Variables
        ---------------------
        JARVIS_REPO_PATH : str
            Path to the main JARVIS repository (default ``"."``).
        JARVIS_PRIME_REPO_PATH : str
            Path to the prime repository (omitted from repos if unset).
        JARVIS_REACTOR_REPO_PATH : str
            Path to the reactor-core repository (omitted from repos if unset).
        JARVIS_INTENT_TEST_INTERVAL_S : float
            Poll interval in seconds (default 300).
        JARVIS_INTENT_DEDUP_COOLDOWN_S : float
            Dedup cooldown in seconds (default 300).
        JARVIS_INTENT_MAX_OPS_HOUR : int
            Hourly operation cap (default 5).
        JARVIS_INTENT_MAX_OPS_DAY : int
            Daily operation cap (default 20).
        JARVIS_INTENT_FILE_COOLDOWN_S : float
            Per-file cooldown in seconds (default 600).
        JARVIS_INTENT_SIGNAL_COOLDOWN_S : float
            Per-signal cooldown in seconds (default 300).
        """
        defaults = cls()

        repos: Dict[str, str] = {}
        test_dirs: Dict[str, str] = {}

        # Always include the main repo
        jarvis_path = os.environ.get("JARVIS_REPO_PATH", ".")
        repos["jarvis"] = jarvis_path
        test_dirs["jarvis"] = "tests/"

        # Optional additional repos
        prime_path = os.environ.get("JARVIS_PRIME_REPO_PATH")
        if prime_path:
            repos["prime"] = prime_path
            test_dirs["prime"] = "tests/"

        reactor_path = os.environ.get("JARVIS_REACTOR_REPO_PATH")
        if reactor_path:
            repos["reactor-core"] = reactor_path
            test_dirs["reactor-core"] = "tests/"

        return cls(
            repos=repos,
            test_dirs=test_dirs,
            poll_interval_s=float(
                os.environ.get(
                    "JARVIS_INTENT_TEST_INTERVAL_S", defaults.poll_interval_s
                )
            ),
            dedup_cooldown_s=float(
                os.environ.get(
                    "JARVIS_INTENT_DEDUP_COOLDOWN_S", defaults.dedup_cooldown_s
                )
            ),
            max_ops_per_hour=int(
                os.environ.get(
                    "JARVIS_INTENT_MAX_OPS_HOUR", defaults.max_ops_per_hour
                )
            ),
            max_ops_per_day=int(
                os.environ.get(
                    "JARVIS_INTENT_MAX_OPS_DAY", defaults.max_ops_per_day
                )
            ),
            file_cooldown_s=float(
                os.environ.get(
                    "JARVIS_INTENT_FILE_COOLDOWN_S", defaults.file_cooldown_s
                )
            ),
            signal_cooldown_s=float(
                os.environ.get(
                    "JARVIS_INTENT_SIGNAL_COOLDOWN_S", defaults.signal_cooldown_s
                )
            ),
        )


# ---------------------------------------------------------------------------
# Helper: build OperationContext from a signal
# ---------------------------------------------------------------------------


def _build_operation_context(signal: IntentSignal) -> Any:
    """Create an :class:`OperationContext` from an :class:`IntentSignal`.

    Lazily imports ``OperationContext`` and ``generate_operation_id`` to
    avoid circular imports at module load time.

    Parameters
    ----------
    signal:
        The intent signal to convert into an operation context.

    Returns
    -------
    OperationContext
        A new context in the CLASSIFY phase, targeting the signal's files.
    """
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.operation_id import generate_operation_id

    op_id = generate_operation_id(signal.repo)

    return OperationContext.create(
        target_files=signal.target_files,
        description=signal.description,
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# IntentEngine
# ---------------------------------------------------------------------------


class IntentEngine:
    """Central orchestrator for the Intent Engine (Layer 1).

    Ties together deduplication, rate limiting, test watching, and governed
    pipeline submission into a single async-first coordinator.

    Parameters
    ----------
    config:
        Engine configuration (repos, intervals, limits).
    governed_loop_service:
        The :class:`GovernedLoopService` instance to submit operations to.
    narrate_fn:
        Optional async callable for voice narration (e.g. ``safe_say``).
        Called when signals are observed but not auto-submitted.
    """

    def __init__(
        self,
        config: IntentEngineConfig,
        governed_loop_service: Any,
        narrate_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = config
        self._gls = governed_loop_service
        self._narrate_fn = narrate_fn

        self._state: str = "inactive"
        self._dedup = DedupTracker(cooldown_s=config.dedup_cooldown_s)
        self._rate_limiter = RateLimiter(
            config=RateLimiterConfig(
                max_ops_per_hour=config.max_ops_per_hour,
                max_ops_per_day=config.max_ops_per_day,
                per_file_cooldown_s=config.file_cooldown_s,
                per_signal_cooldown_s=config.signal_cooldown_s,
            )
        )
        self._watchers: Dict[str, Any] = {}
        self._tasks: List[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current engine state: ``"inactive"``, ``"watching"``, or ``"active"``."""
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the engine and begin watching all configured repositories.

        Transitions from ``"inactive"`` to ``"watching"``.  Creates a
        :class:`TestWatcher` for each configured repo.  If the engine is
        already started, this is a no-op.
        """
        if self._state != "inactive":
            return

        # Lazy import to avoid import-time side effects
        from .test_watcher import TestWatcher

        self._state = "watching"

        for repo_name, repo_path in self._config.repos.items():
            test_dir = self._config.test_dirs.get(repo_name, "tests/")
            watcher = TestWatcher(
                repo=repo_name,
                test_dir=test_dir,
                repo_path=repo_path,
                poll_interval_s=self._config.poll_interval_s,
            )
            self._watchers[repo_name] = watcher

        logger.info(
            "IntentEngine started: state=%s repos=%s",
            self._state,
            list(self._watchers.keys()),
        )

    def stop(self) -> None:
        """Stop the engine, cancel background tasks, and release resources.

        Transitions to ``"inactive"`` regardless of current state.  Stops all
        watchers and cancels all tracked async tasks.
        """
        for watcher in self._watchers.values():
            watcher.stop()

        for task in self._tasks:
            if not task.done():
                task.cancel()

        self._watchers.clear()
        self._tasks.clear()
        self._state = "inactive"

        logger.info("IntentEngine stopped")

    # ------------------------------------------------------------------
    # Signal routing
    # ------------------------------------------------------------------

    async def handle_signal(self, signal: IntentSignal) -> str:
        """Route an intent signal through dedup, rate limiting, and submission.

        This is the core decision function.  The routing logic is:

        1. **Dedup check** -- reject if the signal was recently seen.
        2. **Rate limit check** -- reject if throughput caps are exceeded.
        3. **Mode check**:
           - If ``source == "intent:test_failure"`` AND ``stable``: auto-submit
             to the governed pipeline.
           - Otherwise: observe only (optionally narrate).

        Parameters
        ----------
        signal:
            The intent signal to process.

        Returns
        -------
        str
            One of ``"submitted"``, ``"deduplicated"``, ``"rate_limited"``,
            or ``"observed"``.
        """
        # 1. Dedup check
        if not self._dedup.is_new(signal):
            logger.debug(
                "Signal deduplicated: %s (key=%s)",
                signal.description,
                signal.dedup_key,
            )
            return "deduplicated"

        # 2. Rate limit check (use first target file for per-file check)
        file_path = signal.target_files[0] if signal.target_files else ""
        allowed, reason = self._rate_limiter.check(
            file_path=file_path,
            signal_key=signal.dedup_key,
        )
        if not allowed:
            logger.info(
                "Signal rate-limited (%s): %s", reason, signal.description
            )
            return "rate_limited"

        # 3. Mode check
        if signal.source == "intent:test_failure" and signal.stable:
            return await self._auto_submit(signal, file_path)
        else:
            return await self._observe(signal)

    # ------------------------------------------------------------------
    # Internal routing targets
    # ------------------------------------------------------------------

    async def _auto_submit(self, signal: IntentSignal, file_path: str) -> str:
        """Submit a stable test-failure signal to the governed pipeline.

        Sets engine state to ``"active"`` during submission, restoring the
        previous state in the ``finally`` block.

        Parameters
        ----------
        signal:
            The stable test-failure signal to submit.
        file_path:
            Primary target file (used for rate-limiter recording).

        Returns
        -------
        str
            ``"submitted"`` on success, ``"observed"`` on failure.
        """
        previous_state = self._state
        try:
            self._state = "active"
            ctx = _build_operation_context(signal)
            await self._gls.submit(ctx, trigger_source=signal.source)
            self._rate_limiter.record(
                file_path=file_path,
                signal_key=signal.dedup_key,
            )
            logger.info(
                "Signal submitted to GLS: %s (op_id=%s)",
                signal.description,
                ctx.op_id,
            )
            return "submitted"
        except Exception:
            logger.exception(
                "Failed to submit signal to GLS: %s", signal.description
            )
            return "observed"
        finally:
            self._state = previous_state

    async def _observe(self, signal: IntentSignal) -> str:
        """Observe a signal without submitting it.

        If a ``narrate_fn`` is available, calls it with a descriptive message.

        Parameters
        ----------
        signal:
            The signal to observe.

        Returns
        -------
        str
            Always ``"observed"``.
        """
        logger.info("Signal observed (no auto-submit): %s", signal.description)

        if self._narrate_fn is not None:
            message = (
                f"Detected {signal.source} signal in {signal.repo}: "
                f"{signal.description}"
            )
            try:
                await self._narrate_fn(message)
            except Exception:
                logger.exception("narrate_fn failed for signal: %s", signal.description)

        return "observed"

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll_all(self) -> List[str]:
        """Poll all watchers and handle any emitted signals.

        Returns
        -------
        List[str]
            List of routing outcomes (one per signal processed).
        """
        results: List[str] = []

        for _repo_name, watcher in self._watchers.items():
            try:
                signals = await watcher.poll_once()
            except Exception:
                logger.exception("poll_once failed for watcher: %s", _repo_name)
                continue

            for signal in signals:
                try:
                    outcome = await self.handle_signal(signal)
                    results.append(outcome)
                except Exception:
                    logger.exception(
                        "handle_signal failed for: %s", signal.description
                    )

        return results
