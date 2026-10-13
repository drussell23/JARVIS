"""Foreground reservation on the local generation lane (generation_admission).

Admission, not preemption: a running request is never touched; while a
/session turn is active, background admission stops at capacity-1 so the turn
keeps a slot across its rounds. Capacity is the engine's reported slots.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import generation_admission as ga


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ga.reset_for_tests()
    yield
    ga.reset_for_tests()


def _cap(monkeypatch, n):
    monkeypatch.setattr(ga._Admission, "capacity", lambda self: n)


async def _hold(adm, started: asyncio.Event, release: asyncio.Event):
    async with adm.generation():
        started.set()
        await release.wait()


def test_without_a_turn_background_fills_every_slot(monkeypatch):
    _cap(monkeypatch, 2)

    async def run():
        adm = ga.admission()
        rel = asyncio.Event()
        s1, s2 = asyncio.Event(), asyncio.Event()
        t1 = asyncio.create_task(_hold(adm, s1, rel))
        t2 = asyncio.create_task(_hold(adm, s2, rel))
        await asyncio.wait_for(asyncio.gather(s1.wait(), s2.wait()), 1)
        assert adm.snapshot()["background_inflight"] == 2 and adm.waits == 0
        rel.set()
        await asyncio.gather(t1, t2)
    asyncio.run(run())


def test_a_turn_keeps_one_slot_and_background_resumes_when_it_ends(monkeypatch):
    _cap(monkeypatch, 2)

    async def run():
        adm = ga.admission()
        rel, turn_done = asyncio.Event(), asyncio.Event()
        s1, s2 = asyncio.Event(), asyncio.Event()
        t1 = asyncio.create_task(_hold(adm, s1, rel))
        await asyncio.wait_for(s1.wait(), 1)

        async def turn():
            async with adm.foreground_turn():
                await turn_done.wait()

        tt = asyncio.create_task(turn())
        await asyncio.sleep(0)
        t2 = asyncio.create_task(_hold(adm, s2, rel))
        await asyncio.sleep(0.05)
        assert not s2.is_set(), "the reserved slot was handed to background"
        assert adm.snapshot()["background_inflight"] == 1      # the running one untouched
        turn_done.set()
        await asyncio.wait_for(s2.wait(), 1)                   # resumes when the turn ends
        rel.set()
        await asyncio.gather(t1, t2, tt)
        assert adm.waits == 1
    asyncio.run(run())


def test_foreground_generations_never_wait(monkeypatch):
    _cap(monkeypatch, 1)

    async def run():
        adm = ga.admission()
        rel, s_bg, s_fg = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tbg = asyncio.create_task(_hold(adm, s_bg, rel))
        await asyncio.wait_for(s_bg.wait(), 1)

        async def turn():
            async with adm.foreground_turn():
                assert ga.current_priority() == ga.FOREGROUND
                async with adm.generation():
                    s_fg.set()

        await asyncio.wait_for(turn(), 1)
        assert s_fg.is_set() and ga.current_priority() == ga.BACKGROUND
        rel.set()
        await tbg
    asyncio.run(run())


def test_with_one_slot_background_waits_for_the_whole_turn(monkeypatch):
    _cap(monkeypatch, 1)

    async def run():
        adm = ga.admission()
        done, started, rel = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def turn():
            async with adm.foreground_turn():
                await done.wait()

        tt = asyncio.create_task(turn())
        await asyncio.sleep(0)
        tb = asyncio.create_task(_hold(adm, started, rel))
        await asyncio.sleep(0.05)
        assert not started.is_set()
        done.set()
        await asyncio.wait_for(started.wait(), 1)
        rel.set()
        await asyncio.gather(tt, tb)
    asyncio.run(run())


def test_a_cancelled_waiter_leaks_no_slot(monkeypatch):
    _cap(monkeypatch, 1)

    async def run():
        adm = ga.admission()
        done = asyncio.Event()

        async def turn():
            async with adm.foreground_turn():
                await done.wait()

        tt = asyncio.create_task(turn())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(_hold(adm, asyncio.Event(), asyncio.Event()))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        done.set()
        await tt
        assert adm.snapshot()["background_inflight"] == 0 and adm.snapshot()["turns"] == 0
    asyncio.run(run())


def test_capacity_is_the_engines_reported_slots(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import engine_slots
    engine_slots.reset_for_tests()
    engine_slots.record("http://127.0.0.1:8000", 3)
    adm = ga.admission()
    adm._endpoint = "http://127.0.0.1:8000"
    assert adm.capacity() == 3
    engine_slots.reset_for_tests()


def test_the_switch_turns_the_gate_off(monkeypatch):
    monkeypatch.setenv("JARVIS_FOREGROUND_RESERVATION_ENABLED", "false")
    _cap(monkeypatch, 1)

    async def run():
        adm = ga.admission()
        async with adm.foreground_turn():
            got = asyncio.Event()
            await asyncio.wait_for(_hold(adm, got, _set(asyncio.Event())), 1)
            assert got.is_set()
    asyncio.run(run())


def _set(ev):
    ev.set()
    return ev


def test_every_local_request_passes_the_gate(monkeypatch):
    """LocalPrimeClient.complete is the one seam: it admits, then delegates."""
    from backend.core.ouroboros.governance import local_inference_director as lid
    seen = []

    class _Gate:
        def generation(self, endpoint=""):
            import contextlib

            @contextlib.asynccontextmanager
            async def cm():
                seen.append(endpoint)
                yield
            return cm()

    monkeypatch.setattr(ga, "admission", lambda: _Gate())

    async def fake(self, **kw):
        return "done"

    monkeypatch.setattr(lid.LocalPrimeClient, "_complete_unadmitted", fake)
    client = lid.LocalPrimeClient.__new__(lid.LocalPrimeClient)
    client._cfg = type("C", (), {"base_url": "http://x:8000"})()
    out = asyncio.run(client.complete(system="", user="u", prompt_tokens=1))
    assert out == "done" and seen == ["http://x:8000"]
