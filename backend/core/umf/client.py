"""UMF Client SDK -- thin convenience wrapper over :class:`DeliveryEngine`.

Provides ergonomic helpers for the three most common messaging patterns:

* **publish_command** -- send a command to a target component.
* **send_heartbeat** -- emit a lifecycle heartbeat for liveness/readiness.
* **send_ack / send_nack** -- reply to a previously received message.

Design rules
------------
* Stdlib + sibling UMF modules only.
* All public methods are ``async``.
* Delegates all I/O to :class:`DeliveryEngine` -- no direct ledger or
  transport access.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from backend.core.umf.delivery_engine import DeliveryEngine, Handler, PublishResult
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Stream,
    UmfMessage,
)


class UmfClient:
    """High-level SDK for UMF message publishing and subscription.

    Parameters
    ----------
    repo:
        Repository identifier (e.g. ``"jarvis"``).
    component:
        Component name within the repo (e.g. ``"reactor-core"``).
    instance_id:
        Unique instance identifier for this process.
    session_id:
        Session identifier (survives restarts within a logical session).
    dedup_db_path:
        Filesystem path for the SQLite dedup ledger database.
    expected_capability_hash:
        Optional capability hash forwarded to the delivery engine's
        contract gate.
    """

    def __init__(
        self,
        repo: str,
        component: str,
        instance_id: str,
        session_id: str,
        dedup_db_path: Path,
        expected_capability_hash: Optional[str] = None,
    ) -> None:
        self._source = MessageSource(
            repo=repo,
            component=component,
            instance_id=instance_id,
            session_id=session_id,
        )
        self._engine = DeliveryEngine(
            dedup_db_path=dedup_db_path,
            expected_capability_hash=expected_capability_hash,
        )

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the underlying delivery engine."""
        await self._engine.start()

    async def stop(self) -> None:
        """Stop the underlying delivery engine."""
        await self._engine.stop()

    # ── subscribe / health ────────────────────────────────────────────

    async def subscribe(self, stream: Stream, handler: Handler) -> str:
        """Register a handler for messages on *stream*.

        Returns a subscription ID.
        """
        return await self._engine.subscribe(stream, handler)

    async def health(self) -> Dict[str, Any]:
        """Delegate to the engine's health check."""
        return await self._engine.health()

    # ── publish helpers ───────────────────────────────────────────────

    async def publish_command(
        self,
        target_repo: str,
        target_component: str,
        payload: Dict[str, Any],
        **kwargs: Any,
    ) -> PublishResult:
        """Publish a command message to the specified target.

        Parameters
        ----------
        target_repo:
            Destination repository name.
        target_component:
            Destination component name.
        payload:
            Arbitrary command payload dict.
        **kwargs:
            Extra keyword arguments forwarded to :class:`UmfMessage`.
        """
        msg = UmfMessage(
            stream=Stream.command,
            kind=Kind.command,
            source=self._source,
            target=MessageTarget(repo=target_repo, component=target_component),
            payload=payload,
            **kwargs,
        )
        return await self._engine.publish(msg)

    async def send_heartbeat(
        self,
        state: str,
        liveness: bool = True,
        readiness: bool = True,
        **extra_payload: Any,
    ) -> PublishResult:
        """Emit a lifecycle heartbeat for liveness/readiness monitoring.

        Parameters
        ----------
        state:
            Freeform state label (e.g. ``"ready"``, ``"degraded"``).
        liveness:
            Whether this component considers itself alive.
        readiness:
            Whether this component considers itself ready to serve.
        **extra_payload:
            Additional keys merged into the heartbeat payload.
            Recognised keys: ``last_error_code``, ``queue_depth``,
            ``resource_pressure``.
        """
        payload: Dict[str, Any] = {
            "liveness": liveness,
            "readiness": readiness,
            "subsystem_role": self._source.component,
            "state": state,
            "last_error_code": extra_payload.get("last_error_code", ""),
            "queue_depth": extra_payload.get("queue_depth", 0),
            "resource_pressure": extra_payload.get("resource_pressure", 0.0),
        }
        msg = UmfMessage(
            stream=Stream.lifecycle,
            kind=Kind.heartbeat,
            source=self._source,
            target=MessageTarget(
                repo=self._source.repo,
                component="supervisor",
            ),
            payload=payload,
        )
        return await self._engine.publish(msg)

    async def send_ack(
        self,
        original_message_id: str,
        target_repo: str,
        target_component: str,
        success: bool = True,
        message: str = "",
    ) -> PublishResult:
        """Send an ACK or NACK in reply to a previously received message.

        Parameters
        ----------
        original_message_id:
            The ``message_id`` of the message being acknowledged.
        target_repo:
            Destination repository name for the reply.
        target_component:
            Destination component name for the reply.
        success:
            ``True`` for ACK, ``False`` for NACK.
        message:
            Optional human-readable description of the outcome.
        """
        kind = Kind.ack if success else Kind.nack
        payload: Dict[str, Any] = {"message": message}
        msg = UmfMessage(
            stream=Stream.command,
            kind=kind,
            source=self._source,
            target=MessageTarget(repo=target_repo, component=target_component),
            payload=payload,
            causality_parent_message_id=original_message_id,
        )
        return await self._engine.publish(msg)
