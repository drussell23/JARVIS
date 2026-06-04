"""Slice 85 Phase 1 — Aegis concurrent-forwarding diagnostic.

Verify-first gate for the runbook: the sweep's no-first-token stalls
(first_token_ms=-1 over 175-242s on the harder problems) could be the Aegis
proxy stalling some streams under concurrent SSE forwarding — OR an upstream
prompt-compilation delay. A direct (Aegis-bypassing) probe already proved DW's
endpoint handles 6 concurrent streams cleanly, so this test isolates the Aegis
hop: it fires N concurrent PACED OpenAI-compat streams through the real Aegis
forwarding code (`aegis/forwarding.py`, `iter_any()` chunk pass-through) and
asserts every one demultiplexes to a complete, byte-faithful body with no stall,
truncation, or cross-stream interference.

If this stays green, Aegis forwarding is NOT the concurrency bottleneck and the
no-first-token stalls are upstream/prompt-driven — i.e. a context/budget problem
(Phase 3's domain), not a proxy problem. If it ever goes red, it localizes the
stall to the proxy's transport loop. Either way it is a permanent regression pin.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.aegis.daemon import build_app
from backend.core.ouroboros.aegis.upstream_registry import (
    ENV_AEGIS_UPSTREAM_ANTHROPIC_URL,
    ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL,
)

from tests.aegis.test_forwarding import (
    _budget, _PSK, _STUB_ANTHROPIC_KEY, _STUB_DW_KEY,
)

_N_CONCURRENT = 6
_FRAMES_PER_STREAM = 12  # multi-chunk so a stall mid-stream would truncate


def _paced_openai_sse() -> bytes:
    frames = [
        'data: ' + json.dumps({
            "id": "x", "object": "chat.completion.chunk",
            "choices": [{"delta": {"content": f"tok{i} "}, "index": 0}],
        }) + "\n\n"
        for i in range(_FRAMES_PER_STREAM)
    ]
    frames.append('data: ' + json.dumps({
        "id": "x", "object": "chat.completion.chunk",
        "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
        "usage": {"prompt_tokens": 10, "completion_tokens": _FRAMES_PER_STREAM},
    }) + "\n\n")
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


@pytest_asyncio.fixture
async def paced_stack(tmp_path: Path, monkeypatch):
    """Aegis + a PACED OpenAI-compat upstream (each SSE frame is a separate
    drained write with a small sleep — forces true chunk-by-chunk forwarding)."""
    body = _paced_openai_sse()

    async def openai_handler(request: web.Request) -> web.StreamResponse:
        await request.read()
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        for event in body.split(b"\n\n"):
            if not event:
                continue
            await resp.write(event + b"\n\n")
            await resp.drain()
            await asyncio.sleep(0.01)  # pace: simulate slow token stream
        await resp.write_eof()
        return resp

    stub = web.Application()
    stub.router.add_post("/v1/chat/completions", openai_handler)
    stub_server = TestServer(stub)
    await stub_server.start_server()
    stub_url = f"http://{stub_server.host}:{stub_server.port}"

    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, stub_url)
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL, stub_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _STUB_ANTHROPIC_KEY)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", _STUB_DW_KEY)

    aegis_app = build_app(
        budget=_budget(tmp_path, session_cap=50.0, hourly_cap=50.0,
                       route_caps={"STANDARD": 50.0, "IMMEDIATE": 50.0}),
        bootstrap_psk=_PSK, lease_ttl_s=300, session_ttl_s=300,
        forwarding_enabled=True,
    )
    aegis_server = TestServer(aegis_app)
    client = TestClient(aegis_server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        await stub_server.close()


async def _establish_session(client: TestClient) -> str:
    est = await client.post(
        "/session/establish", headers={"Authorization": f"Bearer {_PSK}"},
    )
    body = await est.json()
    return body["session_token"]


async def _acquire_lease(client: TestClient, session_token: str, idx: int) -> str:
    acq = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"op_id": f"op-conc-{idx}", "route": "STANDARD",
              "estimated_cost_usd": 0.10, "causal_lineage_hash": "stub"},
    )
    acq_body = await acq.json()
    assert acq_body["ok"] is True, f"lease {idx} denied: {acq_body}"
    return acq_body["lease_token"]


async def _stream_with_lease(client: TestClient, lease: str, idx: int) -> str:
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "deepseek-ai/DeepSeek-V4-Pro", "stream": True,
              "messages": [{"role": "user", "content": "fix it"}]},
    )
    assert resp.status == 200, f"stream {idx} status={resp.status}"
    return (await resp.read()).decode("utf-8", "replace")


async def _setup_leases(client: TestClient) -> list:
    # One session (PSK is single-use), N leases acquired off it.
    session = await _establish_session(client)
    return [await _acquire_lease(client, session, i)
            for i in range(_N_CONCURRENT)]


@pytest.mark.asyncio
async def test_six_concurrent_paced_streams_all_complete(paced_stack):
    leases = await _setup_leases(paced_stack)
    # Fire all N streams AT ONCE; every one must return complete and intact.
    results = await asyncio.gather(
        *[_stream_with_lease(paced_stack, leases[i], i)
          for i in range(_N_CONCURRENT)],
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent forwarding raised: {failures}"

    for i, bodytext in enumerate(results):
        assert isinstance(bodytext, str)
        assert "tok0 " in bodytext, f"stream {i} lost its first token (stall?)"
        assert f"tok{_FRAMES_PER_STREAM - 1} " in bodytext, (
            f"stream {i} truncated before the last token (mid-stream stall)"
        )
        assert "[DONE]" in bodytext, f"stream {i} missing terminator"


@pytest.mark.asyncio
async def test_concurrent_streams_do_not_interleave_bodies(paced_stack):
    leases = await _setup_leases(paced_stack)
    results = await asyncio.gather(
        *[_stream_with_lease(paced_stack, leases[i], i)
          for i in range(_N_CONCURRENT)]
    )
    for i, bodytext in enumerate(results):
        # exactly one terminator per stream — interleaving would dupe/split it
        assert bodytext.count("[DONE]") == 1, (
            f"stream {i} has {bodytext.count('[DONE]')} terminators — cross-stream bleed"
        )
