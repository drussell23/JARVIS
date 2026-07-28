"""The Adaptive Payload Router — the lane is a property of the payload.

"Cheap-first or fast-first" is a false choice, and picking either one
statically is wrong for half the traffic:

* a two-word question routed to DW's async tier waits ~60s for its first
  token, which is not a conversation;
* a 40k-token context dump routed to Claude buys latency nobody asked for
  at roughly ten times the price per token.

The deciding fact is not policy, it is **weight**. This module measures the
payload about to go on the wire and names the lane that suits it, leaving
the CASCADE intact either way: the preferred tier is tried first and the
other is still the fallback, so a lane choice can never become a single
point of failure. A heavy query when DW is down still gets answered by
Claude — slower to bill, but answered.

Deliberately a pure decision function. It does not send, does not import a
provider, and holds no state — so the rule can be unit-tested at a
thousand payload sizes without a network, and the one place that DOES send
(``rt_gate.gate_completion``) keeps sole authority over transport.

DRY: the estimator is ``economic_router.estimate_tokens`` — the repo's
existing chars/token heuristic, already trusted for the economic
size-gate. Two estimators would eventually disagree about the same string,
and the cheaper of the two disagreements is the one that routes money.

Env:
  * ``JARVIS_CHAT_ADAPTIVE_LANE_ENABLED``  master (default true)
  * ``JARVIS_CHAT_HEAVY_TOKENS``           fast→heavy boundary (2000)
  * ``JARVIS_CHAT_HEAVY_TTFT_HINT``        latency wording for the notice

NEVER raises: an undecidable payload takes the FAST lane, because the
failure a chat surface cannot absorb is silence.
"""
from __future__ import annotations

import enum
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

ADAPTIVE_LANE_SCHEMA_VERSION: str = "adaptive_lane.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_CHAT_ADAPTIVE_LANE_ENABLED"
HEAVY_TOKENS_ENV_VAR: str = "JARVIS_CHAT_HEAVY_TOKENS"
TTFT_HINT_ENV_VAR: str = "JARVIS_CHAT_HEAVY_TTFT_HINT"

#: The documented DW async time-to-first-token (CLAUDE.md: ~66s on the
#: 0-APPLY soak class). A HINT the operator can correct, never a promise —
#: which is why it is a string and not a computed SLA.
DEFAULT_TTFT_HINT: str = "~60s"

#: Where "a question" stops and "a context dump" starts. 2000 tokens is
#: roughly 8k characters: comfortably above a grounded question (the
#: grounding pack alone caps at 2400 chars) and comfortably below a pasted
#: file or a multi-turn transcript.
DEFAULT_HEAVY_TOKENS: int = 2000


class Lane(str, enum.Enum):
    """Which tier goes FIRST. The other remains the fallback."""

    FAST = "fast"      # Claude-RT — low TTFT, higher $/token
    HEAVY = "heavy"    # DW — async, far cheaper per token


def is_adaptive_lane_enabled() -> bool:
    """Master flag — default true. Off restores the global tier order
    (``rt_gate.claude_first_enabled``) for every payload."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def heavy_token_threshold() -> int:
    """The boundary, read live. A non-positive or malformed value means
    "never heavy" rather than "always heavy": misconfiguration must
    degrade toward the responsive lane, never toward a 60-second wait on
    every keystroke."""
    try:
        value = int(os.environ.get(
            HEAVY_TOKENS_ENV_VAR, str(DEFAULT_HEAVY_TOKENS),
        ))
    except (TypeError, ValueError):
        return DEFAULT_HEAVY_TOKENS
    return value if value > 0 else 0


def ttft_hint() -> str:
    try:
        return (os.environ.get(TTFT_HINT_ENV_VAR, "").strip()
                or DEFAULT_TTFT_HINT)
    except Exception:  # noqa: BLE001
        return DEFAULT_TTFT_HINT


def estimate_payload_tokens(*parts: Any) -> int:
    """Estimated token weight of everything about to go on the wire.

    Variadic because the payload is assembled from pieces the caller
    already holds — the grounded prompt, a system prompt, resolved ``@``
    attachments — and asking the caller to concatenate them first would
    make the measurement depend on their string handling rather than on
    the content. Sequences are walked, so a list of attachment bodies
    counts every one.

    NEVER raises; an unmeasurable part contributes zero rather than
    aborting the estimate."""
    total_chars = 0
    for part in parts:
        try:
            if part is None:
                continue
            if isinstance(part, str):
                total_chars += len(part)
            elif isinstance(part, (list, tuple, set)):
                for item in part:
                    total_chars += len(str(item or ""))
            else:
                total_chars += len(str(part))
        except Exception:  # noqa: BLE001
            continue
    try:
        from backend.core.ouroboros.governance.economic_router import (
            estimate_tokens,
        )
        return int(estimate_tokens(total_chars))
    except Exception:  # noqa: BLE001
        # The same ~4 chars/token rule the estimator implements — used
        # only if that module cannot be imported at all.
        return max(0, total_chars // 4)


def choose_lane(estimated_tokens: int) -> Lane:
    """The rule, isolated so it can be tested at any size. NEVER raises."""
    try:
        if not is_adaptive_lane_enabled():
            return Lane.FAST
        threshold = heavy_token_threshold()
        if threshold <= 0:
            return Lane.FAST
        return Lane.HEAVY if int(estimated_tokens) >= threshold else Lane.FAST
    except Exception:  # noqa: BLE001
        return Lane.FAST


def prefer_for(lane: Lane) -> Optional[str]:
    """The ``prefer`` token ``rt_gate.gate_completion`` understands, or
    None when the global policy should decide (adaptive routing off)."""
    try:
        if not is_adaptive_lane_enabled():
            return None
        return "dw" if lane is Lane.HEAVY else "claude"
    except Exception:  # noqa: BLE001
        return None


def heavy_notice(estimated_tokens: int) -> str:
    """What the operator is told when their query is shunted.

    Says WHY (weight), WHERE (the async tier) and WHAT IT COSTS THEM
    (latency) — a spinner that silently takes a minute reads as a hang,
    and an operator who knows why will wait."""
    try:
        approx = f"{estimated_tokens / 1000:.1f}k" if (
            estimated_tokens >= 1000
        ) else str(int(estimated_tokens))
        return (f"🐢 heavy context (~{approx} tokens) — routing to the "
                f"async DW lane to protect cost ({ttft_hint()} to first "
                f"token)")
    except Exception:  # noqa: BLE001
        return "🐢 heavy context — routing to the async DW lane"


def route(
    *parts: Any,
    notify: Any = None,
) -> "RouteDecision":
    """Measure, decide, and (when heavy) tell the operator. NEVER raises.

    The notice fires HERE rather than at the send site because this is the
    moment the decision is made; emitting it later would race the request
    it is meant to explain."""
    tokens = estimate_payload_tokens(*parts)
    lane = choose_lane(tokens)
    decision = RouteDecision(
        lane=lane,
        estimated_tokens=tokens,
        threshold=heavy_token_threshold(),
        prefer=prefer_for(lane),
    )
    if lane is Lane.HEAVY and callable(notify):
        try:
            notify(heavy_notice(tokens))
        except Exception:  # noqa: BLE001
            logger.debug("[AdaptiveLane] notice degraded", exc_info=True)
    logger.info(
        "[AdaptiveLane] lane=%s tokens=~%d threshold=%d prefer=%s",
        lane.value, tokens, decision.threshold, decision.prefer,
    )
    return decision


class RouteDecision:
    """The decision, as data — so a caller can log it, show it, or assert
    on it without re-deriving the rule."""

    __slots__ = ("lane", "estimated_tokens", "threshold", "prefer")

    def __init__(
        self, lane: Lane, estimated_tokens: int, threshold: int,
        prefer: Optional[str],
    ) -> None:
        self.lane = lane
        self.estimated_tokens = estimated_tokens
        self.threshold = threshold
        self.prefer = prefer

    @property
    def is_heavy(self) -> bool:
        return self.lane is Lane.HEAVY

    def to_dict(self) -> dict:
        return {
            "schema_version": ADAPTIVE_LANE_SCHEMA_VERSION,
            "lane": self.lane.value,
            "estimated_tokens": self.estimated_tokens,
            "threshold": self.threshold,
            "prefer": self.prefer,
        }

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (f"RouteDecision(lane={self.lane.value}, "
                f"~{self.estimated_tokens}t, threshold={self.threshold})")


__all__ = [
    "ADAPTIVE_LANE_SCHEMA_VERSION",
    "DEFAULT_HEAVY_TOKENS",
    "DEFAULT_TTFT_HINT",
    "HEAVY_TOKENS_ENV_VAR",
    "Lane",
    "MASTER_FLAG_ENV_VAR",
    "RouteDecision",
    "TTFT_HINT_ENV_VAR",
    "choose_lane",
    "estimate_payload_tokens",
    "heavy_notice",
    "heavy_token_threshold",
    "is_adaptive_lane_enabled",
    "prefer_for",
    "route",
    "ttft_hint",
]
