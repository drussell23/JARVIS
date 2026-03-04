"""Tests for CloudCapacityController and related types."""

import pytest
import time


def test_cloud_capacity_action_enum():
    """CloudCapacityAction must have all required values."""
    from backend.core.memory_types import CloudCapacityAction

    expected = {
        "STAY_LOCAL",
        "DEGRADE_LOCAL",
        "OFFLOAD_PARTIAL",
        "SPIN_SPOT",
        "FALLBACK_ONDEMAND",
    }
    actual = {a.name for a in CloudCapacityAction}
    assert actual == expected


def test_cloud_capacity_action_is_str_enum():
    """CloudCapacityAction values should be usable as strings."""
    from backend.core.memory_types import CloudCapacityAction

    assert CloudCapacityAction.STAY_LOCAL.value == "stay_local"
    assert CloudCapacityAction.SPIN_SPOT.value == "spin_spot"
