"""Phase 2 — SpinalCord: bidirectional event wiring between Body and Mind.

The SpinalCord wires the Ouroboros Daemon to the system event stream so that:
  - Exploration findings can be streamed UP to the governance channel.
  - Governance decisions can be streamed DOWN from GCP/Mind.

Design principles
-----------------
* **SpinalGate** (``_gate``) is a one-shot ``asyncio.Event`` — once set after
  ``wire()``, it is never cleared.  Phase 3 (REM Sleep) awaits this gate
  before starting, so it can begin in local-only mode even when GCP is
  unreachable.
* **SpinalLiveness** (``_is_live``) is dynamic — it flips on disconnect /
  reconnect and governs whether events are broadcast or buffered locally.
* When not live, both ``stream_up`` and ``stream_down`` append JSON lines to a
  local JSONL buffer so no findings are lost during connectivity gaps.
* ``wire()`` is idempotent — subsequent calls return the original status
  immediately without repeating the transport probe.

Usage::

    cord = SpinalCord(event_stream)
    status = await cord.wire(timeout_s=10.0)

    # Phase 3 waits for the gate before starting
    await cord.wait_for_gate()

    # Streaming up (findings → governance channel)
    await cord.stream_up("finding", {"file": "backend/foo.py", ...})

    # Streaming down (governance decisions → local handling)
    await cord.stream_down("governance_decision", {"op_id": "abc", ...})
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_GOVERNANCE_CHANNEL = "governance"
_WIRE_PROBE_PAYLOAD: Dict[str, Any] = {"type": "spinal_probe", "source": "spinal_cord"}


# ---------------------------------------------------------------------------
# Protocol for the injected event stream
# ---------------------------------------------------------------------------


@runtime_checkable
class EventStreamProtocol(Protocol):
    """Structural interface for the event stream dependency.

    Only the single method used by SpinalCord is declared so that any object
    providing ``broadcast_event`` satisfies the contract without inheriting
    from a concrete base class.
    """

    async def broadcast_event(self, channel: str, payload: Dict[str, Any]) -> int:
        """Broadcast *payload* to all sessions subscribed to *channel*.

        Returns the number of sessions that received the event.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# SpinalStatus
# ---------------------------------------------------------------------------


class SpinalStatus(enum.Enum):
    """Result of a ``SpinalCord.wire()`` call."""

    CONNECTED = "connected"
    DEGRADED = "degraded"


# ---------------------------------------------------------------------------
# SpinalCord
# ---------------------------------------------------------------------------


class SpinalCord:
    """Bidirectional event bridge for the Ouroboros Daemon.

    Parameters
    ----------
    event_stream:
        Any object implementing :class:`EventStreamProtocol`.  In production
        this is the ``PersistentEventStream`` singleton; in tests it is a mock.
    local_buffer_path:
        Path to the JSONL file used as a local buffer when the transport is
        degraded.  Supports ``~`` expansion.  The directory is created on first
        write.
    """

    def __init__(
        self,
        event_stream: Any,
        local_buffer_path: str = "~/.jarvis/ouroboros/pending_findings.jsonl",
    ) -> None:
        self._event_stream = event_stream
        self._local_buffer_path = Path(local_buffer_path).expanduser().resolve()

        # SpinalGate: one-shot, set after wire() regardless of outcome
        self._gate: asyncio.Event = asyncio.Event()

        # SpinalLiveness: dynamic, updated by wire() and disconnect/reconnect
        self._is_live: bool = False

        # Idempotency guard — stores the last returned status once wired
        self._wired: bool = False
        self._last_status: SpinalStatus = SpinalStatus.DEGRADED

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def gate_is_set(self) -> bool:
        """True once the SpinalGate has been opened (after wire())."""
        return self._gate.is_set()

    @property
    def is_live(self) -> bool:
        """True when the transport is confirmed reachable and healthy."""
        return self._is_live

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wait_for_gate(self) -> None:
        """Suspend until the SpinalGate is set.

        Used by Phase 3 (REM Sleep) to wait for Phase 2 to complete before
        starting exploration.  Returns immediately if the gate is already set.
        """
        await self._gate.wait()

    async def wire(self, timeout_s: float = 10.0) -> SpinalStatus:
        """Attempt to verify the event transport by sending a probe broadcast.

        The gate is **always** set after this call — even when the probe fails —
        so Phase 3 can start in local-only mode.

        Parameters
        ----------
        timeout_s:
            Seconds to wait for ``broadcast_event`` to respond before
            considering the connection degraded.

        Returns
        -------
        SpinalStatus
            ``CONNECTED`` when the probe succeeds; ``DEGRADED`` otherwise.
        """
        # Idempotent: return cached result on subsequent calls
        if self._wired:
            logger.debug("SpinalCord.wire() called again — returning cached %s", self._last_status)
            return self._last_status

        status = await self._probe_transport(timeout_s)
        self._last_status = status
        self._wired = True

        if status is SpinalStatus.CONNECTED:
            self._is_live = True
            logger.info("SpinalCord: transport verified — CONNECTED")
        else:
            self._is_live = False
            logger.warning(
                "SpinalCord: transport probe failed — DEGRADED "
                "(Phase 3 will start in local-only mode)"
            )

        # Gate is ALWAYS set — Phase 3 starts regardless of liveness
        self._gate.set()

        return status

    async def stream_up(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Stream an event upward (Body → governance channel / GCP).

        When live, broadcasts on the ``governance`` channel.
        When not live, appends a JSON line to the local JSONL buffer.

        Parameters
        ----------
        event_type:
            Semantic label for the event (e.g. ``"finding"``, ``"epoch_complete"``).
        payload:
            Arbitrary dict containing the event data.
        """
        await self._dispatch(event_type, payload, direction="up")

    async def stream_down(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Stream an event downward (governance decisions → local handling).

        Follows the same live/offline routing logic as ``stream_up``.

        Parameters
        ----------
        event_type:
            Semantic label for the event (e.g. ``"governance_decision"``).
        payload:
            Arbitrary dict containing the decision data.
        """
        await self._dispatch(event_type, payload, direction="down")

    def on_disconnect(self) -> None:
        """Mark the transport as no longer live.

        Called by the event stream layer when a connection is lost.
        Subsequent ``stream_up`` / ``stream_down`` calls will fall back to the
        local JSONL buffer until ``on_reconnect()`` is called.
        """
        self._is_live = False
        logger.info("SpinalCord: transport disconnected — switching to local buffer")

    def on_reconnect(self) -> None:
        """Restore liveness after a reconnect.

        Called by the event stream layer when a connection is re-established.
        Subsequent ``stream_up`` / ``stream_down`` calls will resume live
        broadcasting.
        """
        self._is_live = True
        logger.info("SpinalCord: transport reconnected — resuming live broadcast")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _probe_transport(self, timeout_s: float) -> SpinalStatus:
        """Send a probe broadcast and return the resulting status."""
        try:
            await asyncio.wait_for(
                self._event_stream.broadcast_event(
                    _GOVERNANCE_CHANNEL, _WIRE_PROBE_PAYLOAD
                ),
                timeout=timeout_s,
            )
            return SpinalStatus.CONNECTED
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("SpinalCord: probe failed — %s: %s", type(exc).__name__, exc)
            return SpinalStatus.DEGRADED

    async def _dispatch(
        self, event_type: str, payload: Dict[str, Any], direction: str
    ) -> None:
        """Route event to broadcast or local buffer based on liveness."""
        if self._is_live:
            try:
                await self._event_stream.broadcast_event(
                    _GOVERNANCE_CHANNEL,
                    {"event_type": event_type, "direction": direction, "payload": payload},
                )
                return
            except Exception as exc:
                # Fall through to local buffer on unexpected broadcast failure
                logger.warning(
                    "SpinalCord: broadcast failed for %s — buffering locally: %s",
                    event_type,
                    exc,
                )

        await self._write_to_local_buffer(event_type, payload)

    async def _write_to_local_buffer(
        self, event_type: str, payload: Dict[str, Any]
    ) -> None:
        """Append a JSON line to the local JSONL buffer file."""
        os.makedirs(self._local_buffer_path.parent, exist_ok=True)
        record = json.dumps({"event_type": event_type, "payload": payload})
        with open(self._local_buffer_path, "a", encoding="utf-8") as fh:
            fh.write(record + "\n")
        logger.debug(
            "SpinalCord: buffered %s locally at %s", event_type, self._local_buffer_path
        )
