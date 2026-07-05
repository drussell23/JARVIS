"""Remote intake path (Stage-2 Task 3): Body shim + Brain bridge.

Both classes live in ONE file because they are the two halves of a single
wire contract -- the topic constant and the payload shape
(``IntentEnvelope.to_dict()``) must never drift apart:

  * ``RemoteIntakeRouter`` (Body / Mac side): a router-shaped shim that
    duck-types the ONE method sensors call -- ``async def ingest(envelope)``
    -- and publishes the envelope dict onto the local TrinityEventBus.
    Fire-and-forget: the Brain's REAL ``UnifiedIntakeRouter`` owns dedup,
    ack-parking, and backpressure.
  * ``RemoteIntakeBridge`` (Brain side): subscribes ``TOPIC_REMOTE_SIGNAL``
    on the organism's TrinityEventBus, rehydrates ``IntentEnvelope`` from
    each payload, and feeds it into the injected real router. Malformed
    payloads are logged-and-dropped -- the bus loop must never crash.

Boot seam: ``OrganismBusHost.start()`` constructs + starts the bridge when
its ``router`` param is not None (Task-2 reserved it for exactly this).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import IntentEnvelope

logger = logging.getLogger(__name__)

#: One wire contract, one topic. Body sensors publish here; the Brain bridge
#: subscribes here. Matches the ``intake.remote_signal.*`` outbound allowlist
#: pattern on the Mac-side TrinityBusBridge.
TOPIC_REMOTE_SIGNAL = "intake.remote_signal.body"


class RemoteIntakeRouter:
    """Body-side shim: looks like a router to sensors, publishes to the bus.

    Duck-types ONLY ``async def ingest(envelope) -> str``. Fail-soft: a
    publish failure returns ``"backpressure"`` (a valid router verdict --
    surfaces degradation without crashing the calling sensor).
    """

    def __init__(self, trinity_bus: Any) -> None:
        self._bus = trinity_bus

    async def ingest(self, envelope: Any) -> str:
        try:
            await self._bus.publish_raw(TOPIC_REMOTE_SIGNAL, envelope.to_dict())
        except Exception:  # noqa: BLE001 -- fail-soft, never crash the sensor
            logger.warning(
                "[RemoteIntakeRouter] publish failed -- returning backpressure",
                exc_info=True,
            )
            return "backpressure"
        return "enqueued"


class RemoteIntakeBridge:
    """Brain-side subscriber: remote envelope dicts -> the REAL router.

    Deliberately does NOT dedup -- ``UnifiedIntakeRouter._is_duplicate``
    owns replay suppression; re-implementing it here would mask the real
    contract (and drift).
    """

    def __init__(self, trinity_bus: Any, router: Any) -> None:
        self._bus = trinity_bus
        self._router = router
        self._sub_id: Optional[str] = None

    async def start(self) -> None:
        if self._sub_id is not None:
            return
        self._sub_id = await self._bus.subscribe(
            TOPIC_REMOTE_SIGNAL, self._on_signal)

    async def stop(self) -> None:
        sid, self._sub_id = self._sub_id, None
        if sid is None:
            return
        try:
            await self._bus.unsubscribe(sid)
        except Exception:  # noqa: BLE001 -- fail-soft teardown
            logger.debug("[RemoteIntakeBridge] unsubscribe failed",
                         exc_info=True)

    async def _on_signal(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            logger.warning(
                "[RemoteIntakeBridge] non-dict payload dropped: %r",
                type(payload).__name__)
            return
        try:
            envelope = IntentEnvelope.from_dict(payload)
        except Exception:  # noqa: BLE001 -- malformed: log-and-drop
            logger.warning(
                "[RemoteIntakeBridge] malformed remote envelope dropped "
                "(keys=%s)", sorted(payload.keys()), exc_info=True)
            return
        try:
            verdict = await self._router.ingest(envelope)
            logger.debug(
                "[RemoteIntakeBridge] ingested remote signal source=%s "
                "dedup_key=%s verdict=%s",
                envelope.source, envelope.dedup_key, verdict)
        except Exception:  # noqa: BLE001 -- never crash the bus loop
            logger.warning(
                "[RemoteIntakeBridge] router ingest failed (signal dropped)",
                exc_info=True)
