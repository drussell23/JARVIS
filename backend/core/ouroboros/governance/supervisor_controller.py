"""
Supervisor Ouroboros Controller — Lifecycle Authority
=====================================================

The single authority for starting, stopping, pausing, and resuming
Ouroboros autonomy.  ``unified_supervisor.py`` delegates all autonomy
lifecycle decisions to this controller.

State machine::

    DISABLED ──start()──► SANDBOX ──enable_governed_autonomy()──► GOVERNED
       ▲                    │  ▲                                      │
       │                    │  │                                      │
     stop()            pause() resume()                           pause()
       │                    │  │                                      │
       │                    ▼  │                                      ▼
       ◄──── stop() ◄── READ_ONLY ◄─────────────────────────────  READ_ONLY
                            │
                     emergency_stop()
                            │
                            ▼
                     EMERGENCY_STOP  (resume() raises RuntimeError)

    GOVERNED ◄───wake_from_hibernation()─── HIBERNATION ◄──enter_hibernation()── GOVERNED

    If ``_safe_mode`` is True, start() enters SAFE_MODE instead of SANDBOX.

HIBERNATION_MODE
----------------
A special sibling of READ_ONLY that the controller enters when the
provider substrate (DoubleWord, Claude) is unreachable.  The BG pool is
paused, the idle watchdog is frozen, and no new sandbox or governed
operations are accepted — but interactive surfaces (REPL, voice, CLI)
remain responsive so Derek can still inspect state.  When health probes
confirm providers are back, ``wake_from_hibernation()`` restores the
prior mode (GOVERNED) and resumes the DAG exactly where it left off.
"""

from __future__ import annotations

import enum
import logging
from typing import Optional

logger = logging.getLogger("Ouroboros.Controller")


class AutonomyMode(enum.Enum):
    """Operating modes for the Ouroboros autonomy lifecycle."""

    DISABLED = "DISABLED"
    SANDBOX = "SANDBOX"
    READ_ONLY = "READ_ONLY"
    GOVERNED = "GOVERNED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    SAFE_MODE = "SAFE_MODE"
    # HIBERNATION: entered when the provider substrate is exhausted.
    # No writes, no sandbox ops, no new generation — but interactive
    # surfaces still work so the operator can inspect state and the
    # health prober can wake the organism when providers recover.
    HIBERNATION = "HIBERNATION"


class SupervisorOuroborosController:
    """Single lifecycle authority for JARVIS self-programming autonomy.

    Only this class may start, stop, pause, or resume the Ouroboros loop.
    ``unified_supervisor.py`` delegates to an instance of this controller
    rather than managing autonomy state directly.
    """

    def __init__(self) -> None:
        self._mode: AutonomyMode = AutonomyMode.DISABLED
        self._safe_mode: bool = False
        self._gates_passed: bool = False
        self._emergency_reason: Optional[str] = None
        logger.info("SupervisorOuroborosController initialised — mode=%s", self._mode.value)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mode(self) -> AutonomyMode:
        """Current autonomy mode."""
        return self._mode

    @property
    def writes_allowed(self) -> bool:
        """True only when in GOVERNED mode — the only mode that permits writes.

        HIBERNATION explicitly returns False: the provider substrate is
        down, no generation is possible, and the BG pool is paused.
        """
        return self._mode is AutonomyMode.GOVERNED

    @property
    def sandbox_allowed(self) -> bool:
        """True in SANDBOX or GOVERNED — modes that permit sandboxed execution.

        HIBERNATION excluded: new sandbox ops cannot make progress without
        providers, so admitting them would only pile up stale work.
        """
        return self._mode in (AutonomyMode.SANDBOX, AutonomyMode.GOVERNED)

    @property
    def interactive_allowed(self) -> bool:
        """True in every mode except DISABLED.

        HIBERNATION keeps interactive surfaces live so the operator can
        still inspect health, read logs, and force a wake if needed.
        """
        return self._mode is not AutonomyMode.DISABLED

    @property
    def is_hibernating(self) -> bool:
        """True while the controller is in HIBERNATION mode."""
        return self._mode is AutonomyMode.HIBERNATION

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the autonomy loop.

        If ``_safe_mode`` is set, enters SAFE_MODE (read-only with
        interactive access).  Otherwise enters SANDBOX.
        """
        if self._safe_mode:
            self._mode = AutonomyMode.SAFE_MODE
            logger.warning("start() — entering SAFE_MODE (safe-mode flag is set)")
        else:
            self._mode = AutonomyMode.SANDBOX
            logger.info("start() — entering SANDBOX")

    async def stop(self) -> None:
        """Stop the autonomy loop and reset all transient state."""
        previous = self._mode
        self._mode = AutonomyMode.DISABLED
        self._gates_passed = False
        logger.info("stop() — %s → DISABLED (gates_passed reset)", previous.value)

    async def pause(self) -> None:
        """Pause autonomy — switch to READ_ONLY.

        Refuses to run during HIBERNATION: pause() and hibernation serve
        different purposes (operator-initiated vs. provider-outage), and
        mixing them corrupts the state machine. Use wake_from_hibernation()
        to leave HIBERNATION.
        """
        if self._mode is AutonomyMode.HIBERNATION:
            logger.error("pause() blocked — controller is HIBERNATING")
            raise RuntimeError(
                "Cannot pause while HIBERNATING — use wake_from_hibernation() first"
            )
        previous = self._mode
        self._mode = AutonomyMode.READ_ONLY
        logger.info("pause() — %s → READ_ONLY", previous.value)

    async def resume(self) -> None:
        """Resume from pause.

        Raises ``RuntimeError`` if the controller is in EMERGENCY_STOP —
        a human must clear the emergency before resuming.  Also refuses
        HIBERNATION: the dedicated ``wake_from_hibernation()`` entry
        point (landing in a later step) is the only way out.
        """
        if self._mode is AutonomyMode.EMERGENCY_STOP:
            logger.error(
                "resume() blocked — EMERGENCY_STOP is active (reason: %s)",
                self._emergency_reason,
            )
            raise RuntimeError(
                f"Cannot resume from emergency stop: {self._emergency_reason}"
            )
        if self._mode is AutonomyMode.HIBERNATION:
            logger.error("resume() blocked — controller is HIBERNATING")
            raise RuntimeError(
                "Cannot resume from HIBERNATION — use wake_from_hibernation()"
            )
        previous = self._mode
        self._mode = AutonomyMode.SANDBOX
        logger.info("resume() — %s → SANDBOX", previous.value)

    async def enable_governed_autonomy(self) -> None:
        """Promote to GOVERNED mode (writes allowed).

        Raises ``RuntimeError`` if the governance gates have not been
        passed via :meth:`mark_gates_passed`, or if the controller is
        currently HIBERNATING (wake first).
        """
        if self._mode is AutonomyMode.HIBERNATION:
            logger.error(
                "enable_governed_autonomy() blocked — controller is HIBERNATING"
            )
            raise RuntimeError(
                "Cannot enable governed autonomy while HIBERNATING — "
                "wake_from_hibernation() first"
            )
        if not self._gates_passed:
            logger.error("enable_governed_autonomy() blocked — gates not passed")
            raise RuntimeError(
                "Cannot enable governed autonomy: gates have not been passed"
            )
        previous = self._mode
        self._mode = AutonomyMode.GOVERNED
        logger.info("enable_governed_autonomy() — %s → GOVERNED", previous.value)

    async def mark_gates_passed(self) -> None:
        """Record that all governance gates have been satisfied."""
        self._gates_passed = True
        logger.info("mark_gates_passed() — governance gates satisfied")

    async def emergency_stop(self, reason: str) -> None:
        """Immediately halt all autonomy.

        Stores *reason* and transitions to EMERGENCY_STOP.  Any
        subsequent :meth:`resume` will raise ``RuntimeError`` until
        the emergency is manually cleared.
        """
        self._emergency_reason = reason
        previous = self._mode
        self._mode = AutonomyMode.EMERGENCY_STOP
        logger.critical(
            "emergency_stop() — %s → EMERGENCY_STOP (reason: %s)",
            previous.value,
            reason,
        )
