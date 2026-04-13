"""
CapabilityGapSensor — Ouroboros intake sensor for capability gap events.

Follows the OpportunityMinerSensor pattern exactly:
- Standalone class, async _poll_loop(), no base class.
- Uses make_envelope() from intent_envelope (same as all other sensors).
- Registered in agent_initializer.py at startup.
"""
from __future__ import annotations

import asyncio
import logging

from backend.neural_mesh.synthesis.gap_signal_bus import (
    CapabilityGapEvent,
    GapSignalBus,
    get_gap_signal_bus,
)
from backend.neural_mesh.synthesis.gap_resolution_protocol import (
    GapResolutionProtocol,
    ResolutionMode,
)
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)


class CapabilityGapSensor:
    """Intake sensor that converts CapabilityGapEvents into Ouroboros envelopes.

    Listens on the GapSignalBus and submits an IntentEnvelope to the intake
    router for each event.  The urgency and requires_human_ack fields are
    driven by GapResolutionProtocol.classify_mode() — no hardcoding.

    Parameters
    ----------
    intake_router:
        UnifiedIntakeRouter (or any object with an async ``submit`` method).
    repo:
        Repository name passed through to make_envelope.
    bus:
        GapSignalBus to consume from.  Defaults to the process singleton.
    """

    def __init__(
        self,
        intake_router,
        repo: str,
        bus: GapSignalBus | None = None,
    ) -> None:
        self._router = intake_router
        self._repo = repo
        self._gap_bus: GapSignalBus = bus if bus is not None else get_gap_signal_bus()
        self._protocol = GapResolutionProtocol()
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Schedule the background poll loop as an asyncio task."""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="capability_gap_sensor_poll",
        )

    async def stop(self) -> None:
        """Cancel the poll task and await its cleanup.

        Battle test bt-2026-04-13-031119 surfaced an AttributeError
        at session teardown because this sensor shipped without a
        ``stop()`` and ``SensorRegistry.stop_all`` attempted to call
        it like every other sensor. Tracking the task handle lets
        teardown deterministically drain instead of leaking a
        `Task was destroyed but pending` warning.
        """
        task = self._poll_task
        self._poll_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _poll_loop(self) -> None:
        """Continuously consume events from the bus and forward them as envelopes."""
        while True:
            try:
                event = await self._gap_bus.get()
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("CapabilityGapSensor: error in poll loop")

    async def _poll_once(self) -> None:
        """Consume a single event — used in unit tests."""
        event = await self._gap_bus.get()
        await self._handle(event)

    async def _handle(self, event: CapabilityGapEvent) -> None:
        mode = self._protocol.classify_mode(event)
        urgency = "high" if mode == ResolutionMode.B else "low"
        requires_ack = mode == ResolutionMode.A
        try:
            envelope = make_envelope(
                source="capability_gap",
                description=f"Synthesize agent for {event.task_type}:{event.target_app}",
                target_files=(
                    f"backend/neural_mesh/synthesis/agents/{event.domain_id}.py",
                ),
                repo=self._repo,
                confidence=0.9,
                urgency=urgency,
                evidence={
                    "task_type": event.task_type,
                    "target_app": event.target_app,
                    "dedupe_key": event.dedupe_key,
                    "attempt_key": event.attempt_key,
                    "resolution_mode": mode.value,
                    "domain_id": event.domain_id,
                },
                requires_human_ack=requires_ack,
            )
            await self._router.submit(envelope)
        except Exception:
            logger.exception(
                "CapabilityGapSensor: failed to submit envelope domain_id=%s",
                event.domain_id,
            )
