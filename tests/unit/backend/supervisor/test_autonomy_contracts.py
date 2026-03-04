"""Tests for autonomy contract checking and version comparison."""

import pytest


def test_version_gte_simple():
    """Basic version comparison."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0", "1.0") is True
    assert _version_gte("2.0", "1.0") is True
    assert _version_gte("1.0", "2.0") is False


def test_version_gte_multidigit():
    """Multi-digit segments must compare numerically, not lexically."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.10", "1.9") is True
    assert _version_gte("1.9", "1.10") is False


def test_version_gte_three_segments():
    """Three-segment versions (patch level)."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.1", "1.0.0") is True
    assert _version_gte("1.0.0", "1.0.1") is False
    assert _version_gte("2.0.0", "1.9.9") is True


def test_version_gte_unequal_length():
    """Versions with different segment counts.

    Note: "1.0" is treated as < "1.0.1" because Python tuple comparison
    treats shorter tuples as lesser when prefixes match.
    """
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.0", "1.0") is True
    assert _version_gte("1.0", "1.0.1") is False


def test_version_gte_malformed_inputs():
    """Malformed version strings should return False, not raise."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0-beta", "1.0") is False
    assert _version_gte("v1.0", "1.0") is False
    assert _version_gte("", "1.0") is False
    assert _version_gte("1.0", "abc") is False
