"""Response-begin cooperative race — the 6th live integration gap (Window-1 SIGTERM run).

Live evidence (bt-iso-1782942507): a SIGTERM landed 59s into a streaming op that had
tokens=0 / first_token_ms=-1 and NO InterTokenStall — i.e. the coroutine was parked
inside ``sess.post(...)`` awaiting response HEADERS (ollama holds them through the
entire 1-4min L4 prefill), which is OUTSIDE the readline race added by PR #69809.
cooperative_shutdown fired, nobody was listening, and 3s later the pool cancel killed
the task -> GracefulStreamInterruption never raised -> 0 checkpoints.

Proves: the cooperative race must cover the response-begin await too, so a freeze
during prefill yields instantly with the prefill-seed partial.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.cooperative_shutdown as coop


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


class _StalledEnterCM:
    """Response headers never arrive -- simulates ollama holding the HTTP response
    open through the entire model prefill (the live-observed 1-4min window)."""

    def __init__(self):
        self.exited = False

    async def __aenter__(self):
        await asyncio.sleep(9999)
        raise AssertionError("unreachable")

    async def __aexit__(self, *a):
        self.exited = True
        return False


class _FailingEnterCM:
    """Request fails at response-begin -- the faithful-propagation pin."""

    async def __aenter__(self):
        raise ConnectionResetError("boom at response-begin")

    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, cm):
        self._cm = cm
        self.posted = []

    def post(self, url, **kw):
        self.posted.append(kw)
        return self._cm

    async def close(self):
        pass


def test_shutdown_during_response_begin_freezes_instantly():
    """Cooperative shutdown fired while awaiting response headers (prefill window)
    must raise GracefulStreamInterruption within ~2s carrying the prefill partial --
    NOT hang until an outer cancel bypasses the checkpoint path."""
    coop.reset()
    cm = _StalledEnterCM()
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(cm))

    async def _run():
        async def _fire():
            await asyncio.sleep(0.2)
            coop.request("sigterm")

        asyncio.ensure_future(_fire())
        start = time.monotonic()
        with pytest.raises(lid.GracefulStreamInterruption) as ei:
            await asyncio.wait_for(
                client.complete(system="s", user="u", prompt_tokens=10,
                                stream=True, prefill="partial so far"),
                timeout=5.0,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, "freeze must be preemptive (ms), got %.1fs" % elapsed
        assert ei.value.partial == "partial so far"

    asyncio.run(_run())


def test_response_begin_error_still_propagates_faithfully():
    """No-shutdown path: a genuine request failure at response-begin propagates
    unchanged (the race must not swallow or reclassify real errors)."""
    coop.reset()
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_FailingEnterCM()))

    async def _run():
        with pytest.raises(ConnectionResetError):
            await client.complete(system="s", user="u", prompt_tokens=10,
                                  stream=True)

    asyncio.run(_run())
