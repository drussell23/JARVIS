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

    If ``_safe_mode`` is True, start() enters SAFE_MODE instead of SANDBOX.
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
        """True only when in GOVERNED mode — the only mode that permits writes."""
        return self._mode is AutonomyMode.GOVERNED

    @property
    def sandbox_allowed(self) -> bool:
        """True in SANDBOX or GOVERNED — modes that permit sandboxed execution."""
        return self._mode in (AutonomyMode.SANDBOX, AutonomyMode.GOVERNED)

    @property
    def interactive_allowed(self) -> bool:
        """True in every mode except DISABLED."""
        return self._mode is not AutonomyMode.DISABLED

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
        """Pause autonomy — switch to READ_ONLY."""
        previous = self._mode
        self._mode = AutonomyMode.READ_ONLY
        logger.info("pause() — %s → READ_ONLY", previous.value)

    async def resume(self) -> None:
        """Resume from pause.

        Raises ``RuntimeError`` if the controller is in EMERGENCY_STOP —
        a human must clear the emergency before resuming.
        """
        if self._mode is AutonomyMode.EMERGENCY_STOP:
            logger.error(
                "resume() blocked — EMERGENCY_STOP is active (reason: %s)",
                self._emergency_reason,
            )
            raise RuntimeError(
                f"Cannot resume from emergency stop: {self._emergency_reason}"
            )
        previous = self._mode
        self._mode = AutonomyMode.SANDBOX
        logger.info("resume() — %s → SANDBOX", previous.value)

    async def enable_governed_autonomy(self) -> None:
        """Promote to GOVERNED mode (writes allowed).

        Raises ``RuntimeError`` if the governance gates have not been
        passed via :meth:`mark_gates_passed`.
        """
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
