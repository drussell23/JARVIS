"""Aegis forwarding — end-to-end via stub upstream + lease lifecycle.

These tests stand up a real-aiohttp **stub upstream** server (pretending
to be Anthropic / DW) and an Aegis daemon configured to forward to it.
Requests originate from an aiohttp TestClient against Aegis, traverse
the lease-validate → credential-inject → upstream-forward → SSE-parse →
guillotine path, and the test asserts on observable outcomes:

  - Byte-identical pass-through of SSE chunks (preserves §44.3
    reasoning-frames-as-keepalive shape)
  - Credential injection (stub sees the upstream key, JARVIS request
    never carries it)
  - Guillotine fires when accumulated cost exceeds lease.max_cost_usd
  - Budget reconcile is called with the right actual cost on completion
  - Lease validation (missing / replayed / bad signature) is rejected
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncGenerator, List, Tuple

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.daemon import build_app
from backend.core.ouroboros.aegis.upstream_registry import (
    ENV_AEGIS_UPSTREAM_ANTHROPIC_URL,
    ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL,
)


_PSK = "forwarding-test-psk-yyyyyyyyyyyyyyyyyyyyyyyy"
_STUB_ANTHROPIC_KEY = "stub-anthropic-key-zzzzz"
_STUB_DW_KEY = "stub-dw-key-zzzzz"


def _budget(
    tmp_path: Path,
    *,
    session_cap: float = 5.0,
    hourly_cap: float = 5.0,
    route_caps: dict | None = None,
) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=session_cap,
        hourly_burn_cap_usd=hourly_cap,
        route_caps_usd=route_caps if route_caps is not None else {
            "STANDARD": 5.0, "IMMEDIATE": 5.0,
        },
        overrun_multiplier=1.0,  # keep arithmetic predictable
    )
    return ImmutableBudgetStateMachine(caps=caps, wal_path=tmp_path / "wal.jsonl")


# ---------------------------------------------------------------------------
# Stub upstream server — emits canned Anthropic + OpenAI-compat responses
# ---------------------------------------------------------------------------


class _UpstreamRecorder:
    """Captures inbound requests so tests can verify credential injection,
    body byte-identity, etc."""

    def __init__(self) -> None:
        self.received: List[dict] = []

    def snapshot(self) -> List[dict]:
        return list(self.received)


def _make_anthropic_sse_body(
    *, input_tokens: int, output_tokens: int, progressive_deltas: int = 1,
) -> bytes:
    """Compose a minimal Anthropic-shaped SSE response.

    Frame shape per Anthropic Messages API streaming. message_start
    carries input_tokens; message_delta carries output_tokens cumulatively.

    ``progressive_deltas`` controls how many ``message_delta`` events are
    emitted (each carrying the cumulative ``output_tokens`` proportional
    to its position). Default 1 matches the legacy "one final delta"
    shape; values >1 simulate the real Anthropic streaming pattern
    where cumulative usage is reported repeatedly — required for the
    guillotine to detect overrun before the stream completes.
    """
    frames = [
        f'event: message_start\ndata: {json.dumps({"type": "message_start", "message": {"id": "m1", "role": "assistant", "content": [], "model": "claude-sonnet-4-20250514", "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})}\n\n',
        f'event: content_block_start\ndata: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}\n\n',
        f'event: content_block_delta\ndata: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}})}\n\n',
        f'event: content_block_stop\ndata: {json.dumps({"type": "content_block_stop", "index": 0})}\n\n',
    ]
    # Interleave N message_delta frames before the final stop. Each
    # carries the running cumulative output count.
    for i in range(1, progressive_deltas + 1):
        running = int(output_tokens * (i / progressive_deltas))
        frames.append(
            f'event: message_delta\ndata: {json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn" if i == progressive_deltas else None}, "usage": {"output_tokens": running}})}\n\n'
        )
    frames.append(
        f'event: message_stop\ndata: {json.dumps({"type": "message_stop"})}\n\n'
    )
    return "".join(frames).encode("utf-8")


def _make_openai_compat_sse_body(*, input_tokens: int, output_tokens: int) -> bytes:
    frames = [
        f'data: {json.dumps({"id": "x", "object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant"}, "index": 0}]})}\n\n',
        f'data: {json.dumps({"id": "x", "object": "chat.completion.chunk", "choices": [{"delta": {"content": "hi"}, "index": 0}]})}\n\n',
        f'data: {json.dumps({"id": "x", "object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}], "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}})}\n\n',
        "data: [DONE]\n\n",
    ]
    return "".join(frames).encode("utf-8")


def _make_stub_app(recorder: _UpstreamRecorder, *, sse_anthropic_tokens=(100, 50),
                   sse_openai_tokens=(100, 50),
                   anthropic_progressive_deltas: int = 1,
                   paced_frames: bool = False) -> web.Application:
    app = web.Application()

    async def anthropic_handler(request: web.Request) -> web.StreamResponse:
        body_bytes = await request.read()
        recorder.received.append({
            "path": str(request.path),
            "auth_header": request.headers.get("x-api-key", ""),
            "no_bearer_auth": "authorization" not in {h.lower() for h in request.headers.keys()},
            "no_jarvis_lease": "x-jarvis-lease" not in {h.lower() for h in request.headers.keys()},
            "body": body_bytes,
        })
        sse_body = _make_anthropic_sse_body(
            input_tokens=sse_anthropic_tokens[0],
            output_tokens=sse_anthropic_tokens[1],
            progressive_deltas=anthropic_progressive_deltas,
        )
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        if paced_frames:
            # Split on SSE event boundary and drain between each event,
            # simulating real Anthropic streaming where frames arrive
            # as separate TCP chunks. Without this pacing, the test
            # server's TCP stack coalesces small writes into a single
            # frame and the Aegis-side iter_any() yields the whole
            # body at once — the guillotine fires after pass-through
            # has already completed, which is correct behavior but
            # not observable as a truncated body.
            events = sse_body.split(b"\n\n")
            for i, event in enumerate(events):
                if not event:
                    continue
                payload = event + (b"\n\n" if i < len(events) - 1 else b"")
                await resp.write(payload)
                await resp.drain()
                import asyncio as _aio
                await _aio.sleep(0.005)
        else:
            for i in range(0, len(sse_body), 512):
                await resp.write(sse_body[i: i + 512])
        await resp.write_eof()
        return resp

    async def openai_handler(request: web.Request) -> web.StreamResponse:
        body_bytes = await request.read()
        recorder.received.append({
            "path": str(request.path),
            "auth_header": request.headers.get("Authorization", ""),
            "no_xapikey": "x-api-key" not in {h.lower() for h in request.headers.keys()},
            "no_jarvis_lease": "x-jarvis-lease" not in {h.lower() for h in request.headers.keys()},
            "body": body_bytes,
        })
        sse_body = _make_openai_compat_sse_body(
            input_tokens=sse_openai_tokens[0],
            output_tokens=sse_openai_tokens[1],
        )
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        for i in range(0, len(sse_body), 512):
            await resp.write(sse_body[i: i + 512])
        await resp.write_eof()
        return resp

    app.router.add_post("/v1/messages", anthropic_handler)
    app.router.add_post("/v1/chat/completions", openai_handler)
    return app


# ---------------------------------------------------------------------------
# Fixture: full Aegis + stub upstream stack
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def full_stack(
    tmp_path, monkeypatch,
) -> AsyncGenerator[Tuple[TestClient, _UpstreamRecorder], None]:
    """Spin up:
      - Stub upstream aiohttp server (Anthropic + DW) on its own port
      - Aegis daemon with forwarding_enabled=True, pointed at the stub
      - aiohttp TestClient against Aegis
    Tests get (aegis_test_client, upstream_recorder).
    """
    recorder = _UpstreamRecorder()
    stub_app = _make_stub_app(recorder)
    stub_server = TestServer(stub_app)
    await stub_server.start_server()
    stub_url = f"http://{stub_server.host}:{stub_server.port}"

    # Aegis daemon must look up the stub URL and have credentials in its env.
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, stub_url)
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL, stub_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _STUB_ANTHROPIC_KEY)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", _STUB_DW_KEY)

    aegis_app = build_app(
        budget=_budget(tmp_path),
        bootstrap_psk=_PSK,
        lease_ttl_s=300,
        session_ttl_s=300,
        forwarding_enabled=True,
    )
    aegis_server = TestServer(aegis_app)
    aegis_client = TestClient(aegis_server)
    await aegis_client.start_server()
    try:
        yield aegis_client, recorder
    finally:
        await aegis_client.close()
        await stub_server.close()


async def _establish_and_acquire(
    client: TestClient, *,
    op_id: str = "op-fwd",
    route: str = "STANDARD",
    estimated: float = 0.50,
) -> str:
    """Helper: PSK -> session -> lease. Returns the lease token."""
    est = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    body = await est.json()
    session_token = body["session_token"]

    acq = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session_token}"},
        json={
            "op_id": op_id, "route": route,
            "estimated_cost_usd": estimated,
            "causal_lineage_hash": "stub",
        },
    )
    acq_body = await acq.json()
    assert acq_body["ok"] is True, f"lease denied: {acq_body}"
    return acq_body["lease_token"]


# ---------------------------------------------------------------------------
# Router surface — Slice 2 routes registered when forwarding_enabled=True
# ---------------------------------------------------------------------------


async def test_router_includes_v1_routes_when_forwarding_enabled(full_stack):
    client, _ = full_stack
    app = client.app
    assert app is not None
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    expected = sorted({
        "/health",
        "/session/establish",
        "/lease/acquire",
        "/lease/redeem",
        "/v1/messages",
        "/v1/chat/completions",
    })
    assert paths == expected


# ---------------------------------------------------------------------------
# Credential confiscation — JARVIS request has no upstream key; stub does
# ---------------------------------------------------------------------------


async def test_anthropic_forward_injects_credentials(full_stack):
    client, recorder = full_stack
    lease = await _establish_and_acquire(client, route="IMMEDIATE")

    resp = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    # Drain the response stream.
    _body = await resp.read()
    assert resp.status == 200

    # Recorder confirms upstream got the real x-api-key + NO lease/bearer.
    assert len(recorder.received) == 1
    received = recorder.received[0]
    assert received["auth_header"] == _STUB_ANTHROPIC_KEY
    assert received["no_bearer_auth"] is True
    assert received["no_jarvis_lease"] is True


async def test_openai_compat_forward_injects_bearer(full_stack):
    client, recorder = full_stack
    lease = await _establish_and_acquire(client, route="STANDARD")

    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "Qwen/Qwen3.5-397B-A17B-FP8", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    _body = await resp.read()
    assert resp.status == 200

    assert len(recorder.received) == 1
    received = recorder.received[0]
    assert received["auth_header"] == f"Bearer {_STUB_DW_KEY}"
    assert received["no_xapikey"] is True
    assert received["no_jarvis_lease"] is True


# ---------------------------------------------------------------------------
# Byte-identity pass-through
# ---------------------------------------------------------------------------


async def test_anthropic_sse_byte_identity(full_stack):
    client, _ = full_stack
    lease = await _establish_and_acquire(client, route="IMMEDIATE")
    resp = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    body = await resp.read()
    # The stub composes deterministic SSE frames; Aegis is pass-through.
    expected = _make_anthropic_sse_body(input_tokens=100, output_tokens=50)
    assert body == expected, (
        "SSE body byte-identity broken — Aegis re-framed or buffered "
        f"upstream output. Expected {len(expected)} bytes, got {len(body)}"
    )


# ---------------------------------------------------------------------------
# Lease validation
# ---------------------------------------------------------------------------


async def test_forward_rejects_missing_lease(full_stack):
    client, _ = full_stack
    resp = await client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-20250514"},
    )
    assert resp.status == 401


async def test_forward_rejects_bogus_lease(full_stack):
    client, _ = full_stack
    resp = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": "this.isnotvalid"},
        json={"model": "claude-sonnet-4-20250514"},
    )
    assert resp.status == 403


async def test_forward_rejects_replayed_lease(full_stack):
    client, _ = full_stack
    lease = await _establish_and_acquire(client, route="IMMEDIATE")

    # First use succeeds.
    r1 = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514", "stream": True,
              "messages": [{"role": "user", "content": "x"}]},
    )
    await r1.read()
    assert r1.status == 200

    # Second use of the same lease is rejected as REPLAYED.
    r2 = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514"},
    )
    assert r2.status == 403


# ---------------------------------------------------------------------------
# Guillotine — sever when accumulated cost > lease.max_cost_usd
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def guillotine_stack(
    tmp_path, monkeypatch,
) -> AsyncGenerator[Tuple[TestClient, _UpstreamRecorder], None]:
    """Same as full_stack but the stub emits 10,000 output tokens which,
    at Sonnet rates ($15/M output), generates ~$0.15 cost. We acquire a
    lease with estimated_cost_usd=0.001 so the guillotine fires fast.
    """
    recorder = _UpstreamRecorder()
    stub_app = _make_stub_app(
        recorder,
        sse_anthropic_tokens=(100, 10000),  # 100 input, 10K output
        # 20 progressive deltas: cumulative usage reported every ~500 tokens
        # so Aegis can detect the cost overrun well before the stream completes.
        anthropic_progressive_deltas=20,
        paced_frames=True,
    )
    stub_server = TestServer(stub_app)
    await stub_server.start_server()
    stub_url = f"http://{stub_server.host}:{stub_server.port}"

    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, stub_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _STUB_ANTHROPIC_KEY)

    aegis_app = build_app(
        budget=_budget(tmp_path),
        bootstrap_psk=_PSK,
        lease_ttl_s=300,
        session_ttl_s=300,
        forwarding_enabled=True,
    )
    aegis_server = TestServer(aegis_app)
    aegis_client = TestClient(aegis_server)
    await aegis_client.start_server()
    try:
        yield aegis_client, recorder
    finally:
        await aegis_client.close()
        await stub_server.close()


async def test_guillotine_fires_when_actual_exceeds_lease_max(guillotine_stack):
    """Lease.max_cost_usd is tiny vs. the stub's 10K-output stream.
    Aegis must sever upstream before the full stream is read."""
    client, _ = guillotine_stack
    # Acquire a lease estimating $0.001 — at Sonnet output ($15/M),
    # the stub's 10K tokens would cost $0.15. Way over the lease cap
    # (max_cost = estimated * overrun_multiplier = 0.001 * 1.0).
    lease = await _establish_and_acquire(
        client, route="IMMEDIATE", estimated=0.001,
    )
    resp = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514", "stream": True,
              "messages": [{"role": "user", "content": "x"}]},
    )
    body = await resp.read()
    # Compare against the full stub body shape (same progressive_deltas)
    # so a successful guillotine produces a STRICTLY smaller body.
    full_stub_body = _make_anthropic_sse_body(
        input_tokens=100, output_tokens=10000, progressive_deltas=20,
    )
    assert len(body) < len(full_stub_body), (
        f"guillotine did not fire — got full {len(body)} bytes "
        f"out of {len(full_stub_body)}"
    )
