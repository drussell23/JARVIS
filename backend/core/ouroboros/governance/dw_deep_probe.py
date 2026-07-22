"""DW Deep-Probe — inference-lane health, not metadata-endpoint health.

A ``GET /v1/models`` 200 (stateless REST transport) is NECESSARY but NOT
SUFFICIENT: it says the control plane answers, not that the GPU inference lane
(direct SSE streaming) will survive a real generation. Trusting it produced a
false-positive recovery that woke the swarm onto a still-degraded DW. This probe
tests the lane that actually matters — a minimal streaming generation — and
measures its integrity:

  * **Ephemeral zero-shot payload.** A 1-word system prompt + a 2-char user ping,
    ``max_tokens`` hard-capped at 5. Forces the inference cluster to execute
    while keeping token spend mathematically negligible (no token bleed).

  * **Stream-Integrity via ITL thresholding.** First-token arrival alone is not
    trusted. The probe measures Time-To-First-Token AND the Inter-Token Latency
    across the ≤5 tokens. If mean/peak ITL exceeds a safe threshold (GPUs
    thrashing / stream dropping packets) the verdict is ``DEGRADED`` — overriding
    a 200 OK — so the swarm stays asleep.

  * **Asymmetric timeouts.** The 120s cold-start tolerance applies ONLY to the
    TTFT phase (397B VRAM load); every subsequent inter-token gap keeps the
    aggressive ITL watchdog bound. Both are the existing #70017
    ``watchdog_consume_sse`` dual-phase bounds — this module does NOT re-parse
    SSE.

DRY: composes ``stream_watchdog.watchdog_consume_sse`` / ``fast_abort_response``
(#70017), ``stream_rupture.StreamRuptureError``, and ``provider_heartbeat``'s
transport resolver. Yields the ``() -> bool`` ``probe_fn`` the DW Outage
Forecaster (#70030) injects. Never raises.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional

from backend.core.ouroboros.governance.stream_rupture import StreamRuptureError
from backend.core.ouroboros.governance.stream_watchdog import (
    fast_abort_response,
    watchdog_consume_sse,
)

logger = logging.getLogger("Ouroboros.DWDeepProbe")

_TTFT_ENV = "JARVIS_DW_DEEP_PROBE_TTFT_S"        # cold-start VRAM-load tolerance
_DEFAULT_TTFT_S = 120.0
_ITL_HARD_ENV = "JARVIS_DW_DEEP_PROBE_ITL_HARD_S"  # watchdog rupture bound (stall)
_DEFAULT_ITL_HARD_S = 8.0
_ITL_SAFE_ENV = "JARVIS_DW_DEEP_PROBE_ITL_SAFE_S"  # thrash classification threshold
_DEFAULT_ITL_SAFE_S = 2.0
_MAX_TOKENS = 5

VERDICT_HEALTHY = "HEALTHY"
VERDICT_DEGRADED = "DEGRADED"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class DeepProbeResult:
    healthy: bool
    verdict: str          # VERDICT_HEALTHY | VERDICT_DEGRADED
    ttft_s: float
    mean_itl_s: float
    max_itl_s: float
    tokens: int
    reason: str


def build_probe_payload(model: str, *, max_tokens: int = _MAX_TOKENS) -> dict:
    """Aggressively minimal zero-shot probe body — forces inference, negligible
    token spend. ``max_tokens`` is hard-capped so the cluster executes but the
    generation is mathematically tiny (no background token bleed)."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply."},
            {"role": "user", "content": "ok"},
        ],
        "max_tokens": int(max_tokens),
        "temperature": 0.0,
        "stream": True,
    }


# dispatch_fn(payload) -> an async readline (or a (readline, response) tuple for
# fast-abort). Production streams to DW; tests inject a mock line source.
DispatchFn = Callable[[dict], Awaitable[Any]]


async def deep_probe(
    *,
    dispatch_fn: DispatchFn,
    model: str = "",
    ttft_bound_s: Optional[float] = None,
    itl_hard_s: Optional[float] = None,
    itl_safe_s: Optional[float] = None,
    max_tokens: int = _MAX_TOKENS,
) -> DeepProbeResult:
    """Stream a minimal generation and grade the inference lane. Never raises.

    Verdict:
      * StreamRuptureError (TTFT or ITL hard-breach) -> DEGRADED
      * zero tokens produced                          -> DEGRADED
      * mean/peak ITL over the safe threshold         -> DEGRADED (overrides 200)
      * otherwise                                     -> HEALTHY
    """
    ttft = ttft_bound_s if ttft_bound_s is not None else _env_float(_TTFT_ENV, _DEFAULT_TTFT_S)
    itl_hard = itl_hard_s if itl_hard_s is not None else _env_float(_ITL_HARD_ENV, _DEFAULT_ITL_HARD_S)
    itl_safe = itl_safe_s if itl_safe_s is not None else _env_float(_ITL_SAFE_ENV, _DEFAULT_ITL_SAFE_S)
    payload = build_probe_payload(model, max_tokens=max_tokens)

    token_times: List[float] = []

    def _on_token(_tok: str) -> None:
        token_times.append(time.monotonic())

    _abort_target: dict = {"resp": None}

    def _abort() -> None:
        if _abort_target["resp"] is not None:
            fast_abort_response(_abort_target["resp"])

    start = time.monotonic()
    try:
        dispatched = await dispatch_fn(payload)
        if isinstance(dispatched, tuple):
            readline, resp = dispatched
            _abort_target["resp"] = resp
        else:
            readline = dispatched
        await watchdog_consume_sse(
            readline, ttft_s=ttft, itl_s=itl_hard, abort_fn=_abort,
            provider="doubleword", on_token=_on_token,
        )
    except StreamRuptureError as exc:
        return DeepProbeResult(
            healthy=False, verdict=VERDICT_DEGRADED,
            ttft_s=(token_times[0] - start) if token_times else -1.0,
            mean_itl_s=-1.0, max_itl_s=-1.0, tokens=len(token_times),
            reason=f"stream_rupture:{getattr(exc, 'phase', '?')}",
        )
    except Exception as exc:  # noqa: BLE001 — any dispatch/transport fault = lane down
        return DeepProbeResult(
            healthy=False, verdict=VERDICT_DEGRADED, ttft_s=-1.0,
            mean_itl_s=-1.0, max_itl_s=-1.0, tokens=len(token_times),
            reason=f"dispatch_error:{type(exc).__name__}",
        )

    tokens = len(token_times)
    if tokens == 0:
        return DeepProbeResult(
            healthy=False, verdict=VERDICT_DEGRADED, ttft_s=-1.0,
            mean_itl_s=-1.0, max_itl_s=-1.0, tokens=0, reason="no_tokens",
        )
    ttft_measured = token_times[0] - start
    itls = [token_times[i] - token_times[i - 1] for i in range(1, tokens)]
    mean_itl = (sum(itls) / len(itls)) if itls else 0.0
    max_itl = max(itls) if itls else 0.0

    # Stream-Integrity: terrible ITL overrides a 200 OK — GPUs thrashing.
    if mean_itl > itl_safe or max_itl > (itl_safe * 2.0):
        return DeepProbeResult(
            healthy=False, verdict=VERDICT_DEGRADED, ttft_s=ttft_measured,
            mean_itl_s=mean_itl, max_itl_s=max_itl, tokens=tokens,
            reason=f"itl_thrash mean={mean_itl:.2f}s peak={max_itl:.2f}s safe={itl_safe:.2f}s",
        )
    return DeepProbeResult(
        healthy=True, verdict=VERDICT_HEALTHY, ttft_s=ttft_measured,
        mean_itl_s=mean_itl, max_itl_s=max_itl, tokens=tokens, reason="healthy",
    )


def make_deep_probe_fn(
    dispatch_fn: Optional[DispatchFn] = None, **probe_kwargs: Any,
) -> Callable[[], Awaitable[bool]]:
    """Adapt :func:`deep_probe` into the forecaster's ``probe_fn`` contract
    (``() -> Awaitable[bool]``). Production default streams to DW; injectable."""
    df = dispatch_fn or _default_dw_stream_dispatch

    async def _probe() -> bool:
        res = await deep_probe(dispatch_fn=df, **probe_kwargs)
        logger.info(
            "[DWDeepProbe] verdict=%s ttft=%.1fs mean_itl=%.2fs peak_itl=%.2fs "
            "tokens=%d (%s)",
            res.verdict, res.ttft_s, res.mean_itl_s, res.max_itl_s,
            res.tokens, res.reason,
        )
        return res.healthy

    return _probe


async def _default_dw_stream_dispatch(payload: dict) -> Any:
    """Production dispatch: a real minimal streaming POST to DW, returning an
    async readline over the SSE body + the response (for fast-abort). Composes
    ``provider_heartbeat``'s transport resolver (base URL + auth headers) — no
    parallel transport. Best-effort session cleanup. Never raises to the caller
    beyond transport exceptions (which deep_probe classifies as DEGRADED)."""
    import aiohttp  # lazy — keep module import cheap
    from backend.core.ouroboros.governance.provider_heartbeat import (
        _resolve_dw_probe_transport,
    )

    url, headers = await _resolve_dw_probe_transport()
    headers = {**headers, "Accept": "text/event-stream"}
    session = aiohttp.ClientSession()
    resp = await session.post(url, json=payload, headers=headers)

    async def _readline() -> Any:
        line = await resp.content.readline()
        if not line:
            try:
                resp.release()
            finally:
                await session.close()
        return line

    return (_readline, resp)


__all__ = [
    "DeepProbeResult",
    "VERDICT_DEGRADED",
    "VERDICT_HEALTHY",
    "build_probe_payload",
    "deep_probe",
    "make_deep_probe_fn",
]
