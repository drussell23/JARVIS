"""
Test Dummy Module for Ouroboros Self-Improvement
=================================================

This is a deliberately suboptimal module that Ouroboros can safely
practice improving. It contains code with:
- Performance issues
- Missing type hints
- Poor error handling
- Suboptimal algorithms

The goal is to let Ouroboros improve this code iteratively and learn
from the experience without risking production code.

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import time
from typing import List, Optional


# =============================================================================
# SUBOPTIMAL CODE FOR OUROBOROS TO IMPROVE
# =============================================================================

def find_duplicates(items):
    """
    Find duplicate items in a list.

    This implementation is deliberately O(n^2) - Ouroboros should
    improve it to O(n) using a set/dict.
    """
    duplicates = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i] == items[j] and items[i] not in duplicates:
                duplicates.append(items[i])
    return duplicates


def fibonacci(n):
    """
    Calculate fibonacci number.

    This recursive implementation is deliberately inefficient.
    Ouroboros should improve it with memoization or iteration.
    """
    if n <= 0:
        return 0
    if n == 1:
        return 1
    return fibonacci(n - 1) + fibonacci(n - 2)


def bubble_sort(arr):
    """
    Sort an array using bubble sort.

    This O(n^2) algorithm should be improved to O(n log n).
    """
    n = len(arr)
    for i in range(n):
        for j in range(0, n - i - 1):
            if arr[j] > arr[j + 1]:
                temp = arr[j]
                arr[j] = arr[j + 1]
                arr[j + 1] = temp
    return arr


def search_item(items, target):
    """
    Search for an item in a sorted list.

    Uses linear search - should be improved to binary search.
    """
    for i in range(len(items)):
        if items[i] == target:
            return i
    return -1


def count_words(text):
    """
    Count words in a text.

    No input validation - should handle edge cases better.
    """
    words = text.split(" ")
    count = 0
    for word in words:
        if word != "":
            count = count + 1
    return count


def merge_dicts(dict1, dict2):
    """
    Merge two dictionaries.

    Doesn't handle nested dicts or conflicts properly.
    """
    result = {}
    for key in dict1:
        result[key] = dict1[key]
    for key in dict2:
        result[key] = dict2[key]
    return result


class DataProcessor:
    """
    A simple data processor class.

    Missing type hints, error handling, and docstrings.
    """

    def __init__(self, data):
        self.data = data
        self.processed = False

    def process(self):
        result = []
        for item in self.data:
            result.append(item * 2)
        self.processed = True
        return result

    def filter_positive(self):
        result = []
        for item in self.data:
            if item > 0:
                result.append(item)
        return result

    def get_stats(self):
        if len(self.data) == 0:
            return {}

        total = 0
        for item in self.data:
            total = total + item

        average = total / len(self.data)

        minimum = self.data[0]
        maximum = self.data[0]
        for item in self.data:
            if item < minimum:
                minimum = item
            if item > maximum:
                maximum = item

        return {
            "sum": total,
            "avg": average,
            "min": minimum,
            "max": maximum,
        }


# =============================================================================
# TESTS FOR OUROBOROS TO VALIDATE
# =============================================================================

def test_find_duplicates():
    """Test find_duplicates function."""
    assert find_duplicates([1, 2, 3, 2, 4, 3, 5]) == [2, 3]
    assert find_duplicates([1, 2, 3, 4, 5]) == []
    assert find_duplicates([]) == []
    assert find_duplicates([1, 1, 1]) == [1]
    print("test_find_duplicates: PASSED")


def test_fibonacci():
    """Test fibonacci function."""
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1
    assert fibonacci(5) == 5
    assert fibonacci(10) == 55
    print("test_fibonacci: PASSED")


def test_bubble_sort():
    """Test bubble_sort function."""
    assert bubble_sort([5, 3, 8, 1, 2]) == [1, 2, 3, 5, 8]
    assert bubble_sort([]) == []
    assert bubble_sort([1]) == [1]
    assert bubble_sort([3, 2, 1]) == [1, 2, 3]
    print("test_bubble_sort: PASSED")


def test_search_item():
    """Test search_item function."""
    assert search_item([1, 2, 3, 4, 5], 3) == 2
    assert search_item([1, 2, 3, 4, 5], 6) == -1
    assert search_item([], 1) == -1
    print("test_search_item: PASSED")


def test_count_words():
    """Test count_words function."""
    assert count_words("hello world") == 2
    assert count_words("") == 0
    assert count_words("one") == 1
    assert count_words("  multiple   spaces  ") == 2
    print("test_count_words: PASSED")


def test_merge_dicts():
    """Test merge_dicts function."""
    assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert merge_dicts({}, {}) == {}
    assert merge_dicts({"a": 1}, {"a": 2}) == {"a": 2}
    print("test_merge_dicts: PASSED")


def test_data_processor():
    """Test DataProcessor class."""
    processor = DataProcessor([1, 2, 3, 4, 5])

    # Test process
    result = processor.process()
    assert result == [2, 4, 6, 8, 10]
    assert processor.processed == True

    # Test filter_positive
    processor2 = DataProcessor([-1, 2, -3, 4, -5])
    assert processor2.filter_positive() == [2, 4]

    # Test get_stats
    processor3 = DataProcessor([1, 2, 3, 4, 5])
    stats = processor3.get_stats()
    assert stats["sum"] == 15
    assert stats["avg"] == 3.0
    assert stats["min"] == 1
    assert stats["max"] == 5

    # Test empty
    processor4 = DataProcessor([])
    assert processor4.get_stats() == {}

    print("test_data_processor: PASSED")


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("RUNNING OUROBOROS TEST DUMMY TESTS")
    print("=" * 60)

    test_find_duplicates()
    test_fibonacci()
    test_bubble_sort()
    test_search_item()
    test_count_words()
    test_merge_dicts()
    test_data_processor()

    print("=" * 60)
    print("ALL TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    run_all_tests()
