"""ReasoningProvider — thin protocol for AI reasoning backends.

Wraps any reasoning backend (PrimeRouter, Claude API, heuristic engine)
behind a uniform async interface that returns DecisionEnvelopes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from core.contracts.decision_envelope import DecisionEnvelope, DecisionSource


@runtime_checkable
class ReasoningProvider(Protocol):
    async def reason(
        self, prompt: str, context: Dict[str, Any],
        deadline: Optional[float] = None,
    ) -> DecisionEnvelope: ...

    @property
    def provider_name(self) -> DecisionSource: ...
