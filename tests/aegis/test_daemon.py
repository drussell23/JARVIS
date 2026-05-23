"""Aegis daemon — endpoint integration tests via aiohttp TestServer.

Slice Aegis-1 regression spine, claim #3: the daemon serves /health,
/session/establish, /lease/acquire, /lease/redeem — and ZERO /v1/*
routes (the negative pin proves Slice 1 does NOT forward provider
traffic).
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.daemon import build_app


_PSK = "test-psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _budget(tmp_path: Path) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=1.00,
        hourly_burn_cap_usd=0.50,
        route_caps_usd={
            "IMMEDIATE": 0.50, "STANDARD": 0.25, "COMPLEX": 0.30,
            "BACKGROUND": 0.05, "SPECULATIVE": 0.05,
        },
        overrun_multiplier=1.5,
    )
    return ImmutableBudgetStateMachine(
        caps=caps, wal_path=tmp_path / "spend.jsonl",
    )


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncGenerator[TestClient, None]:
    app = build_app(
        budget=_budget(tmp_path),
        bootstrap_psk=_PSK,
        lease_ttl_s=60,
        session_ttl_s=300,
    )
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


async def test_health_returns_ok(client: TestClient):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert "bind_port" in body
    assert body["psk_consumed"] is False
    assert body["active_session_count"] == 0
    assert "budget" in body


async def test_health_never_leaks_hmac_or_psk(client: TestClient):
    """K + PSK must NEVER appear in the /health response body."""
    resp = await client.get("/health")
    body_text = await resp.text()
    assert _PSK not in body_text, "bootstrap PSK leaked in /health!"
    # K is 32 random bytes — we can't pin a specific value, but we can
    # check that no key-shaped fields exist.
    body = await resp.json()
    for k in body:
        assert "hmac" not in k.lower(), f"suspicious key in /health: {k}"
        assert "key" not in k.lower() or k == "schema_version", (
            f"suspicious key in /health: {k}"
        )


# ---------------------------------------------------------------------------
# /session/establish — Model B (one-shot PSK → scoped session token)
# ---------------------------------------------------------------------------


async def test_session_establish_with_valid_psk_succeeds(client: TestClient):
    resp = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert "session_token" in body
    assert "." in body["session_token"]  # JWT-like format
    assert "expires_at" in body


async def test_session_establish_without_bearer_returns_401(client: TestClient):
    resp = await client.post("/session/establish")
    assert resp.status == 401


async def test_session_establish_wrong_psk_returns_403(client: TestClient):
    resp = await client.post(
        "/session/establish",
        headers={"Authorization": "Bearer wrong-psk"},
    )
    assert resp.status == 403


async def test_session_establish_is_one_shot(client: TestClient):
    first = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    assert first.status == 200

    # Second call with the SAME PSK is rejected — PSK is consumed.
    second = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    assert second.status == 403
    body = await second.json()
    assert body["error"] == "psk_already_consumed"


async def test_health_reflects_psk_consumed_after_establish(client: TestClient):
    await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    resp = await client.get("/health")
    body = await resp.json()
    assert body["psk_consumed"] is True
    assert body["active_session_count"] == 1


# ---------------------------------------------------------------------------
# /lease/acquire
# ---------------------------------------------------------------------------


async def _establish(client: TestClient) -> str:
    """Helper: run /session/establish + return the session token."""
    resp = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    body = await resp.json()
    return body["session_token"]


async def test_lease_acquire_with_valid_session_succeeds(client: TestClient):
    session = await _establish(client)
    resp = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session}"},
        json={
            "op_id": "op-1",
            "route": "STANDARD",
            "estimated_cost_usd": 0.05,
            "causal_lineage_hash": "stub-for-arc-4",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert "lease_token" in body
    assert body["verdict"]["admitted"] is True


async def test_lease_acquire_without_session_returns_401(client: TestClient):
    resp = await client.post(
        "/lease/acquire",
        json={"op_id": "x", "route": "STANDARD", "estimated_cost_usd": 0.01},
    )
    assert resp.status == 401


async def test_lease_acquire_with_bogus_session_returns_403(client: TestClient):
    resp = await client.post(
        "/lease/acquire",
        headers={"Authorization": "Bearer not.a.real.token"},
        json={"op_id": "x", "route": "STANDARD", "estimated_cost_usd": 0.01},
    )
    assert resp.status == 403


async def test_lease_acquire_rejected_when_route_cap_exceeded(client: TestClient):
    session = await _establish(client)
    # STANDARD cap is 0.25; overrun_multiplier 1.5 → 0.20 * 1.5 = 0.30 > 0.25
    resp = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session}"},
        json={"op_id": "op-x", "route": "STANDARD", "estimated_cost_usd": 0.20},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is False
    assert body["verdict"]["admitted"] is False
    assert body["verdict"]["reason"] == "cost_ceiling_exceeded"


async def test_lease_acquire_missing_body_field_returns_400(client: TestClient):
    session = await _establish(client)
    resp = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session}"},
        json={"op_id": "x"},  # missing route + cost
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# /lease/redeem
# ---------------------------------------------------------------------------


async def test_full_lease_lifecycle(client: TestClient):
    """End-to-end: establish → acquire → redeem."""
    session = await _establish(client)

    acq = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session}"},
        json={
            "op_id": "op-1", "route": "STANDARD",
            "estimated_cost_usd": 0.05,
            "causal_lineage_hash": "stub",
        },
    )
    acq_body = await acq.json()
    lease_token = acq_body["lease_token"]

    redeem = await client.post(
        "/lease/redeem",
        headers={"Authorization": f"Bearer {session}"},
        json={"lease_token": lease_token, "actual_cost_usd": 0.04},
    )
    assert redeem.status == 200
    body = await redeem.json()
    assert body["ok"] is True
    assert body["verdict"]["admitted"] is True


async def test_lease_redeem_replay_rejected(client: TestClient):
    """A lease redeemed once cannot be redeemed again."""
    session = await _establish(client)
    acq = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {session}"},
        json={
            "op_id": "op-1", "route": "STANDARD",
            "estimated_cost_usd": 0.05,
        },
    )
    acq_body = await acq.json()
    lease_token = acq_body["lease_token"]

    first = await client.post(
        "/lease/redeem",
        headers={"Authorization": f"Bearer {session}"},
        json={"lease_token": lease_token, "actual_cost_usd": 0.04},
    )
    assert first.status == 200
    body = await first.json()
    assert body["ok"] is True

    second = await client.post(
        "/lease/redeem",
        headers={"Authorization": f"Bearer {session}"},
        json={"lease_token": lease_token, "actual_cost_usd": 0.04},
    )
    body = await second.json()
    assert body["ok"] is False
    assert "replayed" in body["error"]


# ---------------------------------------------------------------------------
# Negative pin: Slice 1 does NOT forward /v1/* upstream traffic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/v1/messages",
    "/v1/chat/completions",
    "/v1/embeddings",
])
async def test_v1_routes_return_404_in_slice_1(client: TestClient, path: str):
    """Slice 1 DARK SUBSTRATE pin: no /v1/* proxy. Slice 2 will add
    these endpoints. This test must be UPDATED (not deleted) when
    Slice 2 lands."""
    resp = await client.post(path, json={})
    assert resp.status == 404, (
        f"{path} unexpectedly served — Slice 1 must NOT forward provider traffic"
    )


async def test_app_router_has_only_expected_routes(client: TestClient):
    """AST-ish pin: enumerate the router and confirm exactly the
    Slice 1 endpoint surface is exposed. Any new route must be a
    deliberate addition."""
    app = client.app
    assert app is not None
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    expected = sorted({
        "/health",
        "/session/establish",
        "/lease/acquire",
        "/lease/redeem",
    })
    assert paths == expected, (
        f"router surface drift: got {paths}, expected {expected}"
    )
