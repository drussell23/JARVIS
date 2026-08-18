"""Cognition Lane Router — Dynamic SLA routing for prompt-shaped inference.

The audit truth: every ``prompt_only`` consumer (Synthesis Engine,
Architecture Agent, DreamEngine, SemanticTriage, IntentDiscovery, the
persona council) rode Doubleword's BATCH plane — the Brain guiding a
realtime Worker through a 24-hour-SLA mail slot. The fix is NOT a blanket
RT migration (that trades deadlocks for 429 storms): it is deterministic
routing by the workload's TEMPORAL class, plus a concurrency ceiling that
makes the RT tier structurally un-stampede-able.

  * SLA_STRICT → the Realtime SSE tier: the SAME primitive proven by the
    council's voice — Aegis call lease, ``service_tier: "priority"``,
    output clamp, per-turn bound with EXPLICIT socket eviction on timeout
    (zombie-billing guard), single-attempt cascade to Claude.
  * SLA_BULK  → the caller stays on the batch plane (the 2× discount is
    what batch is FOR — idle-cycle pre-computation has no deadline).

COGNITIVE CONCURRENCY SEMAPHORE: one process-wide ceiling (env
``JARVIS_COGNITION_RT_CONCURRENCY``, default 3) over every RT-lane
cognition call. A thundering herd queues asynchronously behind it instead
of volleying 429s at the tier. Per-event-loop instances (a Semaphore is
loop-bound; tests and prod each get their own).

ONE implementation (mandate 3): ``CouncilVoice`` delegates here; no second
copy of lease/clamp/eviction/cascade logic exists anywhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.CognitionLanes")

SLA_STRICT = "strict"
SLA_BULK = "bulk"


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


#: Module stats — read by tests and (later) ov doctor. Never authoritative.
stats: Dict[str, int] = {
    "rt_calls": 0, "rt_ok": 0, "rt_evictions": 0,
    "fallback_calls": 0, "fallback_ok": 0,
    "inflight": 0, "peak_inflight": 0,
}

#: Per-loop semaphores (asyncio primitives are loop-bound).
_semaphores: Dict[int, asyncio.Semaphore] = {}


def _rt_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_env_int("JARVIS_COGNITION_RT_CONCURRENCY", 3))
        # bounded book-keeping: drop entries for dead loops
        if len(_semaphores) > 8:
            _semaphores.clear()
        _semaphores[key] = sem
    return sem


async def _default_headers(caller_id: str) -> Dict[str, str]:
    """Aegis session auth + call lease + ZDR — the GENERATE tier's exact
    admission sequence (proven by the council's voice). NEVER raises."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        _aegis_dw_session_auth_header, _dw_apply_zdr,
    )
    auth = dict(await _aegis_dw_session_auth_header())
    try:
        from backend.core.ouroboros.governance.doubleword_provider import (
            _aegis_acquire_call_lease, _aegis_merge_lease_headers,
        )
        lease = await _aegis_acquire_call_lease(
            op_id=f"cognition-{caller_id}"[:48], route="background",
            estimated_cost_usd=0.05)
        auth = _aegis_merge_lease_headers(auth, lease)
    except Exception:  # noqa: BLE001 — lease is harness-context enrichment
        pass
    return _dw_apply_zdr(auth)


class RTPromptTimeout(Exception):
    """RT cognition call exceeded its bound (stream explicitly evicted)."""


async def rt_prompt(
    prompt: str,
    *,
    model: str,
    caller_id: str = "cognition",
    max_tokens: Optional[int] = None,
    response_format: Optional[Dict[str, Any]] = None,
    timeout_s: Optional[float] = None,
    clamp_tokens: Optional[int] = None,
    dw_provider: Any = None,
    session: Any = None,
    base_url: Optional[str] = None,
    auth_headers_fn: Any = None,
    claude: Any = None,
    call_stats: Optional[Dict[str, int]] = None,
) -> str:
    """ONE RT-lane prompt call: semaphore → lease → SSE stream → clamp →
    bound → explicit eviction → Claude cascade. Raises only the fallback's
    terminal error (RT faults always cascade). Fully injectable."""
    import aiohttp

    bound = timeout_s if timeout_s is not None else _env_float(
        "JARVIS_COGNITION_RT_TIMEOUT_S", 90.0)
    clamp = clamp_tokens if clamp_tokens is not None else _env_int(
        "JARVIS_COGNITION_RT_MAX_OUTPUT_TOKENS", 2000)
    if clamp > 0:
        max_tokens = min(int(max_tokens or clamp), clamp)

    def _bump(key: str, delta: int = 1) -> None:
        stats[key] = stats.get(key, 0) + delta
        if call_stats is not None:
            call_stats[key] = call_stats.get(key, 0) + delta

    #: Providers THIS call constructed, and therefore owns. An injected
    #: provider belongs to its caller and is never touched here -- closing
    #: someone else's pooled session would turn a leak into an outage.
    _owned: List[Any] = []

    async def _resolve() -> Tuple[Any, str]:
        nonlocal dw_provider
        if session is not None and base_url is not None:
            return session, base_url
        if dw_provider is None:
            from backend.core.ouroboros.governance.doubleword_provider import (
                DoublewordProvider,
            )
            # WHOEVER CREATES IT, CLOSES IT.
            #
            # This line built a provider on every un-injected call and let it
            # fall out of scope with `rt_prompt`. The provider lazily opens an
            # aiohttp.ClientSession inside `_get_session()`, so each call left
            # one behind for the garbage collector: bt-2026-08-18-021438 logged
            # 20 "Unclosed client session" + 15 "Unclosed connector" warnings,
            # every cluster landing against `[CognitionLanes] RT degraded (rt
            # status 402 ...)` -- one leak per RT attempt.
            #
            # The provider is not at fault: it owns `close()` and closes its
            # session correctly. Nothing called it, because nothing had
            # decided who owned the object.
            dw_provider = DoublewordProvider()
            _owned.append(dw_provider)
        return (session or await dw_provider._get_session(),
                base_url or dw_provider._base_url)

    async def _release_owned() -> None:
        """Close providers this call created. NEVER raises, never blocks exit.

        Runs in a ``finally`` that may execute while the task is being
        cancelled or the loop is closing, where an await can itself raise --
        so every close is individually guarded and a failure to release is
        logged, never propagated. Losing a session at that point is a leak;
        raising here would lose the caller's result or their cancellation."""
        while _owned:
            provider = _owned.pop()
            try:
                await provider.close()
            except Exception:  # noqa: BLE001 — teardown must not surface here
                logger.debug(
                    "[CognitionLanes] owned provider close degraded",
                    exc_info=True,
                )

    async def _headers() -> Dict[str, str]:
        if auth_headers_fn is not None:
            out = auth_headers_fn()
            return await out if asyncio.iscoroutine(out) else out
        return await _default_headers(caller_id)

    async def _rt_attempt() -> str:
        sess, base = await _resolve()
        headers = await _headers()
        body: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        # Tier selector via the ONE canonical seam (provider helper honors
        # the master flag, env tier override, and the per-model rejection
        # cache); literal fallback keeps this lane standalone-injectable.
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (
                apply_rt_service_tier,
            )
            body = apply_rt_service_tier(body, model)
        except Exception:  # noqa: BLE001 — provider import must never gate RT
            body["service_tier"] = "priority"
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if response_format:
            body["response_format"] = response_format
        resp_holder: List[Any] = [None]

        async def _consume() -> str:
            pieces: List[str] = []
            async with sess.post(
                f"{base}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=bound + 10),
            ) as resp:
                resp_holder[0] = resp
                if resp.status >= 300:
                    raise RuntimeError(
                        f"rt status {resp.status}: "
                        f"{(await resp.text())[:160]}")
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = (json.loads(data)["choices"][0]
                                 .get("delta") or {})
                        piece = delta.get("content")
                        if piece:
                            pieces.append(piece)
                    except Exception:  # noqa: BLE001 — tolerate keepalives
                        continue
            return "".join(pieces)

        _bump("rt_calls")
        try:
            text = await asyncio.wait_for(_consume(), timeout=bound)
            _bump("rt_ok")
            return text
        except asyncio.TimeoutError:
            resp = resp_holder[0]
            if resp is not None:
                try:
                    resp.close()          # EXPLICIT eviction — billing stops
                except Exception:  # noqa: BLE001
                    pass
            _bump("rt_evictions")
            raise RTPromptTimeout(
                f"rt cognition call exceeded {bound:.0f}s (stream evicted)")

    # The release wraps EVERY exit: the successful return inside the
    # semaphore, the Claude cascade below it, and any exception or
    # cancellation through either. The RT path fails far more often than it
    # succeeds when a provider is down — which is precisely the run that
    # leaked twenty sessions — so a release reachable only on the happy path
    # would be a release that never runs when it matters.
    try:
        sem = _rt_semaphore()
        async with sem:                   # the Cognitive Concurrency Semaphore
            stats["inflight"] += 1
            stats["peak_inflight"] = max(stats["peak_inflight"],
                                         stats["inflight"])
            try:
                try:
                    return await _rt_attempt()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — timeout OR transport
                    logger.info("[CognitionLanes] RT degraded (%s: %s) — "
                                "cascading to Claude (caller=%s)",
                                type(exc).__name__, str(exc)[:120], caller_id)
            finally:
                stats["inflight"] -= 1

        # Claude cascade OUTSIDE the semaphore — a fallback turn must not hold
        # an RT slot hostage while it waits on a different provider.
        _bump("fallback_calls")
        nonlocal_claude = claude
        if nonlocal_claude is None:
            from backend.core.ouroboros.governance.providers import (
                ClaudeProvider,
            )
            nonlocal_claude = ClaudeProvider(
                api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        text = await nonlocal_claude.prompt_only(
            prompt, caller_id=caller_id, max_tokens=max_tokens,
            timeout_s=_env_float("JARVIS_COGNITION_FALLBACK_TIMEOUT_S", 60.0))
        _bump("fallback_ok")
        return text
    finally:
        await _release_owned()


__all__ = ["SLA_STRICT", "SLA_BULK", "RTPromptTimeout", "rt_prompt", "stats"]
