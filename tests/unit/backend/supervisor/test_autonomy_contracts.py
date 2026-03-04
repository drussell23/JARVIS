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
    """Versions with different segment counts."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.0", "1.0") is True
    assert _version_gte("1.0", "1.0.1") is False
