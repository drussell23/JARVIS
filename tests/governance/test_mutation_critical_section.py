from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import mutation_critical_section as mcs

def test_section_marks_mutating():
    async def go():
        assert mcs.is_mutating("op1") is False
        async with mcs.mutation_section("op1"):
            assert mcs.is_mutating("op1") is True
        assert mcs.is_mutating("op1") is False
    asyncio.run(go())

def test_drain_returns_true_when_idle():
    async def go():
        return await mcs.drain("opX", timeout=0.5)
    assert asyncio.run(go()) is True

def test_drain_waits_then_true():
    async def go():
        async def hold():
            async with mcs.mutation_section("op2"):
                await asyncio.sleep(0.2)
        t = asyncio.create_task(hold()); await asyncio.sleep(0.01)
        ok = await mcs.drain("op2", timeout=2.0); await t
        return ok
    assert asyncio.run(go()) is True

def test_drain_abandons_on_wedge():
    async def go():
        async def wedge():
            async with mcs.mutation_section("op3"):
                await asyncio.sleep(5.0)
        t = asyncio.create_task(wedge()); await asyncio.sleep(0.01)
        ok = await mcs.drain("op3", timeout=0.2)
        t.cancel()
        try: await t
        except asyncio.CancelledError: pass
        return ok
    assert asyncio.run(go()) is False

def test_nested_reentrant_same_op():
    async def go():
        async with mcs.mutation_section("op4"):
            async with mcs.mutation_section("op4"):
                assert mcs.is_mutating("op4") is True
            assert mcs.is_mutating("op4") is True   # still in outer section
        assert mcs.is_mutating("op4") is False
    asyncio.run(go())

def test_exception_in_section_still_decrements():
    async def go():
        try:
            async with mcs.mutation_section("op5"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert mcs.is_mutating("op5") is False
    asyncio.run(go())


def test_maybe_section_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "false")
    async def go():
        async with mcs.maybe_mutation_section("op6"):
            assert mcs.is_mutating("op6") is False
        assert mcs.is_mutating("op6") is False
    asyncio.run(go())


def test_maybe_section_active_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    async def go():
        async with mcs.maybe_mutation_section("op7"):
            assert mcs.is_mutating("op7") is True
        assert mcs.is_mutating("op7") is False
    asyncio.run(go())
