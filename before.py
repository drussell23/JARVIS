"""nth_smallest — return the n-th smallest element from a list.

Spec (read before reading the code below):

* ``n`` is **1-indexed**: ``nth_smallest(values, 1)`` returns the
  SMALLEST element (not the second-smallest).
* Duplicates are counted: for ``values = [1, 1, 2, 3]`` and ``n=2``,
  the answer is ``1`` (the second occurrence in sorted order).
* For ``n < 1`` or ``n > len(values)``, raises ``ValueError``.
* For empty ``values``, raises ``ValueError`` regardless of ``n``.

The implementation below contains an off-by-one bug.  The pytest
file ``test_before.py`` has 5 assertions covering different ``n``
values; multiple assertions fail (not just one), so a partial fix
that handles one case won't pass the suite.
"""
from __future__ import annotations

from typing import List


def nth_smallest(values: List[int], n: int) -> int:
    """Return the n-th smallest element (1-indexed) from ``values``.

    See module docstring for full spec.
    """
    if not values:
        raise ValueError("values must be non-empty")
    if n < 1 or n > len(values):
        raise ValueError(
            f"n must be in [1, {len(values)}], got {n}"
        )
    sorted_values = sorted(values)
    # BUG: ``sorted_values[n]`` is 0-indexed access of (n+1)-th
    # smallest, NOT the 1-indexed n-th smallest the spec requires.
    # Correct code: ``return sorted_values[n - 1]``.
    return sorted_values[n]
