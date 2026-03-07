"""PolicyGate — async protocol for gating autonomous actions.

Every proposed action must pass through a PolicyGate before execution.
The gate evaluates a DecisionEnvelope against runtime context and returns
a PolicyVerdict (ALLOW / DENY / DEFER).

The protocol is async because real policy checks may need to hit
stores, quotas, or lease managers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Protocol, Tuple, runtime_checkable

from core.contracts.decision_envelope import DecisionEnvelope


class VerdictAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    action: VerdictAction
    reason: str
    conditions: Tuple[str, ...]
    envelope_id: str
    gate_name: str
    created_at_epoch: float
    created_at_monotonic: float


@runtime_checkable
class PolicyGate(Protocol):
    async def evaluate(
        self, envelope: DecisionEnvelope, context: Dict[str, Any]
    ) -> PolicyVerdict: ...
