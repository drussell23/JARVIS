"""Phase 3 — REM Sleep Daemon: idle-watch state machine for Ouroboros Zone 7.0.

The RemSleepDaemon owns the background loop that watches for system idle time
and triggers REM epochs.  It is the entry point for proactive, unsupervised
codebase exploration.

Lifecycle
---------
1. ``start()`` creates a background asyncio.Task running ``_daemon_loop()``.
2. The loop first awaits ``spinal_cord.wait_for_gate()`` (blocks until Phase 2
   SpinalCord is wired — even in degraded/local mode).
3. It registers an idle callback on ``proactive_drive.on_eligible(idle_event.set)``
   so any idle transition fires the event automatically.
4. The main loop then cycles:
   IDLE_WATCH → wait for idle event → EXPLORING (run epoch) → COOLDOWN → sleep → repeat.
5. ``stop()`` cancels the token and the background task, awaiting graceful shutdown.
6. ``pause()`` cancels only the current CancellationToken for cooperative yield
   (e.g. user activity resumed).

Usage::

    daemon = RemSleepDaemon(
        oracle=oracle,
        fleet=fleet,
        spinal_cord=spinal_cord,
        intake_router=intake_router,
        proactive_drive=proactive_drive,
        doubleword=doubleword,
        config=config,
    )
    await daemon.start()
    ...
    await daemon.stop()
"""
from __future__ import annotations

import asyncio
import enum
import itertools
import logging
from typing import Any, Dict, Optional

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.rem_epoch import EpochResult, RemEpoch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RemState
# ---------------------------------------------------------------------------


class RemState(enum.Enum):
    """State of the REM Sleep Daemon state machine."""

    IDLE_WATCH = "idle_watch"
    EXPLORING = "exploring"
    ANALYZING = "analyzing"
    PATCHING = "patching"
    COOLDOWN = "cooldown"


# ---------------------------------------------------------------------------
# RemSleepDaemon
# ---------------------------------------------------------------------------


class RemSleepDaemon:
    """Background daemon that watches for idle state and triggers REM epochs.

    Parameters
    ----------
    oracle:
        TheOracle instance for codebase graph queries; forwarded to each epoch.
    fleet:
        Agent fleet providing ExplorationSubagents; forwarded to each epoch.
    spinal_cord:
        SpinalCord for streaming findings upward.
    intake_router:
        Governance intake router for submitting IntentEnvelopes.
    proactive_drive:
        ProactiveDrive whose ``on_eligible`` registers the idle callback.
    doubleword:
        Optional Doubleword provider for deep analysis; forwarded to each epoch.
    config:
        DaemonConfig controlling cooldown, timeouts, and agent limits.
    """

    def __init__(
        self,
        oracle: Any,
        fleet: Any,
        spinal_cord: Any,
        intake_router: Any,
        proactive_drive: Any,
        doubleword: Any,
        config: Any,
        hypothesis_cache_dir: Any = None,
        architect: Any = None,
        narrator: Any = None,
    ) -> None:
        self._oracle = oracle
        self._fleet = fleet
        self._spinal_cord = spinal_cord
        self._intake_router = intake_router
        self._proactive_drive = proactive_drive
        self._doubleword = doubleword
        self._config = config
        self._hypothesis_cache_dir = hypothesis_cache_dir
        self._architect = architect
        self._narrator = narrator

        # State machine
        self._state: RemState = RemState.IDLE_WATCH

        # Monotonically increasing epoch counter
        self._epoch_counter: itertools.count[int] = itertools.count(1)

        # Cooperative cancellation token for the current epoch (if any)
        self._current_token: Optional[CancellationToken] = None

        # Background asyncio.Task handle
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

        # Cumulative metrics
        self._epoch_count: int = 0
        self._total_findings: int = 0
        self._total_envelopes: int = 0
        self._last_epoch_result: Optional[EpochResult] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> RemState:
        """Current state of the REM Sleep state machine."""
        return self._state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the background daemon task and return immediately.

        Idempotent — calling start() when already running is a no-op.
        """
        if self._task is not None and not self._task.done():
            logger.debug("RemSleepDaemon.start() called while already running — no-op")
            return

        self._task = asyncio.create_task(self._daemon_loop(), name="rem_sleep_daemon")
        logger.info("RemSleepDaemon started")

    async def stop(self) -> None:
        """Gracefully cancel the daemon task and await its completion.

        Cancels the current epoch token first to encourage cooperative
        shutdown, then cancels the asyncio.Task itself.
        """
        # Cancel any in-flight epoch cooperatively
        if self._current_token is not None:
            self._current_token.cancel()

        task = self._task
        self._task = None

        if task is None or task.done():
            return

        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        logger.info("RemSleepDaemon stopped")

    def pause(self) -> None:
        """Cooperatively pause the current epoch (e.g. on user activity).

        Cancels the current CancellationToken so the in-flight RemEpoch
        winds down at the next ``token.is_cancelled`` checkpoint.  The
        daemon loop itself continues — it will wait for the next idle event.
        """
        if self._current_token is not None:
            self._current_token.cancel()
            logger.info("RemSleepDaemon: epoch paused (token cancelled)")

    def health(self) -> Dict[str, Any]:
        """Return a health snapshot of the daemon.

        Returns
        -------
        dict with keys:
            state          — current RemState value string
            epoch_count    — total completed epochs
            total_findings — cumulative findings across all epochs
            total_envelopes — cumulative envelopes submitted
            last_epoch     — dict summary of the last EpochResult, or None
        """
        last: Optional[Dict[str, Any]] = None
        if self._last_epoch_result is not None:
            r = self._last_epoch_result
            last = {
                "epoch_id": r.epoch_id,
                "findings": r.findings_count,
                "envelopes_submitted": r.envelopes_submitted,
                "duration_s": r.duration_s,
                "cancelled": r.cancelled,
                "error": r.error,
            }
        return {
            "state": self._state.value,
            "epoch_count": self._epoch_count,
            "total_findings": self._total_findings,
            "total_envelopes": self._total_envelopes,
            "last_epoch": last,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _transition(self, new_state: RemState) -> None:
        """Log and update the state machine."""
        if new_state is not self._state:
            logger.debug(
                "RemSleepDaemon: %s → %s", self._state.value, new_state.value
            )
        self._state = new_state

    def _next_epoch_id(self) -> int:
        """Return the next monotonic epoch identifier."""
        return next(self._epoch_counter)

    async def _run_epoch(self) -> None:
        """Construct and execute a single RemEpoch, then update metrics."""
        epoch_id = self._next_epoch_id()
        self._current_token = CancellationToken(epoch_id=epoch_id)

        await self._spinal_cord.stream_up("rem.epoch_start", {"epoch_id": epoch_id})
        if self._narrator is not None:
            await self._narrator.on_event("rem.epoch_start", {"epoch_id": epoch_id})

        self._transition(RemState.EXPLORING)

        epoch = RemEpoch(
            epoch_id=epoch_id,
            oracle=self._oracle,
            fleet=self._fleet,
            spinal_cord=self._spinal_cord,
            intake_router=self._intake_router,
            doubleword=self._doubleword,
            config=self._config,
            hypothesis_cache_dir=self._hypothesis_cache_dir,
            architect=self._architect,
        )

        try:
            result: EpochResult = await epoch.run(self._current_token)
        except Exception as exc:
            logger.exception("RemSleepDaemon: epoch %d raised: %s", epoch_id, exc)
            self._current_token = None
            return

        # Update cumulative metrics
        self._epoch_count += 1
        self._total_findings += result.findings_count
        self._total_envelopes += result.envelopes_submitted
        self._last_epoch_result = result

        await self._spinal_cord.stream_up("rem.epoch_complete", {
            "epoch_id": epoch_id,
            "findings_count": result.findings_count,
            "envelopes_submitted": result.envelopes_submitted,
            "duration_s": result.duration_s,
        })
        if self._narrator is not None:
            await self._narrator.on_event("rem.epoch_complete", {
                "findings_count": result.findings_count,
                "envelopes_submitted": result.envelopes_submitted,
            })

        self._current_token = None

        logger.info(
            "RemSleepDaemon: epoch %d complete — findings=%d envelopes=%d "
            "duration=%.2fs cancelled=%s error=%s",
            epoch_id,
            result.findings_count,
            result.envelopes_submitted,
            result.duration_s,
            result.cancelled,
            result.error,
        )

    async def _daemon_loop(self) -> None:
        """Main background loop: idle-watch → explore → cooldown → repeat.

        Steps
        -----
        1. Await spinal_cord.wait_for_gate() to block until Phase 2 is ready.
        2. Register idle callback: proactive_drive.on_eligible(idle_event.set).
        3. Loop:
           a. IDLE_WATCH — wait for the idle_event (set by proactive_drive on
              any idle transition).
           b. Clear the event and run an epoch.
           c. COOLDOWN — sleep for config.rem_cooldown_s.
           d. Repeat.
        4. Catch CancelledError and return gracefully.
        """
        try:
            # Phase 2 gate — wait until SpinalCord is wired (even degraded)
            logger.info("RemSleepDaemon: awaiting SpinalCord gate (Phase 2)…")
            await self._spinal_cord.wait_for_gate()
            logger.info("RemSleepDaemon: Phase 2 gate open — registering idle callback")

            # Idle event — set by proactive_drive when all queues become idle
            idle_event: asyncio.Event = asyncio.Event()
            self._proactive_drive.on_eligible(idle_event.set)

            while True:
                # Wait for the system to become idle
                self._transition(RemState.IDLE_WATCH)
                logger.debug("RemSleepDaemon: waiting for idle signal…")
                await idle_event.wait()
                idle_event.clear()

                # Execute one epoch
                await self._run_epoch()

                # Cooldown before watching for the next idle window
                self._transition(RemState.COOLDOWN)
                logger.debug(
                    "RemSleepDaemon: cooldown %.1fs", self._config.rem_cooldown_s
                )
                await asyncio.sleep(self._config.rem_cooldown_s)

        except asyncio.CancelledError:
            logger.info("RemSleepDaemon: daemon loop cancelled — exiting cleanly")
            return
