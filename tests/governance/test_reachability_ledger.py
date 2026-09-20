"""Execution provenance, and the guarantee that concurrency cannot corrupt it.

The audit that produced this ledger found the micro-fix had run 532 times and
repaired nothing, the retry ladder had never regenerated, and 43 capability
switches were dark. Every one of those was *registered*; most were *invoked*;
none were ever *effective*, and nothing could tell the difference.

So the load-bearing property here is not that counting works. It is that
``effective`` cannot be satisfied by code that merely ran.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.reachability_ledger import (
    ReachabilityLedger,
    Tier,
    track_reachability,
    tracks_reachability,
)


# ---------------------------------------------------------------------------
# The three tiers are not interchangeable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_is_the_weakest_evidence():
    """A dead subsystem registers perfectly. That is the whole problem."""
    lg = ReachabilityLedger()
    await lg.registered("cap")
    state = await lg.state("cap")
    assert state.registered == 1
    assert state.invoked == 0
    assert state.dormant is True
    assert state.inert is False


@pytest.mark.asyncio
async def test_invoked_without_effect_is_the_alarm():
    """``micro_fix_pre 532 / effective 0`` is the shape this exists to name."""
    lg = ReachabilityLedger()
    for _ in range(532):
        await lg.invoked("micro_fix", op_id="op")
    state = await lg.state("micro_fix")
    assert state.invoked == 532
    assert state.effective == 0
    assert state.inert is True
    assert [s.capability for s in await lg.inert()] == ["micro_fix"]


@pytest.mark.asyncio
async def test_effective_clears_the_alarm():
    lg = ReachabilityLedger()
    await lg.invoked("cap", op_id="op")
    await lg.effective("cap", op_id="op")
    assert await lg.inert() == []


@pytest.mark.asyncio
async def test_inert_respects_min_invocations():
    """One invocation is not yet evidence of inertness."""
    lg = ReachabilityLedger()
    await lg.invoked("cap", op_id="op")
    assert await lg.inert(min_invocations=5) == []
    assert len(await lg.inert(min_invocations=1)) == 1


# ---------------------------------------------------------------------------
# Effectiveness has a criterion the caller does not get to assert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reformatting_is_not_effective():
    """Whitespace is not repair. If it counted, a loop that changed nothing
    would look identical to one that fixed the bug."""
    lg = ReachabilityLedger()
    async with track_reachability("cap", op_id="op", ledger=lg) as effect:
        assert effect.ast_mutation("x = 1\n", "x  =  1\n") is False
    assert (await lg.state("cap")).effective == 0


@pytest.mark.asyncio
async def test_comment_only_change_is_not_effective():
    lg = ReachabilityLedger()
    async with track_reachability("cap", op_id="op", ledger=lg) as effect:
        assert effect.ast_mutation("x = 1\n", "# note\nx = 1\n") is False
    assert (await lg.state("cap")).effective == 0


@pytest.mark.asyncio
async def test_structural_change_is_effective():
    lg = ReachabilityLedger()
    async with track_reachability("cap", op_id="op", ledger=lg) as effect:
        assert effect.ast_mutation("x = 1\n", "x = 2\n") is True
    assert (await lg.state("cap")).effective == 1


@pytest.mark.asyncio
async def test_empty_result_is_not_effective():
    lg = ReachabilityLedger()
    async with track_reachability("cap", op_id="op", ledger=lg) as effect:
        assert effect.ast_mutation("x = 1\n", "") is False
    assert (await lg.state("cap")).effective == 0


@pytest.mark.asyncio
async def test_invocation_is_recorded_even_when_the_body_raises():
    """A capability that raised still ran. A ledger that counts only clean
    exits under-reports exactly the paths most worth seeing."""
    lg = ReachabilityLedger()
    with pytest.raises(RuntimeError):
        async with track_reachability("cap", op_id="op", ledger=lg):
            raise RuntimeError("boom")
    state = await lg.state("cap")
    assert state.invoked == 1
    assert state.effective == 0


# ---------------------------------------------------------------------------
# Concurrency — the same read-modify-write shape as the admission race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_invocations_are_not_dropped():
    """VALIDATE fans candidates out under asyncio.gather and the sentinel
    runs goals concurrently. A counter that loses increments under that load
    would under-report inertness, which is the one error this ledger cannot
    be allowed to make."""
    lg = ReachabilityLedger()
    await asyncio.gather(*[
        lg.invoked("cap", op_id=f"op{i}") for i in range(500)
    ])
    assert (await lg.state("cap")).invoked == 500


@pytest.mark.asyncio
async def test_concurrent_mixed_tiers_are_exact():
    lg = ReachabilityLedger()
    await asyncio.gather(
        *[lg.registered("cap") for _ in range(50)],
        *[lg.invoked("cap", op_id="o") for _ in range(120)],
        *[lg.effective("cap", op_id="o") for _ in range(30)],
    )
    state = await lg.state("cap")
    assert (state.registered, state.invoked, state.effective) == (50, 120, 30)


@pytest.mark.asyncio
async def test_concurrent_capabilities_do_not_bleed():
    lg = ReachabilityLedger()
    await asyncio.gather(*[
        lg.invoked(f"cap{i % 7}", op_id="o") for i in range(700)
    ])
    snap = await lg.snapshot()
    assert sorted(snap) == [f"cap{i}" for i in range(7)]
    assert all(s.invoked == 100 for s in snap.values())


@pytest.mark.asyncio
async def test_concurrent_writes_produce_one_intact_line_each(tmp_path):
    """Durability under the same load: every record must be a complete,
    parseable line. A torn append is a silently corrupted audit trail."""
    path = tmp_path / "reach.jsonl"
    lg = ReachabilityLedger(path=path)
    await asyncio.gather(*[
        lg.invoked("cap", op_id=f"op{i}", detail="x" * 50) for i in range(300)
    ])
    lines = path.read_text().splitlines()
    assert len(lines) == 300
    for line in lines:
        record = json.loads(line)
        assert record["capability"] == "cap"
        assert record["tier"] == Tier.INVOKED.value


# ---------------------------------------------------------------------------
# Decorator form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decorator_marks_truthy_returns_effective():
    lg = ReachabilityLedger()

    @tracks_reachability("cap", ledger=lg)
    async def _work(*, op_id=""):
        return "result"

    assert await _work(op_id="op") == "result"
    state = await lg.state("cap")
    assert (state.invoked, state.effective) == (1, 1)


@pytest.mark.asyncio
async def test_decorator_falsy_return_is_invoked_not_effective():
    lg = ReachabilityLedger()

    @tracks_reachability("cap", ledger=lg)
    async def _work(*, op_id=""):
        return None

    await _work(op_id="op")
    state = await lg.state("cap")
    assert (state.invoked, state.effective) == (1, 0)


@pytest.mark.asyncio
async def test_ledger_without_path_keeps_counting(tmp_path):
    """No configured path must not disable the counters — they are the part
    the alarm reads."""
    lg = ReachabilityLedger(path=None)
    await lg.invoked("cap", op_id="op")
    assert (await lg.state("cap")).invoked == 1
