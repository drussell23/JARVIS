"""Slice 241 T2 — graduate the RT-vs-batch transport hedge to default-ON.

The hedge (dw_transport_hedge.hedged_race) was BUILT + wired into the DW provider
(_s189_transport_hedge_active → hedged_race) but gated default-OFF
(JARVIS_DW_TRANSPORT_HEDGE_ENABLED, §33.1 "opt-in per deployment"). T2 graduates it:
race the RT stream against the stable batch path, take the first success, cancel the
loser; if RT ruptures mid-generation, the rupture is swallowed and the surviving
batch payload is returned seamlessly — no live_transport exception escapes. This is
the only remaining CODE lever for DW transport volatility (it can't help a wholesale
DW outage where BOTH arms fail, but it makes a partial RT rupture invisible).

These tests pin the graduated default + the rupture-pivot guarantee (your Phase 3:
RT drops mid-stream → the completed batch payload is returned without a transport
error). The deep race mechanics are already covered by slice188/189/190/194/227.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import dw_transport_hedge as h


class TestHedgeGraduation:
    def test_default_now_on(self, monkeypatch):
        # T2 graduation: with no env override, the hedge is ACTIVE.
        monkeypatch.delenv("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", raising=False)
        assert h.transport_hedge_enabled() is True

    def test_kill_switch_reverts(self, monkeypatch):
        # rollback contract: =0 reverts to the legacy single-stream path.
        monkeypatch.setenv("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", "0")
        assert h.transport_hedge_enabled() is False


class TestRupturePivot:
    """Phase 3 — RT ruptures mid-generation → batch payload returned, no throw."""

    async def test_rt_rupture_pivots_to_batch(self):
        async def fast():  # RT drops "at the 50% byte mark"
            await asyncio.sleep(0.001)
            raise RuntimeError("live_transport: peer reset mid-stream (50%)")

        async def stable():  # batch survives and completes
            await asyncio.sleep(0.005)
            return "batch_payload"

        out = await h.hedged_race(fast, stable, is_rupture=lambda e: True)
        assert out == "batch_payload"  # rupture swallowed, batch returned seamlessly

    async def test_rt_success_cancels_batch_loser(self):
        cancelled = {"batch": False}

        async def fast():
            return "rt_payload"

        async def stable():
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                cancelled["batch"] = True
                raise
            return "batch_payload"

        out = await h.hedged_race(fast, stable)
        assert out == "rt_payload"  # RT primacy on success
        await asyncio.sleep(0.01)
        assert cancelled["batch"] is True  # loser cancelled to preserve bandwidth

    async def test_both_arms_fail_raises(self):
        async def fast():
            raise RuntimeError("rt down")

        async def stable():
            raise RuntimeError("batch down")

        # wholesale DW outage: both arms rupture → the hedge cannot save it (honest
        # limit — it surfaces the failure rather than hanging).
        with pytest.raises(BaseException):
            await h.hedged_race(fast, stable, is_rupture=lambda e: True)

    async def test_rupture_pivot_emits_invisible_save_telemetry(self):
        # on_outcome(winner, rupture_swallowed) must report that batch won AFTER an
        # RT rupture — the "proactive capital-save made the rupture invisible" signal.
        seen = {}

        async def fast():
            await asyncio.sleep(0.001)
            raise RuntimeError("live_transport rupture")

        async def stable():
            await asyncio.sleep(0.005)
            return "batch_payload"

        def _outcome(winner, rupture_swallowed):
            seen["winner"] = winner
            seen["rupture_swallowed"] = rupture_swallowed

        out = await h.hedged_race(
            fast, stable, is_rupture=lambda e: True, on_outcome=_outcome,
        )
        assert out == "batch_payload"
        assert seen["winner"] == "batch"
        assert seen["rupture_swallowed"] is True
