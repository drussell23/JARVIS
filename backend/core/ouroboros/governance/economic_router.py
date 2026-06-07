"""Slice 124 — The Autonomous Economic Failover Router.

When the cheap primary provider (DoubleWord) returns a HARD economic block —
HTTP 402 ("Account balance too low") or 429 (rate limit) — a BACKGROUND op must
not stall the organism. But blindly cascading every blocked op to Claude burns
expensive credits. The EconomicRouter makes a BOUNDED economic decision:

  • MICRO-OP (small context) → cascade to the CHEAPEST configured Claude tier
    (preserve momentum, minimal burn).
  • MASSIVE context → stay QUEUED until the cheap provider returns (don't pay
    Sonnet/Opus prices for a big background op).
  • Mutation safety is PRESERVED: a mutating op still requires the existing
    opt-in (JARVIS_BACKGROUND_ALLOW_FALLBACK); only read-only micro-ops cascade
    autonomously (read-only carries no write risk — Rule 0d refuses every
    mutating tool under is_read_only, the same contract the Defect #5 cascade
    relies on).

This composes the EXISTING BACKGROUND→Claude cascade (candidate_generator's
"queue"-tolerance read-only reflex); it ADDS the economic size-gate + the cheap
model selection, it does not replace the cascade.

NO HARDCODED MODELS (CLAUDE.md mandate). The cheapest Claude tier is resolved
from ``JARVIS_ECONOMIC_FAILOVER_MODEL`` (the policy yaml defines no cheap tier).
Empty/unset → returns "" and the caller composes the default fallback provider.

Master switch: ``JARVIS_ECONOMIC_ROUTER_ENABLED`` (default **false**, §33.1).
"""

from __future__ import annotations

import dataclasses
import enum
import os
from typing import Optional

_ENV_MASTER = "JARVIS_ECONOMIC_ROUTER_ENABLED"
_ENV_MICRO_TOKENS = "JARVIS_ECONOMIC_MICRO_OP_TOKENS"
_ENV_FAILOVER_MODEL = "JARVIS_ECONOMIC_FAILOVER_MODEL"
_ENV_ALLOW_MUTATING = "JARVIS_BACKGROUND_ALLOW_FALLBACK"  # reuse the existing knob

_DEFAULT_MICRO_OP_TOKENS = 1500
_CHARS_PER_TOKEN = 4  # standard rough estimate


def economic_router_enabled() -> bool:
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def micro_op_token_limit() -> int:
    try:
        return max(1, int(os.getenv(_ENV_MICRO_TOKENS, _DEFAULT_MICRO_OP_TOKENS)))
    except (TypeError, ValueError):
        return _DEFAULT_MICRO_OP_TOKENS


def economic_failover_model() -> str:
    """The cheapest Claude tier for micro-op failover — resolved from env, never
    hardcoded. Empty when unset (caller uses the default fallback provider)."""
    return (os.getenv(_ENV_FAILOVER_MODEL, "") or "").strip()


def _allow_mutating_fallback() -> bool:
    return os.getenv(_ENV_ALLOW_MUTATING, "").strip().lower() in ("1", "true", "yes", "on")


def is_hard_economic_block(error_text: Optional[str]) -> Optional[str]:
    """Classify a provider failure as a HARD economic block. Returns "402" /
    "429" / None. 402 = balance/credits exhausted; 429 = rate-limited. Both mean
    "this provider can't serve right now" — distinct from a transient transport
    blip (which the existing cascade/sever logic already handles)."""
    if not error_text:
        return None
    t = str(error_text).lower()
    if "402" in t or "balance too low" in t or "add credits" in t or "insufficient" in t or "payment" in t:
        return "402"
    if "429" in t or "rate limit" in t or "too many requests" in t or "ratelimit" in t:
        return "429"
    return None


def estimate_tokens(prompt_chars: int) -> int:
    """Rough token estimate from character count (~4 chars/token)."""
    return max(0, int(prompt_chars)) // _CHARS_PER_TOKEN


class EconomicAction(str, enum.Enum):
    NO_OP = "no_op"                # router disabled / not a hard block → defer to existing logic
    CASCADE_CHEAP = "cascade_cheap"  # micro-op → cheap Claude now
    QUEUE = "queue"                # too large / unsafe → wait for the cheap provider


@dataclasses.dataclass(frozen=True)
class EconomicDecision:
    action: EconomicAction
    model: str = ""        # cheap model id (may be "" → caller uses default fallback)
    tokens: int = 0
    reason: str = ""


def decide(
    *,
    route: str,
    error_text: Optional[str],
    prompt_chars: int,
    is_read_only: bool,
) -> EconomicDecision:
    """The bounded economic failover decision. Pure — no I/O, no side effects."""
    if not economic_router_enabled():
        return EconomicDecision(EconomicAction.NO_OP, reason="router_disabled")

    block = is_hard_economic_block(error_text)
    if block is None:
        # Not an economic block (transport blip, parse error, etc.) — let the
        # existing sever/cascade logic own it.
        return EconomicDecision(EconomicAction.NO_OP, reason="not_economic_block")

    # Only the cost-optimized background/speculative tiers are the router's
    # concern; STANDARD/IMMEDIATE/COMPLEX already cascade to Claude by policy.
    if route not in ("background", "speculative"):
        return EconomicDecision(EconomicAction.NO_OP, reason=f"route_{route}_not_managed")

    tokens = estimate_tokens(prompt_chars)
    limit = micro_op_token_limit()
    if tokens > limit:
        # Massive context — too expensive to pay Claude prices; wait for cheap DW.
        return EconomicDecision(
            EconomicAction.QUEUE, tokens=tokens,
            reason=f"context_{tokens}tok_exceeds_micro_limit_{limit}_block_{block}",
        )

    # Micro-op. Mutation safety: a mutating op still needs the existing opt-in.
    if not (is_read_only or _allow_mutating_fallback()):
        return EconomicDecision(
            EconomicAction.QUEUE, tokens=tokens,
            reason=f"micro_op_but_mutating_no_optin_block_{block}",
        )

    return EconomicDecision(
        EconomicAction.CASCADE_CHEAP,
        model=economic_failover_model(),  # "" → caller composes default fallback
        tokens=tokens,
        reason=f"micro_op_{tokens}tok<= {limit}_readonly_{is_read_only}_block_{block}",
    )


__all__ = [
    "economic_router_enabled",
    "micro_op_token_limit",
    "economic_failover_model",
    "is_hard_economic_block",
    "estimate_tokens",
    "EconomicAction",
    "EconomicDecision",
    "decide",
]
