"""Cognition Lane Router — Dynamic SLA routing + Concurrency Semaphore."""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance import cognition_lanes as cl


class _FakeClaude:
    def __init__(self):
        self.calls = []

    async def prompt_only(self, prompt, *, caller_id="", max_tokens=None,
                          timeout_s=60.0, **kw):
        self.calls.append(prompt)
        return "claude-fallback-text"


async def _sse_server(handler):
    from aiohttp import web
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


def _reset_stats():
    for k in cl.stats:
        cl.stats[k] = 0
    cl._semaphores.clear()


# ---------------------------------------------------------------------------
# THE MANDATE TEST — Thundering Herd: 5 concurrent RT calls, semaphore 3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thundering_herd_queues_behind_semaphore(monkeypatch):
    """5 concurrent strict-SLA calls: max 3 in flight (server-observed AND
    client-observed), zero 429s, ALL 5 complete."""
    import aiohttp
    from aiohttp import web

    monkeypatch.setenv("JARVIS_COGNITION_RT_CONCURRENCY", "3")
    _reset_stats()
    server_state = {"inflight": 0, "peak": 0, "served": 0}

    async def _serve(request):
        server_state["inflight"] += 1
        server_state["peak"] = max(server_state["peak"],
                                   server_state["inflight"])
        if server_state["inflight"] > 3:
            # what the real tier would do to a stampede:
            server_state["inflight"] -= 1
            return web.json_response({"error": "rate limited"}, status=429)
        try:
            resp = web.StreamResponse()
            resp.headers["Content-Type"] = "text/event-stream"
            await resp.prepare(request)
            await asyncio.sleep(0.15)          # hold the slot — force overlap
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
            await resp.write(b"data: [DONE]\n\n")
            server_state["served"] += 1
            return resp
        finally:
            server_state["inflight"] -= 1

    runner, base = await _sse_server(_serve)
    session = aiohttp.ClientSession()
    try:
        results = await asyncio.gather(*[
            cl.rt_prompt(f"herd-{i}", model="m", caller_id=f"c{i}",
                         session=session, base_url=base,
                         auth_headers_fn=lambda: {},
                         claude=_FakeClaude())
            for i in range(5)
        ])
        assert results == ["ok"] * 5               # ALL 5 completed
        assert server_state["served"] == 5
        assert server_state["peak"] <= 3           # server never saw a stampede
        assert cl.stats["peak_inflight"] <= 3      # client-side ceiling held
        assert cl.stats["rt_ok"] == 5
        assert cl.stats["fallback_calls"] == 0     # zero 429 cascades
    finally:
        await session.close()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Dynamic SLA routing at the prompt_only gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_only_strict_routes_to_rt(monkeypatch):
    """sla='strict' delegates to rt_prompt (batch cycle never touched —
    the batch path would explode on network in this test)."""
    captured = {}

    async def _fake_rt(prompt, **kw):
        captured.update(kw, prompt=prompt)
        return "rt-routed"

    monkeypatch.setattr(cl, "rt_prompt", _fake_rt)
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    dw = DoublewordProvider()
    out = await dw.prompt_only("plan the fix", caller_id="synthesis_engine",
                               max_tokens=1234,
                               response_format={"type": "json_object"},
                               sla="strict")
    assert out == "rt-routed"
    assert captured["prompt"] == "plan the fix"
    assert captured["caller_id"] == "synthesis_engine"
    assert captured["max_tokens"] == 1234
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["model"]                       # provider default resolved


def test_sla_gate_is_wired_before_the_batch_cycle():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "backend" / "core" / "ouroboros" / "governance"
           / "doubleword_provider.py").read_text()
    gate = src.index('if sla == "strict":')
    batch = src.index("Slice 27 Phase 2 — Aegis-unified auth bridge",
                      gate)                        # batch body AFTER the gate
    assert gate < batch


def test_strict_consumers_are_tagged():
    """AST-ish pin: the migrated cognition sites carry sla='strict'; the
    already-RT-gated ones (semantic_triage, reasoning_agent via rt_gate)
    need no tag — and dream_engine (Claude-side bulk) carries none."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    syn = (root / "backend/core/ouroboros/roadmap/synthesis_engine.py").read_text()
    intent = (root / "backend/core/ouroboros/governance/intake/sensors/"
              "intent_discovery_sensor.py").read_text()
    dream = (root / "backend/core/ouroboros/consciousness/dream_engine.py").read_text()
    assert 'sla="strict"' in syn
    assert 'sla="strict"' in intent
    assert 'sla="strict"' not in dream             # bulk stays bulk


# ---------------------------------------------------------------------------
# lane mechanics: clamp, eviction, cascade (shared-primitive regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rt_stall_evicts_and_cascades(monkeypatch):
    import aiohttp
    from aiohttp import web

    _reset_stats()
    seen = {"disconnected": 0}

    async def _stall(request):
        body = await request.json()
        assert body["max_tokens"] <= 2000          # the clamp, enforced
        resp = web.StreamResponse()
        resp.headers["Content-Type"] = "text/event-stream"
        await resp.prepare(request)
        try:
            for _ in range(600):
                await asyncio.sleep(0.05)
                await resp.write(b": keepalive\n\n")
        except Exception:
            seen["disconnected"] += 1
            raise
        return resp

    runner, base = await _sse_server(_stall)
    session = aiohttp.ClientSession()
    claude = _FakeClaude()
    try:
        out = await cl.rt_prompt("q", model="m", timeout_s=0.3,
                                 max_tokens=9999,   # clamped to 2000
                                 session=session, base_url=base,
                                 auth_headers_fn=lambda: {}, claude=claude)
        await asyncio.sleep(0.2)
        assert out == "claude-fallback-text"
        assert cl.stats["rt_evictions"] == 1
        assert seen["disconnected"] == 1           # eviction, server-observed
        assert claude.calls == ["q"]
    finally:
        await session.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_council_voice_delegates_to_shared_primitive():
    """DRY pin: CouncilVoice.prompt_only calls cognition_lanes.rt_prompt —
    no second lane implementation exists in the council layer."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "backend/api/hive_council_layer.py").read_text()
    assert "from backend.core.ouroboros.governance.cognition_lanes import rt_prompt" in src
    assert '"service_tier":' not in src           # the SSE body (code, not docs) lives ONE place
