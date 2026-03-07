"""ActionExecutor — thin protocol for executing committed actions.

Wraps any action execution (apply_label, deliver_notification, etc.)
behind a uniform async interface that returns ActionOutcome.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from core.contracts.decision_envelope import DecisionEnvelope
from core.contracts.policy_gate import PolicyVerdict


class ActionOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


@runtime_checkable
class ActionExecutor(Protocol):
    async def execute(
        self, envelope: DecisionEnvelope, verdict: PolicyVerdict,
        commit_id: str,
    ) -> ActionOutcome: ...
