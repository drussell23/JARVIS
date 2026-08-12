"""Attention-path regression spine — the review clock spends ATTENDED
seconds, not wall-clock ones.

The defect these pin: a Yellow-tier review raised while the cockpit was
detached burned its whole ``JARVIS_REVIEW_TIMEOUT_S`` window against an
empty socket, auto-EXPIRED, and discarded verified work — indistinguishably
from an operator who looked and said no.

These tests drive REAL clocks and the REAL asyncio rendezvous. Nothing
here asserts on source text: a string assertion is a test of spelling and
cannot fail for a runtime reason.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance import attention_ledger as op_presence
from backend.core.ouroboros.governance.attention_ledger import (
    AttentionLedger,
    SOURCE_COCKPIT,
    get_attention_ledger,
    reset_attention_ledger_for_tests,
)
from backend.core.ouroboros.governance.review_coordinator import (
    ATTENTION_GATE_ENV_VAR,
    CANCEL_POLL_ENV_VAR,
    WALL_MULT_ENV_VAR,
    ReviewCoordinator,
    ReviewDecision,
)


@pytest.fixture(autouse=True)
def _clean_presence(monkeypatch):
    reset_attention_ledger_for_tests()
    monkeypatch.setenv(op_presence.FLAP_GRACE_ENV_VAR, "0")
    monkeypatch.setenv(CANCEL_POLL_ENV_VAR, "0.02")
    monkeypatch.setenv(WALL_MULT_ENV_VAR, "0")  # unbounded unless a test asks
    yield
    reset_attention_ledger_for_tests()


# ===========================================================================
# The ledger
# ===========================================================================


def test_ledger_advances_only_while_attended():
    p = AttentionLedger()
    assert p.snapshot().attended_elapsed == 0.0
    assert not p.snapshot().armed

    time.sleep(0.05)
    # Nobody attached — no time may accrue, ever.
    assert p.snapshot().attended_elapsed == 0.0

    p.set_count(SOURCE_COCKPIT, 1)
    time.sleep(0.05)
    p.set_count(SOURCE_COCKPIT, 0)
    charged = p.snapshot().attended_elapsed
    assert 0.04 <= charged <= 0.20

    time.sleep(0.08)
    # Detached time is not merely uncounted — it is unreachable.
    assert p.snapshot().attended_elapsed == pytest.approx(charged, abs=1e-9)


def test_set_count_is_idempotent_and_takes_counts_not_deltas():
    p = AttentionLedger()
    p.set_count(SOURCE_COCKPIT, 2)
    e1 = p.snapshot().epoch
    p.set_count(SOURCE_COCKPIT, 2)  # republish of the same truth
    assert p.snapshot().epoch == e1, "no-op republish must not wake waiters"

    # A double-drop cannot underflow; a missed detach cannot leak.
    p.set_count(SOURCE_COCKPIT, 0)
    p.set_count(SOURCE_COCKPIT, 0)
    assert p.snapshot().count == 0
    p.set_count(SOURCE_COCKPIT, -5)
    assert p.snapshot().count == 0


def test_flapping_accrues_no_phantom_delta():
    """Ten rapid connect/disconnect cycles must charge only connected
    seconds — the budget neither resets nor gains a grace window."""
    p = AttentionLedger()
    connected = 0.0
    for _ in range(10):
        t0 = time.monotonic()
        p.set_count(SOURCE_COCKPIT, 1)
        time.sleep(0.005)
        p.set_count(SOURCE_COCKPIT, 0)
        connected += time.monotonic() - t0
        time.sleep(0.005)  # detached gap — must not be charged
    charged = p.snapshot().attended_elapsed
    assert charged <= connected + 0.01
    assert charged >= connected - 0.01


def test_multi_source_presence_is_a_union():
    p = AttentionLedger()
    p.set_count("cockpit", 1)
    p.set_count("ide_stream", 1)
    p.set_count("cockpit", 0)
    # One surface leaving does not end attention while another remains.
    assert p.snapshot().count == 1
    assert p.snapshot().attended
    p.set_count("ide_stream", 0)
    assert not p.snapshot().attended


@pytest.mark.asyncio
async def test_change_future_cannot_miss_an_edge():
    p = AttentionLedger()
    snap = p.snapshot()
    p.set_count(SOURCE_COCKPIT, 1)  # edge passes BEFORE we register
    fut = p.change_future(snap.epoch)
    assert fut.done(), "a stale epoch must resolve immediately"


@pytest.mark.asyncio
async def test_waiters_self_evict_on_cancel_and_on_resolve():
    p = AttentionLedger()
    fut = p.change_future(p.snapshot().epoch)
    assert len(p._waiters) == 1
    fut.cancel()
    await asyncio.sleep(0)
    assert len(p._waiters) == 0, "abandoned waiter leaked into the ledger"

    fut2 = p.change_future(p.snapshot().epoch)
    p.set_count(SOURCE_COCKPIT, 1)
    await asyncio.wait_for(fut2, timeout=1.0)
    assert len(p._waiters) == 0


# ===========================================================================
# The clock
# ===========================================================================


def _coordinator_with_pending(op_id: str):
    coord = ReviewCoordinator()
    event = asyncio.Event()
    box: list = []
    coord._pending[op_id] = (event, box)
    return coord, event, box


@pytest.mark.asyncio
async def test_unarmed_process_keeps_legacy_wall_clock_expiry(monkeypatch):
    """HEADLESS INVARIANT: no operator has ever attached, so there is no
    attention path to preserve and the gate must stay inert. A soak must
    not have its reviews silently pinned."""
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    coord, event, _ = _coordinator_with_pending("op-headless")
    t0 = time.monotonic()
    decision, reason, attended = await coord._wait_with_cancel(
        event, 0.2, None,
    )
    assert decision is None
    # The reason names WHICH clock ran out — unarmed spends wall-clock.
    assert reason == "wall_budget"
    assert attended == 0.0
    assert time.monotonic() - t0 < 1.0  # expired on wall-clock, promptly


@pytest.mark.asyncio
async def test_armed_and_detached_pauses_the_clock(monkeypatch):
    """THE FIX: an operator attached once, then left. The budget must not
    burn against the empty socket."""
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    presence = get_attention_ledger()
    presence.set_count(SOURCE_COCKPIT, 1)   # arms the gate
    presence.set_count(SOURCE_COCKPIT, 0)   # ...and leaves

    coord, event, _ = _coordinator_with_pending("op-away")
    task = asyncio.ensure_future(coord._wait_with_cancel(event, 0.2, None))
    with pytest.raises(asyncio.TimeoutError):
        # 0.2s budget, 0.6s of wall-clock — legacy would have EXPIRED.
        await asyncio.wait_for(asyncio.shield(task), timeout=0.6)

    presence.set_count(SOURCE_COCKPIT, 1)   # operator returns
    decision, reason, attended = await asyncio.wait_for(task, timeout=2.0)
    assert decision is None
    assert reason == "attended_budget"
    # It expired on ~0.2s of ATTENDED time, not the ~0.8s of wall-clock.
    assert 0.15 <= attended <= 0.45


@pytest.mark.asyncio
async def test_partial_budget_survives_a_detach_and_resumes(monkeypatch):
    """The clock is neither reset nor advanced by an absence."""
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    presence = get_attention_ledger()
    presence.set_count(SOURCE_COCKPIT, 1)

    coord, event, _ = _coordinator_with_pending("op-partial")
    task = asyncio.ensure_future(coord._wait_with_cancel(event, 0.30, None))
    await asyncio.sleep(0.15)               # ~half the budget spent
    presence.set_count(SOURCE_COCKPIT, 0)
    await asyncio.sleep(0.40)               # long absence, charged to nobody
    assert not task.done(), "the paused clock expired anyway"

    presence.set_count(SOURCE_COCKPIT, 1)
    t_resume = time.monotonic()
    decision, reason, attended = await asyncio.wait_for(task, timeout=2.0)
    resumed_for = time.monotonic() - t_resume
    assert decision is None and reason == "attended_budget"
    # Only the REMAINING ~0.15s was owed — it did not restart at 0.30.
    assert resumed_for < 0.30
    assert 0.28 <= attended <= 0.50


@pytest.mark.asyncio
async def test_operator_decision_resolves_even_while_paused(monkeypatch):
    """An /accept arriving over HTTP while no cockpit is attached must
    still land — the pause suspends the clock, not the rendezvous."""
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    presence = get_attention_ledger()
    presence.set_count(SOURCE_COCKPIT, 1)
    presence.set_count(SOURCE_COCKPIT, 0)

    coord, event, box = _coordinator_with_pending("op-remote")
    task = asyncio.ensure_future(coord._wait_with_cancel(event, 0.1, None))
    await asyncio.sleep(0.2)
    assert not task.done()

    assert coord.record_accept("op-remote") is True
    decision, reason, _ = await asyncio.wait_for(task, timeout=2.0)
    assert decision is ReviewDecision.ACCEPTED
    assert reason == "operator"


@pytest.mark.asyncio
async def test_ceiling_bounds_an_unattended_pause(monkeypatch):
    """A paused review is still bounded — the fix for 'discarded while
    nobody watched' must not become 'pinned forever'."""
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    monkeypatch.setenv(WALL_MULT_ENV_VAR, "3")   # 0.1s budget → 0.3s ceiling
    presence = get_attention_ledger()
    presence.set_count(SOURCE_COCKPIT, 1)
    presence.set_count(SOURCE_COCKPIT, 0)

    coord, event, _ = _coordinator_with_pending("op-ceiling")
    t0 = time.monotonic()
    decision, reason, attended = await coord._wait_with_cancel(
        event, 0.1, None,
    )
    wall = time.monotonic() - t0
    assert decision is None
    # Honest provenance: never confused with a considered rejection.
    assert reason == "unattended_ceiling"
    assert attended < 0.05
    assert 0.25 <= wall <= 1.5


@pytest.mark.asyncio
async def test_cancel_still_fires_while_paused(monkeypatch):
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    presence = get_attention_ledger()
    presence.set_count(SOURCE_COCKPIT, 1)
    presence.set_count(SOURCE_COCKPIT, 0)

    coord, event, _ = _coordinator_with_pending("op-cancel")
    cancelled = {"v": False}
    task = asyncio.ensure_future(
        coord._wait_with_cancel(event, 0.1, lambda: cancelled["v"]),
    )
    await asyncio.sleep(0.15)
    assert not task.done()
    cancelled["v"] = True
    decision, reason, _ = await asyncio.wait_for(task, timeout=2.0)
    assert decision is ReviewDecision.REJECTED
    assert reason == "cancelled"


@pytest.mark.asyncio
async def test_flapping_operator_does_not_reset_or_inflate_the_budget(
    monkeypatch,
):
    """Twenty flaps inside the wait. The budget must be spent by connected
    time only — no reset, no phantom accrual, no waiter leak."""
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    monkeypatch.setenv(op_presence.FLAP_GRACE_ENV_VAR, "0.05")
    presence = get_attention_ledger()

    coord, event, _ = _coordinator_with_pending("op-flap")
    task = asyncio.ensure_future(coord._wait_with_cancel(event, 0.20, None))

    connected = 0.0
    for _ in range(20):
        t0 = time.monotonic()
        presence.set_count(SOURCE_COCKPIT, 1)
        await asyncio.sleep(0.01)
        presence.set_count(SOURCE_COCKPIT, 0)
        connected += time.monotonic() - t0
        await asyncio.sleep(0.01)
        if task.done():
            break

    presence.set_count(SOURCE_COCKPIT, 1)
    decision, reason, attended = await asyncio.wait_for(task, timeout=3.0)
    assert decision is None and reason == "attended_budget"
    # Generous upper bound (one poll tick of overshoot is legal); the
    # point is that ~20 flaps did not multiply or reset the budget.
    assert 0.18 <= attended <= 0.45
    assert len(presence._waiters) == 0, "flapping leaked presence waiters"


@pytest.mark.asyncio
async def test_no_task_or_waiter_leak_after_a_completed_wait(monkeypatch):
    monkeypatch.setenv(ATTENTION_GATE_ENV_VAR, "true")
    presence = get_attention_ledger()
    presence.set_count(SOURCE_COCKPIT, 1)
    presence.set_count(SOURCE_COCKPIT, 0)

    before = len(asyncio.all_tasks())
    coord, event, _ = _coordinator_with_pending("op-leak")
    task = asyncio.ensure_future(coord._wait_with_cancel(event, 0.05, None))
    await asyncio.sleep(0.1)
    coord.record_reject("op-leak")
    await asyncio.wait_for(task, timeout=2.0)
    await asyncio.sleep(0.05)
    assert len(presence._waiters) == 0
    assert len(asyncio.all_tasks()) <= before + 1


# ===========================================================================
# The transport seam
# ===========================================================================


def test_cockpit_bridge_publishes_presence_from_its_client_set():
    """The bridge must project ``_clients`` into the ledger — not
    maintain a second, independently-mutated counter."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )

    bridge = CockpitAttachBridge.__new__(CockpitAttachBridge)
    bridge._clients = set()
    presence = get_attention_ledger()

    w1, w2 = object(), object()
    bridge._clients.add(w1)
    bridge._publish_presence()
    assert presence.snapshot().count == 1
    assert presence.snapshot().armed

    bridge._clients.add(w2)
    bridge._publish_presence()
    assert presence.snapshot().count == 2

    bridge._clients.clear()
    bridge._publish_presence()
    assert presence.snapshot().count == 0
