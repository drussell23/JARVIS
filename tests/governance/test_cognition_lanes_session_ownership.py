"""Whoever creates the provider closes it.

`bt-2026-08-18-021438` logged 20 "Unclosed client session" + 15 "Unclosed
connector" warnings. Every cluster landed against `[CognitionLanes] RT
degraded (rt status 402 ...)` -- one leak per RT attempt.

`rt_prompt._resolve()` constructed a `DoublewordProvider` whenever none was
injected, and the provider lazily opens an `aiohttp.ClientSession` inside
`_get_session()`. The provider was bound to `rt_prompt`'s own scope, so both
it and its session fell to the garbage collector when the call returned. The
provider was never at fault: it owns `close()` and closes its session
correctly. Nothing called it, because nothing had decided who owned it.

Measured against the real code, three un-injected calls: **3 unclosed
sessions before, 0 after**.

The second test is the one that matters most for safety. An INJECTED provider
belongs to its caller, is usually pooled, and is shared across calls --
closing it here would convert a leak into an outage for everyone else holding
it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import cognition_lanes as cl


class _FakeClaude:
    """The cascade target, so no test ever needs a network or a key."""

    def __init__(self) -> None:
        self.calls = 0

    async def prompt_only(self, *a, **k) -> str:
        self.calls += 1
        return "fallback"


class _FakeProvider:
    """Stands in for DoublewordProvider: records whether it was closed."""

    def __init__(self, *a, **k) -> None:
        self.closed = 0
        self._base_url = "http://provider.invalid"

    async def _get_session(self):
        raise RuntimeError("rt status 402: Account balance too low")

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def constructed(monkeypatch):
    """Intercept the provider class rt_prompt imports, recording instances."""
    made = []
    from backend.core.ouroboros.governance import doubleword_provider as dwp

    def _factory(*a, **k):
        p = _FakeProvider()
        made.append(p)
        return p

    monkeypatch.setattr(dwp, "DoublewordProvider", _factory)
    return made


@pytest.mark.asyncio
async def test_a_provider_this_call_created_is_closed(constructed):
    claude = _FakeClaude()
    out = await cl.rt_prompt("hi", model="m", caller_id="t",
                             timeout_s=0.2, claude=claude)
    assert out == "fallback"
    assert len(constructed) == 1, "the un-injected path must construct one"
    assert constructed[0].closed == 1, (
        "the provider this call created was left for the garbage collector — "
        "the exact shape of the 20 leaked sessions"
    )


@pytest.mark.asyncio
async def test_an_injected_provider_is_never_closed():
    """Closing someone else's pooled session turns a leak into an outage."""
    injected = _FakeProvider()
    claude = _FakeClaude()
    await cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                       dw_provider=injected, claude=claude)
    assert injected.closed == 0


@pytest.mark.asyncio
async def test_an_injected_session_constructs_no_provider_at_all(constructed):
    """`session` + `base_url` short-circuits `_resolve` before construction."""
    class _Sess:
        def post(self, *a, **k):
            raise RuntimeError("rt status 402: Account balance too low")

    await cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                       session=_Sess(), base_url="http://x.invalid",
                       claude=_FakeClaude())
    assert constructed == []


@pytest.mark.asyncio
async def test_release_runs_on_the_cascade_path(constructed):
    """The RT path fails far more often than it succeeds when a provider is
    down — which is precisely the run that leaked. A release reachable only
    on the happy path would never run when it matters."""
    await cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                       claude=_FakeClaude())
    assert constructed[0].closed == 1


@pytest.mark.asyncio
async def test_release_runs_when_the_cascade_itself_fails(constructed):
    """Both lanes down: the caller gets the terminal error AND no leak."""
    class _DeadClaude:
        async def prompt_only(self, *a, **k):
            raise RuntimeError("claude down too")

    with pytest.raises(RuntimeError, match="claude down too"):
        await cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                           claude=_DeadClaude())
    assert constructed[0].closed == 1


@pytest.mark.asyncio
async def test_release_runs_on_cancellation(constructed):
    """Cancellation must still release. A `finally` that awaits while being
    cancelled is exactly where a naive release would be skipped."""
    started = asyncio.Event()

    class _SlowClaude:
        async def prompt_only(self, *a, **k):
            started.set()
            await asyncio.sleep(30)

    task = asyncio.ensure_future(
        cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                     claude=_SlowClaude())
    )
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert constructed and constructed[0].closed == 1


@pytest.mark.asyncio
async def test_a_failing_close_never_surfaces_to_the_caller(monkeypatch):
    """Losing a session is a leak; raising here would lose the caller's
    result. The release is guarded per-provider."""
    from backend.core.ouroboros.governance import doubleword_provider as dwp

    class _BadClose(_FakeProvider):
        async def close(self):
            raise RuntimeError("close exploded")

    monkeypatch.setattr(dwp, "DoublewordProvider", lambda *a, **k: _BadClose())
    out = await cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                             claude=_FakeClaude())
    assert out == "fallback"


@pytest.mark.asyncio
async def test_release_is_idempotent_and_drains_the_ledger(constructed):
    """Two sequential calls each own and release exactly their own provider."""
    for _ in range(2):
        await cl.rt_prompt("hi", model="m", caller_id="t", timeout_s=0.2,
                           claude=_FakeClaude())
    assert len(constructed) == 2
    assert all(p.closed == 1 for p in constructed)
