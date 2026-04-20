"""Phase B Slice 2 — EventChannelServer activation + webhook round-trip.

Integration tests that spin up the **real** EventChannelServer on an
ephemeral port, POST a GitHub ``issues`` webhook payload over HTTP, and
verify the sensor short-circuit produces a ``source="github_issue"``
envelope on the router — shape-identical to the poll path.

This is the end-to-end proof for gap #4 at the HTTP boundary: no
polling, just a push arriving at the event channel and ending as an
IntentEnvelope in the intake router.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional

import aiohttp
import pytest

from backend.core.ouroboros.governance.event_channel import EventChannelServer
from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
    GitHubIssueSensor,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


def _sensor(router: Any) -> GitHubIssueSensor:
    return GitHubIssueSensor(
        repo="jarvis",
        router=router,
        poll_interval_s=3600.0,
        repos=(
            ("jarvis", "drussell23/JARVIS-AI-Agent", "backend/"),
        ),
    )


def _payload(action: str = "opened", number: int = 7777) -> dict:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "Bug: integration test",
            "body": "Traceback from integration test.",
            "labels": [{"name": "bug"}],
            "created_at": "2026-04-19T22:00:00Z",
            "html_url": f"https://github.com/drussell23/JARVIS-AI-Agent/issues/{number}",
        },
        "repository": {"full_name": "drussell23/JARVIS-AI-Agent"},
    }


async def _bound_port(server: EventChannelServer) -> int:
    """Read the ephemeral port the server actually bound to."""
    # aiohttp exposes the concrete TCP socket on the site's server.
    site = server._site
    assert site is not None, "server not started"
    sock = site._server.sockets[0]  # type: ignore[attr-defined]
    return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_post_issues_opens_routes_to_sensor(
    monkeypatch: Any,
) -> None:
    """Real HTTP POST /webhook/github 'issues/opened' -> envelope on router.

    Proves the full gap #4 path: external HTTP push -> EventChannelServer
    short-circuit -> GitHubIssueSensor.ingest_webhook -> envelope with
    source='github_issue' lands in the router (same shape the poll
    path would produce). Zero polling involved.
    """
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EVENT_CHANNELS_ENABLED", "true")

    router = _SpyRouter()
    sensor = _sensor(router)
    server = EventChannelServer(
        router=router,
        port=0,  # ask the OS for an ephemeral port
        host="127.0.0.1",
        github_issue_sensor=sensor,
    )

    await server.start()
    try:
        port = await _bound_port(server)
        url = f"http://127.0.0.1:{port}/webhook/github"
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
        }
        async with aiohttp.ClientSession() as client:
            async with client.post(
                url, data=json.dumps(_payload(action="opened", number=111)), headers=headers,
            ) as resp:
                assert resp.status == 200, f"expected 200, got {resp.status}"

        assert len(router.envelopes) == 1, (
            f"expected 1 envelope, got {len(router.envelopes)}"
        )
        env = router.envelopes[0]
        assert env.source == "github_issue"
        assert env.evidence.get("category") == "github_issue"
        assert env.evidence.get("issue_number") == 111
        assert env.evidence.get("via") == "webhook"
        assert env.evidence.get("webhook_action") == "opened"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_http_post_closed_action_does_not_emit(monkeypatch: Any) -> None:
    """'issues/closed' is a work-complete signal, not new work. No envelope."""
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "true")

    router = _SpyRouter()
    sensor = _sensor(router)
    server = EventChannelServer(
        router=router, port=0, host="127.0.0.1",
        github_issue_sensor=sensor,
    )

    await server.start()
    try:
        port = await _bound_port(server)
        url = f"http://127.0.0.1:{port}/webhook/github"
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues"}
        async with aiohttp.ClientSession() as client:
            async with client.post(
                url, data=json.dumps(_payload(action="closed")), headers=headers,
            ) as resp:
                assert resp.status == 200
        assert router.envelopes == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_http_post_with_flag_off_falls_through_to_generic_path(
    monkeypatch: Any,
) -> None:
    """Flag off -> short-circuit inactive -> envelope goes through the
    generic ``_route_event`` path, producing ``source='runtime_health'``
    (pre-Slice-1 behavior). Proves the flag is the only switch."""
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "false")

    router = _SpyRouter()
    sensor = _sensor(router)
    server = EventChannelServer(
        router=router, port=0, host="127.0.0.1",
        github_issue_sensor=sensor,
    )

    await server.start()
    try:
        port = await _bound_port(server)
        url = f"http://127.0.0.1:{port}/webhook/github"
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues"}
        async with aiohttp.ClientSession() as client:
            async with client.post(
                url, data=json.dumps(_payload()), headers=headers,
            ) as resp:
                assert resp.status == 200

        # Generic path ran: an envelope WAS produced but with the
        # pre-Slice-1 shape (source='runtime_health'). This is the
        # exact behavior we want to NOT regress when the flag is off.
        assert len(router.envelopes) == 1
        env = router.envelopes[0]
        assert env.source == "runtime_health", (
            f"flag-off path must produce runtime_health envelope, got {env.source}"
        )
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_health_endpoint_reports_webhook_mode(monkeypatch: Any) -> None:
    """/channel/health exposes github_issue_sensor.webhook_mode + counters."""
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "true")

    router = _SpyRouter()
    sensor = _sensor(router)
    server = EventChannelServer(
        router=router, port=0, host="127.0.0.1",
        github_issue_sensor=sensor,
    )

    await server.start()
    try:
        port = await _bound_port(server)
        # Fire one webhook first so the counters are non-zero
        async with aiohttp.ClientSession() as client:
            await client.post(
                f"http://127.0.0.1:{port}/webhook/github",
                data=json.dumps(_payload(action="opened", number=42)),
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "issues",
                },
            )
            async with client.get(f"http://127.0.0.1:{port}/channel/health") as resp:
                assert resp.status == 200
                body = await resp.json()

        ghis = body.get("github_issue_sensor", {})
        assert ghis.get("wired") is True
        assert ghis.get("webhook_mode") is True
        assert ghis.get("webhooks_emitted") == 1
        assert ghis.get("last_webhook_at", 0) > 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_slow_sensor_does_not_hang_http_request(monkeypatch: Any) -> None:
    """Sensor.ingest_webhook that hangs must be cut by the 30s wait_for.

    Uses a monkeypatched method that sleeps for 2s and a lowered timeout
    via monkeypatching ``asyncio.wait_for`` — if the channel ever forgot
    its timeout wrapper we'd hang the HTTP worker forever. This proves
    the wrapper is in place.
    """
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "true")

    router = _SpyRouter()
    sensor = _sensor(router)

    # Replace ingest_webhook with one that sleeps longer than we'll wait.
    # Then monkeypatch asyncio.wait_for in the event_channel module to
    # a 0.2s budget so the test completes quickly while exercising the
    # timeout path.
    async def _slow_ingest(payload: Any) -> bool:
        await asyncio.sleep(2.0)
        return True

    sensor.ingest_webhook = _slow_ingest  # type: ignore[assignment]

    from backend.core.ouroboros.governance import event_channel as ec

    real_wait_for = ec.asyncio.wait_for

    async def _fast_timeout(coro: Any, timeout: float) -> Any:
        return await real_wait_for(coro, timeout=0.2)

    monkeypatch.setattr(ec.asyncio, "wait_for", _fast_timeout)

    server = EventChannelServer(
        router=router, port=0, host="127.0.0.1",
        github_issue_sensor=sensor,
    )

    await server.start()
    try:
        port = await _bound_port(server)
        url = f"http://127.0.0.1:{port}/webhook/github"
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues"}
        async with aiohttp.ClientSession() as client:
            # Even with the slow ingest, the HTTP response must come back
            # in under 1s (timeout is 0.2s).
            start = asyncio.get_event_loop().time()
            async with client.post(
                url, data=json.dumps(_payload()), headers=headers,
            ) as resp:
                assert resp.status == 200
            elapsed = asyncio.get_event_loop().time() - start
            assert elapsed < 1.0, (
                f"HTTP request should have completed in <1s (timeout cut at "
                f"0.2s), took {elapsed:.2f}s"
            )
        # Timeout path means emitted=False, so no envelope on router
        assert router.envelopes == []
    finally:
        await server.stop()
