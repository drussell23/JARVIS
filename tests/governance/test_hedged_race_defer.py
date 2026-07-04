from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.dw_transport_hedge import hedged_race

pytestmark = pytest.mark.asyncio


class Boom(RuntimeError):
    pass


async def test_defer_stable_never_fires_batch_when_fast_wins():
    fired = {"stable": 0}

    async def fast():
        return "rt-result"

    async def stable():
        fired["stable"] += 1
        return "batch-result"

    out = await hedged_race(fast, stable, prefer_fast=True, defer_stable=True)
    assert out == "rt-result"
    await asyncio.sleep(0)  # drain any stray scheduling
    assert fired["stable"] == 0  # STRUCTURAL: batch never ignited -> zero double-spend


async def test_defer_stable_ignites_on_fast_failure_and_wins():
    fired = {"stable": 0}

    async def fast():
        raise Boom("rt ruptured")

    async def stable():
        fired["stable"] += 1
        return "batch-result"

    out = await hedged_race(
        fast, stable, prefer_fast=True, defer_stable=True,
        is_rupture=lambda e: isinstance(e, Boom),
    )
    assert out == "batch-result"
    assert fired["stable"] == 1  # ignited exactly once, event-driven


async def test_defer_stable_both_fail_raises_and_reports_abandoned():
    seen = {}

    async def fast():
        raise Boom("rt dead")

    async def stable():
        raise ValueError("batch dead")

    def on_abandoned(fe, se):
        seen["fast"], seen["stable"] = fe, se

    with pytest.raises(ValueError):
        await hedged_race(
            fast, stable, prefer_fast=True, defer_stable=True,
            is_rupture=lambda e: isinstance(e, Boom),
            on_abandoned=on_abandoned,
        )
    assert isinstance(seen["fast"], Boom)
    assert isinstance(seen["stable"], ValueError)


async def test_legacy_mode_unchanged_first_completed_wins():
    async def fast():
        await asyncio.sleep(0.05)
        return "rt"

    async def stable():
        return "batch"

    out = await hedged_race(fast, stable)  # defer_stable default False
    assert out == "batch"  # legacy FIRST_COMPLETED byte-identical


async def test_eager_prefer_fast_buffer_mode_still_works():
    async def fast():
        await asyncio.sleep(0.05)
        return "rt"

    async def stable():
        return "batch"

    out = await hedged_race(fast, stable, prefer_fast=True, defer_stable=False)
    assert out == "rt"  # batch buffered, RT success supersedes (s227 preserved)
