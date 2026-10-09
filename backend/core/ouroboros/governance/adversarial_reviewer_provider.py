"""The adversarial reviewer's model lane.

``AdversarialReviewerService`` was complete and called on every op -- and
never reviewed one: nothing ever handed it a provider, so every op that was
not SAFE_AUTO ended ``skipped reason=no_provider`` (2026-10-09). This is
that provider. It is not a new lane: it is ``rt_gate.gate_completion_detailed``
-- the one router every pipeline-side model call already uses -- which tries
the local lane first where it is enabled, drops any paid lane the paid-lane
authority refuses (so spend is governed where all spend is governed), and
counts the call in the gateway's in-flight ledger so idle work cannot evict
the weights under it.

Lives OUTSIDE the service on purpose: the service's authority cage pins its
only I/O to its audit ledger; the integration layer (the hook) supplies this.
"""
from __future__ import annotations

import os
from typing import Optional

from backend.core.ouroboros.governance.adversarial_reviewer_service import (
    ReviewProviderResult,
)

_ENV_MAX_TOKENS = "JARVIS_ADVERSARIAL_REVIEW_MAX_TOKENS"
_ENV_TIMEOUT_S = "JARVIS_ADVERSARIAL_REVIEW_TIMEOUT_S"
_DEFAULT_MAX_TOKENS = 1024


def _positive_env(env: str) -> Optional[float]:
    try:
        value = float(os.environ.get(env, "") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


def review_max_tokens() -> int:
    return int(_positive_env(_ENV_MAX_TOKENS) or _DEFAULT_MAX_TOKENS)


def review_timeout_s() -> Optional[float]:
    """Per-review wall budget; unset -> the gate's own RT timeout."""
    return _positive_env(_ENV_TIMEOUT_S)


class RtGateReviewProvider:
    """``ReviewProvider`` over the unified RT gate. ``review`` is a
    coroutine; the service awaits it."""

    caller_id = "adversarial_reviewer"

    async def review(self, prompt: str) -> ReviewProviderResult:
        from backend.core.ouroboros.governance.rt_gate import (  # noqa: PLC0415
            gate_completion_detailed,
        )
        text, tier = await gate_completion_detailed(
            prompt,
            caller_id=self.caller_id,
            max_tokens=review_max_tokens(),
            timeout_s=review_timeout_s(),
        )
        # The gate does not report spend; a metered tier is reachable only
        # when the paid-lane authority admitted it, and its cost is booked
        # by that provider's own ledger. The tier is recorded as the model.
        return ReviewProviderResult(raw_response=text, cost_usd=0.0, model_used=tier)


_default: Optional[RtGateReviewProvider] = None


def default_review_provider() -> RtGateReviewProvider:
    global _default
    if _default is None:
        _default = RtGateReviewProvider()
    return _default


__all__ = [
    "RtGateReviewProvider", "default_review_provider",
    "review_max_tokens", "review_timeout_s",
]
