"""
Langfuse Observability Transport
=================================

CommProtocol transport that maps governance events to Langfuse traces.

Maps the 5-phase CommMessage lifecycle to Langfuse:
  INTENT     -> trace.start() with operation metadata
  PLAN       -> span with reasoning chain / routing data
  HEARTBEAT  -> span with resource metrics (sampled, not every heartbeat)
  DECISION   -> span with outcome + provider + cost
  POSTMORTEM -> span with root cause + error classification

Configuration via environment variables:
  LANGFUSE_PUBLIC_KEY  -- Langfuse public key (required to enable)
  LANGFUSE_SECRET_KEY  -- Langfuse secret key (required to enable)
  LANGFUSE_HOST        -- Langfuse endpoint (default: https://cloud.langfuse.com)
  LANGFUSE_HEARTBEAT_SAMPLE_RATE -- Only record every Nth heartbeat (default: 5)

If langfuse is not installed or keys are not set, the transport is a silent no-op.
Failures are fault-isolated: they are logged at DEBUG level but never raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.comm_protocol import CommMessage

logger = logging.getLogger("Ouroboros.LangfuseTransport")


class LangfuseTransport:
    """CommProtocol transport that emits governance events to Langfuse.

    Implements the transport interface (``async send(msg: CommMessage)``)
    expected by :class:`CommProtocol`.  All public methods are fault-isolated:
    exceptions are logged at DEBUG level and never propagated.

    Parameters
    ----------
    langfuse_client:
        An already-constructed ``Langfuse`` instance.  When ``None``
        (the default), the transport attempts to create one from
        environment variables.  If creation fails the transport becomes
        a silent no-op.
    heartbeat_sample_rate:
        Only emit every *N*-th heartbeat per operation to reduce noise.
        Defaults to ``LANGFUSE_HEARTBEAT_SAMPLE_RATE`` env var, or 5.
    """

    def __init__(
        self,
        langfuse_client: Any = None,
        heartbeat_sample_rate: Optional[int] = None,
    ) -> None:
        self._langfuse: Any = langfuse_client if langfuse_client is not None else self._create_client()
        self._traces: Dict[str, Any] = {}  # op_id -> langfuse trace
        self._heartbeat_counters: Dict[str, int] = {}  # op_id -> count
        if heartbeat_sample_rate is not None:
            self._heartbeat_sample_rate = max(1, heartbeat_sample_rate)
        else:
            self._heartbeat_sample_rate = int(
                os.getenv("LANGFUSE_HEARTBEAT_SAMPLE_RATE", "5")
            )

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    @staticmethod
    def _create_client() -> Any:
        """Create a Langfuse client from environment variables.

        Returns ``None`` when the ``langfuse`` package is not installed or
        the required ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``
        environment variables are not set.
        """
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        if not public_key or not secret_key:
            logger.debug("Langfuse keys not set -- transport disabled")
            return None
        try:
            from langfuse import Langfuse  # type: ignore[import-untyped]

            client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
            logger.info("Langfuse transport initialised")
            return client
        except ImportError:
            logger.info("langfuse package not installed -- transport disabled")
            return None
        except Exception as exc:
            logger.warning("Langfuse client creation failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """``True`` when the underlying Langfuse client is available."""
        return self._langfuse is not None

    # ------------------------------------------------------------------
    # Transport interface
    # ------------------------------------------------------------------

    async def send(self, msg: "CommMessage") -> None:  # noqa: F821
        """Process a :class:`CommMessage` and emit to Langfuse.

        This method satisfies the CommProtocol transport contract.
        Failures are logged but **never** raised (fault-isolated).
        """
        if self._langfuse is None:
            return

        try:
            await self._dispatch(msg)
        except Exception as exc:
            logger.debug(
                "Langfuse send error for op=%s type=%s: %s",
                msg.op_id,
                msg.msg_type.value,
                exc,
            )

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: "CommMessage") -> None:  # noqa: F821
        """Route *msg* to the appropriate Langfuse handler."""
        # Deferred import to avoid circular imports at module load time.
        from backend.core.ouroboros.governance.comm_protocol import MessageType

        handler = {
            MessageType.INTENT: self._on_intent,
            MessageType.PLAN: self._on_plan,
            MessageType.HEARTBEAT: self._on_heartbeat,
            MessageType.DECISION: self._on_decision,
            MessageType.POSTMORTEM: self._on_postmortem,
        }.get(msg.msg_type)

        if handler is not None:
            await handler(msg)

    # ------------------------------------------------------------------
    # Phase handlers
    # ------------------------------------------------------------------

    async def _on_intent(self, msg: "CommMessage") -> None:  # noqa: F821
        """Start a new Langfuse trace for this operation."""
        if not hasattr(self._langfuse, "trace") or not callable(self._langfuse.trace):
            # SDK version mismatch — disable transport to avoid repeated errors
            logger.warning(
                "Langfuse client has no .trace() method (SDK version mismatch?) "
                "— disabling transport",
            )
            self._langfuse = None
            return
        payload = msg.payload
        trace = self._langfuse.trace(
            name="ouroboros-op",
            id=msg.op_id,
            metadata={
                "goal": payload.get("goal", ""),
                "target_files": payload.get("target_files", []),
                "risk_tier": payload.get("risk_tier", ""),
                "blast_radius": payload.get("blast_radius", 0),
                "trigger_source": payload.get("trigger_source", "unknown"),
                "correlation_id": getattr(msg, "correlation_id", ""),
            },
            tags=["ouroboros", payload.get("risk_tier", "unknown")],
        )
        self._traces[msg.op_id] = trace

    async def _on_plan(self, msg: "CommMessage") -> None:  # noqa: F821
        """Record planning / routing decisions as a span."""
        trace = self._traces.get(msg.op_id)
        if trace is None:
            return
        source = msg.payload.get("source", "unknown")
        trace.span(
            name=f"plan-{source}",
            metadata=msg.payload,
        )

    async def _on_heartbeat(self, msg: "CommMessage") -> None:  # noqa: F821
        """Record heartbeats, sampled at ``_heartbeat_sample_rate``."""
        count = self._heartbeat_counters.get(msg.op_id, 0) + 1
        self._heartbeat_counters[msg.op_id] = count

        if count % self._heartbeat_sample_rate != 0:
            return

        trace = self._traces.get(msg.op_id)
        if trace is None:
            return
        trace.span(
            name="heartbeat",
            metadata=msg.payload,
        )

    async def _on_decision(self, msg: "CommMessage") -> None:  # noqa: F821
        """Record the decision span, update the trace, and flush."""
        trace = self._traces.get(msg.op_id)
        if trace is None:
            return

        outcome = msg.payload.get("outcome", "unknown")
        level = (
            "WARNING" if outcome in ("blocked", "escalated") else "DEFAULT"
        )
        trace.span(
            name=f"decision-{outcome}",
            metadata=msg.payload,
            level=level,
        )
        trace.update(
            metadata={
                "outcome": outcome,
                "provider": msg.payload.get("provider_used", ""),
            },
        )
        self._cleanup_op(msg.op_id)
        await asyncio.to_thread(self._langfuse.flush)

    async def _on_postmortem(self, msg: "CommMessage") -> None:  # noqa: F821
        """Record failure details as an ERROR span and flush."""
        trace = self._traces.get(msg.op_id)
        if trace is None:
            return

        trace.span(
            name="postmortem",
            metadata=msg.payload,
            level="ERROR",
        )
        trace.update(
            metadata={
                "outcome": "postmortem",
                "error": msg.payload.get("root_cause", ""),
                "failed_phase": msg.payload.get("failed_phase", ""),
            },
        )
        self._cleanup_op(msg.op_id)
        await asyncio.to_thread(self._langfuse.flush)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _cleanup_op(self, op_id: str) -> None:
        """Remove tracking state for a completed operation."""
        self._traces.pop(op_id, None)
        self._heartbeat_counters.pop(op_id, None)

    async def shutdown(self) -> None:
        """Flush any pending Langfuse data on transport shutdown.

        Called by the supervisor or orchestrator during graceful shutdown.
        Failures are silently swallowed.
        """
        if self._langfuse is not None:
            try:
                await asyncio.to_thread(self._langfuse.flush)
            except Exception:
                pass
