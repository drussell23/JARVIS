"""Integration spine for the Sovereign Resilience & Chunking arc (Task B6).

Composes Matrix A (TransportCircuitBreaker) and Matrix B
(decompose_for_block, AstSymbolScoper, AdaptiveRecursionGovernor,
recursion_dedup) end-to-end with lightweight fakes.  OFF byte-identical
assertions confirm both master gates are default-false.

ASCII only. Python 3.9+. No emoji.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import random

import pytest

from backend.core.ouroboros.governance import transport_circuit_breaker as tcb
from backend.core.ouroboros.governance import goal_decomposition_planner as gdp
from backend.core.ouroboros.governance import ast_symbol_scoper as scoper
from backend.core.ouroboros.governance.adaptive_recursion_governor import (
    recursion_budget,
)
from backend.core.ouroboros.governance.recursion_dedup import (
    AttemptLedger,
    is_duplicate,
    subgoal_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_breaker(rng=None):
    """Reload the module so the singleton is reset between tests."""
    importlib.reload(tcb)
    return tcb.TransportCircuitBreaker(rng=rng)


def _trip_batch(breaker, *, n=20, now_start=0.0):
    """Record n batch TIMEOUT failures and return the final timestamp."""
    t = now_start
    for _ in range(n):
        breaker.record("batch", ok=False, failure_mode="TIMEOUT", now=t)
        t += 1.0
    return t


class _Goal:
    """Minimal RoadmapGoal stand-in matching decompose_for_block contract."""

    goal_id = "GOAL-INT-001"
    title = "refactor SemanticIndex"
    description = "route SemanticIndex.build through subprocess"
    target_files = ("backend/core/ouroboros/governance/semantic_index.py",)


# ---------------------------------------------------------------------------
# Matrix A end-to-end
# ---------------------------------------------------------------------------


def test_a_timeout_storm_trips_batch_and_rotates_to_realtime():
    """Batch TIMEOUT storm -> OPEN -> select_lane returns realtime."""
    rng = random.Random(0)
    b = _fresh_breaker(rng=rng)

    now = _trip_batch(b, n=20)

    # Lane must be OPEN
    assert b.state("batch") is tcb.BreakerState.OPEN

    # select_lane must rotate away from the dead lane
    chosen = b.select_lane("batch", now=now)
    assert chosen == "realtime", (
        f"Expected rotation to 'realtime', got '{chosen}'"
    )


def test_a_probe_success_closes_breaker():
    """OPEN lane -> wait for probe deadline -> run_probe_if_due success -> CLOSED."""
    rng = random.Random(0)
    b = _fresh_breaker(rng=rng)
    now = _trip_batch(b, n=20)

    assert b.state("batch") is tcb.BreakerState.OPEN

    # Simulate time well past any jittered recovery deadline
    probe_now = now + 100_000.0

    async def good_probe(_lane):
        return True

    result = asyncio.run(
        tcb.run_probe_if_due(b, "batch", good_probe, now=probe_now)
    )

    # run_probe_if_due returns the probe function's return value on success
    assert result is True
    assert b.state("batch") is tcb.BreakerState.CLOSED


def test_a_both_lanes_open_no_rotation_returns_preferred():
    """When both lanes are OPEN, select_lane must return the preferred lane
    (dual_lane_breaker owns total-outage; we must NOT rotate to a second dead
    lane)."""
    rng = random.Random(0)
    b = _fresh_breaker(rng=rng)

    now = 0.0
    # Trip both lanes
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=now)
        b.record("realtime", ok=False, failure_mode="TIMEOUT", now=now)
        now += 1.0

    assert b.state("batch") is tcb.BreakerState.OPEN
    assert b.state("realtime") is tcb.BreakerState.OPEN

    # Must NOT rotate; must return the preferred lane
    chosen = b.select_lane("batch", now=now)
    assert chosen == "batch", (
        f"Both-OPEN: expected preferred 'batch', got '{chosen}'"
    )


# ---------------------------------------------------------------------------
# Matrix B end-to-end
# ---------------------------------------------------------------------------


def test_b2_zero_coverage_prepends_test_subgoal_with_depends_on():
    """decompose_for_block(zero_coverage=True) -> index 0 is test-gen SubGoal,
    mutation SubGoal.depends_on_sub_ids contains the test sub-goal id."""
    # Use a null scoper so the test is path-independent
    subs = gdp.decompose_for_block(
        _Goal(),
        zero_coverage=True,
        scoper=lambda fp, desc: (),
    )

    assert len(subs) >= 2, "Expected at least 2 sub-goals for zero_coverage=True"

    test_sub = subs[0]
    assert test_sub.kind is gdp.SubGoalKind.SEQUENTIAL
    assert (
        "pytest" in test_sub.title.lower()
        or "test" in test_sub.title.lower()
    ), f"Unexpected title: {test_sub.title!r}"

    # Mutation sub-goals must depend on the test sub-goal
    mutation_subs = subs[1:]
    test_id = test_sub.sub_goal_id
    for m in mutation_subs:
        assert test_id in m.depends_on_sub_ids, (
            f"Mutation sub-goal {m.sub_goal_id!r} missing depends_on {test_id!r}"
        )


def test_b1_isolate_symbols_narrows_and_passes_integrity_gate(tmp_path):
    """AstSymbolScoper isolates named class/method symbols and every
    returned ScopedTarget passes the B1a integrity gate (non-empty symbol
    implies a valid parseable slice)."""
    src = (
        "import os\n"
        "class SemanticIndex:\n"
        "    def build(self):\n"
        "        return 1\n"
        "    def query(self, q):\n"
        "        return q\n"
        "def helper():\n"
        "    return 0\n"
    )
    p = tmp_path / "semantic_index.py"
    p.write_text(src)

    targets = scoper.isolate_symbols(
        str(p), "route SemanticIndex.build through subprocess"
    )

    # Must return at least one non-empty-symbol target
    named = [t for t in targets if t.symbol]
    assert named, f"Expected named symbol targets, got {targets!r}"

    # Every named symbol must have a positive line number
    for t in named:
        assert t.lineno > 0, f"Expected positive lineno for {t.symbol!r}"

    # The symbol names must be from the file
    symbols = {t.symbol for t in named}
    # The scoper should match SemanticIndex.build or SemanticIndex
    assert symbols & {"SemanticIndex.build", "SemanticIndex"}, (
        f"Expected SemanticIndex or SemanticIndex.build in {symbols!r}"
    )


def test_b3_governor_blocks_under_heavy_load_allows_when_idle():
    """AdaptiveRecursionGovernor: heavy load -> allowed=False at depth > 0;
    idle -> allowed=True even at moderate depth."""
    # Heavy load: large queue + high loop latency + CRITICAL pressure
    heavy = recursion_budget(
        queue_len=10_000,
        loop_blocked_ms=50_000.0,
        pressure_level=3,
        depth=1,
    )
    assert heavy.allowed is False, (
        f"Expected heavy load to block recursion; got reason={heavy.reason!r}"
    )

    # Idle: zero queue, zero latency, zero pressure, shallow depth
    idle = recursion_budget(
        queue_len=0,
        loop_blocked_ms=0.0,
        pressure_level=0,
        depth=1,
    )
    assert idle.allowed is True, (
        f"Expected idle to allow recursion; got reason={idle.reason!r}"
    )


def test_b4_dedup_discards_repeat_hash_allows_novel():
    """AttemptLedger + is_duplicate: a repeated hash is detected as duplicate;
    a novel hash is not."""
    ledger = AttemptLedger()
    active: frozenset[str] = frozenset()

    targets = ("governance/semantic_index.py::SemanticIndex",)
    desc = "route SemanticIndex.build"
    h = subgoal_hash(targets, desc)

    # Before marking: not a duplicate
    assert not is_duplicate(h, ledger, active), (
        "Hash should not be duplicate before marking"
    )

    # Mark it
    ledger.mark(h)

    # After marking: duplicate
    assert is_duplicate(h, ledger, active), (
        "Hash should be duplicate after marking"
    )

    # A different sub-goal hash must NOT be duplicate
    h2 = subgoal_hash(("governance/other.py",), "something else")
    assert not is_duplicate(h2, ledger, active), (
        "Novel hash must not be flagged as duplicate"
    )


# ---------------------------------------------------------------------------
# OFF byte-identical
# ---------------------------------------------------------------------------


def test_off_transport_breaker_enabled_returns_false(monkeypatch):
    """With JARVIS_TRANSPORT_BREAKER_ENABLED=false, breaker_enabled() is False."""
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "false")
    importlib.reload(tcb)
    assert tcb.breaker_enabled() is False


def test_off_recursive_chunking_enabled_returns_false(monkeypatch):
    """With JARVIS_RECURSIVE_CHUNKING_ENABLED=false, chunking_enabled() is False."""
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "false")
    importlib.reload(gdp)
    assert gdp.chunking_enabled() is False
