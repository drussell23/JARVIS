"""AegisClient — session lifecycle + lease acquire/redeem against
a live in-process Aegis daemon (via aiohttp TestServer)."""
from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator, Tuple

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.client import (
    AegisClient,
    AegisClientError,
    ENV_AEGIS_BOOTSTRAP_PSK,
    ENV_AEGIS_URL,
    is_enabled,
)
from backend.core.ouroboros.aegis.daemon import build_app


_PSK = "test-client-psk-xxxxxxxxxxxxxxxxxxxxxxxx"


def _budget(tmp_path: Path) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=1.0, hourly_burn_cap_usd=0.5,
        route_caps_usd={"STANDARD": 0.5, "IMMEDIATE": 0.5},
        overrun_multiplier=1.5,
    )
    return ImmutableBudgetStateMachine(caps=caps, wal_path=tmp_path / "wal.jsonl")


@pytest_asyncio.fixture
async def aegis_daemon_url(tmp_path, monkeypatch) -> AsyncGenerator[Tuple[TestClient, str], None]:
    """Spin up an in-process Aegis daemon and return its URL.

    Also pre-populates JARVIS_AEGIS_URL + JARVIS_AEGIS_BOOTSTRAP_PSK
    in env so the AegisClient.get() singleton can resolve them.
    """
    app = build_app(
        budget=_budget(tmp_path),
        bootstrap_psk=_PSK,
        lease_ttl_s=60,
        session_ttl_s=300,
    )
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    base_url = f"http://{server.host}:{server.port}"
    monkeypatch.setenv(ENV_AEGIS_URL, base_url)
    monkeypatch.setenv(ENV_AEGIS_BOOTSTRAP_PSK, _PSK)

    # Reset the singleton so this test's URL is what get() picks up.
    await AegisClient.reset_for_tests()

    try:
        yield test_client, base_url
    finally:
        await AegisClient.reset_for_tests()
        await test_client.close()


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_false_when_env_unset(monkeypatch):
    monkeypatch.delenv(ENV_AEGIS_URL, raising=False)
    monkeypatch.delenv(ENV_AEGIS_BOOTSTRAP_PSK, raising=False)
    assert is_enabled() is False


def test_is_enabled_false_when_only_url_set(monkeypatch):
    monkeypatch.setenv(ENV_AEGIS_URL, "http://x")
    monkeypatch.delenv(ENV_AEGIS_BOOTSTRAP_PSK, raising=False)
    assert is_enabled() is False


def test_is_enabled_true_when_both_set(monkeypatch):
    monkeypatch.setenv(ENV_AEGIS_URL, "http://127.0.0.1:1")
    monkeypatch.setenv(ENV_AEGIS_BOOTSTRAP_PSK, "secret")
    assert is_enabled() is True


# ---------------------------------------------------------------------------
# AegisClient.get() singleton + session establish
# ---------------------------------------------------------------------------


async def test_get_raises_when_env_unset(monkeypatch):
    monkeypatch.delenv(ENV_AEGIS_URL, raising=False)
    monkeypatch.delenv(ENV_AEGIS_BOOTSTRAP_PSK, raising=False)
    await AegisClient.reset_for_tests()
    with pytest.raises(AegisClientError, match="JARVIS_AEGIS_URL"):
        await AegisClient.get()


async def test_client_establishes_session_on_first_acquire(aegis_daemon_url):
    client = await AegisClient.get()
    assert client.has_session is False  # not yet acquired

    lease = await client.acquire_lease(
        op_id="op-1", route="STANDARD", estimated_cost_usd=0.05,
    )
    assert isinstance(lease, str)
    assert "." in lease  # JWT-like
    assert client.has_session is True


async def test_acquire_lease_returns_distinct_tokens(aegis_daemon_url):
    client = await AegisClient.get()
    a = await client.acquire_lease(
        op_id="op-a", route="STANDARD", estimated_cost_usd=0.01,
    )
    b = await client.acquire_lease(
        op_id="op-b", route="STANDARD", estimated_cost_usd=0.01,
    )
    assert a != b


async def test_acquire_raises_when_cap_exceeded(aegis_daemon_url):
    client = await AegisClient.get()
    # session_cap_usd=1.0, overrun=1.5 → max single est ≈ 0.66 before fail
    with pytest.raises(AegisClientError, match="cost_ceiling_exceeded"):
        await client.acquire_lease(
            op_id="op-too-big", route="STANDARD", estimated_cost_usd=10.0,
        )


async def test_redeem_lease_fire_and_forget(aegis_daemon_url):
    client = await AegisClient.get()
    lease = await client.acquire_lease(
        op_id="op-r", route="STANDARD", estimated_cost_usd=0.05,
    )
    # Should not raise even if reconcile turns out to be impossible.
    await client.redeem_lease(lease_token=lease, actual_cost_usd=0.04)


async def test_singleton_is_shared_across_calls(aegis_daemon_url):
    a = await AegisClient.get()
    b = await AegisClient.get()
    assert a is b


async def test_reset_for_tests_clears_singleton(aegis_daemon_url):
    a = await AegisClient.get()
    await AegisClient.reset_for_tests()
    b = await AegisClient.get()
    assert a is not b
