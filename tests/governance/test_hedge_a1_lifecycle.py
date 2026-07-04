"""Task 5 -- the A1 race-lifecycle matrix (load-bearing validation, mandate #4).

Proves, deterministically and through the REAL ``hedged_race`` +
``resolve_hedge_arm_policy`` (fake async arms, zero network, zero real
providers), the full claim of mandate #4:

    "under a BACKGROUND, write-intent operation with empty complexity, the
    RT loop is prioritized, the batch arm remains un-ignited (zero token
    spend), and the event-driven fallback ignition triggers flawlessly if
    and only if the RT arm encounters a terminal rupture."

The mandate is an "if and only if" claim about batch ignition. This module
documents WHICH test proves WHICH direction of that biconditional:

    BATCH FIRES  <==>  the RT (fast) arm terminally failed.

  * "only if" (batch fires -> RT terminally failed; equivalently, RT
    succeeds -> batch NEVER fires):
      - test_full_a1_path_rt_tool_arm_wins_batch_never_billed
        (this file): RT succeeds, ``billed["batch"] == 0``.
      - test_defer_without_prefer_fast_collapses_to_eager (this file):
        isolates that deferral itself (not incidental timing) is what
        gates ignition -- proves the OTHER half of the guard, that
        ``defer_stable`` requires BOTH flags to hold, not just one.

  * "if" (RT terminally failed -> batch fires and wins):
      - test_rupture_fallback_preserved_with_defer (this file): RT raises
        a classified rupture -> stable ignites exactly once and wins.
      - test_non_rupture_fast_failure_still_ignites_stable (this file):
        RT raises a PLAIN (non-rupture) exception -> stable STILL ignites
        and wins, proving ignition fires on ANY terminal fast failure, not
        only ``is_rupture``-classified ones -- the "iff" is not
        accidentally narrower than advertised.

  * Supporting lifecycle guarantees (not part of the biconditional, but
    required for "flawlessly"):
      - test_read_only_op_keeps_legacy_concurrent_race: read-only ops keep
        the pre-existing concurrent race (this feature must not regress
        the legacy path it did not target).
      - test_outer_cancellation_while_deferred_leaves_no_orphan: outer
        cancellation while the stable arm is still deferred must not leak
        tasks or accidentally ignite the never-fired stable arm.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.dw_transport_hedge import (
    hedged_race,
    resolve_hedge_arm_policy,
)

pytestmark = pytest.mark.asyncio


async def test_full_a1_path_rt_tool_arm_wins_batch_never_billed():
    """A1 write-intent op end-to-end at the race layer: policy resolves RT-
    priority+defer; RT (tool-loop) arm succeeds; batch arm NEVER fires."""
    p = resolve_hedge_arm_policy(
        complexity="moderate", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert (p.prefer_fast, p.defer_stable) == (True, True)
    billed = {"batch": 0}

    async def rt_arm():
        await asyncio.sleep(0.01)  # tool rounds take time
        return {"content": "patch", "tool_calls": 3}

    async def batch_arm():
        billed["batch"] += 1
        return {"content": "blind-patch", "tool_calls": 0}

    out = await hedged_race(
        rt_arm, batch_arm,
        prefer_fast=p.prefer_fast, defer_stable=p.defer_stable,
    )
    assert out["tool_calls"] == 3          # the tool-loop candidate won
    assert billed["batch"] == 0            # transactional isolation: single arm billed


async def test_rupture_fallback_preserved_with_defer():
    class Rupture(RuntimeError):
        pass

    async def rt_arm():
        raise Rupture("stream severed")

    async def batch_arm():
        return {"content": "stable", "tool_calls": 0}

    out = await hedged_race(
        rt_arm, batch_arm, prefer_fast=True, defer_stable=True,
        is_rupture=lambda e: isinstance(e, Rupture),
    )
    assert out["content"] == "stable"      # hedge's raison d'etre intact


async def test_read_only_op_keeps_legacy_concurrent_race():
    p = resolve_hedge_arm_policy(
        complexity="trivial", route="background", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert (p.prefer_fast, p.defer_stable) == (False, False)

    async def rt_arm():
        await asyncio.sleep(0.05)
        return "rt"

    async def batch_arm():
        return "batch"

    out = await hedged_race(rt_arm, batch_arm, prefer_fast=p.prefer_fast,
                            defer_stable=p.defer_stable)
    assert out == "batch"                  # cheap reflex ops keep the fast batch win


async def test_outer_cancellation_while_deferred_leaves_no_orphan():
    """Task 2 review coverage gap: cancel the OUTER race task while the
    stable arm is still deferred (never ignited). No orphaned tasks, the
    stable thunk is never invoked, and the cancellation propagates
    cleanly through hedged_race's own cancellation handling."""
    stable_calls = {"count": 0}
    current = asyncio.current_task()

    async def fast_never_finishes():
        await asyncio.sleep(3600)  # deliberately never resolves in-test
        return "unreachable"

    async def stable_counter():
        stable_calls["count"] += 1
        return "unreachable-stable"

    race_task = asyncio.ensure_future(
        hedged_race(
            fast_never_finishes, stable_counter,
            prefer_fast=True, defer_stable=True,
        )
    )
    # Two cooperative-scheduling yields -- test scheduling, not production
    # timing -- to let hedged_race create its internal fast task and reach
    # its `await asyncio.wait(...)` suspension point before we cancel.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    race_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await race_task

    # Let any cancellation unwind fully settle.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert stable_calls["count"] == 0      # deferred arm was NEVER invoked
    leaked = [
        t for t in asyncio.all_tasks()
        if t is not current and not t.done()
    ]
    assert leaked == []                    # no orphaned tasks survive


async def test_defer_without_prefer_fast_collapses_to_eager():
    """defer_stable=True alone (without prefer_fast) must NOT defer --
    _defer requires BOTH flags (``_defer = defer_stable and prefer_fast``).
    The stable thunk is invoked at race start (not gated on fast failure).

    Both arms here are instant, no-internal-await coroutines by design, so
    they can complete within the SAME event-loop tick; when both finish
    together, ``asyncio.wait``'s ``done`` set is iterated with arbitrary
    (object-hash) ordering whenever ``prefer_fast=False`` (legacy path,
    no deterministic fast-first ordering applied) -- so which of the two
    equally-valid results wins the race is not itself deterministic and
    is NOT what this test asserts (asserting `out == "rt-instant"` would
    be a real, observed flake -- the exact "no wall-clock races" failure
    mode this suite is required to avoid). What IS deterministic, and is
    the actual proof point: the stable coroutine's body starts executing
    -- and increments its counter -- on its own first scheduling step,
    which always happens before hedged_race's post-`asyncio.wait` cancel
    logic can run (cancellation of an already-completed/started task is a
    no-op for code that already executed). So stable invocation count is
    unconditionally 1, proving eager (non-deferred) ignition."""
    stable_calls = {"count": 0}

    async def fast_arm():
        return "rt-instant"

    async def stable_arm():
        stable_calls["count"] += 1
        return "batch-instant"

    out = await hedged_race(
        fast_arm, stable_arm, prefer_fast=False, defer_stable=True,
    )
    assert out in ("rt-instant", "batch-instant")  # legacy race, either is a valid winner
    assert stable_calls["count"] == 1       # eager: stable was fired at race start


async def test_non_rupture_fast_failure_still_ignites_stable():
    """Ignition fires on ANY terminal fast-arm failure, not only ones
    classified as a 'rupture' by is_rupture. A plain ValueError (with
    is_rupture always False) must still ignite and let stable win."""
    async def fast_arm():
        raise ValueError("plain non-rupture failure")

    async def stable_arm():
        return "stable-won"

    out = await hedged_race(
        fast_arm, stable_arm, prefer_fast=True, defer_stable=True,
        is_rupture=lambda e: False,
    )
    assert out == "stable-won"
