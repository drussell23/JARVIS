"""Known-good fix for problem_001 nth_smallest.

This file is the REFERENCE solution used by Phase 1.5.B's spine test
to sanity-check that the test suite IS solvable when the off-by-one
is corrected.  It is NOT loaded by the canonical
``list_corpus_problems`` walker (underscore-prefixed → skipped per
the convention pinned in Phase 1.5.A).

Operators should NEVER point the production l2_exercise_seed module
at this file as a fixture — it would defeat the purpose of the
corpus (we want providers to discover the fix themselves, not be
handed it).

The only difference vs ``before.py`` is the single-character fix
on the return line: ``sorted_values[n - 1]`` instead of
``sorted_values[n]``.  Same module docstring + same signature + same
edge-case handling, so the fix verifiably addresses the off-by-one
WITHOUT introducing other behavior changes (which would make
empirical hardness validation ambiguous).
"""
from __future__ import annotations

from typing import List


def nth_smallest(values: List[int], n: int) -> int:
    if not values:
        raise ValueError("values must be non-empty")
    if n < 1 or n > len(values):
        raise ValueError(
            f"n must be in [1, {len(values)}], got {n}"
        )
    sorted_values = sorted(values)
    # FIX: 1-indexed n → 0-indexed access uses n - 1.
    return sorted_values[n - 1]
