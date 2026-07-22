"""DW Deep-Probe — inference-lane health with ITL stream-integrity thresholding.

Mandated bulletproof (async, mocked SSE stream):
  1. Acceptable TTFT but TERRIBLE ITL → classified DEGRADED (overrides the 200
     OK) and keeps the swarm asleep (probe_fn → False).
  2. The probe payload strictly enforces max_tokens=5 (negligible token spend).
  3. A HEALTHY stream → HEALTHY, and drives the forecaster's watch loop to
     awaken the swarm (recovered=True with a slow-start AIMD controller).

Plus: hard-ITL rupture → DEGRADED; a transport/dispatch fault → DEGRADED.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from backend.core.ouroboros.governance.dw_deep_probe import (
    VERDICT_DEGRADED,
    VERDICT_HEALTHY,
    build_probe_payload,
    deep_probe,
    make_deep_probe_fn,
)
from backend.core.ouroboros.governance.dw_outage_forecaster import watch_for_recovery


def _sse_token(text: str) -> bytes:
    return f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}'.encode()


def _mock_stream(gaps_and_tokens):
    """Build a dispatch_fn whose readline yields (gap, token) pairs then closes.
    Each ``gap`` is a real (tiny) asyncio.sleep so the watchdog measures true
    inter-token latency — thresholds in the tests are scaled to match."""
    seq = list(gaps_and_tokens)

    async def _dispatch(payload):
        it = iter(seq)

        async def _readline():
            try:
                gap, tok = next(it)
            except StopIteration:
                return b""            # stream closed cleanly
            if gap:
                await asyncio.sleep(gap)
            return _sse_token(tok)

        return _readline

    return _dispatch


# ---------------------------------------------------------------------------
# (2) Ephemeral zero-shot payload — max_tokens strictly 5.
# ---------------------------------------------------------------------------


async def test_probe_payload_is_minimal_and_capped() -> None:
    payload = build_probe_payload("Qwen/Qwen3.5-397B-A17B-FP8")
    assert payload["max_tokens"] == 5                    # hard cap — negligible spend
    assert payload["stream"] is True                     # must exercise the SSE lane
    # 1-word system prompt + a tiny user ping — aggressively minimal.
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert len(payload["messages"][0]["content"].split()) <= 2


# ---------------------------------------------------------------------------
# (1) Acceptable TTFT, terrible ITL → DEGRADED (override 200), swarm asleep.
# ---------------------------------------------------------------------------


async def test_terrible_itl_classified_degraded_keeps_swarm_asleep() -> None:
    # TTFT fine (first token ~0), but tokens 2-5 arrive with big gaps → thrash.
    dispatch = _mock_stream([
        (0.0, "a"),      # first token — good TTFT
        (0.12, "b"),     # then large inter-token gaps
        (0.12, "c"),
        (0.12, "d"),
    ])
    res = await deep_probe(
        dispatch_fn=dispatch, ttft_bound_s=120.0,
        itl_hard_s=8.0,      # generous hard bound → NO rupture
        itl_safe_s=0.05,     # tight safe threshold → mean ITL 0.12 > 0.05
    )
    assert res.verdict == VERDICT_DEGRADED
    assert res.healthy is False
    assert res.mean_itl_s > 0.05
    assert "itl_thrash" in res.reason

    # The forecaster's probe_fn therefore reports "still down" → swarm asleep.
    probe_fn = make_deep_probe_fn(
        dispatch, ttft_bound_s=120.0, itl_hard_s=8.0, itl_safe_s=0.05,
    )
    assert await probe_fn() is False


async def test_hard_itl_stall_ruptures_to_degraded() -> None:
    # A gap that exceeds the ITL HARD bound → watchdog StreamRuptureError.
    dispatch = _mock_stream([
        (0.0, "a"),
        (0.20, "b"),    # 0.20s gap > 0.05s hard bound → rupture
    ])
    res = await deep_probe(
        dispatch_fn=dispatch, ttft_bound_s=120.0,
        itl_hard_s=0.05,     # aggressive hard bound
        itl_safe_s=2.0,
    )
    assert res.verdict == VERDICT_DEGRADED
    assert "stream_rupture" in res.reason


async def test_dispatch_fault_is_degraded_not_crash() -> None:
    async def _boom(payload):
        raise ConnectionResetError("DW socket reset")

    res = await deep_probe(dispatch_fn=_boom, ttft_bound_s=1.0)
    assert res.verdict == VERDICT_DEGRADED
    assert res.healthy is False
    assert "dispatch_error" in res.reason


# ---------------------------------------------------------------------------
# (3) Healthy stream → HEALTHY → forecaster awakens the swarm.
# ---------------------------------------------------------------------------


async def test_healthy_stream_awakens_forecaster() -> None:
    # Fast, steady tokens — low TTFT, low ITL.
    healthy_dispatch = _mock_stream([
        (0.0, "o"), (0.01, "k"), (0.01, "!"), (0.01, "!"), (0.01, "!"),
    ])
    res = await deep_probe(
        dispatch_fn=healthy_dispatch, ttft_bound_s=120.0,
        itl_hard_s=8.0, itl_safe_s=0.05,
    )
    assert res.verdict == VERDICT_HEALTHY
    assert res.healthy is True
    assert res.tokens == 5
    assert res.mean_itl_s <= 0.05

    # Feed the healthy deep-probe into the forecaster's watch loop → it awakens
    # the swarm (recovered) with a slow-start (limit 1→2) AIMD controller.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    probe_fn = make_deep_probe_fn(
        healthy_dispatch, ttft_bound_s=120.0, itl_hard_s=8.0, itl_safe_s=0.05,
    )
    clock = {"t": 0.0}

    async def _sleep(s):
        clock["t"] += s

    outcome = await watch_for_recovery(
        conn, probe_fn, now_fn=lambda: clock["t"], sleep_fn=_sleep, max_probes=3,
    )
    assert outcome.recovered is True
    assert outcome.aimd.limit <= 2          # slow-start, not slammed at max
    assert outcome.aimd.max_limit >= 1
