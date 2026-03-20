"""
Reasoning Chain Bridge
======================

Bridges the ReasoningChainOrchestrator into the governance CommProtocol,
converting chain decisions into PLAN messages and stamping results onto
OperationContext for audit trail visibility.

Integration is optional -- if the reasoning chain is inactive or not configured,
the bridge is a no-op and the pipeline continues unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol

logger = logging.getLogger(__name__)


class ReasoningChainBridge:
    """Bridges reasoning chain decisions into governance CommProtocol.

    Wraps the ReasoningChainOrchestrator and:
    1. Runs chain classification on the operation description
    2. Emits results as PLAN messages via CommProtocol
    3. Returns a dict suitable for stamping onto OperationContext

    If the chain is inactive, all methods return None (no-op).
    """

    def __init__(self, comm: CommProtocol) -> None:
        self._comm = comm
        self._orchestrator = self._try_load_orchestrator()

    @staticmethod
    def _try_load_orchestrator() -> Any:
        """Lazy-load the reasoning chain orchestrator. Returns None if unavailable."""
        try:
            from backend.core.reasoning_chain_orchestrator import (
                get_reasoning_chain_orchestrator,
            )
            orch = get_reasoning_chain_orchestrator()
            if orch and orch._config.is_active():
                return orch
        except Exception as exc:
            logger.debug("Reasoning chain not available: %s", exc)
        return None

    @property
    def is_active(self) -> bool:
        """True when the underlying orchestrator is loaded and active."""
        return self._orchestrator is not None

    async def classify_with_reasoning(
        self,
        command: str,
        op_id: str,
        deadline: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run reasoning chain and emit results as PLAN message.

        Returns a dict suitable for OperationContext.reasoning_chain_result,
        or None if chain is inactive / failed / not triggered.
        """
        if not self._orchestrator:
            return None

        try:
            result = await asyncio.wait_for(
                self._orchestrator.process(
                    command=command,
                    context={},
                    trace_id=op_id,
                    deadline=deadline,
                ),
                timeout=self._orchestrator._config.expansion_timeout + 1.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Reasoning chain timed out for op=%s", op_id)
            return None
        except Exception as exc:
            logger.warning("Reasoning chain error for op=%s: %s", op_id, exc)
            return None

        if not result or not result.handled:
            return None

        # Emit as PLAN message via CommProtocol.
        # CommProtocol.emit_plan expects steps (List[str]) and rollback_strategy (str).
        try:
            await self._comm.emit_plan(
                op_id=op_id,
                steps=result.expanded_intents,
                rollback_strategy="reasoning_chain_rollback",
            )
        except Exception:
            logger.debug("Failed to emit reasoning chain PLAN", exc_info=True)

        return {
            "expanded_intents": result.expanded_intents,
            "phase": result.phase.value if hasattr(result.phase, "value") else str(result.phase),
            "success_rate": result.success_rate,
            "needs_confirmation": result.needs_confirmation,
            "intent_count": len(result.expanded_intents),
        }
