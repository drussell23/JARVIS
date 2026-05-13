"""Regression spine for ``unified_intake_router._PRIORITY_MAP``.

Pins the structural invariants that prevent priority-starvation
of injected envelopes — the failure mode observed in stage-1
SWE-Bench-Pro wiring soak 2026-05-12 (session
``bt-2026-05-13-040242``):

* Our envelope's ``source="swe_bench_pro"`` was NOT in
  ``_PRIORITY_MAP``, so ``_compute_priority`` defaulted to
  ``base = 99``.  With ``urgency="low"`` (boost = -1, subtracted
  from base) the final priority was 100 — strictly worse than
  every other in-flight signal.  Result: 21-minute dequeue lag
  while 16 other ops were dispatched ahead.  ``asyncio.PriorityQueue``
  always returned the lowest int first, and sensors kept emitting
  envelopes with priority ≤ 99, so ours never reached the head.

Two layers of pins:

1. **SWE-Bench-Pro specific** — `"swe_bench_pro"` must be in
   ``_PRIORITY_MAP`` with a tier strictly worse than
   ``test_failure`` (runtime fires must always win) and strictly
   better than the unmapped-source default of 99 (so it never
   regresses to the starvation tier).

2. **Systemic (belt-and-suspenders)** — every source in
   ``intent_envelope._VALID_SOURCES`` must appear in either
   ``_PRIORITY_MAP`` OR the explicit ``_PRIORITY_MAP_DEFERRED``
   allowlist.  This is the "next new source forgot the map"
   regression: a new source name added to ``_VALID_SOURCES``
   without a corresponding ``_PRIORITY_MAP`` entry (or an
   explicit ``_PRIORITY_MAP_DEFERRED`` opt-in) fails the test
   at CI time instead of silently starving in production.

The deferred allowlist documents existing technical debt (12
pre-existing sources that fall through to base=99).  Migration
removes them as their tiers are assigned with intent.

This is the structural fix for what was proved in the
2026-05-12 triage: unknown source → base 99 + low urgency →
worst queue slot.  Not "router deadlock", not "idle-timeout
misconfig", and **not** a special-case in the dequeue loop.
Map registration is the load-bearing surface.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    _PRIORITY_MAP,
    _PRIORITY_MAP_DEFERRED,
)


# ---------------------------------------------------------------------------
# SWE-Bench-Pro source-specific pins
# ---------------------------------------------------------------------------


def test_swe_bench_pro_source_is_in_priority_map():
    """The exact registration that closes the 21-min dequeue lag."""
    assert "swe_bench_pro" in _PRIORITY_MAP, (
        "swe_bench_pro source is not in _PRIORITY_MAP — envelopes "
        "from the SWE-Bench-Pro harness inject hook will fall "
        "through to base=99 and starve under any contended queue. "
        "This is the regression observed 2026-05-12 (21-min dequeue "
        "lag in session bt-2026-05-13-040242).  Register a tier."
    )


def test_swe_bench_pro_priority_strictly_worse_than_test_failure():
    """Runtime fires must always preempt benchmark eval work."""
    assert _PRIORITY_MAP["swe_bench_pro"] > _PRIORITY_MAP["test_failure"], (
        f"swe_bench_pro priority ({_PRIORITY_MAP['swe_bench_pro']}) is "
        f"not strictly worse than test_failure "
        f"({_PRIORITY_MAP['test_failure']}) — benchmark eval work "
        "should never preempt actual runtime failure signals."
    )


def test_swe_bench_pro_priority_strictly_better_than_unmapped_default():
    """Must not regress to the starvation tier (base=99 for unmapped)."""
    # _compute_priority uses 99 as the unmapped-source fallback;
    # anything that ends up at ≥ 99 lives in starvation territory.
    assert _PRIORITY_MAP["swe_bench_pro"] < 99, (
        f"swe_bench_pro priority ({_PRIORITY_MAP['swe_bench_pro']}) is "
        "not strictly better than the unmapped-source default of 99 — "
        "this would re-introduce the starvation bug the map entry "
        "was supposed to close."
    )


def test_swe_bench_pro_priority_is_backlog_peer():
    """Deliberate tier choice — peer with backlog (queued eval work).

    Documents the rationale via the assertion message rather than a
    free-floating comment: the tier is a load-bearing design
    decision, not arbitrary.  See PRD §40.7.10-priority-map.
    """
    assert _PRIORITY_MAP["swe_bench_pro"] == _PRIORITY_MAP["backlog"], (
        f"swe_bench_pro priority ({_PRIORITY_MAP['swe_bench_pro']}) "
        f"diverged from backlog ({_PRIORITY_MAP['backlog']}) — these "
        "should remain peers (queued evaluation work, neither runtime "
        "fire nor low-priority background fuzz).  If the tier needs "
        "to change, update both peers + PRD rationale together."
    )


# ---------------------------------------------------------------------------
# Systemic regression — every valid source must have a documented tier
# ---------------------------------------------------------------------------


def test_every_valid_source_has_priority_or_deferred_opt_in():
    """Belt-and-suspenders: a new source added to _VALID_SOURCES
    without a corresponding _PRIORITY_MAP entry MUST explicitly opt
    into _PRIORITY_MAP_DEFERRED — otherwise it silently starves
    under contention.

    This is the regression that would have caught the SWE-Bench-Pro
    omission at CI time, before stage-1 soak.  Adding a source now
    requires a deliberate decision: assign a tier OR document that
    starvation is acceptable.
    """
    mapped = set(_PRIORITY_MAP.keys())
    deferred = set(_PRIORITY_MAP_DEFERRED)
    covered = mapped | deferred
    missing = _VALID_SOURCES - covered
    assert not missing, (
        f"Sources in _VALID_SOURCES with no _PRIORITY_MAP entry AND "
        f"no _PRIORITY_MAP_DEFERRED opt-in: {sorted(missing)}.  Each "
        "MUST either join _PRIORITY_MAP with a deliberate tier OR be "
        "added to _PRIORITY_MAP_DEFERRED with a comment justifying "
        "why default starvation is acceptable.  Otherwise envelopes "
        "from that source will fall through to base=99 and starve "
        "under any contended queue (see 2026-05-12 SWE-Bench-Pro "
        "21-min dequeue lag)."
    )


def test_deferred_and_mapped_are_disjoint():
    """A source cannot be both mapped AND deferred — that's a sign
    of stale migration state."""
    overlap = set(_PRIORITY_MAP.keys()) & set(_PRIORITY_MAP_DEFERRED)
    assert not overlap, (
        f"Sources appearing in BOTH _PRIORITY_MAP and "
        f"_PRIORITY_MAP_DEFERRED: {sorted(overlap)}.  When a deferred "
        "source gets a deliberate tier, remove it from "
        "_PRIORITY_MAP_DEFERRED in the same edit."
    )


def test_deferred_sources_are_all_valid_sources():
    """The deferred allowlist must reference real source names —
    typos or stale entries would silently no-op the systemic
    regression."""
    invalid = set(_PRIORITY_MAP_DEFERRED) - _VALID_SOURCES
    assert not invalid, (
        f"_PRIORITY_MAP_DEFERRED references source names that aren't "
        f"in _VALID_SOURCES: {sorted(invalid)}.  Either fix the typo "
        "or remove the stale entry — a deferred entry for a source "
        "that doesn't exist provides false coverage."
    )


def test_mapped_sources_are_all_valid_sources():
    """The priority map must reference real source names — same
    rationale as the deferred-allowlist invariant."""
    invalid = set(_PRIORITY_MAP.keys()) - _VALID_SOURCES
    assert not invalid, (
        f"_PRIORITY_MAP references source names that aren't in "
        f"_VALID_SOURCES: {sorted(invalid)}.  Either add them to "
        "_VALID_SOURCES (intent_envelope.py) or fix the typo — a "
        "priority for a source that envelope validation will reject "
        "is dead code."
    )


# ---------------------------------------------------------------------------
# Priority-arithmetic regression — proves the dequeue ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,urgency,expected_priority",
    [
        # swe_bench_pro: base=2, low urgency boost=-1, priority = 2-(-1) = 3
        ("swe_bench_pro", "low", 3),
        # swe_bench_pro: base=2, high urgency boost=1, priority = 2-1 = 1
        ("swe_bench_pro", "high", 1),
        # test_failure: base=1, low urgency, priority = 1-(-1) = 2
        ("test_failure", "low", 2),
        # voice_human always wins
        ("voice_human", "normal", 0),
    ],
)
def test_priority_arithmetic_matches_design(source, urgency, expected_priority):
    """Concrete arithmetic check: priority = base - urgency_boost
    (subtraction matches the comment at line ~76).  This proves
    that the dequeue ordering aligns with operator intent for the
    SWE-Bench-Pro tier, not just that the entry exists.

    Notably: swe_bench_pro@low (priority 3) beats every deferred
    source @ low (priority 100) by a margin of 97 — which is what
    closes the 21-min lag.
    """
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        _PRIORITY_MAP,
        _URGENCY_BOOST,
    )

    base = _PRIORITY_MAP.get(source, 99)
    boost = _URGENCY_BOOST.get(urgency, 0)
    # Mirror the _compute_priority math without dragging the full
    # goal-tracker / dependency-credit machinery into this unit test.
    # Those add subtractions too but for a source+urgency-only check
    # the unweighted form is the load-bearing invariant.
    computed = base - boost
    assert computed == expected_priority, (
        f"{source}@{urgency}: expected priority {expected_priority}, "
        f"got {computed} (base={base}, boost={boost})"
    )
