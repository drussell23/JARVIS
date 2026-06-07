"""Slice 131 Phase 2b — The Asynchronous Claude Batch Engine.

BACKGROUND / SPECULATIVE ops are non-urgent — they do not need a real-time
response, so they should ride the Anthropic **Message Batches** endpoint
(``/v1/messages/batches``) for a flat **50% discount** instead of the standard
completions path.

This engine owns the full async lifecycle:

    pack → dispatch → AWAITING_BATCH (poll) → retrieve → inject as real-time

with a hard **FALLBACK invariant**: if the Batch API is disabled, the route is
ineligible, the create call 4xx/5xx's, the poll times out, or a result errors,
the engine **seamlessly falls back to the supplied real-time ``fallback``** so the
op is never starved. Gated ``JARVIS_BATCH_ROUTING_ENABLED`` default-FALSE → OFF
never touches the batch path.

The Anthropic batch client (sync ``client.messages.batches.create/retrieve/
results``) is **injectable**; the engine drives its blocking calls via
``asyncio.to_thread`` so the event loop is never blocked, and is tested with a
fake client (no network). NO model string is hardcoded here — the caller passes
the model (resolved upstream from ``brain_selection_policy.yaml``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_BATCH_ROUTING_ENABLED"
_ENV_POLL_INTERVAL = "JARVIS_BATCH_POLL_INTERVAL_S"
_ENV_MAX_WAIT = "JARVIS_BATCH_MAX_WAIT_S"

_DEFAULT_POLL_INTERVAL = 30.0   # Anthropic batches typically complete <1h
_DEFAULT_MAX_WAIT = 3600.0      # 1h; hard ceiling below the API's 24h max

# Non-urgent routes eligible for batch delegation.
_ELIGIBLE_ROUTES = frozenset({"background", "speculative"})


def batch_routing_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. Re-read each call → hot-revert.
    NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def is_batch_eligible(route: Any) -> bool:
    """True only for non-urgent routes (BACKGROUND / SPECULATIVE)."""
    return str(route or "").strip().lower() in _ELIGIBLE_ROUTES


def _poll_interval_s() -> float:
    try:
        return max(0.001, float(os.getenv(_ENV_POLL_INTERVAL, _DEFAULT_POLL_INTERVAL)))
    except (TypeError, ValueError):
        return _DEFAULT_POLL_INTERVAL


def _max_wait_s() -> float:
    try:
        return max(0.01, float(os.getenv(_ENV_MAX_WAIT, _DEFAULT_MAX_WAIT)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_WAIT


def pack_batch_request(
    custom_id: str, prompt: str, model: str, *,
    max_tokens: int = 16000, system: Optional[str] = None,
) -> Dict[str, Any]:
    """Package one op into a Message Batches request (the ``.jsonl`` row shape:
    ``{custom_id, params}``). ``params`` is a non-streaming Messages payload."""
    params: Dict[str, Any] = {
        "model": str(model),
        "max_tokens": int(max_tokens),
        "messages": [{"role": "user", "content": str(prompt or "")}],
    }
    if system:
        params["system"] = system
    return {"custom_id": str(custom_id), "params": params}


class BatchState(str, enum.Enum):
    """Closed lifecycle taxonomy."""

    COMPLETED = "completed"                    # batched result returned
    FELL_BACK_DISABLED = "fell_back_disabled"  # master off
    FELL_BACK_INELIGIBLE = "fell_back_ineligible"
    FELL_BACK_FAULT = "fell_back_fault"        # create/transport 4xx/5xx/error
    FELL_BACK_TIMEOUT = "fell_back_timeout"    # poll exceeded max_wait
    FELL_BACK_ERROR = "fell_back_error"        # batch result errored/missing


@dataclasses.dataclass
class BatchOutcome:
    result: Any
    state: BatchState
    batch_id: Optional[str] = None


_Fallback = Callable[[], Awaitable[Any]]


class ClaudeBatchEngine:
    """Async batch dispatch + reconciliation with a hard real-time fallback."""

    def __init__(self, *, client: Any = None) -> None:
        self._client = client  # sync Anthropic-like client; None → lazy default

    def _batches(self) -> Optional[Any]:
        c = self._client
        if c is None:
            return None
        try:
            return c.messages.batches
        except Exception:  # noqa: BLE001
            return None

    async def _dispatch(self, requests: List[Dict[str, Any]]) -> Optional[str]:
        batches = self._batches()
        if batches is None:
            return None
        batch = await asyncio.to_thread(batches.create, requests=requests)
        return getattr(batch, "id", None)

    async def poll_until_complete(self, batch_id: str) -> bool:
        """Poll batch status until ``ended`` or ``max_wait``. AWAITING_BATCH.
        Returns True iff it ended in time. NEVER raises (caller falls back)."""
        batches = self._batches()
        if batches is None:
            return False
        deadline = time.monotonic() + _max_wait_s()
        interval = _poll_interval_s()
        while time.monotonic() < deadline:
            b = await asyncio.to_thread(batches.retrieve, batch_id)
            if str(getattr(b, "processing_status", "")) == "ended":
                return True
            await asyncio.sleep(interval)
        return False

    async def _retrieve(self, batch_id: str, custom_id: str) -> Optional[Any]:
        """Pull the result for ``custom_id``. Returns the message on success,
        None on errored/missing (caller falls back)."""
        batches = self._batches()
        if batches is None:
            return None
        rows = await asyncio.to_thread(lambda: list(batches.results(batch_id)))
        for row in rows:
            if getattr(row, "custom_id", None) != custom_id:
                continue
            res = getattr(row, "result", None)
            if res is not None and getattr(res, "type", "") == "succeeded":
                return getattr(res, "message", None)
            return None  # errored / canceled / expired
        return None

    async def generate_or_fallback(
        self,
        *,
        prompt: str,
        model: str,
        route: Any,
        fallback: _Fallback,
        custom_id: str = "op",
        max_tokens: int = 16000,
        system: Optional[str] = None,
    ) -> BatchOutcome:
        """Route a non-urgent op through the Batch API for 50% off, else fall
        back to ``fallback`` (the real-time generation path). The FALLBACK
        invariant holds for every failure mode. NEVER raises."""
        if not batch_routing_enabled():
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_DISABLED)
        if not is_batch_eligible(route):
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_INELIGIBLE)

        batch_id: Optional[str] = None
        try:
            req = pack_batch_request(
                custom_id, prompt, model, max_tokens=max_tokens, system=system,
            )
            batch_id = await self._dispatch([req])
            if not batch_id:
                logger.debug("[BatchEngine] dispatch returned no id → fallback")
                return BatchOutcome(await fallback(), BatchState.FELL_BACK_FAULT, batch_id)
        except Exception as exc:  # noqa: BLE001 — 4xx/5xx/transport → fallback
            logger.info("[BatchEngine] dispatch fault (%s) → real-time fallback", exc)
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_FAULT, batch_id)

        # AWAITING_BATCH — poll to completion (bounded).
        try:
            ended = await self.poll_until_complete(batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("[BatchEngine] poll fault (%s) → fallback", exc)
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_FAULT, batch_id)
        if not ended:
            logger.info("[BatchEngine] batch %s exceeded max_wait → fallback", batch_id)
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_TIMEOUT, batch_id)

        # Retrieve + inject as if real-time.
        try:
            message = await self._retrieve(batch_id, custom_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("[BatchEngine] retrieve fault (%s) → fallback", exc)
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_FAULT, batch_id)
        if message is None:
            logger.info("[BatchEngine] batch %s result errored/missing → fallback", batch_id)
            return BatchOutcome(await fallback(), BatchState.FELL_BACK_ERROR, batch_id)

        logger.info("[BatchEngine] BATCH_HIT %s — 50%% discount (route=%s)", batch_id, route)
        return BatchOutcome(message, BatchState.COMPLETED, batch_id)


__all__ = [
    "batch_routing_enabled",
    "is_batch_eligible",
    "pack_batch_request",
    "BatchState",
    "BatchOutcome",
    "ClaudeBatchEngine",
]
