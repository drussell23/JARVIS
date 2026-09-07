"""Context budget — every large-file limit derives from the served model's window.

Why this exists
---------------
Two flat numbers decided whether a file was "big": ``JARVIS_DW_BIG_FILE_LINE_
THRESHOLD`` (300 lines) and ``JARVIS_DW_MAX_CONTEXT_TOKENS`` (8000 tokens). Both
were sized for a cloud lane years ago and describe nothing about the model that
actually answers on the local lane. That lane already NEGOTIATES its context
window from measured hardware — node VRAM minus the served model's bytes,
divided by the model's own KV bytes per token, clamped to its trained context
(``CandidateGenerator._negotiate_num_ctx`` → ``derive_safe_num_ctx``). This
module turns that negotiated window into the ONE budget every big-file decision
reads:

    ingest_ceiling = window − output_reserve − fixed_overhead

* ``window``          the negotiated ``num_ctx`` (tokens) for the endpoint
* ``output_reserve``  what the model needs to ANSWER (its output ratio)
* ``fixed_overhead``  what the prompt spends before any source: system prompt,
                      map-reduce framing, the AST-Signature Anchor

A file whose source exceeds the ceiling is big; a Radius of Relevance must fit
the node budget; nothing is ever a literal. The budget is primed once per
endpoint by the async lane (network + hardware reads) and served from a
process cache to the synchronous readers (``exceeds_ceiling``, ``is_big_file``),
which never block. With no primed budget the ceiling derives from the same
floor the negotiator falls back to — still not a literal of this module's own.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.ContextBudget")

_ENV_OUTPUT_RESERVE_FRACTION = "JARVIS_CONTEXT_OUTPUT_RESERVE_FRACTION"
_ENV_BUDGET_TTL_S = "JARVIS_CONTEXT_BUDGET_TTL_S"
_ENV_CEILING_OVERRIDE = "JARVIS_CONTEXT_INGEST_CEILING_TOKENS"   # explicit operator override only
_ENV_CHARS_PER_TOKEN = "JARVIS_CONTEXT_CHARS_PER_TOKEN"

#: The coarse chars-per-token estimate every existing token counter in this
#: package uses (``intelligent_chunking.estimate_tokens``,
#: ``chunked_generation._CoarseTokenCounter``). One place to tune it.
DEFAULT_CHARS_PER_TOKEN = 4


def chars_per_token() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_CHARS_PER_TOKEN, "").strip() or DEFAULT_CHARS_PER_TOKEN))
    except ValueError:
        return DEFAULT_CHARS_PER_TOKEN


def estimate_tokens(text: str) -> int:
    """Deterministic coarse estimate; never raises."""
    return max(0, len(text or "")) // chars_per_token()


def output_reserve_fraction() -> float:
    """Share of the window reserved for the model's ANSWER. Derived from the
    local lane's configured output ratio (``est_output_tokens = prompt_tokens ×
    ratio`` → reserve = ratio / (1 + ratio)); the env overrides; clamped."""
    raw = os.environ.get(_ENV_OUTPUT_RESERVE_FRACTION, "").strip()
    if raw:
        try:
            return min(0.9, max(0.05, float(raw)))
        except ValueError:
            pass
    try:
        from backend.core.ouroboros.governance.local_inference_director import LocalConfig
        ratio = float(getattr(LocalConfig.from_env(), "output_ratio", 0.0) or 0.0)
        if ratio > 0:
            return min(0.9, max(0.05, ratio / (1.0 + ratio)))
    except Exception:  # noqa: BLE001
        pass
    return 0.25


def budget_ttl_s() -> float:
    """How long a primed budget is trusted before the lane re-negotiates
    (hardware/model can change under a running organism)."""
    try:
        return max(1.0, float(os.environ.get(_ENV_BUDGET_TTL_S, "").strip() or 300.0))
    except ValueError:
        return 300.0


def fallback_window_tokens() -> int:
    """The window when nothing has been negotiated: the SAME floor the
    negotiator itself falls back to (``JARVIS_NUM_CTX_FLOOR``), never a literal
    of this module's own."""
    try:
        from backend.core.ouroboros.governance import local_inference_director as lid
        return int(lid._int_env("JARVIS_NUM_CTX_FLOOR", lid._NUM_CTX_FLOOR_DEFAULT))
    except Exception:  # noqa: BLE001
        return 4096


@dataclass(frozen=True)
class ContextBudget:
    """The one budget every large-file decision reads. Tokens throughout."""

    endpoint: str
    window_tokens: int
    output_reserve_tokens: int
    fixed_overhead_tokens: int = 0
    served_model: str = ""
    negotiated: bool = False          # False → derived from the floor, not the node
    primed_at: float = 0.0

    @property
    def ingest_ceiling_tokens(self) -> int:
        """Max source tokens the prompt may carry WHOLE."""
        return max(1, self.window_tokens - self.output_reserve_tokens - self.fixed_overhead_tokens)

    @property
    def node_budget_tokens(self) -> int:
        """Max tokens a single Radius of Relevance may occupy (same ceiling —
        a node is the whole ingest of a map-reduce prompt)."""
        return self.ingest_ceiling_tokens

    def with_overhead(self, *fixed_texts: str) -> "ContextBudget":
        """A copy whose fixed overhead is the measured size of the prompt parts
        that precede any source (system prompt, framing, anchor)."""
        return replace(self, fixed_overhead_tokens=sum(estimate_tokens(t) for t in fixed_texts))

    def fits(self, text: str) -> bool:
        return estimate_tokens(text) <= self.ingest_ceiling_tokens

    def line_threshold_for(self, source: str) -> int:
        """The line count at which THIS file crosses the ceiling — derived from
        the file's own average line length, so a dense file crosses sooner
        than a sparse one. Reporting only; decisions compare tokens."""
        lines = (source or "").count("\n") + 1
        avg_chars = max(1.0, len(source or "") / max(1, lines))
        return max(1, int(self.ingest_ceiling_tokens * chars_per_token() / avg_chars))

    def fresh(self, now_ts: Optional[float] = None) -> bool:
        return (float(now_ts if now_ts is not None else time.time()) - self.primed_at) <= budget_ttl_s()


def _budget_from_window(endpoint: str, window: int, *, served_model: str = "", negotiated: bool) -> ContextBudget:
    window = max(1, int(window))
    reserve = max(1, int(window * output_reserve_fraction()))
    return ContextBudget(
        endpoint=str(endpoint or ""), window_tokens=window, output_reserve_tokens=reserve,
        served_model=str(served_model or ""), negotiated=negotiated, primed_at=time.time(),
    )


def _ceiling_override() -> Optional[int]:
    raw = os.environ.get(_ENV_CEILING_OVERRIDE, "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _budget_from_ceiling(endpoint: str, ceiling: int, *, served_model: str = "") -> ContextBudget:
    """A budget whose ingest ceiling IS the operator's number: the window is
    back-derived so reserve + ceiling reconstruct it exactly."""
    ceiling = max(1, int(ceiling))
    frac = output_reserve_fraction()
    window = max(ceiling + 1, int(round(ceiling / max(1e-6, 1.0 - frac))))
    reserve = max(1, window - ceiling)
    return ContextBudget(
        endpoint=str(endpoint or ""), window_tokens=window, output_reserve_tokens=reserve,
        served_model=str(served_model or ""), negotiated=False, primed_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Priming (async lane) and the process cache (sync readers)
# ---------------------------------------------------------------------------

_CACHE: Dict[str, ContextBudget] = {}
_DEFAULT_KEY = ""
_LOCKS: Dict[str, "asyncio.Lock"] = {}


def _lock_for(endpoint: str) -> "asyncio.Lock":
    lock = _LOCKS.get(endpoint)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[endpoint] = lock
    return lock


Negotiator = Callable[[str], Awaitable[Optional[int]]]


async def prime_budget(
    endpoint: str, negotiator: Negotiator, *, served_model: str = "", force: bool = False,
) -> ContextBudget:
    """Negotiate the window for *endpoint* through *negotiator* (the lane's
    own ``_negotiate_num_ctx``) and cache the budget. A negotiation that yields
    nothing primes a FLOOR-derived budget (still not a literal) so readers
    never see an unbounded state. NEVER raises."""
    key = str(endpoint or "")
    override = _ceiling_override()
    if override is not None:
        # The operator fixed the ceiling: that is authority, not a hint. No
        # hardware/network negotiation is spent on a number already decided
        # (and a unit test that pins the ceiling never touches the node).
        budget = _budget_from_ceiling(key, override, served_model=served_model)
        _CACHE[key] = budget
        _CACHE[_DEFAULT_KEY] = budget
        return budget
    cached = _CACHE.get(key)
    if cached is not None and cached.fresh() and not force:
        return cached
    async with _lock_for(key):
        cached = _CACHE.get(key)
        if cached is not None and cached.fresh() and not force:
            return cached
        window: Optional[int] = None
        try:
            window = await negotiator(key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — negotiation is advisory; the floor is not
            logger.debug("[ContextBudget] negotiation degraded for %s", key, exc_info=True)
        if window and window > 0:
            budget = _budget_from_window(key, window, served_model=served_model, negotiated=True)
        else:
            budget = _budget_from_window(key, fallback_window_tokens(), served_model=served_model, negotiated=False)
        _CACHE[key] = budget
        _CACHE[_DEFAULT_KEY] = budget   # the lane's latest budget serves endpoint-less readers
        logger.info(
            "[ContextBudget] primed %s: window=%d reserve=%d ceiling=%d negotiated=%s model=%s",
            key or "-", budget.window_tokens, budget.output_reserve_tokens,
            budget.ingest_ceiling_tokens, budget.negotiated, budget.served_model or "-",
        )
        return budget


def current_budget(endpoint: Optional[str] = None) -> Optional[ContextBudget]:
    """The primed budget for *endpoint* (or the lane's latest). ``None`` when
    nothing has been primed in this process."""
    if endpoint:
        b = _CACHE.get(str(endpoint))
        if b is not None:
            return b
    return _CACHE.get(_DEFAULT_KEY)


def ingest_ceiling_tokens(endpoint: Optional[str] = None) -> int:
    """The ceiling the synchronous readers use. An explicit operator override
    wins; else the primed budget; else a floor-derived budget."""
    override = _ceiling_override()
    if override is not None:
        return override
    b = current_budget(endpoint)
    if b is None:
        b = _budget_from_window(_DEFAULT_KEY, fallback_window_tokens(), negotiated=False)
    return b.ingest_ceiling_tokens


def exceeds_ceiling(source: str, endpoint: Optional[str] = None) -> bool:
    return estimate_tokens(source) > ingest_ceiling_tokens(endpoint)


def reset_cache() -> None:
    """Tests and FSM→DORMANT: forget every primed budget."""
    _CACHE.clear()


__all__ = [
    "ContextBudget", "DEFAULT_CHARS_PER_TOKEN", "budget_ttl_s", "chars_per_token", "current_budget",
    "estimate_tokens", "exceeds_ceiling", "fallback_window_tokens", "ingest_ceiling_tokens",
    "output_reserve_fraction", "prime_budget", "reset_cache",
]
