"""TriagePolicyGate — wraps NotificationPolicy behind the PolicyGate protocol.

Zero behavior change. The existing NotificationPolicy.decide_action() logic
is preserved exactly. This adapter adds typed envelope/verdict interface.
"""

from __future__ import annotations

import time
from typing import Any

from core.contracts.decision_envelope import DecisionEnvelope
from core.contracts.policy_gate import PolicyVerdict, VerdictAction
from core.contracts.policy_context import PolicyContext
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.policy import NotificationPolicy
from autonomy.email_triage.schemas import (
    EmailFeatures, ScoringResult, TriagedEmail,
)


# Action -> VerdictAction mapping
_ACTION_TO_VERDICT = {
    "immediate": VerdictAction.ALLOW,
    "summary": VerdictAction.DEFER,
    "label_only": VerdictAction.DENY,
    "quarantine": VerdictAction.DENY,
}


class TriagePolicyGate:
    """Wraps NotificationPolicy behind the PolicyGate protocol.

    Satisfies PolicyGate via structural subtyping (duck typing).
    The evaluate() method:
    1. Builds a minimal TriagedEmail from envelope payload + PolicyContext
    2. Calls NotificationPolicy.decide_action()
    3. Wraps the result in a PolicyVerdict
    """

    def __init__(self, policy: NotificationPolicy, config: TriageConfig) -> None:
        self._policy = policy
        self._config = config

    async def evaluate(
        self, envelope: DecisionEnvelope, context: Any,
    ) -> PolicyVerdict:
        payload = envelope.payload
        msg_id = payload.get("message_id", "")
        score_val = payload.get("score", 0)
        tier_val = payload.get("tier", 4)

        # Build minimal EmailFeatures for NotificationPolicy
        features = EmailFeatures(
            message_id=msg_id,
            sender=f"unknown@{context.sender_domain}" if hasattr(context, "sender_domain") else "unknown",
            sender_domain=context.sender_domain if hasattr(context, "sender_domain") else "",
            subject="",
            snippet="",
            is_reply=context.is_reply if hasattr(context, "is_reply") else False,
            has_attachment=context.has_attachment if hasattr(context, "has_attachment") else False,
            label_ids=context.label_ids if hasattr(context, "label_ids") else (),
            keywords=(),
            sender_frequency="occasional",
            urgency_signals=(),
            extraction_confidence=envelope.confidence,
            extraction_source=envelope.source.value,
        )

        scoring = ScoringResult(
            score=score_val,
            tier=tier_val,
            tier_label=self._config.label_for_tier(tier_val),
            breakdown={},
            idempotency_key=f"{msg_id}:{self._config.scoring_version}",
        )

        triaged = TriagedEmail(
            features=features,
            scoring=scoring,
            notification_action="",
            processed_at=time.time(),
        )

        action_str, explanation = self._policy.decide_action(triaged)

        verdict_action = _ACTION_TO_VERDICT.get(action_str, VerdictAction.DENY)
        allowed = verdict_action == VerdictAction.ALLOW

        return PolicyVerdict(
            allowed=allowed,
            action=verdict_action,
            reason=action_str,
            conditions=explanation.reasons if explanation else (),
            envelope_id=envelope.envelope_id,
            gate_name="triage_policy",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
