"""PSK + session-token lifecycle (binding correction #1).

Model B: bootstrap PSK is single-use → mints scoped session token (TTL).
Tests:
  - PSK consumes irrevocably on first /session/establish
  - Session-token TTL honored (expired token rejected)
  - Re-establishing AFTER PSK consumption fails (PSK is one-shot)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import AsyncGenerator

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.daemon import build_app
from backend.core.ouroboros.aegis.lease import (
    TokenVerdictKind,
    validate_session_token,
)


_PSK = "lifecycle-psk-aaaaaaaaaaaaaaaaaaaaaaaa"


def _budget(tmp_path: Path) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=1.0, hourly_burn_cap_usd=0.5,
        route_caps_usd={"STANDARD": 0.5}, overrun_multiplier=1.5,
    )
    return ImmutableBudgetStateMachine(caps=caps, wal_path=tmp_path / "wal.jsonl")


@pytest_asyncio.fixture
async def client_with_short_session(tmp_path) -> AsyncGenerator[TestClient, None]:
    """Build a client whose session-token TTL is 1 second so we can
    deterministically test expiry without sleeping forever."""
    app = build_app(
        budget=_budget(tmp_path),
        bootstrap_psk=_PSK,
        lease_ttl_s=60,
        session_ttl_s=1,  # short for the expiry test
    )
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


# ---------------------------------------------------------------------------
# PSK consumption discipline
# ---------------------------------------------------------------------------


async def test_psk_consumed_after_single_use(client_with_short_session: TestClient):
    client = client_with_short_session
    first = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    assert first.status == 200

    # /health should reflect consumption.
    h = await client.get("/health")
    body = await h.json()
    assert body["psk_consumed"] is True

    # Second establish with same PSK is rejected.
    second = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    assert second.status == 403


async def test_wrong_psk_does_not_consume(client_with_short_session: TestClient):
    """A failed PSK attempt must NOT mark PSK consumed (operator may
    retry with the correct value). Confirms via subsequent valid use."""
    client = client_with_short_session
    bad = await client.post(
        "/session/establish",
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad.status == 403

    good = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    assert good.status == 200, "valid PSK should still work after bad attempt"


# ---------------------------------------------------------------------------
# Session-token TTL
# ---------------------------------------------------------------------------


async def test_session_token_expires_after_ttl(client_with_short_session: TestClient):
    """After TTL elapses, calls bearing the expired session token are
    rejected."""
    client = client_with_short_session
    establish = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    body = await establish.json()
    token = body["session_token"]

    # Wait > TTL (configured to 1 second in the fixture).
    time.sleep(1.2)

    resp = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {token}"},
        json={"op_id": "x", "route": "STANDARD", "estimated_cost_usd": 0.01},
    )
    assert resp.status == 403


async def test_session_token_valid_within_ttl(client_with_short_session: TestClient):
    client = client_with_short_session
    establish = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    body = await establish.json()
    token = body["session_token"]

    # Immediately use it — should be valid.
    resp = await client.post(
        "/lease/acquire",
        headers={"Authorization": f"Bearer {token}"},
        json={"op_id": "x", "route": "STANDARD", "estimated_cost_usd": 0.01},
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Validation function direct unit (no HTTP)
# ---------------------------------------------------------------------------


def test_validate_session_token_with_inactive_jti_returns_replayed():
    """Even a structurally valid + non-expired token is rejected if
    the daemon revoked it (jti not in active_jti)."""
    from backend.core.ouroboros.aegis.lease import mint_session_token
    K = b"x" * 32
    now = time.time()
    token = mint_session_token(K, now_s=now, ttl_s=60)[0]
    # Empty active set — simulates a daemon restart wiping sessions.
    verdict = validate_session_token(K, token, now_s=now + 1, active_jti=set())
    assert verdict.kind is TokenVerdictKind.REPLAYED
