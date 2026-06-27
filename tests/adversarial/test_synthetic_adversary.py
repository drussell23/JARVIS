"""tests/adversarial/test_synthetic_adversary.py -- TDD suite for SyntheticAdversary.

Covers (per plan spec Task 3):
  (1) Each FailureSource fault emitted deterministically when scheduled:
      LIVE_TRANSPORT (conn-close), LIVE_HTTP_5XX (503), LIVE_HTTP_429 (429 +
      Retry-After), LIVE_PARSE_ERROR (200 + malformed body), LIVE_STREAM_STALL
      (SSE stall + no [DONE]).
  (2) No scheduled fault → healthy 200 + well-formed response.
  (3) Independent paths: /models healthy + /chat/completions LIVE_TRANSPORT
      → GET /models returns 200 AND POST /chat/completions fails (run-#11).
  (4) count-bounded faults: fail first N then heal.
  (5) env_overrides() returns localhost URLs matching the server port.

asyncio_mode = auto (pytest.ini), so no explicit @pytest.mark.asyncio needed.
JARVIS_ADVERSARY_STALL_S is set to a small value to make the stall test fast.
"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
import pytest

# Ensure repo root on sys.path
import sys
import os as _os
_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.chaos_injector import FakeClock  # reused (chaos_injector.py:76)
from tests.adversarial.fault_injector import FaultInjector, FaultType  # reused
from backend.core.ouroboros.governance.topology_sentinel import FailureSource  # reused taxonomy
from scripts.synthetic_adversary import SyntheticAdversary

# Keep stall duration tiny in tests (no real blocking)
_STALL_PATCH_S = "0.05"


# ── helpers ─────────────────────────────────────────────────────────────────── #

def _make_adversary(start_t: float = 0.0) -> tuple[SyntheticAdversary, FakeClock]:
    """Return (adversary, clock) with FakeClock starting at start_t."""
    clock = FakeClock(start=start_t)
    adv = SyntheticAdversary(clock=clock)
    return adv, clock


async def _get(session: aiohttp.ClientSession, url: str, **kwargs) -> aiohttp.ClientResponse:
    return await session.get(url, **kwargs)


async def _post(session: aiohttp.ClientSession, url: str, body: dict | None = None, **kwargs) -> aiohttp.ClientResponse:
    return await session.post(url, json=body or {}, **kwargs)


# ── fixture ──────────────────────────────────────────────────────────────────── #

@pytest.fixture(autouse=True)
def patch_stall_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep stall short so LIVE_STREAM_STALL tests don't block."""
    monkeypatch.setenv("JARVIS_ADVERSARY_STALL_S", _STALL_PATCH_S)


# ── (1) Each FailureSource emitted deterministically ─────────────────────────── #

class TestFaultDeterminism:
    """Each FailureSource fault fires when scheduled; one per endpoint per test."""

    async def test_live_http_5xx_returns_503(self) -> None:
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=FailureSource.LIVE_HTTP_5XX,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _post(sess, f"{urls['doubleword']}/chat/completions")
                assert resp.status == 503, f"Expected 503 for LIVE_HTTP_5XX, got {resp.status}"
                body = await resp.json()
                assert "error" in body
        finally:
            await adv.stop()

    async def test_live_http_429_returns_429_with_retry_after(self) -> None:
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/models",
            fault=FailureSource.LIVE_HTTP_429,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _get(sess, f"{urls['doubleword']}/models")
                assert resp.status == 429, f"Expected 429, got {resp.status}"
                assert "Retry-After" in resp.headers, "Missing Retry-After header"
                assert int(resp.headers["Retry-After"]) > 0
        finally:
            await adv.stop()

    async def test_live_parse_error_returns_200_with_malformed_json(self) -> None:
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=FailureSource.LIVE_PARSE_ERROR,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _post(sess, f"{urls['doubleword']}/chat/completions")
                assert resp.status == 200, f"LIVE_PARSE_ERROR should return 200, got {resp.status}"
                raw = await resp.text()
                with pytest.raises((json.JSONDecodeError, ValueError)):
                    json.loads(raw)
        finally:
            await adv.stop()

    async def test_live_stream_stall_opens_sse_and_stalls(self) -> None:
        """Server opens SSE connection, sends keep-alive, then stalls.
        Client receives the content-type header and the keep-alive comment but
        NO [DONE] token.  JARVIS_ADVERSARY_STALL_S is patched to 0.05s so the
        test completes in <1s total.
        """
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=FailureSource.LIVE_STREAM_STALL,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                timeout = aiohttp.ClientTimeout(total=2.0)
                async with sess.post(
                    f"{urls['doubleword']}/chat/completions",
                    json={"stream": True},
                    timeout=timeout,
                ) as resp:
                    assert resp.status == 200
                    assert "text/event-stream" in resp.content_type, (
                        f"Expected SSE content-type, got {resp.content_type}"
                    )
                    # Read everything the server sends
                    raw = await resp.read()
                    text = raw.decode()
                    assert "[DONE]" not in text, (
                        "LIVE_STREAM_STALL must NOT send [DONE] token"
                    )
                    assert "adversary-stall" in text, (
                        "Expected keep-alive comment in stalled SSE stream"
                    )
        finally:
            await adv.stop()

    async def test_live_transport_causes_connection_failure(self) -> None:
        """LIVE_TRANSPORT aborts the TCP connection; client sees an error."""
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=FailureSource.LIVE_TRANSPORT,
        )
        urls = await adv.start()
        connection_failed = False
        try:
            timeout = aiohttp.ClientTimeout(total=3.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                try:
                    async with sess.post(
                        f"{urls['doubleword']}/chat/completions",
                        json={"model": "test"},
                    ) as resp:
                        # Either we get a 5xx (if abort didn't fully suppress response)
                        if resp.status >= 500:
                            connection_failed = True
                        await resp.read()
                except (
                    aiohttp.ServerDisconnectedError,
                    aiohttp.ClientConnectionError,
                    aiohttp.ClientPayloadError,
                    aiohttp.ServerConnectionError,
                    ConnectionResetError,
                ):
                    connection_failed = True
        finally:
            await adv.stop()
        assert connection_failed, (
            "Expected connection failure (5xx or disconnect) for LIVE_TRANSPORT"
        )


# ── (2) No fault → healthy response ─────────────────────────────────────────── #

class TestHealthyResponse:
    """With no scheduled fault the server returns well-formed healthy responses."""

    async def test_models_healthy_200(self) -> None:
        adv, _ = _make_adversary()
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _get(sess, f"{urls['doubleword']}/models")
                assert resp.status == 200
                body = await resp.json()
                assert body["object"] == "list"
                assert len(body["data"]) >= 1
                assert "id" in body["data"][0]
        finally:
            await adv.stop()

    async def test_chat_completions_healthy_sse(self) -> None:
        adv, _ = _make_adversary()
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{urls['doubleword']}/chat/completions",
                    json={"stream": True, "model": "test-model"},
                ) as resp:
                    assert resp.status == 200
                    assert "text/event-stream" in resp.content_type
                    raw = await resp.read()
                    text = raw.decode()
                    assert "[DONE]" in text, "Healthy SSE must contain [DONE]"
        finally:
            await adv.stop()

    async def test_chat_completions_healthy_json(self) -> None:
        adv, _ = _make_adversary()
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _post(
                    sess,
                    f"{urls['doubleword']}/chat/completions",
                    body={"stream": False, "model": "test-model"},
                )
                assert resp.status == 200
                body = await resp.json()
                assert body["object"] == "chat.completion"
                assert len(body["choices"]) >= 1
                assert body["choices"][0]["finish_reason"] == "stop"
        finally:
            await adv.stop()


# ── (3) Independent path control — the run-#11 condition ─────────────────────── #

class TestIndependentPaths:
    """/models and /chat/completions are independently controllable."""

    async def test_models_healthy_while_chat_fails_live_transport(self) -> None:
        """Schedule /chat/completions=LIVE_TRANSPORT, /models=NO FAULT.

        GET /models must return 200.
        POST /chat/completions must fail (the exact run-#11 condition).
        """
        adv, _ = _make_adversary()
        # Only schedule generation path; probe path gets no schedule → healthy
        adv.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=FailureSource.LIVE_TRANSPORT,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                # HeavyProbe path (/models) must be healthy
                models_resp = await _get(sess, f"{urls['doubleword']}/models")
                assert models_resp.status == 200, (
                    f"/models should return 200 (probe healthy), got {models_resp.status}"
                )
                models_body = await models_resp.json()
                assert models_body["object"] == "list"

                # Generation path (/chat/completions) must fail
                gen_failed = False
                try:
                    async with sess.post(
                        f"{urls['doubleword']}/chat/completions",
                        json={"model": "test"},
                        timeout=aiohttp.ClientTimeout(total=3.0),
                    ) as resp:
                        if resp.status >= 500:
                            gen_failed = True
                        await resp.read()
                except (
                    aiohttp.ServerDisconnectedError,
                    aiohttp.ClientConnectionError,
                    aiohttp.ClientPayloadError,
                    aiohttp.ServerConnectionError,
                    ConnectionResetError,
                ):
                    gen_failed = True
                assert gen_failed, (
                    "POST /chat/completions should fail (LIVE_TRANSPORT) "
                    "while GET /models stays healthy"
                )
        finally:
            await adv.stop()

    async def test_models_fails_while_chat_healthy(self) -> None:
        """Converse: /models=LIVE_HTTP_5XX, /chat/completions=no fault."""
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/models",
            fault=FailureSource.LIVE_HTTP_5XX,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                models_resp = await _get(sess, f"{urls['doubleword']}/models")
                assert models_resp.status == 503

                chat_resp = await _post(
                    sess,
                    f"{urls['doubleword']}/chat/completions",
                    body={"stream": False},
                )
                assert chat_resp.status == 200
        finally:
            await adv.stop()

    async def test_both_paths_can_have_different_faults(self) -> None:
        """/models=LIVE_HTTP_429, /chat/completions=LIVE_HTTP_5XX simultaneously."""
        adv, _ = _make_adversary()
        adv.schedule(route="doubleword", endpoint="/models", fault=FailureSource.LIVE_HTTP_429)
        adv.schedule(route="doubleword", endpoint="/chat/completions", fault=FailureSource.LIVE_HTTP_5XX)
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                models_resp = await _get(sess, f"{urls['doubleword']}/models")
                assert models_resp.status == 429
                chat_resp = await _post(sess, f"{urls['doubleword']}/chat/completions")
                assert chat_resp.status == 503
        finally:
            await adv.stop()


# ── (4) count-bounded faults ──────────────────────────────────────────────────── #

class TestCountBoundedFaults:
    """Faults with a count limit heal after exhaustion."""

    async def test_fail_first_n_then_heal(self) -> None:
        """count=3: first 3 POST /chat/completions → 503; 4th → 200."""
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=FailureSource.LIVE_HTTP_5XX,
            count=3,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                statuses = []
                for _ in range(4):
                    resp = await _post(
                        sess,
                        f"{urls['doubleword']}/chat/completions",
                        body={"stream": False},
                    )
                    statuses.append(resp.status)
                    await resp.read()

                assert statuses[:3] == [503, 503, 503], (
                    f"First 3 requests should be 503, got {statuses[:3]}"
                )
                assert statuses[3] == 200, (
                    f"4th request should heal to 200 after count exhausted, got {statuses[3]}"
                )
        finally:
            await adv.stop()

    async def test_count_1_single_shot_then_heal(self) -> None:
        """count=1: first request fails, second is healthy."""
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/models",
            fault=FailureSource.LIVE_HTTP_5XX,
            count=1,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                first = await _get(sess, f"{urls['doubleword']}/models")
                assert first.status == 503
                await first.read()
                second = await _get(sess, f"{urls['doubleword']}/models")
                assert second.status == 200
        finally:
            await adv.stop()

    async def test_count_none_infinite_fault(self) -> None:
        """count=None (default): fault fires on every request."""
        adv, _ = _make_adversary()
        adv.schedule(
            route="doubleword",
            endpoint="/models",
            fault=FailureSource.LIVE_HTTP_5XX,
            count=None,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                for _ in range(5):
                    resp = await _get(sess, f"{urls['doubleword']}/models")
                    assert resp.status == 503, "Infinite fault must persist"
                    await resp.read()
        finally:
            await adv.stop()


# ── (5) env_overrides returns localhost URLs ──────────────────────────────────── #

class TestEnvOverrides:
    """env_overrides() returns the correct localhost URLs after start()."""

    async def test_env_overrides_returns_localhost_urls(self) -> None:
        adv, _ = _make_adversary()
        urls = await adv.start()
        try:
            overrides = adv.env_overrides()
            # All values must be localhost URLs
            for key, val in overrides.items():
                assert val.startswith("http://127.0.0.1:"), (
                    f"env_overrides()[{key!r}] should be a localhost URL, got {val!r}"
                )
            # Required keys present
            assert "DOUBLEWORD_BASE_URL" in overrides
            assert "JARVIS_PRIME_URL" in overrides
            assert "JARVIS_REACTOR_URL" in overrides
            assert "JARVIS_AEGIS_URL" in overrides
            # DW URL must end with /dw (so base/chat/completions resolves correctly)
            assert overrides["DOUBLEWORD_BASE_URL"].endswith("/dw"), (
                "DOUBLEWORD_BASE_URL must end with /dw so providers construct "
                "correct endpoint URLs"
            )
        finally:
            await adv.stop()

    async def test_env_overrides_port_matches_start_urls(self) -> None:
        adv, _ = _make_adversary()
        start_urls = await adv.start()
        try:
            overrides = adv.env_overrides()
            dw_url = overrides["DOUBLEWORD_BASE_URL"]
            # Both start() and env_overrides() must agree on the port
            assert dw_url == start_urls["doubleword"], (
                f"env_overrides() DW URL {dw_url!r} != start() URL {start_urls['doubleword']!r}"
            )
        finally:
            await adv.stop()

    async def test_env_overrides_raises_before_start(self) -> None:
        adv, _ = _make_adversary()
        with pytest.raises(RuntimeError, match="start\\(\\)"):
            adv.env_overrides()


# ── clock-based scheduling ────────────────────────────────────────────────────── #

class TestClockBasedScheduling:
    """Faults respect the FakeClock `at` parameter (no real sleeps)."""

    async def test_fault_inactive_before_at(self) -> None:
        """Fault scheduled at t=10; at t=0 the server serves healthy."""
        clock = FakeClock(start=0.0)
        adv = SyntheticAdversary(clock=clock)
        adv.schedule(
            route="doubleword",
            endpoint="/models",
            fault=FailureSource.LIVE_HTTP_5XX,
            at=10.0,
        )
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                # t=0: before activation → healthy
                resp = await _get(sess, f"{urls['doubleword']}/models")
                assert resp.status == 200, f"Before at=10: expected 200, got {resp.status}"
                await resp.read()

                # Advance clock to t=10 → fault activates
                clock.advance(10.0)
                resp = await _get(sess, f"{urls['doubleword']}/models")
                assert resp.status == 503, f"At at=10: expected 503, got {resp.status}"
                await resp.read()
        finally:
            await adv.stop()

    async def test_multiple_faults_sequential_clock_activation(self) -> None:
        """Two faults at different times on the same endpoint."""
        clock = FakeClock(start=0.0)
        adv = SyntheticAdversary(clock=clock)
        # at=5: 429; at=15: 5xx
        adv.schedule(route="doubleword", endpoint="/models",
                     fault=FailureSource.LIVE_HTTP_429, at=5.0, count=1)
        adv.schedule(route="doubleword", endpoint="/models",
                     fault=FailureSource.LIVE_HTTP_5XX, at=15.0, count=1)
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                # t=0: healthy
                r = await _get(sess, f"{urls['doubleword']}/models")
                assert r.status == 200
                await r.read()

                clock.advance(6.0)  # t=6, first fault active
                r = await _get(sess, f"{urls['doubleword']}/models")
                assert r.status == 429
                await r.read()

                clock.advance(10.0)  # t=16, second fault active (first exhausted)
                r = await _get(sess, f"{urls['doubleword']}/models")
                assert r.status == 503
                await r.read()

                # After second fault exhausted → healthy again
                r = await _get(sess, f"{urls['doubleword']}/models")
                assert r.status == 200
                await r.read()
        finally:
            await adv.stop()


# ── prime / reactor stubs ─────────────────────────────────────────────────────── #

class TestPrimeReactorStubs:
    """Prime and Reactor stubs respond healthy by default."""

    async def test_prime_healthy(self) -> None:
        adv, _ = _make_adversary()
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _get(sess, f"{urls['prime']}/v1/completions")
                assert resp.status == 200
                body = await resp.json()
                assert body["status"] == "ok"
        finally:
            await adv.stop()

    async def test_reactor_healthy(self) -> None:
        adv, _ = _make_adversary()
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _get(sess, f"{urls['reactor']}/v1/telemetry/events")
                assert resp.status == 200
                body = await resp.json()
                assert body["status"] == "ok"
        finally:
            await adv.stop()

    async def test_prime_fault_injectable(self) -> None:
        adv, _ = _make_adversary()
        adv.schedule(route="prime", endpoint="/v1/completions",
                     fault=FailureSource.LIVE_HTTP_5XX)
        urls = await adv.start()
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await _get(sess, f"{urls['prime']}/v1/completions")
                assert resp.status == 503
        finally:
            await adv.stop()


# ── reuse verification — FaultInjector / FakeClock / FailureSource actually used #

class TestReuseVerification:
    """Verify the reused modules are genuinely exercised (not just imported)."""

    def test_fault_injector_used_internally(self) -> None:
        """SyntheticAdversary holds a FaultInjector instance."""
        adv, _ = _make_adversary()
        assert isinstance(adv._injector, FaultInjector), (
            "SyntheticAdversary._injector must be a FaultInjector"
        )

    def test_fake_clock_drives_scheduling(self) -> None:
        """FakeClock advance changes which faults are active."""
        clock = FakeClock(start=0.0)
        adv = SyntheticAdversary(clock=clock)
        adv.schedule(route="doubleword", endpoint="/models",
                     fault=FailureSource.LIVE_HTTP_5XX, at=100.0)
        # At t=0: no fault
        assert adv._get_active_fault("doubleword", "/models") is None
        # Advance past activation time
        clock.advance(101.0)
        fault = adv._get_active_fault("doubleword", "/models")
        assert fault is not None
        assert fault == FailureSource.LIVE_HTTP_5XX

    def test_failure_source_taxonomy_is_the_sot(self) -> None:
        """FailureSource (topology_sentinel.py) values are accepted by schedule()."""
        adv, _ = _make_adversary()
        # All five DW HTTP faults from topology_sentinel.py:429-437
        for fs in (
            FailureSource.LIVE_TRANSPORT,
            FailureSource.LIVE_HTTP_5XX,
            FailureSource.LIVE_HTTP_429,
            FailureSource.LIVE_PARSE_ERROR,
            FailureSource.LIVE_STREAM_STALL,
        ):
            adv.schedule(route="doubleword", endpoint="/chat/completions",
                         fault=fs, count=0)  # count=0: never fires; just registers

        # All 5 entries added without error
        fs_values = {str(e.fault) for e in adv._faults}
        assert len(fs_values) == 5
