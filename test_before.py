"""Pytest assertions for nth_smallest.

5 cases covering different n values:
  * smallest (n=1)
  * 2nd smallest with duplicates
  * 3rd smallest with mixed values
  * largest (n=len(values))
  * out-of-range n (raises ValueError)

The buggy implementation in before.py uses ``sorted_values[n]``
(0-indexed access of (n+1)-th element), so multiple test cases
fail simultaneously.  Only a coherent off-by-one fix
(``sorted_values[n - 1]``) passes the entire suite.
"""
from __future__ import annotations

import pytest

from before import nth_smallest


def test_smallest():
    """n=1 must return the smallest element (1-indexed)."""
    assert nth_smallest([3, 1, 4, 1, 5, 9, 2, 6], 1) == 1


def test_second_smallest_with_duplicates():
    """n=2 must return the second-smallest (duplicates counted)."""
    # sorted([3,1,4,1,5,9,2,6]) = [1,1,2,3,4,5,6,9]; 2nd = 1
    assert nth_smallest([3, 1, 4, 1, 5, 9, 2, 6], 2) == 1


def test_third_smallest():
    """n=3 must return the third-smallest."""
    # sorted([3,1,4,1,5,9,2,6]) = [1,1,2,3,4,5,6,9]; 3rd = 2
    assert nth_smallest([3, 1, 4, 1, 5, 9, 2, 6], 3) == 2


def test_largest_via_n_equals_length():
    """n=len(values) must return the largest element."""
    # sorted([3,1,4,1,5,9,2,6]) = [1,1,2,3,4,5,6,9]; 8th = 9
    assert nth_smallest([3, 1, 4, 1, 5, 9, 2, 6], 8) == 9


def test_out_of_range_raises_value_error():
    """n > len(values) and n < 1 both raise ValueError."""
    with pytest.raises(ValueError):
        nth_smallest([1, 2, 3], 4)
    with pytest.raises(ValueError):
        nth_smallest([1, 2, 3], 0)
    with pytest.raises(ValueError):
        nth_smallest([], 1)
