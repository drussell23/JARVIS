"""RT Gate Router — Claude-RT-first completion for synchronous pipeline gates.

Phase 2 of the provider-positioning pivot (2026-07-16). The pipeline's
synchronous GATES (triage, critique, lint, heal, narrate, plan, review) are
LATENCY-SENSITIVE: an op the soak/human is waiting on blocks behind each one.
They were historically pointed at DoubleWord's token-priced queue — buying a
sub-cent discount with minutes of wall clock. This module is the ONE place
that encodes the correct positioning:

    Claude sells TIME  → gates buy time  → Claude-RT first.
    DW sells TOKENS    → kept as the OPPORTUNISTIC fallback (its stream-free
                         RT primitive ``complete_sync``), so a Claude outage
                         degrades a gate to slower-but-alive instead of dead.

Design contract (Mandate 3 — no duplicated fallback logic in gate classes):
  * ``gate_completion()`` is the single entry point. Gates pass their prompt
    and (optionally) their injected provider handles; ALL ordering, timeout,
    and fallback policy lives here.
  * Claude resolution is two-tier: an injected ``claude_provider`` (uses its
    resilient ``prompt_only``) else the provider-less, Aegis-wrapped
    ``claude_fallback.claude_inference`` — so gates that were constructed
    with only a DW handle still reach Claude without rewiring constructors.
  * Failures RAISE ``GateProviderExhaustedError`` (typed) only when every
    tier is exhausted; each gate keeps its own fail-open/fail-closed
    semantics around that exception — gate logic is NOT rewritten here
    (Mandate 1).
  * ``JARVIS_GATE_CLAUDE_FIRST_ENABLED`` (default TRUE) is the master; OFF
    restores DW-first ordering (one-flip rollback, byte-equivalent priority).

Env knobs:
  * ``JARVIS_GATE_CLAUDE_FIRST_ENABLED``  (default true)
  * ``JARVIS_GATE_RT_TIMEOUT_S``          (default 60; floor 5)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

_DEFAULT_SYSTEM = (
    "You are a senior AI reasoning engine for the JARVIS Trinity ecosystem. "
    "Think step by step and return well-structured output."
)


class GateProviderExhaustedError(RuntimeError):
    """Every RT tier (Claude injected → Claude fallback → DW-RT) failed for a
    gate completion. Gates decide fail-open vs fail-closed on this."""


def claude_first_enabled() -> bool:
    """Master — ``JARVIS_GATE_CLAUDE_FIRST_ENABLED`` (default true)."""
    return os.environ.get(
        "JARVIS_GATE_CLAUDE_FIRST_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def gate_rt_timeout_s() -> float:
    """Per-tier RT budget (``JARVIS_GATE_RT_TIMEOUT_S``, default 60s)."""
    try:
        return max(5.0, float(os.environ.get("JARVIS_GATE_RT_TIMEOUT_S", "60")))
    except (TypeError, ValueError):
        return 60.0


async def _try_claude(
    prompt: str,
    *,
    caller_id: str,
    max_tokens: int,
    response_format: Optional[Dict[str, Any]],
    timeout_s: float,
    claude_provider: Any = None,
) -> Optional[str]:
    """Claude tier: injected provider's resilient ``prompt_only``, else the
    provider-less Aegis-wrapped ``claude_inference``. Returns text or None
    (this tier's failure is non-fatal — the caller cascades)."""
    # 1a. Injected ClaudeProvider (the resilient, budget-gated path).
    if claude_provider is not None and hasattr(claude_provider, "prompt_only"):
        try:
            raw = await asyncio.wait_for(
                claude_provider.prompt_only(
                    prompt=prompt,
                    caller_id=caller_id,
                    response_format=response_format,
                    max_tokens=max_tokens,
                    timeout_s=timeout_s,
                ),
                timeout=timeout_s + 5.0,
            )
            if raw:
                return raw
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — tier boundary, cascade on
            logger.info(
                "[RTGate] claude(injected) tier failed for %s (%s: %s)",
                caller_id, type(exc).__name__, exc,
            )
    # 1b. Provider-less Claude (constructs its own Aegis-wrapped client).
    try:
        from backend.core.ouroboros.claude_fallback import claude_inference

        raw = await asyncio.wait_for(
            claude_inference(
                prompt,
                caller_id=caller_id,
                response_format=response_format,
                max_tokens=max_tokens,
            ),
            timeout=timeout_s + 5.0,
        )
        if raw:
            return raw
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[RTGate] claude(fallback) tier failed for %s (%s: %s)",
            caller_id, type(exc).__name__, exc,
        )
    return None


async def _try_dw_rt(
    prompt: str,
    *,
    caller_id: str,
    system_prompt: str,
    max_tokens: int,
    response_format: Optional[Dict[str, Any]],
    timeout_s: float,
    dw_provider: Any = None,
    dw_model: Optional[str] = None,
) -> Optional[str]:
    """DW tier — the stream-free RT primitive ``complete_sync`` ONLY (never
    the 24h batch queue: a gate blocks an op, so a batch wait is forbidden
    here by design). Returns text or None."""
    if dw_provider is None or not hasattr(dw_provider, "complete_sync"):
        return None
    try:
        res = await asyncio.wait_for(
            dw_provider.complete_sync(
                prompt,
                system_prompt=system_prompt,
                caller_id=caller_id,
                model=dw_model,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                response_format=response_format,
            ),
            timeout=timeout_s + 5.0,
        )
        raw = getattr(res, "content", "") or ""
        if raw:
            return raw
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[RTGate] dw_rt tier failed for %s (%s: %s)",
            caller_id, type(exc).__name__, exc,
        )
    return None


async def gate_completion(
    prompt: str,
    *,
    caller_id: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 512,
    response_format: Optional[Dict[str, Any]] = None,
    timeout_s: Optional[float] = None,
    claude_provider: Any = None,
    dw_provider: Any = None,
    dw_model: Optional[str] = None,
) -> str:
    """Single-turn RT completion for a synchronous pipeline gate.

    Claude-RT first (buying time), DW-RT opportunistic fallback (availability),
    per :mod:`rt_gate` module contract. Raises
    :class:`GateProviderExhaustedError` when every tier fails — the caller's
    own fail-open/fail-closed semantics take over from there. Never returns
    an empty string.
    """
    t = timeout_s if timeout_s is not None else gate_rt_timeout_s()
    sys_p = system_prompt or _DEFAULT_SYSTEM

    async def _claude() -> Optional[str]:
        return await _try_claude(
            prompt, caller_id=caller_id, max_tokens=max_tokens,
            response_format=response_format, timeout_s=t,
            claude_provider=claude_provider,
        )

    async def _dw() -> Optional[str]:
        return await _try_dw_rt(
            prompt, caller_id=caller_id, system_prompt=sys_p,
            max_tokens=max_tokens, response_format=response_format,
            timeout_s=t, dw_provider=dw_provider, dw_model=dw_model,
        )

    tiers = (_claude, _dw) if claude_first_enabled() else (_dw, _claude)
    for tier in tiers:
        raw = await tier()
        if raw:
            return raw
    raise GateProviderExhaustedError(
        f"gate_completion exhausted all RT tiers (caller={caller_id}, "
        f"order={'claude,dw' if claude_first_enabled() else 'dw,claude'})"
    )
