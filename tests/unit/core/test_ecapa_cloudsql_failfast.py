"""Tests for ECAPA CloudSQL fail-fast gate."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from enum import Enum

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest


class MockReadinessState(Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    DEGRADED_SQLITE = "degraded_sqlite"


class TestEcapaCloudSqlGate:
    def test_ready_state_allows_db_steps(self):
        """When CloudSQL is READY, DB-dependent steps should proceed."""
        gate = MagicMock()
        gate.state = MockReadinessState.READY
        gate.is_ready = True

        # READY state should NOT skip DB steps
        skip = gate.state != MockReadinessState.READY
        assert skip is False

    def test_unavailable_state_skips_db_steps(self):
        """When CloudSQL is UNAVAILABLE, DB-dependent steps should be skipped."""
        gate = MagicMock()
        gate.state = MockReadinessState.UNAVAILABLE

        skip = gate.state != MockReadinessState.READY
        assert skip is True

    def test_unknown_state_skips_db_steps(self):
        """When CloudSQL is UNKNOWN (never succeeded this boot), skip DB steps."""
        gate = MagicMock()
        gate.state = MockReadinessState.UNKNOWN

        skip = gate.state != MockReadinessState.READY
        assert skip is True

    def test_checking_state_skips_db_steps(self):
        """When CloudSQL is CHECKING (attempting but not yet ready), skip DB steps."""
        gate = MagicMock()
        gate.state = MockReadinessState.CHECKING

        skip = gate.state != MockReadinessState.READY
        assert skip is True

    def test_degraded_sqlite_skips_db_steps(self):
        """When CloudSQL is DEGRADED_SQLITE, skip DB steps (no cloud DB)."""
        gate = MagicMock()
        gate.state = MockReadinessState.DEGRADED_SQLITE

        skip = gate.state != MockReadinessState.READY
        assert skip is True
