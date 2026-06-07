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
import pathlib
from typing import Any, Optional

_ENV_MASTER = "JARVIS_ECONOMIC_ROUTER_ENABLED"
_ENV_MICRO_TOKENS = "JARVIS_ECONOMIC_MICRO_OP_TOKENS"
_ENV_FAILOVER_MODEL = "JARVIS_ECONOMIC_FAILOVER_MODEL"
_ENV_ALLOW_MUTATING = "JARVIS_BACKGROUND_ALLOW_FALLBACK"  # reuse the existing knob
_ENV_RECLASSIFY = "JARVIS_ECONOMIC_RECLASSIFY_ENABLED"  # Slice 127

_DEFAULT_MICRO_OP_TOKENS = 1500
_CHARS_PER_TOKEN = 4  # standard rough estimate


def economic_router_enabled() -> bool:
    """Master gate. **Graduated to default-TRUE (Slice 131)** — the router only
    acts on the provider-FAILURE path (DW 402/429), so default-on cannot raise
    spend on the happy path; it only adds a cheap-tier failover instead of a
    hard stall. Hot-revert: ``=false``."""
    return os.getenv(_ENV_MASTER, "true").strip().lower() not in ("0", "false", "no", "off")


# Canonical no-hardcode source for the cheap-tier failover model.
_POLICY_PATH = pathlib.Path(__file__).parent / "brain_selection_policy.yaml"


def _low_cost_model_from_policy(policy_path: Optional[pathlib.Path] = None) -> str:
    """Resolve the cheap Claude tier from ``brain_selection_policy.yaml``
    (``cost_optimization.claude_low_cost_model``). NO hardcoded model string in
    this module (CLAUDE.md mandate) — the default lives in the YAML config.
    NEVER raises; returns "" on any read/parse failure."""
    path = policy_path or _POLICY_PATH
    try:
        import yaml  # lazy — keep module import-light + yaml-optional
        with open(path, "r", encoding="utf-8") as fh:
            data: Any = yaml.safe_load(fh) or {}
        val = (data.get("cost_optimization", {}) or {}).get("claude_low_cost_model", "")
        return str(val or "").strip()
    except Exception:  # noqa: BLE001 — failure-soft
        return ""


def economic_reclassify_enabled() -> bool:
    """Slice 127 master gate for ECONOMIC RECLASSIFICATION in the provider
    retry classifier. Default **FALSE** per §33.1. When ON, a provider
    "credit balance too low" / "insufficient funds" failure is classified as
    the recoverable ``TERMINAL_QUOTA`` instead of the sticky ``TERMINAL_CONFIG``
    that would otherwise sticky-brick the op. Lives here (next to the canonical
    economic detector) so the PURE-DATA classifier stays env-free. NEVER raises."""
    return os.getenv(_ENV_RECLASSIFY, "false").strip().lower() in ("1", "true", "yes", "on")


def micro_op_token_limit() -> int:
    try:
        return max(1, int(os.getenv(_ENV_MICRO_TOKENS, _DEFAULT_MICRO_OP_TOKENS)))
    except (TypeError, ValueError):
        return _DEFAULT_MICRO_OP_TOKENS


def economic_failover_model(policy_path: Optional[pathlib.Path] = None) -> str:
    """The cheapest Claude tier for micro-op failover. Resolution order
    (CLAUDE.md no-hardcode mandate):
      1. ``JARVIS_ECONOMIC_FAILOVER_MODEL`` env override (operator wins).
      2. ``brain_selection_policy.yaml`` → ``cost_optimization.claude_low_cost_model``.
      3. "" (caller composes the default fallback provider).
    NEVER raises."""
    env = (os.getenv(_ENV_FAILOVER_MODEL, "") or "").strip()
    if env:
        return env
    return _low_cost_model_from_policy(policy_path)


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
    # Slice 127: Anthropic phrases it "credit balance IS too low", which the
    # original "balance too low" marker missed — that miss let the
    # bt-2026-06-07-040933 failure fall through to TERMINAL_CONFIG and
    # sticky-brick 16 ops. Markers cover DW ("402"/"balance too low") AND
    # Anthropic ("credit balance"/"purchase credit"/"upgrade or purchase").
    _markers_402 = (
        "402", "balance too low", "balance is too low", "credit balance",
        "add credits", "purchase credit", "upgrade or purchase",
        "insufficient", "payment",
    )
    if any(m in t for m in _markers_402):
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
    "economic_reclassify_enabled",
    "micro_op_token_limit",
    "economic_failover_model",
    "is_hard_economic_block",
    "estimate_tokens",
    "EconomicAction",
    "EconomicDecision",
    "decide",
]
