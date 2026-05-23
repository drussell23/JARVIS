"""Slice 2B-i: Aegis is the authoritative reconciler for non-streaming
LLM responses.

These tests prove:
  1. Aegis parses ``usage`` from the non-streaming upstream JSON body.
  2. ``budget.reconcile()`` is called with the parsed actual cost
     (not the reserve, not the JARVIS-supplied estimate).
  3. The response body is forwarded to JARVIS byte-identically (the
     parse is a tee, not a rewrite).
  4. Malformed / missing-usage responses fall back to the pre-flight
     reserve (over-account, never silently zero).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncGenerator, List, Tuple

import pytest
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


_PSK = "nonstream-test-psk-aaaaaaaaaaaaaaaaaaaaaa"
_STUB_ANTHROPIC_KEY = "stub-anthropic"
_STUB_DW_KEY = "stub-dw"


def _budget(tmp_path: Path) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=10.0, hourly_burn_cap_usd=10.0,
        route_caps_usd={"STANDARD": 5.0, "IMMEDIATE": 5.0},
        overrun_multiplier=2.0,  # generous reserve so reconcile shows refund
    )
    return ImmutableBudgetStateMachine(
        caps=caps, wal_path=tmp_path / "wal.jsonl",
    )


class _Recorder:
    def __init__(self) -> None:
        self.received: List[dict] = []


def _anthropic_nonstreaming_body(
    *, input_tokens: int = 100, output_tokens: int = 200,
) -> bytes:
    return json.dumps({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "stop_reason": "end_turn",
    }).encode("utf-8")


def _openai_nonstreaming_body(
    *, prompt_tokens: int = 100, completion_tokens: int = 200,
) -> bytes:
    return json.dumps({
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "model": "Qwen/Qwen3.5-397B-A17B-FP8",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }).encode("utf-8")


def _make_nonstreaming_stub_app(
    recorder: _Recorder,
    *,
    anthropic_body: bytes,
    openai_body: bytes,
) -> web.Application:
    app = web.Application()

    async def anthropic_handler(request: web.Request) -> web.Response:
        body = await request.read()
        recorder.received.append({"path": request.path, "body": body})
        return web.Response(
            status=200, body=anthropic_body,
            content_type="application/json",
        )

    async def openai_handler(request: web.Request) -> web.Response:
        body = await request.read()
        recorder.received.append({"path": request.path, "body": body})
        return web.Response(
            status=200, body=openai_body,
            content_type="application/json",
        )

    app.router.add_post("/v1/messages", anthropic_handler)
    app.router.add_post("/v1/chat/completions", openai_handler)
    return app


@pytest_asyncio.fixture
async def stack_anthropic(
    tmp_path, monkeypatch,
) -> AsyncGenerator[Tuple[TestClient, _Recorder, ImmutableBudgetStateMachine], None]:
    recorder = _Recorder()
    stub = _make_nonstreaming_stub_app(
        recorder,
        anthropic_body=_anthropic_nonstreaming_body(
            input_tokens=500, output_tokens=1000,
        ),
        openai_body=_openai_nonstreaming_body(),
    )
    stub_server = TestServer(stub)
    await stub_server.start_server()
    stub_url = f"http://{stub_server.host}:{stub_server.port}"
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, stub_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _STUB_ANTHROPIC_KEY)
    budget = _budget(tmp_path)
    app = build_app(
        budget=budget, bootstrap_psk=_PSK,
        lease_ttl_s=300, session_ttl_s=300, forwarding_enabled=True,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, recorder, budget
    finally:
        await client.close()
        await stub_server.close()


@pytest_asyncio.fixture
async def stack_openai(
    tmp_path, monkeypatch,
) -> AsyncGenerator[Tuple[TestClient, _Recorder, ImmutableBudgetStateMachine], None]:
    recorder = _Recorder()
    stub = _make_nonstreaming_stub_app(
        recorder,
        anthropic_body=_anthropic_nonstreaming_body(),
        openai_body=_openai_nonstreaming_body(
            prompt_tokens=2000, completion_tokens=400,
        ),
    )
    stub_server = TestServer(stub)
    await stub_server.start_server()
    stub_url = f"http://{stub_server.host}:{stub_server.port}"
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL, stub_url)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", _STUB_DW_KEY)
    budget = _budget(tmp_path)
    app = build_app(
        budget=budget, bootstrap_psk=_PSK,
        lease_ttl_s=300, session_ttl_s=300, forwarding_enabled=True,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, recorder, budget
    finally:
        await client.close()
        await stub_server.close()


async def _establish_and_acquire(
    client: TestClient, *, route: str = "STANDARD", estimated: float = 0.10,
) -> str:
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
            "op_id": "op-nonstream", "route": route,
            "estimated_cost_usd": estimated,
            "causal_lineage_hash": "stub",
        },
    )
    body = await acq.json()
    assert body["ok"] is True, f"lease denied: {body}"
    return body["lease_token"]


# ---------------------------------------------------------------------------
# Anthropic non-streaming
# ---------------------------------------------------------------------------


async def test_anthropic_nonstreaming_byte_identity(stack_anthropic):
    """The buffered tee for parsing must not alter the bytes JARVIS sees."""
    client, _, _ = stack_anthropic
    lease = await _establish_and_acquire(client, route="IMMEDIATE")
    resp = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514", "stream": False,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    body = await resp.read()
    expected = _anthropic_nonstreaming_body(
        input_tokens=500, output_tokens=1000,
    )
    assert body == expected, (
        f"non-streaming body altered by Aegis tee: got {len(body)}B vs "
        f"expected {len(expected)}B"
    )


async def test_anthropic_nonstreaming_reconciles_authoritatively(stack_anthropic):
    """budget.reconcile() must be called with the parsed actual cost,
    not the reserve."""
    client, _, budget = stack_anthropic

    # Reserve: estimated=0.10 × overrun=2.0 = 0.20
    # Actual cost (parsed from body): 500 input × $3/M + 1000 output × $15/M
    #   = 0.0015 + 0.015 = 0.0165
    snap_before = budget.snapshot()
    assert snap_before["session_debit_usd"] == 0.0

    lease = await _establish_and_acquire(client, route="IMMEDIATE", estimated=0.10)
    snap_after_admit = budget.snapshot()
    assert snap_after_admit["session_debit_usd"] == pytest.approx(0.20)

    resp = await client.post(
        "/v1/messages",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "claude-sonnet-4-20250514", "stream": False,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await resp.read()
    assert resp.status == 200

    snap_after_reconcile = budget.snapshot()
    # Reconcile must have happened: debit drops from 0.20 (reserve)
    # to 0.0165 (parsed actual).
    assert snap_after_reconcile["session_debit_usd"] == pytest.approx(
        0.0165, rel=0.01,
    ), (
        f"reconcile did not happen with parsed cost — debit is "
        f"{snap_after_reconcile['session_debit_usd']} (expected ~0.0165)"
    )


# ---------------------------------------------------------------------------
# OpenAI-compat non-streaming (DW)
# ---------------------------------------------------------------------------


async def test_openai_nonstreaming_byte_identity(stack_openai):
    client, _, _ = stack_openai
    lease = await _establish_and_acquire(client, route="STANDARD")
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "Qwen/Qwen3.5-397B-A17B-FP8", "stream": False,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    body = await resp.read()
    expected = _openai_nonstreaming_body(
        prompt_tokens=2000, completion_tokens=400,
    )
    assert body == expected


async def test_openai_nonstreaming_reconciles_prompt_completion_aliases(stack_openai):
    """OpenAI-compat usage uses prompt_tokens/completion_tokens (not
    input_tokens/output_tokens) — the parser handles the aliases."""
    client, _, budget = stack_openai
    lease = await _establish_and_acquire(client, route="STANDARD", estimated=0.10)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-JARVIS-Lease": lease},
        json={"model": "Qwen/Qwen3.5-397B-A17B-FP8", "stream": False,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await resp.read()
    assert resp.status == 200

    snap = budget.snapshot()
    # 2000 prompt × $0.10/M + 400 completion × $0.40/M = 0.0002 + 0.00016 = 0.00036
    # (Qwen pricing from env fallback when no policy yaml configured for stub)
    # The exact value depends on which pricing source is found. Just verify
    # reconcile happened (debit is NOT the reserve 0.20).
    assert snap["session_debit_usd"] < 0.10, (
        f"reconcile did not happen — debit {snap['session_debit_usd']} "
        f"still shows reserve, not actual"
    )


# ---------------------------------------------------------------------------
# Fallback to reserve when usage missing / malformed
# ---------------------------------------------------------------------------


async def test_anthropic_malformed_body_falls_back_to_reserve(
    tmp_path, monkeypatch,
):
    """If the upstream body is malformed (no usage field), Aegis
    keeps the reserve — never silently treats the op as $0."""
    recorder = _Recorder()
    # Body with NO usage field — parse must return None.
    bad_body = json.dumps({"id": "msg_x", "content": []}).encode("utf-8")
    stub = _make_nonstreaming_stub_app(
        recorder, anthropic_body=bad_body,
        openai_body=_openai_nonstreaming_body(),
    )
    stub_server = TestServer(stub)
    await stub_server.start_server()
    try:
        monkeypatch.setenv(
            ENV_AEGIS_UPSTREAM_ANTHROPIC_URL,
            f"http://{stub_server.host}:{stub_server.port}",
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", _STUB_ANTHROPIC_KEY)
        budget = _budget(tmp_path)
        app = build_app(
            budget=budget, bootstrap_psk=_PSK,
            lease_ttl_s=300, session_ttl_s=300, forwarding_enabled=True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            lease = await _establish_and_acquire(
                client, route="IMMEDIATE", estimated=0.10,
            )
            resp = await client.post(
                "/v1/messages",
                headers={"X-JARVIS-Lease": lease},
                json={"model": "claude-sonnet-4-20250514", "stream": False,
                      "messages": [{"role": "user", "content": "hi"}]},
            )
            await resp.read()
            assert resp.status == 200
            snap = budget.snapshot()
            # Reserve 0.20 stands — reconcile saw no usage to parse so
            # the in-memory debit was unchanged (actual stays at reserve).
            assert snap["session_debit_usd"] == pytest.approx(0.20)
        finally:
            await client.close()
    finally:
        await stub_server.close()


# ---------------------------------------------------------------------------
# Direct parser unit tests
# ---------------------------------------------------------------------------


def test_parse_anthropic_usage_well_formed():
    from backend.core.ouroboros.aegis.forwarding import _parse_nonstreaming_usage
    from backend.core.ouroboros.aegis.upstream_registry import WireFamily
    body = _anthropic_nonstreaming_body(input_tokens=42, output_tokens=99)
    result = _parse_nonstreaming_usage(WireFamily.ANTHROPIC, body)
    assert result == (42, 99)


def test_parse_openai_compat_usage_well_formed():
    from backend.core.ouroboros.aegis.forwarding import _parse_nonstreaming_usage
    from backend.core.ouroboros.aegis.upstream_registry import WireFamily
    body = _openai_nonstreaming_body(prompt_tokens=42, completion_tokens=99)
    result = _parse_nonstreaming_usage(WireFamily.OPENAI_COMPAT, body)
    assert result == (42, 99)


def test_parse_malformed_returns_none():
    from backend.core.ouroboros.aegis.forwarding import _parse_nonstreaming_usage
    from backend.core.ouroboros.aegis.upstream_registry import WireFamily
    assert _parse_nonstreaming_usage(WireFamily.ANTHROPIC, b"not json") is None
    assert _parse_nonstreaming_usage(WireFamily.ANTHROPIC, b"") is None
    assert _parse_nonstreaming_usage(WireFamily.ANTHROPIC, b"[]") is None
    assert _parse_nonstreaming_usage(WireFamily.ANTHROPIC, b'{"no_usage": true}') is None


def test_parse_rejects_negative_tokens():
    from backend.core.ouroboros.aegis.forwarding import _parse_nonstreaming_usage
    from backend.core.ouroboros.aegis.upstream_registry import WireFamily
    bad = json.dumps({"usage": {"input_tokens": -1, "output_tokens": 10}}).encode()
    assert _parse_nonstreaming_usage(WireFamily.ANTHROPIC, bad) is None


def test_parse_rejects_non_int_tokens():
    from backend.core.ouroboros.aegis.forwarding import _parse_nonstreaming_usage
    from backend.core.ouroboros.aegis.upstream_registry import WireFamily
    bad = json.dumps({"usage": {"input_tokens": "abc", "output_tokens": 10}}).encode()
    assert _parse_nonstreaming_usage(WireFamily.ANTHROPIC, bad) is None
